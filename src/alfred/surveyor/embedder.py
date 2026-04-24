"""Stage 2: Ollama embedding + Milvus Lite upsert/delete."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import numpy as np
import structlog
from pymilvus import CollectionSchema, DataType, FieldSchema, MilvusClient

from .config import MilvusConfig, OllamaConfig
from .parser import VaultRecord, build_embedding_text, parse_file
from .state import PipelineState

log = structlog.get_logger()

# Retry config
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0
# Throttle between sequential embedding requests (seconds)
EMBED_THROTTLE = 0.2


class Embedder:
    def __init__(
        self,
        ollama_cfg: OllamaConfig,
        milvus_cfg: MilvusConfig,
        vault_path: Path,
        state: PipelineState,
    ) -> None:
        self.api_key = ollama_cfg.api_key
        if self.api_key:
            # OpenAI-compatible endpoint (e.g. OpenRouter)
            self.embed_url = f"{ollama_cfg.base_url}/embeddings"
        else:
            # Native Ollama endpoint
            self.embed_url = f"{ollama_cfg.base_url}/api/embeddings"
        self.model = ollama_cfg.model
        self.embedding_dims = ollama_cfg.embedding_dims
        self.vault_path = vault_path
        self.state = state

        # Persistent HTTP client for embedding calls (connection pooling)
        self._http: httpx.AsyncClient | None = None

        # Milvus Lite client — retry on lock contention from prior process
        self.collection_name = milvus_cfg.collection_name
        import time
        for attempt in range(4):
            try:
                self.milvus = MilvusClient(uri=milvus_cfg.uri)
                break
            except Exception as e:
                if attempt < 3:
                    delay = 2.0 * (2 ** attempt)
                    log.warning("embedder.milvus_retry", attempt=attempt + 1, delay=delay, error=str(e))
                    time.sleep(delay)
                else:
                    raise
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        """Create the Milvus collection if it doesn't exist, or recreate on dim mismatch.

        If the existing collection's embedding dim differs from the configured
        value (e.g. user swapped embedding model), drop the collection and
        invalidate the pipeline's file-hash state so a full re-embed runs on
        the next pass. Without the state invalidation the daemon would reload
        the stale hash map and skip all files. Ports upstream a3a44a4 +
        f45d05d + 99cbd25 into one path.
        """
        if self.milvus.has_collection(self.collection_name):
            info = self.milvus.describe_collection(self.collection_name)
            for f in info.get("fields", []):
                if f.get("name") == "embedding":
                    existing_dim = f.get("params", {}).get("dim")
                    if existing_dim is not None and int(existing_dim) != self.embedding_dims:
                        log.warning(
                            "embedder.dim_mismatch",
                            existing=existing_dim,
                            configured=self.embedding_dims,
                            action="drop_and_recreate",
                        )
                        self.milvus.drop_collection(self.collection_name)
                        # Invalidate pipeline file-hash state and persist so a
                        # full re-embed survives the next daemon restart.
                        try:
                            files_state = getattr(self.state, "files", None)
                            if isinstance(files_state, dict):
                                files_state.clear()
                                self.state.save()
                        except Exception as exc:
                            log.warning("embedder.state_invalidate_failed", error=str(exc))
                        break
            else:
                return

        schema = CollectionSchema(
            fields=[
                FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=512),
                FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=self.embedding_dims),
                FieldSchema(name="record_type", dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name="name", dtype=DataType.VARCHAR, max_length=512),
            ],
            description="Vault file embeddings",
        )
        self.milvus.create_collection(
            collection_name=self.collection_name,
            schema=schema,
        )
        # Create index for vector search
        index_params = self.milvus.prepare_index_params()
        index_params.add_index(
            field_name="embedding",
            index_type="FLAT",
            metric_type="COSINE",
        )
        self.milvus.create_index(
            collection_name=self.collection_name,
            index_params=index_params,
        )
        log.info("embedder.collection_created", name=self.collection_name)

    async def _ensure_http(self) -> httpx.AsyncClient:
        """Lazily create and reuse a persistent HTTP client."""
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=60.0)
        return self._http

    async def close(self) -> None:
        """Clean up the persistent HTTP client."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()

    async def _get_embedding(self, text: str, rel_path: str = "") -> list[float] | None:
        """Call embedding API with retry. Supports Ollama and OpenAI-compatible endpoints.

        ``rel_path`` is purely for logging — it lets the retry/failure lines name
        the offending record so operators don't have to cross-reference batch
        contents when a single file trips the context-length window.
        """
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            body = {"model": self.model, "input": text}
        else:
            body = {"model": self.model, "prompt": text}

        text_len = len(text)
        client = await self._ensure_http()
        for attempt in range(MAX_RETRIES):
            try:
                resp = await client.post(
                    self.embed_url,
                    json=body,
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                if self.api_key:
                    return data["data"][0]["embedding"]
                return data["embedding"]
            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as e:
                detail = ""
                if isinstance(e, httpx.HTTPStatusError):
                    detail = e.response.text[:200]
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                log.warning(
                    "embedder.embed_retry",
                    attempt=attempt + 1,
                    path=rel_path,
                    embed_text_len=text_len,
                    error=str(e),
                    detail=detail,
                    delay=delay,
                )
                await asyncio.sleep(delay)
        log.error(
            "embedder.embed_failed",
            max_retries=MAX_RETRIES,
            path=rel_path,
            embed_text_len=text_len,
        )
        return None

    async def process_diff(
        self, new_paths: list[str], changed_paths: list[str], deleted_paths: list[str]
    ) -> dict[str, VaultRecord]:
        """Embed new/changed files, delete removed ones. Returns parsed records."""
        records: dict[str, VaultRecord] = {}
        # Track records that never made it to Milvus, broken down by reason so
        # operators can tell context-length trips (embed_failed) from parse
        # errors or empty-text skips. Previously diff_processed logged
        # upserted=len(to_embed) which counted ATTEMPTS, not successes — after
        # an embed_failed the log line claimed the vector was upserted even
        # though Milvus was never written, leaving state and vector store
        # silently out of sync. See project_embedder_context_length_bug memo.
        failed_embed: list[str] = []
        parse_errors: list[str] = []
        empty_skipped: list[str] = []

        # Upsert new + changed
        to_embed = new_paths + changed_paths
        for rel_path in to_embed:
            try:
                record = parse_file(self.vault_path, rel_path)
            except Exception as e:
                log.warning("embedder.parse_error", path=rel_path, error=str(e))
                parse_errors.append(rel_path)
                continue

            text = build_embedding_text(record)
            if not text.strip():
                log.debug("embedder.empty_text", path=rel_path)
                empty_skipped.append(rel_path)
                continue

            embedding = await self._get_embedding(text, rel_path=rel_path)
            if embedding is None:
                failed_embed.append(rel_path)
                continue

            # Throttle between requests to reduce Ollama pressure
            await asyncio.sleep(EMBED_THROTTLE)

            # Upsert to Milvus
            self.milvus.upsert(
                collection_name=self.collection_name,
                data=[{
                    "id": rel_path,
                    "embedding": embedding,
                    "record_type": record.record_type,
                    "name": record.frontmatter.get("name", rel_path),
                }],
            )
            self.state.mark_embedded(rel_path)
            records[rel_path] = record
            log.debug("embedder.upserted", path=rel_path)

        # Delete removed
        for rel_path in deleted_paths:
            try:
                self.milvus.delete(
                    collection_name=self.collection_name,
                    filter=f'id == "{rel_path}"',
                )
            except Exception as e:
                log.warning("embedder.delete_error", path=rel_path, error=str(e))
            self.state.remove_file(rel_path)
            log.debug("embedder.deleted", path=rel_path)

        # Emit a distinct event for records that tried to embed but failed, so
        # operators auditing Milvus freshness can see which vectors are now
        # stale and plan a targeted re-embed. Keep the event separate from the
        # success counter rather than folding it in, so post-hoc log greps like
        # ``grep diff_processed`` still summarize what actually landed.
        if failed_embed:
            log.warning(
                "embedder.diff_failed_records",
                count=len(failed_embed),
                records=failed_embed,
            )

        log.info(
            "embedder.diff_processed",
            upserted=len(records),
            attempted=len(to_embed),
            embed_failed=len(failed_embed),
            parse_errors=len(parse_errors),
            empty_skipped=len(empty_skipped),
            deleted=len(deleted_paths),
        )
        return records

    def get_all_embeddings(self) -> tuple[list[str], np.ndarray] | None:
        """Retrieve all embeddings from Milvus as (paths, matrix).

        Returns None if collection is empty.
        """
        PAGE_SIZE = 16_000  # Milvus caps query limit at 16,384
        all_results: list[dict] = []
        offset = 0

        while True:
            page = self.milvus.query(
                collection_name=self.collection_name,
                filter="",
                output_fields=["id", "embedding"],
                limit=PAGE_SIZE,
                offset=offset,
            )
            if not page:
                break
            all_results.extend(page)
            if len(page) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

        if not all_results:
            return None

        paths = [r["id"] for r in all_results]
        vectors = np.array([r["embedding"] for r in all_results], dtype=np.float32)
        return paths, vectors
