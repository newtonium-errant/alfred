"""Tests for the Phase 2 attribution-routing extensions to reply_dispatch.

Covers:
- Mixed-list routing: same item_number space, dispatcher picks email
  vs attribution by which list claims the number.
- "6 confirm" flips the marker, writes to vault, appends corpus row.
- "6 reject" strips the section, drops the audit entry, appends
  corpus row preserving rejected content.
- Modifier/tier on an attribution item → unparsed (with reason).
- "reject" on an email item → unparsed (with reason).
- Mixed reply: "1 down, 6 confirm" routes both correctly.
- Idempotency: confirming an already-confirmed marker is a no-op.
- File deleted between scan and confirm: graceful skip + error.
- Marker_id removed from frontmatter: graceful skip.
- all_ok ✅ confirms email items + attribution items in one shot.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from alfred.daily_sync.config import AttributionConfig, DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.corpus import iter_corrections
from alfred.daily_sync.reply_dispatch import handle_daily_sync_reply
from alfred.vault.attribution import (
    AuditEntry,
    append_audit_entry,
    parse_audit_entries,
)


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email_corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.attribution = AttributionConfig(
        enabled=True,
        batch_size=5,
        scan_paths=[],
        corpus_path=str(tmp_path / "attribution_corpus.jsonl"),
    )
    return cfg


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "note").mkdir(parents=True)
    return vault


def _email_item(num: int, *, priority: str) -> dict:
    return {
        "item_number": num,
        "record_path": f"note/Email{num}.md",
        "classifier_priority": priority,
        "classifier_action_hint": None,
        "classifier_reason": f"reason {num}",
        "sender": f"sender{num}@example.com",
        "subject": f"Subject {num}",
        "snippet": f"Snippet {num}",
    }


def _attribution_item(num: int, marker_id: str, *, record_path: str) -> dict:
    return {
        "item_number": num,
        "record_path": record_path,
        "marker_id": marker_id,
        "agent": "salem",
        "date": "2026-04-23T18:44:00+00:00",
        "section_title": "Test Section",
        "reason": "talker conversation turn (session=abc123)",
        "content_preview": "Wrapped content preview text.",
    }


def _seed_record(
    vault: Path,
    rel_path: str,
    *,
    marker_id: str,
    content: str = "wrapped content body",
    confirmed: bool = False,
) -> Path:
    file_path = vault / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    fm: dict = {"type": "note", "name": rel_path.removesuffix(".md").rsplit("/", 1)[-1]}
    entry = AuditEntry(
        marker_id=marker_id,
        agent="salem",
        date="2026-04-23T18:44:00+00:00",
        section_title="Test Section",
        reason="talker conversation turn",
        confirmed_by_andrew=confirmed,
        confirmed_at=("2026-04-23T19:00:00+00:00" if confirmed else None),
    )
    append_audit_entry(fm, entry)
    body = (
        f'<!-- BEGIN_INFERRED marker_id="{marker_id}" -->\n'
        f"{content}\n"
        f'<!-- END_INFERRED marker_id="{marker_id}" -->'
    )
    post = frontmatter.Post(body, **fm)
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return file_path


def _seed_state(
    cfg: DailySyncConfig,
    *,
    items: list[dict] | None = None,
    attribution_items: list[dict] | None = None,
    message_ids: list[int] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "date": "2026-04-24",
        "message_ids": message_ids or [100],
    }
    if items is not None:
        payload["items"] = items
    if attribution_items is not None:
        payload["attribution_items"] = attribution_items
    save_state(cfg.state.path, {"last_batch": payload})


def _read_attr_corpus(cfg: DailySyncConfig) -> list[dict]:
    path = Path(cfg.attribution.corpus_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --- routing -------------------------------------------------------------


def test_attribution_confirm_flips_marker_and_writes_corpus(tmp_path: Path):
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="inf-x-1")
    _seed_state(cfg, attribution_items=[
        _attribution_item(6, "inf-x-1", record_path="note/A.md"),
    ])

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="6 confirm",
        vault_path=vault,
    )
    assert result is not None
    assert result["attribution_count"] == 1
    assert result["email_count"] == 0
    # Marker flipped in frontmatter
    post = frontmatter.load(str(vault / "note/A.md"))
    entries = parse_audit_entries(post.metadata)
    assert len(entries) == 1
    assert entries[0].confirmed_by_andrew is True
    assert entries[0].confirmed_at is not None
    # Body still contains the BEGIN/END marker pair (confirm doesn't strip)
    assert "BEGIN_INFERRED" in post.content
    assert "END_INFERRED" in post.content
    # Corpus row exists
    rows = _read_attr_corpus(cfg)
    assert len(rows) == 1
    assert rows[0]["type"] == "attribution_confirm"
    assert rows[0]["marker_id"] == "inf-x-1"
    assert rows[0]["andrew_action"] == "confirm"


def test_attribution_reject_strips_section_and_writes_corpus(tmp_path: Path):
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    _seed_record(
        vault, "note/A.md",
        marker_id="inf-x-2",
        content="this is the rejected content",
    )
    _seed_state(cfg, attribution_items=[
        _attribution_item(6, "inf-x-2", record_path="note/A.md"),
    ])

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="6 reject",
        vault_path=vault,
    )
    assert result is not None
    assert result["attribution_count"] == 1
    # Section stripped from body, audit entry removed from frontmatter
    post = frontmatter.load(str(vault / "note/A.md"))
    assert "BEGIN_INFERRED" not in post.content
    assert "this is the rejected content" not in post.content
    entries = parse_audit_entries(post.metadata)
    assert entries == []
    # Corpus row preserves the rejected content
    rows = _read_attr_corpus(cfg)
    assert len(rows) == 1
    assert rows[0]["type"] == "attribution_reject"
    assert rows[0]["andrew_action"] == "reject"
    assert "Wrapped content preview text." in rows[0]["original_section_content"]


def test_mixed_email_and_attribution_in_one_reply(tmp_path: Path):
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    _seed_record(vault, "note/Attr.md", marker_id="inf-mixed-1")
    _seed_state(
        cfg,
        items=[_email_item(1, priority="medium")],
        attribution_items=[_attribution_item(6, "inf-mixed-1", record_path="note/Attr.md")],
    )

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="1 down, 6 confirm",
        vault_path=vault,
    )
    assert result is not None
    assert result["email_count"] == 1
    assert result["attribution_count"] == 1
    # Email row applied
    email_rows = list(iter_corrections(cfg.corpus.path))
    assert len(email_rows) == 1
    assert email_rows[0].andrew_priority == "low"
    # Attribution marker flipped
    post = frontmatter.load(str(vault / "note/Attr.md"))
    entries = parse_audit_entries(post.metadata)
    assert entries[0].confirmed_by_andrew is True


def test_modifier_on_attribution_item_unparsed(tmp_path: Path):
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="inf-mod-1")
    _seed_state(cfg, attribution_items=[
        _attribution_item(6, "inf-mod-1", record_path="note/A.md"),
    ])

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="6 down",
        vault_path=vault,
    )
    assert result is not None
    assert result["attribution_count"] == 0
    assert any("attribution items only accept" in u for u in result["unparsed"])
    # Marker unchanged
    post = frontmatter.load(str(vault / "note/A.md"))
    entries = parse_audit_entries(post.metadata)
    assert entries[0].confirmed_by_andrew is False


def test_reject_on_email_item_unparsed(tmp_path: Path):
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    _seed_state(
        cfg,
        items=[_email_item(1, priority="medium")],
    )

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="1 reject",
        vault_path=vault,
    )
    assert result is not None
    assert result["email_count"] == 0
    assert any("`reject` is" in u for u in result["unparsed"])


def test_idempotent_confirm_already_confirmed(tmp_path: Path):
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="inf-idem-1", confirmed=True)
    _seed_state(cfg, attribution_items=[
        _attribution_item(6, "inf-idem-1", record_path="note/A.md"),
    ])

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="6 confirm",
        vault_path=vault,
    )
    assert result is not None
    # Treated as no-op success — count is 0 (no NEW corpus row), no error
    assert result["attribution_count"] == 0
    # Corpus stays empty (no double-write)
    rows = _read_attr_corpus(cfg)
    assert rows == []


def test_record_deleted_between_scan_and_confirm(tmp_path: Path):
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    # Stage state pointing at a record we never created
    _seed_state(cfg, attribution_items=[
        _attribution_item(6, "inf-missing-1", record_path="note/Missing.md"),
    ])

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="6 confirm",
        vault_path=vault,
    )
    assert result is not None
    assert result["attribution_count"] == 0
    assert any("no longer exists" in u for u in result["unparsed"])


def test_marker_id_removed_from_frontmatter_treated_as_resolved(tmp_path: Path):
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    # Seed record where the audit entry has been manually removed but the
    # state file still references the (now-stale) marker_id.
    file_path = vault / "note/A.md"
    post = frontmatter.Post("plain body", **{"type": "note", "name": "A"})
    file_path.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    _seed_state(cfg, attribution_items=[
        _attribution_item(6, "inf-stale-1", record_path="note/A.md"),
    ])

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="6 confirm",
        vault_path=vault,
    )
    assert result is not None
    assert result["attribution_count"] == 0
    # Treated as no-op success — no error in unparsed
    assert all("inf-stale-1" not in u for u in result["unparsed"])


def test_all_ok_confirms_email_and_attribution(tmp_path: Path):
    vault = _make_vault(tmp_path)
    cfg = _config(tmp_path)
    _seed_record(vault, "note/A.md", marker_id="inf-allok-1")
    _seed_state(
        cfg,
        items=[_email_item(1, priority="medium")],
        attribution_items=[_attribution_item(6, "inf-allok-1", record_path="note/A.md")],
    )

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="✅",
        vault_path=vault,
    )
    assert result is not None
    assert result["all_ok"] is True
    assert result["email_count"] == 1
    assert result["attribution_count"] == 1
    # Marker flipped
    post = frontmatter.load(str(vault / "note/A.md"))
    entries = parse_audit_entries(post.metadata)
    assert entries[0].confirmed_by_andrew is True


def test_attribution_without_vault_path_records_error(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_state(cfg, attribution_items=[
        _attribution_item(6, "inf-novault-1", record_path="note/A.md"),
    ])

    result = handle_daily_sync_reply(
        cfg, parent_message_id=100, reply_text="6 confirm",
        # vault_path omitted on purpose
    )
    assert result is not None
    assert result["attribution_count"] == 0
    assert any("vault_path not provided" in u for u in result["unparsed"])
