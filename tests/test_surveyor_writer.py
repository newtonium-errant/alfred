"""Tests for ``alfred.surveyor.writer.VaultWriter.write_alfred_tags``.

The skip-if-equal contract is a drift guard: the surveyor labeler re-proposes
``alfred_tags`` on every cluster sweep, and cluster-membership shifts cause
semantically identical tag lists to come through as "new" writes. Without
the normalization early-return, every sweep would churn the vault's git
history and re-fire the audit log for no functional change.

These tests lock the contract in place against the writer's two key
surfaces: the early-return path (no write) and the genuine-change path
(write + audit-log entry).
"""

from __future__ import annotations

import json
from pathlib import Path
from textwrap import dedent

import frontmatter

from alfred.surveyor.state import PipelineState
from alfred.surveyor.writer import VaultWriter


def _seed_record(vault: Path, rel: str, tags: list[str] | None) -> None:
    """Write a minimal tagged note into ``vault/rel``."""
    fm_tags = f"alfred_tags: {tags!r}\n" if tags is not None else ""
    content = dedent(
        f"""\
        ---
        type: note
        name: Seed Note
        created: 2026-04-19
        {fm_tags}---

        body
        """
    )
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


class TestWriteAlfredTags:
    def test_skips_when_normalized_tags_match(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Existing tags already equal (after sort+dedupe) the proposed
        # tags → writer must NOT touch the file and must NOT append to
        # the audit log. Tag order differs + duplicates present in the
        # input to exercise the normalization step.
        _seed_record(tmp_vault, "note/Seed.md", ["alpha", "beta"])
        mtime_before = (tmp_vault / "note/Seed.md").stat().st_mtime_ns

        audit_path = tmp_path / "vault_audit.log"
        state = PipelineState(tmp_path / "surveyor_state.json")
        writer = VaultWriter(tmp_vault, state, audit_log_path=audit_path)

        writer.write_alfred_tags("note/Seed.md", ["beta", "alpha", "beta"])

        mtime_after = (tmp_vault / "note/Seed.md").stat().st_mtime_ns
        assert mtime_before == mtime_after, "file must not be rewritten"
        assert not audit_path.exists(), "audit log must not be touched"
        assert "note/Seed.md" not in state.pending_writes

    def test_writes_when_tags_differ(
        self, tmp_vault: Path, tmp_path: Path
    ) -> None:
        # Proposed tags differ from existing → writer rewrites the file,
        # updates state, and appends exactly one audit-log line tagged
        # ``surveyor`` / ``modify``.
        _seed_record(tmp_vault, "note/Seed.md", ["alpha"])

        audit_path = tmp_path / "vault_audit.log"
        state = PipelineState(tmp_path / "surveyor_state.json")
        writer = VaultWriter(tmp_vault, state, audit_log_path=audit_path)

        writer.write_alfred_tags("note/Seed.md", ["alpha", "beta"])

        post = frontmatter.load(str(tmp_vault / "note/Seed.md"))
        assert post.metadata["alfred_tags"] == ["alpha", "beta"]
        assert "note/Seed.md" in state.files, "state must track new hash"

        audit_lines = audit_path.read_text(encoding="utf-8").splitlines()
        assert len(audit_lines) == 1
        entry = json.loads(audit_lines[0])
        assert entry["tool"] == "surveyor"
        assert entry["op"] == "modify"
        assert entry["path"] == "note/Seed.md"
