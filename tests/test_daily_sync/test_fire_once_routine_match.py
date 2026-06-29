"""Self-correcting matcher Phase 2b — daemon persist wiring pin.

``fire_once`` must consume the routine_match section's batch and persist it
into ``last_batch.routine_match_items`` (keyed off the same message_ids as the
other section batches) so the reply dispatcher can route a confirm/reject. This
pins the consume → _build_state_payload → persist path end-to-end.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

from alfred.daily_sync.config import DailySyncConfig, RoutineMatchConfig
from alfred.daily_sync.confidence import load_state
from alfred.daily_sync.daemon import fire_once
from alfred.routine import match_calibration as mc


def _config(tmp_path: Path, pending: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    cfg.routine_match = RoutineMatchConfig(enabled=True, pending_path=str(pending))
    return cfg


def _patch_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_send_batch(
        user_id: int, chunks: list[str], *,
        dedupe_key: str | None = None, client_name: str | None = None,
    ) -> dict[str, Any]:
        return {"telegram_message_ids": [9001]}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "send_outbound_batch", _fake_send_batch)


async def test_fire_once_persists_routine_match_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending = tmp_path / "pending.jsonl"
    mc.append_pending(pending, mc.PendingMatch(
        query="walk doggo", matched_to="Walk dog", record="Daily",
        confidence=0.4, completion_date="2026-06-28",
    ))
    cfg = _config(tmp_path, pending)
    _patch_transport(monkeypatch)

    result = await fire_once(cfg, tmp_path, user_id=42, today=date(2026, 6, 28))

    assert result["routine_match_items_count"] == 1
    state = load_state(cfg.state.path)
    items = state["last_batch"]["routine_match_items"]
    assert len(items) == 1
    assert items[0]["query"] == "walk doggo"
    assert items[0]["matched_to"] == "Walk dog"
    # item_number carried so the dispatcher can route "item N confirm".
    assert isinstance(items[0]["item_number"], int) and items[0]["item_number"] >= 1


async def test_fire_once_omits_routine_match_items_when_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pending matches → no routine_match_items key in the batch (the
    section still renders its ILB sentinel, but there's nothing to route)."""
    pending = tmp_path / "pending.jsonl"  # absent
    cfg = _config(tmp_path, pending)
    _patch_transport(monkeypatch)

    result = await fire_once(cfg, tmp_path, user_id=42, today=date(2026, 6, 28))

    assert result["routine_match_items_count"] == 0
    state = load_state(cfg.state.path)
    # last_batch may be absent entirely (no items of any kind) — but if present,
    # it must not carry routine_match_items.
    batch = state.get("last_batch") or {}
    assert "routine_match_items" not in batch
