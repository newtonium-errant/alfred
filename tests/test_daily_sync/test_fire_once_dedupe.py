"""Tests for the two-path dedupe key in ``daily_sync.daemon.fire_once``.

The auto-fire (daemon loop, 09:00) keeps the date-based dedupe key
``daily-sync-{date}`` so a scheduling glitch doesn't double-push to
Telegram. The ``/calibrate`` slash command path passes ``manual=True``,
which appends a unique ``-calibrate-{uuid8}`` suffix so an explicit
out-of-cycle fire is NOT short-circuited by the transport server's
24h idempotency window when the auto-fire already ran today.

Bug context (live-confirmed 2026-04-23): every /calibrate after the
first send of the day silently dropped at the server because the
dedupe key collided with the morning's auto-fire entry in
``data/transport_state.json``. Andrew saw the "firing now…" ack but
no second message. This test would have caught it.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.daemon import fire_once


def _config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _patch_transport(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch ``send_outbound_batch`` to record every call, returning empty ids."""
    captured: list[dict[str, Any]] = []

    async def _fake_send_batch(
        user_id: int,
        chunks: list[str],
        *,
        dedupe_key: str | None = None,
        client_name: str | None = None,
    ) -> dict[str, Any]:
        captured.append(
            {
                "user_id": user_id,
                "chunks": list(chunks),
                "dedupe_key": dedupe_key,
                "client_name": client_name,
            }
        )
        # Return a plausible server response so fire_once stays on the
        # happy path and we exercise the dedupe-key argument it actually
        # passes to the transport.
        return {"telegram_message_ids": [9001]}

    import alfred.transport.client as client_mod

    monkeypatch.setattr(client_mod, "send_outbound_batch", _fake_send_batch)
    return captured


async def test_auto_fire_uses_date_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default (auto-fire) path keeps the date-only dedupe key.

    This is the existing behaviour and MUST stay green — the date-based
    key is what protects the natural 09:00 fire from double-pushing on
    daemon restart loops or scheduling jitter.
    """
    cfg = _config(tmp_path)
    captured = _patch_transport(monkeypatch)

    today = date(2026, 4, 23)
    result = await fire_once(cfg, tmp_path, user_id=42, today=today)

    assert len(captured) == 1
    assert captured[0]["dedupe_key"] == "daily-sync-2026-04-23"
    assert result["dedupe_key"] == "daily-sync-2026-04-23"


async def test_manual_fire_uses_unique_dedupe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``manual=True`` (``/calibrate``) appends a unique uuid8 suffix.

    The shape is ``daily-sync-{date}-calibrate-{8-hex-chars}``. The
    ``calibrate-`` prefix keeps the audit trail in
    ``data/transport_state.json`` greppable.
    """
    cfg = _config(tmp_path)
    captured = _patch_transport(monkeypatch)

    today = date(2026, 4, 23)
    result = await fire_once(cfg, tmp_path, user_id=42, today=today, manual=True)

    assert len(captured) == 1
    key = captured[0]["dedupe_key"]
    assert key is not None
    pattern = r"^daily-sync-2026-04-23-calibrate-[0-9a-f]{8}$"
    assert re.match(pattern, key), f"unexpected manual dedupe shape: {key!r}"
    assert result["dedupe_key"] == key


async def test_two_manual_fires_same_day_get_distinct_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the live bug.

    Two ``/calibrate`` invocations on the same day must produce two
    DIFFERENT dedupe keys so the transport server's 24h idempotency
    window does not short-circuit the second send. Before this fix
    both calls used ``daily-sync-{date}`` and the second silently
    matched the first's send_log entry → no Telegram push.
    """
    cfg = _config(tmp_path)
    captured = _patch_transport(monkeypatch)

    today = date(2026, 4, 23)
    await fire_once(cfg, tmp_path, user_id=42, today=today, manual=True)
    await fire_once(cfg, tmp_path, user_id=42, today=today, manual=True)

    assert len(captured) == 2
    key_a = captured[0]["dedupe_key"]
    key_b = captured[1]["dedupe_key"]
    assert key_a != key_b, (
        "Two /calibrate fires in the same day must NOT share a dedupe key — "
        "that's the original bug (transport server short-circuits the second)."
    )
    # Both still carry the calibrate tag for audit-trail grep.
    assert key_a.startswith("daily-sync-2026-04-23-calibrate-")
    assert key_b.startswith("daily-sync-2026-04-23-calibrate-")


async def test_manual_fire_does_not_collide_with_auto_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An auto-fire then a /calibrate the same day must use different keys.

    This is the exact production sequence that broke: 14:55 UTC the
    auto-fire sent under ``daily-sync-2026-04-23``; later /calibrate
    invocations re-used the same key and got server-deduped. The
    manual path now sidesteps the auto-fire's entry entirely.
    """
    cfg = _config(tmp_path)
    captured = _patch_transport(monkeypatch)

    today = date(2026, 4, 23)
    await fire_once(cfg, tmp_path, user_id=42, today=today)  # auto
    await fire_once(cfg, tmp_path, user_id=42, today=today, manual=True)

    assert len(captured) == 2
    assert captured[0]["dedupe_key"] == "daily-sync-2026-04-23"
    assert captured[1]["dedupe_key"] != captured[0]["dedupe_key"]
    assert captured[1]["dedupe_key"].startswith(
        "daily-sync-2026-04-23-calibrate-"
    )
