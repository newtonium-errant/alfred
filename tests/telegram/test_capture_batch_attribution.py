"""c4: capture_batch.write_summary_to_session_record stamps an
attribution_audit entry + BEGIN_INFERRED markers around the
``## Structured Summary`` block when the structuring succeeds.

Failure-mode writes (``structured_flag="failed"``) skip the wrap —
there's no inferred prose to attribute, just a human-readable error.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import frontmatter
import pytest

from alfred.telegram.capture_batch import (
    StructuredSummary,
    render_failure_markdown,
    render_summary_markdown,
    write_summary_to_session_record,
)


def _seed_session(vault: Path, rel_path: str) -> Path:
    full = vault / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        "---\n"
        "type: session\n"
        "name: Capture Test\n"
        "created: '2026-04-23'\n"
        "---\n\n"
        "# Transcript\n\nSome user content.\n",
        encoding="utf-8",
    )
    return full


def _read(path: Path) -> tuple[dict, str]:
    post = frontmatter.load(str(path))
    return dict(post.metadata), post.content


def test_capture_batch_success_wraps_summary(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    rel = "session/Voice Session — Test.md"
    full = _seed_session(vault, rel)

    summary = StructuredSummary(
        topics=["alfred design"],
        decisions=["ship c4"],
        action_items=[],
        key_insights=[],
        open_questions=[],
        raw_contradictions=[],
    )
    md = render_summary_markdown(summary)

    asyncio.run(write_summary_to_session_record(vault, rel, md, "true"))
    fm, content = _read(full)

    assert "BEGIN_INFERRED" in content
    assert "END_INFERRED" in content
    assert "## Structured Summary" in content
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list)
    assert len(audit) == 1
    entry = audit[0]
    assert entry["agent"] == "salem"
    assert entry["section_title"] == "Structured Summary"
    assert "capture batch structuring" in entry["reason"]
    assert rel in entry["reason"]
    assert entry["confirmed_by_andrew"] is False


def test_capture_batch_failure_skips_wrapping(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    rel = "session/Voice Session — Failed.md"
    full = _seed_session(vault, rel)

    md = render_failure_markdown("API timeout")

    asyncio.run(write_summary_to_session_record(vault, rel, md, "failed"))
    fm, content = _read(full)

    assert "BEGIN_INFERRED" not in content
    # The failure marker still landed via the dynamic-block wrapper.
    assert "Structuring failed" in content
    assert "attribution_audit" not in fm


def test_capture_batch_preserves_existing_audit(tmp_path: Path):
    """A second capture-batch write on the same record must merge with
    any prior attribution_audit entries, not overwrite them."""
    vault = tmp_path / "vault"
    vault.mkdir()
    rel = "session/Voice Session — Merge.md"
    full = vault / rel
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(
        "---\n"
        "type: session\n"
        "name: Merge Test\n"
        "created: '2026-04-23'\n"
        "attribution_audit:\n"
        "  - marker_id: inf-20260420-salem-deadbe\n"
        "    agent: salem\n"
        "    date: '2026-04-20T00:00:00+00:00'\n"
        "    section_title: Prior Section\n"
        "    reason: prior write\n"
        "    confirmed_by_andrew: false\n"
        "    confirmed_at: null\n"
        "---\n\n"
        "# Transcript\n\nbody.\n",
        encoding="utf-8",
    )

    summary = StructuredSummary(topics=["x"])
    md = render_summary_markdown(summary)
    asyncio.run(write_summary_to_session_record(vault, rel, md, "true"))

    fm, _ = _read(full)
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list)
    assert len(audit) == 2
    ids = {e["marker_id"] for e in audit}
    assert "inf-20260420-salem-deadbe" in ids
