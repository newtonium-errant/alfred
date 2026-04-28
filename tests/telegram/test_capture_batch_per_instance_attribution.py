"""Per-instance attribution slug for the capture-batch summary writer.

Item 2 of the deferred Hypatia hardcoding sweep
(``project_hardcoding_followups.md``): ``capture_batch.write_summary
_to_session_record`` was hardcoded to ``agent="salem"`` — Hypatia
capture sessions got tagged as Salem in their summary frontmatter.

This test exercises the new ``agent_slug`` keyword: pass
``"salem"``, ``"hypatia"``, etc., and the BEGIN_INFERRED audit_audit
entry's ``agent`` field carries that exact slug. The legacy default
of ``"salem"`` is preserved by the existing
``test_capture_batch_attribution.py`` suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import frontmatter

from alfred.telegram.capture_batch import (
    StructuredSummary,
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
        "created: '2026-04-26'\n"
        "---\n\n"
        "# Transcript\n\nSome user content.\n",
        encoding="utf-8",
    )
    return full


def _read(path: Path) -> tuple[dict, str]:
    post = frontmatter.load(str(path))
    return dict(post.metadata), post.content


def _summary_md() -> str:
    summary = StructuredSummary(
        topics=["multi-instance attribution"],
        decisions=[],
        action_items=[],
        key_insights=[],
        open_questions=[],
        raw_contradictions=[],
    )
    return render_summary_markdown(summary)


def test_capture_batch_stamps_hypatia_slug_when_passed(tmp_path: Path):
    """A Hypatia-side capture stamps ``agent: hypatia`` on the audit entry."""
    vault = tmp_path / "vault"
    vault.mkdir()
    rel = "session/Voice Session — Hypatia.md"
    full = _seed_session(vault, rel)

    asyncio.run(
        write_summary_to_session_record(
            vault, rel, _summary_md(), "true", agent_slug="hypatia",
        )
    )
    fm, content = _read(full)

    assert "BEGIN_INFERRED" in content
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    assert audit[0]["agent"] == "hypatia"


def test_capture_batch_stamps_salem_slug_explicit_pass(tmp_path: Path):
    """Salem-side capture (explicitly threaded slug) lands ``agent: salem``."""
    vault = tmp_path / "vault"
    vault.mkdir()
    rel = "session/Voice Session — Salem.md"
    full = _seed_session(vault, rel)

    asyncio.run(
        write_summary_to_session_record(
            vault, rel, _summary_md(), "true", agent_slug="salem",
        )
    )
    fm, _ = _read(full)
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    assert audit[0]["agent"] == "salem"


def test_capture_batch_default_slug_preserves_legacy_behavior(tmp_path: Path):
    """No-keyword call keeps the historical ``"salem"`` default."""
    vault = tmp_path / "vault"
    vault.mkdir()
    rel = "session/Voice Session — Default.md"
    full = _seed_session(vault, rel)

    asyncio.run(
        write_summary_to_session_record(vault, rel, _summary_md(), "true")
    )
    fm, _ = _read(full)
    audit = fm.get("attribution_audit")
    assert isinstance(audit, list) and len(audit) == 1
    assert audit[0]["agent"] == "salem"
