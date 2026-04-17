"""Safe frontmatter write-back — alfred_tags and relationships."""

from __future__ import annotations

import os
from pathlib import Path

import frontmatter
import structlog

from .state import PipelineState
from .utils import compute_md5_bytes

log = structlog.get_logger()


class VaultWriter:
    def __init__(self, vault_path: Path, state: PipelineState) -> None:
        self.vault_path = vault_path
        self.state = state

    def write_alfred_tags(self, rel_path: str, tags: list[str]) -> None:
        """Set alfred_tags in frontmatter.

        Skips the write when the normalized (sorted + deduped) new tag list
        equals the normalized existing tag list. Cluster membership shifts
        cause the labeler to re-propose tags every sweep; rewriting the file
        with semantically identical content churns the vault's git history
        for no gain, so we early-return on equality.
        """
        full_path = self.vault_path / rel_path
        if not full_path.exists():
            log.warning("writer.file_not_found", path=rel_path)
            return

        try:
            raw = full_path.read_text(encoding="utf-8")
            post = frontmatter.loads(raw)
        except Exception as e:
            log.warning("writer.parse_error", path=rel_path, error=str(e))
            return

        existing = post.metadata.get("alfred_tags", []) or []
        if isinstance(existing, str):
            existing = [existing]

        # Normalize by sorting + deduplicating. ["a", "b"] vs ["b", "a", "b"]
        # compare equal so duplicate-seeded inputs don't drive spurious writes.
        norm_existing = sorted(set(str(t) for t in existing))
        norm_new = sorted(set(str(t) for t in tags))

        if norm_existing == norm_new:
            log.info(
                "writer.tags_unchanged",
                path=rel_path,
                tag_count=len(norm_new),
            )
            return

        post.metadata["alfred_tags"] = tags
        self._write_atomic(full_path, rel_path, post)
        log.info(
            "writer.tags_updated",
            path=rel_path,
            before_count=len(norm_existing),
            after_count=len(norm_new),
            tags=tags,
        )

    def write_relationships(self, rel_path: str, new_rels: list[dict]) -> None:
        """Append machine-generated relationships (only those with confidence < 1.0).

        Never touch human-authored entries (those without a confidence field).
        """
        if not new_rels:
            return

        full_path = self.vault_path / rel_path
        if not full_path.exists():
            log.warning("writer.file_not_found", path=rel_path)
            return

        try:
            raw = full_path.read_text(encoding="utf-8")
            post = frontmatter.loads(raw)
        except Exception as e:
            log.warning("writer.parse_error", path=rel_path, error=str(e))
            return

        existing_rels: list[dict] = post.metadata.get("relationships", [])

        # Dedupe the incoming batch against itself before touching the file.
        # The labeler sometimes emits near-duplicate rels in a single call
        # (same source+target, possibly with different confidence/context);
        # without this pass each near-dupe would drive a separate append +
        # log line. Key on (target, type) so genuinely distinct relationship
        # types between the same pair survive if the policy ever changes,
        # while exact-pair dupes collapse to the first occurrence.
        deduped_new: list[dict] = []
        seen_new: set[tuple[str, str]] = set()
        for rel in new_rels:
            key = (rel.get("target", ""), rel.get("type", ""))
            if not key[0]:
                continue
            if key in seen_new:
                continue
            seen_new.add(key)
            deduped_new.append(rel)

        # Build set of existing machine-generated relationship targets
        existing_targets = set()
        for rel in existing_rels:
            if "confidence" in rel:
                existing_targets.add(rel.get("target", ""))

        # Only add truly new relationships
        added = 0
        for rel in deduped_new:
            target = rel.get("target", "")
            if target and target not in existing_targets:
                existing_rels.append(rel)
                existing_targets.add(target)
                added += 1

        if added == 0:
            return

        post.metadata["relationships"] = existing_rels
        self._write_atomic(full_path, rel_path, post)
        log.info("writer.relationships_written", path=rel_path, added=added)

    def _write_atomic(self, full_path: Path, rel_path: str, post: frontmatter.Post) -> None:
        """Write file atomically and register expected hash in state."""
        content = frontmatter.dumps(post) + "\n"
        content_bytes = content.encode("utf-8")
        expected_md5 = compute_md5_bytes(content_bytes)

        # Mark pending write BEFORE writing so the watcher ignores it
        self.state.mark_pending_write(rel_path, expected_md5)

        # Atomic write: .tmp → rename
        tmp_path = full_path.with_suffix(".md.tmp")
        try:
            tmp_path.write_bytes(content_bytes)
            os.replace(tmp_path, full_path)
        except OSError as e:
            log.error("writer.write_error", path=rel_path, error=str(e))
            # Clean up pending write on failure
            self.state.pending_writes.pop(rel_path, None)
            if tmp_path.exists():
                tmp_path.unlink()
            return

        # Update file hash in state
        self.state.update_file(rel_path, expected_md5)
