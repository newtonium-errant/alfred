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
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0


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

        # Milvus Lite client
        self.milvus = MilvusClient(uri=milvus_cfg.uri)
        self.collection_name = milvus_cfg.collection_name
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        """Create the Milvus collection if it doesn't exist."""
        if self.milvus.has_collection(self.collection_name):
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

    async def _get_embedding(self, text: str) -> list[float] | None:
        """Call embedding API with retry. Supports Ollama and OpenAI-compatible endpoints."""
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            body = {"model": self.model, "input": text}
        else:
            body = {"model": self.model, "prompt": text}

        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
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
                delay = RETRY_BASE_DELAY * (2 ** attempt)
                log.warning("embedder.embed_retry", attempt=attempt + 1, error=str(e), delay=delay)
                await asyncio.sleep(delay)
        log.error("embedder.embed_failed", max_retries=MAX_RETRIES)
        return None

    async def process_diff(
        self, new_paths: list[str], changed_paths: list[str], deleted_paths: list[str]
    ) -> dict[str, VaultRecord]:
        """Embed new/changed files, delete removed ones. Returns parsed records."""
        records: dict[str, VaultRecord] = {}

        # Upsert new + changed
        to_embed = new_paths + changed_paths
        for rel_path in to_embed:
            try:
                record = parse_file(self.vault_path, rel_path)
            except Exception as e:
                log.warning("embedder.parse_error", path=rel_path, error=str(e))
                continue

            text = build_embedding_text(record)
            if not text.strip():
                log.debug("embedder.empty_text", path=rel_path)
                continue

            embedding = await self._get_embedding(text)
            if embedding is None:
                continue

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

        log.info(
            "embedder.diff_processed",
            upserted=len(to_embed),
            deleted=len(deleted_paths),
        )
        return records

    def get_all_embeddings(self) -> tuple[list[str], np.ndarray] | None:
        """Retrieve all embeddings from Milvus as (paths, matrix).

        Returns None if collection is empty.
        """
        results = self.milvus.query(
            collection_name=self.collection_name,
            filter="",
            output_fields=["id", "embedding"],
            limit=100_000,
        )
        if not results:
            return None

        paths = [r["id"] for r in results]
        vectors = np.array([r["embedding"] for r in results], dtype=np.float32)
        return paths, vectors
