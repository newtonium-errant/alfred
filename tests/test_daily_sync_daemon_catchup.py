"""Catch-up-on-startup tests for the daily_sync daemon.

Mirror of ``tests/test_brief_daemon_catchup.py`` for the daily_sync
daemon. Same incident-class fix: if a host restart leaves the daemon
booting AFTER today's 09:00 ADT fire window passed, the catch-up
shape fires immediately on boot.

Tests:
  1. Catch-up fires when window passed + state.last_fired_date != today.
  2. No catch-up when window passed + today already fired (idempotent).
  3. No catch-up when window not yet passed.
  4. ``catchup_fired`` log emits with required fields.
  5. State-write parity — catch-up updates ``last_fired_date`` like
     the scheduled fire path does.

The daily_sync daemon's fire path is ``fire_once`` (vs brief's
``generate_brief``); the test stubs that to record the call and
write the state shape parity matches.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import structlog

from alfred.common.schedule import ScheduleConfig
from alfred.daily_sync.confidence import load_state
from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.daemon import run_daemon


HALIFAX = ZoneInfo("America/Halifax")


def _make_daily_sync_config(tmp_path: Path) -> DailySyncConfig:
    """Build a minimal DailySyncConfig pointing at tmp_path for state.

    The catch-up shape only reads:
      * config.schedule.time / timezone
      * config.state.path
      * config.friction_analyzer.enabled / log_path (read in fire_once,
        but our fake fire_once skips that surface)
      * (and whatever fire_once reads, which we stub out)
    Everything else can default per the dataclass.
    """
    from alfred.daily_sync.config import StateConfig

    state_path = tmp_path / "daily_sync_state.json"
    return DailySyncConfig(
        enabled=True,
        schedule=ScheduleConfig(time="09:00", timezone="America/Halifax"),
        state=StateConfig(path=str(state_path)),
    )


async def _run_with_timeout(coro, timeout_seconds: float = 0.5) -> None:
    """Run ``coro`` until the first ``sleep_until`` blocks, then cancel.

    Same shape as the brief daemon catchup tests — catch-up runs
    BEFORE the while loop's long sleep, so the timeout cancels the
    sleep and lets the test observe catch-up side effects.
    """
    try:
        await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        pass


# ---------------------------------------------------------------------------
# Catch-up fires when window passed + no fire today
# ---------------------------------------------------------------------------


async def test_catchup_fires_when_window_passed_and_no_fire_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon boots at 14:00 ADT (5h past 09:00 window), state shows
    no fire today → fire_once called exactly once before the daemon
    enters its sleep loop."""
    config = _make_daily_sync_config(tmp_path)
    frozen_now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr("alfred.daily_sync.daemon.datetime", _FrozenDT)

    fire_calls = []

    async def _fake_fire_once(
        cfg, vault_path, user_id, today=None, *, manual=False,
        raw_config=None,
    ):
        fire_calls.append({
            "today": today.isoformat() if today else None,
            "manual": manual,
        })
        # Mirror real fire_once's state-write side effect.
        from alfred.daily_sync.confidence import save_state
        state = load_state(cfg.state.path)
        state["last_fired_date"] = (today or frozen_now.date()).isoformat()
        save_state(cfg.state.path, state)
        return {
            "ok": True,
            "items_count": 0,
            "attribution_items_count": 0,
            "proposal_items_count": 0,
            "pending_items_count": 0,
            "radar_items_count": 0,
            "friction_items_count": 0,
            "message_ids": [],
            "body": "",
            "dedupe_key": "daily-sync-2026-05-28",
        }

    monkeypatch.setattr(
        "alfred.daily_sync.daemon.fire_once", _fake_fire_once,
    )

    await _run_with_timeout(
        run_daemon(config, vault_path=tmp_path / "vault", user_id=123),
    )

    # Catch-up fired exactly once with today's date.
    assert len(fire_calls) == 1
    assert fire_calls[0]["today"] == "2026-05-28"
    assert fire_calls[0]["manual"] is False


# ---------------------------------------------------------------------------
# No catch-up when today already fired
# ---------------------------------------------------------------------------


async def test_no_catchup_when_today_already_fired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon restart after a successful 09:00 fire today MUST NOT
    re-fire — idempotency contract via state.last_fired_date."""
    config = _make_daily_sync_config(tmp_path)
    frozen_now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)

    # Pre-populate state with today's fire.
    from alfred.daily_sync.confidence import save_state
    state = {"last_fired_date": "2026-05-28"}
    save_state(config.state.path, state)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr("alfred.daily_sync.daemon.datetime", _FrozenDT)

    fire_calls = []

    async def _fake_fire_once(*args, **kwargs):
        fire_calls.append(kwargs.get("today"))
        return {
            "ok": True, "items_count": 0,
            "attribution_items_count": 0, "proposal_items_count": 0,
            "pending_items_count": 0, "radar_items_count": 0,
            "friction_items_count": 0, "message_ids": [],
            "body": "", "dedupe_key": "x",
        }

    monkeypatch.setattr(
        "alfred.daily_sync.daemon.fire_once", _fake_fire_once,
    )

    await _run_with_timeout(
        run_daemon(config, vault_path=tmp_path / "vault", user_id=123),
    )

    assert fire_calls == []


# ---------------------------------------------------------------------------
# No catch-up when window not yet passed
# ---------------------------------------------------------------------------


async def test_no_catchup_when_window_not_yet_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon boots at 06:00 ADT, before 09:00 window — the catch-up
    path stays silent (no ``daily_sync.daemon.catchup_fired`` event
    emits).

    Pinned 2026-05-28 (team-lead pytest dx review): the assertion
    intentionally narrows to the catchup_fired log event count, NOT
    the total fire count. Reason: the frozen-clock test rig
    (``_FrozenDT.now()`` returns a constant) interacts with the
    daemon's natural sleep loop so the loop fires after
    ``sleep_until`` returns instantly (target - now never advances).
    The total-fire-count assertion would conflate "catch-up didn't
    fire" (what this test pins) with "daemon didn't fire at all"
    (out of scope). catchup_fired log is the canary."""
    config = _make_daily_sync_config(tmp_path)
    frozen_now = datetime(2026, 5, 28, 6, 0, tzinfo=HALIFAX)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr("alfred.daily_sync.daemon.datetime", _FrozenDT)

    async def _fake_fire_once(*args, **kwargs):
        return {
            "ok": True, "items_count": 0,
            "attribution_items_count": 0, "proposal_items_count": 0,
            "pending_items_count": 0, "radar_items_count": 0,
            "friction_items_count": 0, "message_ids": [],
            "body": "", "dedupe_key": "x",
        }

    monkeypatch.setattr(
        "alfred.daily_sync.daemon.fire_once", _fake_fire_once,
    )

    with structlog.testing.capture_logs() as captured:
        await _run_with_timeout(
            run_daemon(config, vault_path=tmp_path / "vault", user_id=123),
        )

    # Pin: NO ``daily_sync.daemon.catchup_fired`` log event emitted.
    # Whether the natural sleep loop fires within the test timeout
    # is independent of the catch-up gate this test pins.
    catchup_events = [
        c for c in captured
        if c.get("event") == "daily_sync.daemon.catchup_fired"
    ]
    assert catchup_events == [], (
        f"Expected no catchup_fired event (window hasn't passed); "
        f"got: {catchup_events}"
    )


# ---------------------------------------------------------------------------
# catchup_fired log emission pin (builder.md rule #9)
# ---------------------------------------------------------------------------


async def test_catchup_fired_log_event_emits_with_required_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin ``daily_sync.daemon.catchup_fired`` event carries date +
    intended/actual fire times + delay_seconds. Operator-grep target
    for incident counting + lateness distribution."""
    config = _make_daily_sync_config(tmp_path)
    frozen_now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)  # 5h past 09:00

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr("alfred.daily_sync.daemon.datetime", _FrozenDT)

    async def _fake_fire_once(cfg, vault_path, user_id, today=None, **kwargs):
        from alfred.daily_sync.confidence import save_state
        state = load_state(cfg.state.path)
        state["last_fired_date"] = (today or frozen_now.date()).isoformat()
        save_state(cfg.state.path, state)
        return {
            "ok": True, "items_count": 0,
            "attribution_items_count": 0, "proposal_items_count": 0,
            "pending_items_count": 0, "radar_items_count": 0,
            "friction_items_count": 0, "message_ids": [],
            "body": "", "dedupe_key": "x",
        }

    monkeypatch.setattr(
        "alfred.daily_sync.daemon.fire_once", _fake_fire_once,
    )

    with structlog.testing.capture_logs() as captured:
        await _run_with_timeout(
            run_daemon(config, vault_path=tmp_path / "vault", user_id=123),
        )

    catchup_events = [
        c for c in captured
        if c.get("event") == "daily_sync.daemon.catchup_fired"
    ]
    assert len(catchup_events) == 1
    event = catchup_events[0]
    assert event["date"] == "2026-05-28"
    assert "intended_fire_time" in event
    assert "actual_fire_time" in event
    assert "delay_seconds" in event
    assert isinstance(event["delay_seconds"], (int, float))
    # 14:00 - 09:00 = 5h = 18000s.
    assert event["delay_seconds"] == pytest.approx(18000.0, abs=1.0)


# ---------------------------------------------------------------------------
# State-write parity
# ---------------------------------------------------------------------------


async def test_catchup_fire_updates_last_fired_date_identically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catch-up writes state.last_fired_date the same way a scheduled
    fire does. Pin so a refactor that bypasses fire_once for catch-up
    (and forgets the state-write) surfaces here."""
    config = _make_daily_sync_config(tmp_path)
    frozen_now = datetime(2026, 5, 28, 14, 0, tzinfo=HALIFAX)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr("alfred.daily_sync.daemon.datetime", _FrozenDT)

    async def _fake_fire_once(cfg, vault_path, user_id, today=None, **kwargs):
        from alfred.daily_sync.confidence import save_state
        state = load_state(cfg.state.path)
        state["last_fired_date"] = (today or frozen_now.date()).isoformat()
        save_state(cfg.state.path, state)
        return {
            "ok": True, "items_count": 0,
            "attribution_items_count": 0, "proposal_items_count": 0,
            "pending_items_count": 0, "radar_items_count": 0,
            "friction_items_count": 0, "message_ids": [],
            "body": "", "dedupe_key": "x",
        }

    monkeypatch.setattr(
        "alfred.daily_sync.daemon.fire_once", _fake_fire_once,
    )

    await _run_with_timeout(
        run_daemon(config, vault_path=tmp_path / "vault", user_id=123),
    )

    # Re-read state from disk and verify parity with the scheduled
    # fire's state shape.
    state = load_state(config.state.path)
    assert state.get("last_fired_date") == "2026-05-28"
