"""Tests for the Daily Sync reply dispatcher.

Covers:
- reply_targets_daily_sync matches when message_id is in persisted batch.
- reply_targets_daily_sync rejects when no batch persisted.
- handle_daily_sync_reply returns None when reply isn't aimed at us.
- all_ok reply writes one corpus row per item with confirmed tier.
- Per-item correction writes the right andrew_priority + note.
- Modifier resolution against the batch's classifier_priority.
- Unparseable fragments land in the result's unparsed list.
- Item-number out of range produces an error in the result.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.corpus import iter_corrections
from alfred.daily_sync.reply_dispatch import (
    handle_daily_sync_reply,
    reply_targets_daily_sync,
)


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _seed_batch(cfg: DailySyncConfig, *, items: list[dict], message_ids: list[int]) -> None:
    """Persist a fake last_batch into the daily-sync state file."""
    save_state(cfg.state.path, {
        "last_batch": {
            "date": "2026-04-22",
            "items": items,
            "message_ids": message_ids,
        },
    })


def _item(num: int, *, priority: str, sender: str = "alice@example.com") -> dict:
    return {
        "item_number": num,
        "record_path": f"note/Item{num}.md",
        "classifier_priority": priority,
        "classifier_action_hint": None,
        "classifier_reason": f"reason {num}",
        "sender": sender,
        "subject": f"Subject {num}",
        "snippet": f"Snippet {num}",
    }


def test_reply_targets_matches_persisted_id(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100, 101])
    assert reply_targets_daily_sync(cfg, 100) is True
    assert reply_targets_daily_sync(cfg, 101) is True
    assert reply_targets_daily_sync(cfg, 999) is False


def test_reply_targets_no_batch_returns_false(tmp_path: Path):
    cfg = _config(tmp_path)
    assert reply_targets_daily_sync(cfg, 100) is False


def test_handle_returns_none_when_not_a_match(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])
    result = handle_daily_sync_reply(cfg, parent_message_id=999, reply_text="✅")
    assert result is None


def test_handle_all_ok_writes_one_row_per_item(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(1, priority="high"),
        _item(2, priority="medium"),
        _item(3, priority="low"),
    ], message_ids=[100])

    result = handle_daily_sync_reply(cfg, parent_message_id=100, reply_text="✅")
    assert result is not None
    assert result["all_ok"] is True
    assert result["confirmed_count"] == 3
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 3
    # andrew_priority echoes classifier_priority on all_ok
    for row in rows:
        assert row.andrew_priority == row.classifier_priority


def test_handle_per_item_modifier_resolution(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(1, priority="medium"),
        _item(2, priority="high"),
    ], message_ids=[100])

    # "1 down" → medium goes to low; "2 down" → high goes to medium
    result = handle_daily_sync_reply(cfg, 100, "1 down, 2 down")
    assert result is not None
    assert result["confirmed_count"] == 2
    rows = list(iter_corrections(cfg.corpus.path))
    by_path = {r.record_path: r for r in rows}
    assert by_path["note/Item1.md"].andrew_priority == "low"
    assert by_path["note/Item2.md"].andrew_priority == "medium"


def test_handle_explicit_tier_with_note(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(2, priority="medium")], message_ids=[100])

    result = handle_daily_sync_reply(
        cfg, 100, "2: actually high — Jamie was waiting"
    )
    assert result is not None
    assert result["confirmed_count"] == 1
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 1
    row = rows[0]
    assert row.andrew_priority == "high"
    assert "Jamie was waiting" in row.andrew_reason


def test_handle_item_number_out_of_range(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[_item(1, priority="medium")], message_ids=[100])

    result = handle_daily_sync_reply(cfg, 100, "5 down")
    assert result is not None
    assert result["confirmed_count"] == 0
    assert any("not in last batch" in u for u in result["unparsed"])


def test_handle_mixed_valid_and_invalid(tmp_path: Path):
    cfg = _config(tmp_path)
    _seed_batch(cfg, items=[
        _item(1, priority="medium"),
        _item(2, priority="high"),
    ], message_ids=[100])

    result = handle_daily_sync_reply(cfg, 100, "1 down, totally not a fragment")
    assert result is not None
    assert result["confirmed_count"] == 1
    assert result["unparsed"]
    rows = list(iter_corrections(cfg.corpus.path))
    assert len(rows) == 1
    assert rows[0].record_path == "note/Item1.md"


def test_handle_corpus_row_carries_metadata(tmp_path: Path):
    cfg = _config(tmp_path)
    items = [_item(1, priority="medium", sender="jamie@example.com")]
    _seed_batch(cfg, items=items, message_ids=[100])
    handle_daily_sync_reply(cfg, 100, "1 down")
    rows = list(iter_corrections(cfg.corpus.path))
    assert rows[0].sender == "jamie@example.com"
    assert rows[0].subject == "Subject 1"
    assert rows[0].snippet == "Snippet 1"
    assert rows[0].timestamp  # set
