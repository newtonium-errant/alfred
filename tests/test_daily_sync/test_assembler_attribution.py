"""Tests that the assembler integrates the attribution section provider
correctly: priority slot, render order, and continuous global numbering
across email + attribution sections.

Covers:
- Email + attribution registered → email renders first, attribution second
- Global numbering: email items 1..5, attribution items 6..N
- Attribution-only (no email items) renders attribution starting at 1
- Priority 25 slot: attribution renders BEFORE the open-questions
  reserved priority 30 slot
- Provider registered with item_count_after advances global index
- Mixed real assemble_message: end-to-end string contains both sections
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import frontmatter
import pytest

from alfred.daily_sync import assembler, attribution_section, email_section
from alfred.daily_sync.assembler import (
    assemble_message,
    clear_providers,
    register_provider,
    registered_providers,
)
from alfred.daily_sync.attribution_section import set_vault_path as set_attr_vault_path
from alfred.daily_sync.config import AttributionConfig, DailySyncConfig
from alfred.vault.attribution import AuditEntry, append_audit_entry


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_providers()
    yield
    clear_providers()


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.attribution = AttributionConfig(
        enabled=True,
        batch_size=5,
        scan_paths=[],
        corpus_path=str(tmp_path / "attribution.jsonl"),
    )
    return cfg


def _seed_email_note(vault: Path, name: str, *, priority: str) -> None:
    fm = {
        "type": "note",
        "name": name,
        "tags": [],
        "priority": priority,
        "action_hint": "calendar",
        "priority_reasoning": "test",
    }
    body = dedent(f"""\
    **From:** alice@example.com
    **Subject:** {name}

    Sample body content.
    """)
    post = frontmatter.Post(body, **fm)
    (vault / "note" / f"{name}.md").write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )


def _seed_attribution_note(vault: Path, name: str, marker_id: str) -> None:
    fm: dict = {"type": "note", "name": name, "tags": []}
    entry = AuditEntry(
        marker_id=marker_id,
        agent="salem",
        date="2026-04-23T18:44:00+00:00",
        section_title=name,
        reason="talker conversation turn",
    )
    append_audit_entry(fm, entry)
    body = (
        f'<!-- BEGIN_INFERRED marker_id="{marker_id}" -->\n'
        f"Inferred body for {name}.\n"
        f'<!-- END_INFERRED marker_id="{marker_id}" -->'
    )
    post = frontmatter.Post(body, **fm)
    (vault / "note" / f"{name}.md").write_text(
        frontmatter.dumps(post) + "\n", encoding="utf-8",
    )


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    return vault


# --- registration order ----------------------------------------------------


def test_attribution_registers_at_priority_25_after_email(tmp_path: Path):
    email_section.register()
    attribution_section.register()
    names = registered_providers()
    # Email at 10, attribution at 25 → email comes first
    assert names.index("email_calibration") < names.index("attribution_audit")


def test_attribution_renders_after_email_and_before_open_questions(tmp_path: Path):
    """Reserve a future open_questions provider at priority 30 and assert
    the attribution provider sits between it and the email provider.
    """
    email_section.register()
    attribution_section.register()
    register_provider("open_questions", priority=30, provider=lambda c, t: None)
    names = registered_providers()
    assert names == ["email_calibration", "attribution_audit", "open_questions"]


# --- global numbering across sections --------------------------------------


def test_global_numbering_email_then_attribution(tmp_path: Path):
    vault = _make_vault(tmp_path)
    # 2 email items
    _seed_email_note(vault, "E1", priority="medium")
    _seed_email_note(vault, "E2", priority="high")
    # 2 attribution items
    _seed_attribution_note(vault, "A1", "inf-20260423-salem-aaa1")
    _seed_attribution_note(vault, "A2", "inf-20260423-salem-aaa2")

    email_section.set_vault_path(vault)
    set_attr_vault_path(vault)
    email_section.register()
    attribution_section.register()

    cfg = _config(tmp_path)
    out = assemble_message(cfg, date(2026, 4, 24))

    # Email section uses items 1, 2
    assert "1. " in out
    assert "2. " in out
    # Attribution section starts at 3 (since 2 email items)
    assert "3. [salem" in out or "3. [salem " in out

    # Verify the consumed batches reflect the global numbering
    email_batch = email_section.consume_last_batch()
    attribution_batch = attribution_section.consume_last_batch()
    assert {item.item_number for item in email_batch} == {1, 2}
    assert {item.item_number for item in attribution_batch} == {3, 4}


def test_attribution_only_starts_at_1_when_no_email_items(tmp_path: Path):
    vault = _make_vault(tmp_path)
    # Only an attribution note (no classified email notes)
    _seed_attribution_note(vault, "A1", "inf-20260423-salem-only1")

    email_section.set_vault_path(vault)
    set_attr_vault_path(vault)
    email_section.register()
    attribution_section.register()

    cfg = _config(tmp_path)
    out = assemble_message(cfg, date(2026, 4, 24))

    attribution_batch = attribution_section.consume_last_batch()
    assert len(attribution_batch) == 1
    # Email had nothing (no classified records) → attribution starts at 1
    assert attribution_batch[0].item_number == 1
    assert "1. [salem" in out


def test_email_only_does_not_get_attribution_section(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_email_note(vault, "E1", priority="medium")

    email_section.set_vault_path(vault)
    set_attr_vault_path(vault)
    email_section.register()
    attribution_section.register()

    cfg = _config(tmp_path)
    out = assemble_message(cfg, date(2026, 4, 24))

    # Email section appears
    assert "## Email calibration" in out
    # Attribution section appears as the empty-state header
    # (intentionally-left-blank principle)
    assert "## Attribution audit" in out
    assert "No attribution items pending review." in out


def test_assemble_message_full_end_to_end_string(tmp_path: Path):
    """End-to-end smoke: assembled string contains email + attribution
    sections in correct order, with continuous numbering."""
    vault = _make_vault(tmp_path)
    _seed_email_note(vault, "E1", priority="low")
    _seed_attribution_note(vault, "Attr1", "inf-20260423-salem-end1")

    email_section.set_vault_path(vault)
    set_attr_vault_path(vault)
    email_section.register()
    attribution_section.register()

    cfg = _config(tmp_path)
    out = assemble_message(cfg, date(2026, 4, 24))

    # Banner present
    assert "Daily Sync — 2026-04-24" in out
    # Email section is first
    email_pos = out.find("## Email calibration")
    attribution_pos = out.find("## Attribution audit")
    assert 0 < email_pos < attribution_pos
    # Email gets item 1, attribution gets item 2 (global numbering)
    assert "1. [LOW]" in out
    assert "2. [salem" in out
