"""Feed Phase A — the load-bearing sync parity pin.

``fire_once`` must produce a BYTE-IDENTICAL assembled body and persisted
``last_batch`` whether the feed is enabled or disabled — the feed emission is a
pure side-write after ``save_state``. This drives a real fire (deterministic
routine_match batch) both ways and compares.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from alfred.daily_sync.config import DailySyncConfig, RoutineMatchConfig
from alfred.daily_sync.confidence import load_state
from alfred.daily_sync.daemon import fire_once
from alfred.routine import match_calibration as mc


def _config(tmp_path: Path) -> DailySyncConfig:
    pending = tmp_path / "pending.jsonl"
    mc.append_pending(pending, mc.PendingMatch(
        query="walk doggo", matched_to="Walk dog", record="Daily",
        confidence=0.4, completion_date="2026-06-28",
    ))
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.routine_match = RoutineMatchConfig(enabled=True, pending_path=str(pending))
    return cfg


def _patch_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_send_batch(user_id, chunks, *, dedupe_key=None, client_name=None):
        return {"telegram_message_ids": [9001]}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "send_outbound_batch", _fake_send_batch)


async def _fire(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, feed_enabled: bool):
    cfg = _config(tmp_path)
    _patch_transport(monkeypatch)
    raw_config = {"feed": {"enabled": feed_enabled, "store_path": str(tmp_path / "feed.jsonl")}}
    result = await fire_once(cfg, tmp_path, user_id=42, today=date(2026, 6, 28), raw_config=raw_config)
    batch = load_state(cfg.state.path).get("last_batch") or {}
    return result, batch


async def test_body_and_last_batch_byte_identical_feed_on_vs_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    on_result, on_batch = await _fire(tmp_path / "on", monkeypatch, feed_enabled=True)
    off_result, off_batch = await _fire(tmp_path / "off", monkeypatch, feed_enabled=False)

    # 1. The assembled body is byte-identical.
    assert on_result["body"] == off_result["body"]

    # 2. The persisted last_batch is identical modulo fired_at (wall-clock,
    #    orthogonal to the feed). Compare the canonical JSON of everything else.
    on_batch.pop("fired_at", None)
    off_batch.pop("fired_at", None)
    assert json.dumps(on_batch, sort_keys=True) == json.dumps(off_batch, sort_keys=True)

    # 3. Non-vacuous: the feed actually ran when enabled (store populated) and
    #    did NOT when disabled (no store file) — so the parity above is real.
    assert (tmp_path / "on" / "feed.jsonl").is_file()
    assert not (tmp_path / "off" / "feed.jsonl").exists()


async def test_feed_store_has_the_routine_match_item_after_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alfred.feed import FeedStore

    _result, _batch = await _fire(tmp_path, monkeypatch, feed_enabled=True)
    folded = FeedStore(tmp_path / "feed.jsonl").load()
    rm = [it for it in folded.values() if it.kind == "routine_match"]
    assert len(rm) == 1
    assert rm[0].id == "routine_match:walk doggo|Daily"
    assert rm[0].state == "open"
