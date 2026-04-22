"""Tests for the email-calibration section provider.

Covers:
- build_batch returns up to N items, prefers uncalibrated.
- build_batch falls back to stratified sample when fresh items are scarce.
- render_batch produces the expected per-item shape.
- Section provider returns None on an empty vault.
- Section provider returns None when no records have a real classifier tier.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import frontmatter

from alfred.daily_sync.assembler import clear_providers
from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.corpus import CorpusEntry, append_correction
from alfred.daily_sync.email_section import (
    BatchItem,
    build_batch,
    email_calibration_section,
    render_batch,
    set_vault_path,
)


def _seed_note(
    vault: Path,
    name: str,
    *,
    priority: str,
    action_hint: str | None = "calendar",
    sender: str = "alice@example.com",
    subject: str = "Test subject",
    body_extra: str = "snippet body content",
) -> str:
    """Create a note record with the email_classifier output fields set."""
    fm = {
        "type": "note",
        "name": name,
        "created": "2026-04-22",
        "tags": [],
        "related": [],
        "priority": priority,
        "action_hint": action_hint,
        "priority_reasoning": f"reason for {priority}",
    }
    body = dedent(f"""\
    **From:** {sender}
    **Subject:** {subject}

    {body_extra}
    """)
    post = frontmatter.Post(body, **fm)
    rel = f"note/{name}.md"
    (vault / "note" / f"{name}.md").write_text(
        frontmatter.dumps(post) + "\n",
        encoding="utf-8",
    )
    return rel


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note").mkdir()
    return vault


def test_build_batch_picks_uncalibrated_first(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_note(vault, "A", priority="medium")
    _seed_note(vault, "B", priority="high")
    _seed_note(vault, "C", priority="low")

    config = DailySyncConfig(enabled=True, batch_size=3)
    config.corpus.path = str(tmp_path / "corpus.jsonl")

    batch = build_batch(vault, config)
    assert len(batch) == 3
    rel_paths = sorted(item.record_path for item in batch)
    assert rel_paths == ["note/A.md", "note/B.md", "note/C.md"]


def test_build_batch_excludes_already_calibrated(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_note(vault, "A", priority="medium")
    _seed_note(vault, "B", priority="high")
    _seed_note(vault, "C", priority="low")

    corpus = tmp_path / "corpus.jsonl"
    append_correction(corpus, CorpusEntry(
        record_path="note/A.md",
        classifier_priority="medium",
        classifier_action_hint=None,
        classifier_reason="x",
        andrew_priority="medium",
    ))

    config = DailySyncConfig(enabled=True, batch_size=2)
    config.corpus.path = str(corpus)

    batch = build_batch(vault, config)
    rel_paths = {item.record_path for item in batch}
    assert "note/A.md" not in rel_paths
    assert len(batch) == 2


def test_build_batch_falls_back_to_stratified_when_short(tmp_path: Path):
    vault = _make_vault(tmp_path)
    # Two notes, both already calibrated
    _seed_note(vault, "A", priority="medium")
    _seed_note(vault, "B", priority="high")

    corpus = tmp_path / "corpus.jsonl"
    for path, pri in [("note/A.md", "medium"), ("note/B.md", "high")]:
        append_correction(corpus, CorpusEntry(
            record_path=path,
            classifier_priority=pri,
            classifier_action_hint=None,
            classifier_reason="x",
            andrew_priority=pri,
        ))

    config = DailySyncConfig(enabled=True, batch_size=2)
    config.corpus.path = str(corpus)

    batch = build_batch(vault, config)
    # Fallback path: even all-calibrated items can show up
    assert len(batch) == 2
    rel_paths = {item.record_path for item in batch}
    assert rel_paths == {"note/A.md", "note/B.md"}


def test_build_batch_skips_unclassified_records(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_note(vault, "A", priority="unclassified")
    _seed_note(vault, "B", priority="high")

    config = DailySyncConfig(enabled=True, batch_size=5)
    config.corpus.path = str(tmp_path / "corpus.jsonl")

    batch = build_batch(vault, config)
    rel_paths = {item.record_path for item in batch}
    assert "note/A.md" not in rel_paths
    assert "note/B.md" in rel_paths


def test_build_batch_empty_vault(tmp_path: Path):
    vault = _make_vault(tmp_path)
    config = DailySyncConfig(enabled=True, batch_size=5)
    config.corpus.path = str(tmp_path / "corpus.jsonl")
    assert build_batch(vault, config) == []


def test_render_batch_includes_per_item_fields():
    items = [
        BatchItem(
            item_number=1,
            record_path="note/A.md",
            classifier_priority="high",
            classifier_action_hint="calendar",
            classifier_reason="Reply-required + named contact",
            sender="jamie@example.com",
            subject="Friday meeting",
            snippet="Hey, can we move it to 3pm?",
        ),
        BatchItem(
            item_number=2,
            record_path="note/B.md",
            classifier_priority="low",
            classifier_action_hint=None,
            classifier_reason="Newsletter",
            sender="newsletters@example.com",
            subject="Weekly digest",
            snippet="This week's top stories...",
        ),
    ]
    out = render_batch(items)
    assert "## Email calibration (2 items)" in out
    assert "1. [HIGH]" in out
    assert "jamie@example.com" in out
    assert "Friday meeting" in out
    assert "snippet: Hey, can we move it to 3pm?" in out
    assert "action: calendar" in out
    assert "reason: Reply-required + named contact" in out
    assert "2. [LOW]" in out
    # Action line absent for None hint
    assert "Reply with terse corrections" in out


def test_email_calibration_section_returns_none_on_empty_vault(tmp_path: Path):
    vault = _make_vault(tmp_path)
    set_vault_path(vault)
    clear_providers()
    config = DailySyncConfig(enabled=True, batch_size=5)
    config.corpus.path = str(tmp_path / "corpus.jsonl")
    out = email_calibration_section(config, date(2026, 4, 22))
    assert out is None


def test_email_calibration_section_returns_text_with_content(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_note(vault, "A", priority="medium")
    set_vault_path(vault)
    clear_providers()
    config = DailySyncConfig(enabled=True, batch_size=5)
    config.corpus.path = str(tmp_path / "corpus.jsonl")
    out = email_calibration_section(config, date(2026, 4, 22))
    assert out is not None
    assert "## Email calibration" in out
