"""Tests for the attribution-audit section provider (Phase 2 of audit arc).

Covers:
- Scanning a tmp_vault: surfaces only unconfirmed entries
- Sort order: most recent unconfirmed first
- batch_size cap respected
- Empty-state: emits "No attribution items pending review."
- Item rendering format matches the spec
- Global item indexing via start_index parameter
- consume_last_batch persists per-item mapping
- scan_paths restriction
- Records that point to a marker_id not in the body still surface
- Confirmed entries are filtered out
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import frontmatter
import pytest

from alfred.daily_sync.assembler import clear_providers
from alfred.daily_sync.attribution_section import (
    AttributionItem,
    attribution_audit_section,
    build_batch,
    consume_last_batch,
    peek_last_batch_count,
    render_batch,
    set_vault_path,
)
from alfred.daily_sync.config import AttributionConfig, DailySyncConfig
from alfred.vault.attribution import AuditEntry, append_audit_entry, with_inferred_marker


def _seed_record(
    vault: Path,
    rel_path: str,
    *,
    audit_entries: list[AuditEntry],
    body_sections: list[tuple[str, str]] | None = None,
    extra_fm: dict | None = None,
) -> Path:
    """Create a record with the given audit entries and optional wrapped sections.

    ``body_sections`` is a list of ``(marker_id, content)`` pairs. Each
    pair is rendered as a BEGIN_INFERRED/END_INFERRED block so the
    section provider can pull the content preview.
    """
    file_path = vault / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fm: dict = {
        "type": "note",
        "name": rel_path.rsplit("/", 1)[-1].removesuffix(".md"),
        "tags": [],
    }
    if extra_fm:
        fm.update(extra_fm)
    for entry in audit_entries:
        append_audit_entry(fm, entry)
    body_parts: list[str] = []
    for marker_id, content in (body_sections or []):
        body_parts.append(
            f'<!-- BEGIN_INFERRED marker_id="{marker_id}" -->\n'
            f"{content}\n"
            f'<!-- END_INFERRED marker_id="{marker_id}" -->'
        )
    body = "\n\n".join(body_parts) if body_parts else ""
    post = frontmatter.Post(body, **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return file_path


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    return vault


def _entry(
    *,
    marker_id: str,
    agent: str = "salem",
    date: str = "2026-04-23T18:44:00+00:00",
    section_title: str = "Test Section",
    reason: str = "talker conversation turn",
    confirmed: bool = False,
    confirmed_at: str | None = None,
) -> AuditEntry:
    return AuditEntry(
        marker_id=marker_id,
        agent=agent,
        date=date,
        section_title=section_title,
        reason=reason,
        confirmed_by_andrew=confirmed,
        confirmed_at=confirmed_at,
    )


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.attribution = AttributionConfig(
        enabled=True,
        batch_size=5,
        scan_paths=[],
        corpus_path=str(tmp_path / "attribution_corpus.jsonl"),
    )
    return cfg


# --- build_batch ----------------------------------------------------------


def test_build_batch_surfaces_only_unconfirmed(tmp_path: Path):
    vault = _make_vault(tmp_path)
    confirmed = _entry(
        marker_id="inf-20260423-salem-aaa111",
        confirmed=True,
        confirmed_at="2026-04-23T19:00:00+00:00",
    )
    unconfirmed = _entry(marker_id="inf-20260423-salem-bbb222")
    _seed_record(
        vault, "note/A.md",
        audit_entries=[confirmed, unconfirmed],
        body_sections=[
            ("inf-20260423-salem-aaa111", "first content (confirmed)"),
            ("inf-20260423-salem-bbb222", "second content (unconfirmed)"),
        ],
    )
    cfg = _config(tmp_path)
    batch = build_batch(vault, cfg)
    assert len(batch) == 1
    assert batch[0].marker_id == "inf-20260423-salem-bbb222"


def test_build_batch_sorts_newest_first(tmp_path: Path):
    vault = _make_vault(tmp_path)
    older = _entry(
        marker_id="inf-20260420-salem-old111",
        date="2026-04-20T10:00:00+00:00",
        section_title="Older",
    )
    newer = _entry(
        marker_id="inf-20260423-salem-new222",
        date="2026-04-23T18:00:00+00:00",
        section_title="Newer",
    )
    _seed_record(
        vault, "note/A.md",
        audit_entries=[older],
        body_sections=[("inf-20260420-salem-old111", "older body")],
    )
    _seed_record(
        vault, "note/B.md",
        audit_entries=[newer],
        body_sections=[("inf-20260423-salem-new222", "newer body")],
    )
    cfg = _config(tmp_path)
    batch = build_batch(vault, cfg)
    assert [item.marker_id for item in batch] == [
        "inf-20260423-salem-new222",
        "inf-20260420-salem-old111",
    ]


def test_build_batch_caps_at_batch_size(tmp_path: Path):
    vault = _make_vault(tmp_path)
    for i in range(10):
        marker_id = f"inf-20260423-salem-item{i:03d}"
        _seed_record(
            vault, f"note/Item{i}.md",
            audit_entries=[_entry(
                marker_id=marker_id,
                date=f"2026-04-23T{10 + i:02d}:00:00+00:00",
            )],
            body_sections=[(marker_id, f"content {i}")],
        )
    cfg = _config(tmp_path)
    cfg.attribution.batch_size = 3
    batch = build_batch(vault, cfg)
    assert len(batch) == 3
    # Newest first
    assert batch[0].marker_id == "inf-20260423-salem-item009"


def test_build_batch_empty_vault(tmp_path: Path):
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    assert build_batch(vault, cfg) == []


def test_build_batch_global_start_index(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_record(
        vault, "note/A.md",
        audit_entries=[_entry(marker_id="inf-20260423-salem-x")],
        body_sections=[("inf-20260423-salem-x", "body x")],
    )
    cfg = _config(tmp_path)
    batch = build_batch(vault, cfg, start_index=6)
    assert len(batch) == 1
    assert batch[0].item_number == 6


def test_build_batch_respects_scan_paths(tmp_path: Path):
    vault = _make_vault(tmp_path)
    (vault / "person").mkdir()
    _seed_record(
        vault, "note/InNote.md",
        audit_entries=[_entry(marker_id="inf-20260423-salem-note1")],
        body_sections=[("inf-20260423-salem-note1", "note content")],
    )
    _seed_record(
        vault, "person/InPerson.md",
        audit_entries=[_entry(marker_id="inf-20260423-salem-pers1")],
        body_sections=[("inf-20260423-salem-pers1", "person content")],
    )
    cfg = _config(tmp_path)
    cfg.attribution.scan_paths = ["person"]
    batch = build_batch(vault, cfg)
    assert len(batch) == 1
    assert batch[0].marker_id == "inf-20260423-salem-pers1"


def test_build_batch_skips_confirmed_via_confirmed_at(tmp_path: Path):
    """An entry with confirmed_by_andrew=False but confirmed_at non-null
    is still considered confirmed (defensive against a partial-write)."""
    vault = _make_vault(tmp_path)
    weird = _entry(
        marker_id="inf-20260423-salem-weird",
        confirmed=False,
        confirmed_at="2026-04-23T20:00:00+00:00",
    )
    _seed_record(
        vault, "note/A.md",
        audit_entries=[weird],
        body_sections=[("inf-20260423-salem-weird", "x")],
    )
    cfg = _config(tmp_path)
    assert build_batch(vault, cfg) == []


def test_build_batch_attribution_disabled(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_record(
        vault, "note/A.md",
        audit_entries=[_entry(marker_id="inf-20260423-salem-x")],
        body_sections=[("inf-20260423-salem-x", "body x")],
    )
    cfg = _config(tmp_path)
    cfg.attribution.enabled = False
    assert build_batch(vault, cfg) == []


# --- render_batch ---------------------------------------------------------


def test_render_batch_empty_state_intentionally_left_blank():
    out = render_batch([])
    assert "## Attribution audit" in out
    assert "No attribution items pending review." in out


def test_render_batch_item_format_matches_spec():
    item = AttributionItem(
        item_number=6,
        record_path="note/Marker Smoke Test.md",
        marker_id="inf-20260423-salem-fc766c",
        agent="salem",
        date="2026-04-23T18:44:00+00:00",
        section_title="Marker Smoke Test",
        reason="talker conversation turn (session=78a7c5a2)",
        content_preview="Testing the attribution audit marker. This content should be wrapped.",
    )
    out = render_batch([item])
    assert "## Attribution audit (1 item)" in out
    assert "6. [salem 2026-04-23 18:44 — note/Marker Smoke Test]" in out
    assert 'Section: "Marker Smoke Test"' in out
    assert 'Content: "Testing the attribution audit marker.' in out
    assert "Reason: talker conversation turn (session=78a7c5a2)" in out
    assert "N confirm" in out  # reply hint


def test_render_batch_multiple_items_pluralizes():
    items = [
        AttributionItem(
            item_number=6,
            record_path="note/A.md",
            marker_id="inf-20260423-salem-a",
            agent="salem",
            date="2026-04-23T10:00:00+00:00",
            section_title="A",
            reason="r",
            content_preview="aaa",
        ),
        AttributionItem(
            item_number=7,
            record_path="note/B.md",
            marker_id="inf-20260423-salem-b",
            agent="salem",
            date="2026-04-23T11:00:00+00:00",
            section_title="B",
            reason="r",
            content_preview="bbb",
        ),
    ]
    out = render_batch(items)
    assert "## Attribution audit (2 items)" in out


# --- attribution_audit_section (provider entry point) --------------------


def test_section_provider_returns_empty_state_when_nothing_pending(tmp_path: Path):
    vault = _make_vault(tmp_path)
    set_vault_path(vault)
    clear_providers()
    cfg = _config(tmp_path)
    out = attribution_audit_section(cfg, date(2026, 4, 24))
    assert out is not None
    assert "No attribution items pending review." in out


def test_section_provider_returns_none_when_disabled(tmp_path: Path):
    vault = _make_vault(tmp_path)
    set_vault_path(vault)
    clear_providers()
    cfg = _config(tmp_path)
    cfg.attribution.enabled = False
    out = attribution_audit_section(cfg, date(2026, 4, 24))
    assert out is None


def test_section_provider_returns_none_when_vault_unset(tmp_path: Path):
    set_vault_path(tmp_path / "does-not-exist")
    clear_providers()
    cfg = _config(tmp_path)
    out = attribution_audit_section(cfg, date(2026, 4, 24))
    assert out is None


def test_section_provider_renders_pending_items(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_record(
        vault, "note/A.md",
        audit_entries=[_entry(marker_id="inf-20260423-salem-aaa")],
        body_sections=[("inf-20260423-salem-aaa", "wrapped content")],
    )
    set_vault_path(vault)
    clear_providers()
    cfg = _config(tmp_path)
    out = attribution_audit_section(cfg, date(2026, 4, 24))
    assert out is not None
    assert "## Attribution audit (1 item)" in out
    assert "inf-20260423-salem-aaa" not in out  # marker_id NOT shown
    assert "salem" in out


def test_section_provider_consumes_last_batch_after_render(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_record(
        vault, "note/A.md",
        audit_entries=[_entry(marker_id="inf-20260423-salem-aaa")],
        body_sections=[("inf-20260423-salem-aaa", "wrapped content")],
    )
    set_vault_path(vault)
    clear_providers()
    cfg = _config(tmp_path)
    attribution_audit_section(cfg, date(2026, 4, 24))
    items = consume_last_batch()
    assert len(items) == 1
    assert items[0].record_path == "note/A.md"
    assert items[0].marker_id == "inf-20260423-salem-aaa"
    # Second consume returns empty (cleared)
    assert consume_last_batch() == []


def test_section_provider_global_start_index_kwarg(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_record(
        vault, "note/A.md",
        audit_entries=[_entry(marker_id="inf-20260423-salem-x")],
        body_sections=[("inf-20260423-salem-x", "x")],
    )
    set_vault_path(vault)
    clear_providers()
    cfg = _config(tmp_path)
    attribution_audit_section(cfg, date(2026, 4, 24), start_index=6)
    items = consume_last_batch()
    assert items[0].item_number == 6


def test_peek_last_batch_count_non_destructive(tmp_path: Path):
    vault = _make_vault(tmp_path)
    _seed_record(
        vault, "note/A.md",
        audit_entries=[_entry(marker_id="inf-20260423-salem-x")],
        body_sections=[("inf-20260423-salem-x", "x")],
    )
    set_vault_path(vault)
    clear_providers()
    cfg = _config(tmp_path)
    attribution_audit_section(cfg, date(2026, 4, 24))
    assert peek_last_batch_count() == 1
    # Calling peek twice still shows the same count (non-destructive)
    assert peek_last_batch_count() == 1
    consume_last_batch()
    assert peek_last_batch_count() == 0
