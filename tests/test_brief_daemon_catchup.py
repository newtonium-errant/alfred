"""Catch-up-on-startup tests for the brief daemon.

Closes the false-FAIL class surfaced 2026-05-28: a host restart mid-day
left the brief daemon sleeping until tomorrow's 06:00, even though the
daemon was alive and would have fired had it been up at 06:00 ADT.
The catch-up shape fires the brief immediately on boot when the window
has already passed AND state shows no successful fire today.

Tests:
  1. Catch-up fires when window passed + state shows no fire today.
  2. No catch-up when window passed + today already fired (idempotent).
  3. No catch-up when window not yet passed (normal sleep).
  4. ``catchup_fired`` log emits with date + intended/actual times +
     delay_seconds.
  5. Catch-up fire path updates state identically to scheduled fire
     (state-write parity).

The ``while True`` loop is broken via ``asyncio.wait_for`` timeout —
the catch-up logic runs BEFORE the loop, so the timeout cancels the
loop's long sleep_until and the test observes the catch-up's side
effects (generate_brief call count + state shape + log events).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import structlog

from alfred.brief.config import BriefConfig
from alfred.brief.daemon import run_daemon
from alfred.brief.state import BriefRun, StateManager
from alfred.common.schedule import ScheduleConfig


HALIFAX = ZoneInfo("America/Halifax")


def _make_brief_config(tmp_path: Path) -> BriefConfig:
    """Build a minimal BriefConfig pointing at tmp_path for state.

    The catch-up shape only reads:
      * config.schedule.time / timezone
      * config.state.path
      * (and whatever generate_brief reads, which we stub out)
    Everything else can default.
    """
    from alfred.brief.config import StateConfig

    state_path = tmp_path / "brief_state.json"
    return BriefConfig(
        vault_path=str(tmp_path / "vault"),
        schedule=ScheduleConfig(time="06:00", timezone="America/Halifax"),
        state=StateConfig(path=str(state_path)),
    )


async def _run_with_timeout(coro, timeout_seconds: float = 0.5) -> None:
    """Run ``coro`` until the first ``sleep_until`` blocks, then cancel.

    Catch-up runs synchronously-await-style BEFORE the while loop's
    long sleep_until. The timeout lets catch-up complete + the sleep
    enter, then we cancel cleanly so the test moves on.
    """
    try:
        await asyncio.wait_for(coro, timeout=timeout_seconds)
    except asyncio.TimeoutError:
        # Expected — the while True loop's sleep_until is the long
        # blocker; cancelling it via timeout is how we break out.
        pass


# ---------------------------------------------------------------------------
# Catch-up fires when window passed + no fire today
# ---------------------------------------------------------------------------


async def test_catchup_fires_when_window_passed_and_no_fire_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The canonical incident shape: 10:30 ADT boot, 06:00 fire window
    long passed, state shows no fire today → generate_brief is called
    exactly once before the daemon enters its sleep loop."""
    config = _make_brief_config(tmp_path)

    # Freeze "now" to 10:30 Halifax 2026-05-28 (4.5h past 06:00).
    frozen_now = datetime(2026, 5, 28, 10, 30, tzinfo=HALIFAX)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr("alfred.brief.daemon.datetime", _FrozenDT)

    # Stub generate_brief so the catch-up call resolves without
    # touching weather / vault / etc.
    generate_calls = []

    async def _fake_generate_brief(cfg, state_mgr, refresh=False):
        generate_calls.append({
            "date": frozen_now.date().isoformat(),
            "state_runs_at_call": len(state_mgr.state.runs),
        })
        # Mirror the real generate_brief side effect: append a
        # successful BriefRun so a follow-up state-check sees the
        # fire as completed.
        state_mgr.state.add_run(BriefRun(
            date=frozen_now.date().isoformat(),
            generated_at=frozen_now.isoformat(),
            vault_path="run/morning-briefs/test.md",
            sections=["catch-up"],
            success=True,
        ))
        state_mgr.save()
        return "run/morning-briefs/test.md"

    monkeypatch.setattr(
        "alfred.brief.daemon.generate_brief", _fake_generate_brief,
    )

    await _run_with_timeout(run_daemon(config))

    # Catch-up fired exactly once.
    assert len(generate_calls) == 1
    assert generate_calls[0]["date"] == "2026-05-28"
    # State pre-fire had zero runs; the call appended one.
    assert generate_calls[0]["state_runs_at_call"] == 0


# ---------------------------------------------------------------------------
# No catch-up when today already fired (idempotent)
# ---------------------------------------------------------------------------


async def test_no_catchup_when_today_already_fired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Daemon restart AFTER a successful fire today (e.g. operator
    bounce after 07:00 ADT) MUST NOT re-fire — idempotency contract."""
    config = _make_brief_config(tmp_path)
    frozen_now = datetime(2026, 5, 28, 10, 30, tzinfo=HALIFAX)

    # Pre-populate state with today's successful fire.
    state_mgr = StateManager(config.state.path)
    state_mgr.state.add_run(BriefRun(
        date="2026-05-28",
        generated_at=frozen_now.isoformat(),
        vault_path="run/morning-briefs/2026-05-28.md",
        sections=["weather"],
        success=True,
    ))
    state_mgr.save()

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr("alfred.brief.daemon.datetime", _FrozenDT)

    generate_calls = []

    async def _fake_generate_brief(cfg, state_mgr, refresh=False):
        generate_calls.append(frozen_now.date().isoformat())
        return None

    monkeypatch.setattr(
        "alfred.brief.daemon.generate_brief", _fake_generate_brief,
    )

    await _run_with_timeout(run_daemon(config))

    # NO catch-up fire — today's state row keeps the gate closed.
    assert generate_calls == []


# ---------------------------------------------------------------------------
# No catch-up when window not yet passed
# ---------------------------------------------------------------------------


async def test_no_catchup_when_window_not_yet_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Normal case: daemon boots at 03:00 ADT, before 06:00 window —
    the catch-up path stays silent (no ``brief.daemon.catchup_fired``
    event emits).

    Pinned 2026-05-28 (team-lead pytest dx review): the assertion
    intentionally narrows to the catchup_fired log event count, NOT
    the total fire count. Reason: the frozen-clock test rig
    (``_FrozenDT.now()`` returns a constant) interacts with the
    daemon's natural sleep loop in a way that the loop fires after
    ``sleep_until`` returns instantly (because target - now never
    advances), then the test's assertion would conflate two
    independent things: "catch-up didn't fire" (what this test pins)
    vs "daemon didn't fire at all" (out of scope — the loop's
    sleep_until interaction with the mock isn't what we're testing).
    The catchup_fired log event is the canary; that's the only signal
    this test should pin."""
    config = _make_brief_config(tmp_path)
    frozen_now = datetime(2026, 5, 28, 3, 0, tzinfo=HALIFAX)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr("alfred.brief.daemon.datetime", _FrozenDT)

    async def _fake_generate_brief(cfg, state_mgr, refresh=False):
        return None

    monkeypatch.setattr(
        "alfred.brief.daemon.generate_brief", _fake_generate_brief,
    )

    with structlog.testing.capture_logs() as captured:
        await _run_with_timeout(run_daemon(config))

    # Pin: NO ``brief.daemon.catchup_fired`` log event emitted.
    # Whether the natural sleep loop fires within the test timeout
    # is independent of the catch-up gate this test pins.
    catchup_events = [
        c for c in captured
        if c.get("event") == "brief.daemon.catchup_fired"
    ]
    assert catchup_events == [], (
        f"Expected no catchup_fired event (window hasn't passed); "
        f"got: {catchup_events}"
    )


# ---------------------------------------------------------------------------
# catchup_fired log event pins (per builder.md rule #9)
# ---------------------------------------------------------------------------


async def test_catchup_fired_log_event_emits_with_required_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per builder.md rule #9: pin the production log emission via
    structlog.testing.capture_logs. The catchup_fired event MUST carry
    date + intended_fire_time + actual_fire_time + delay_seconds so
    operators can grep for incidents and characterise lateness
    distribution."""
    config = _make_brief_config(tmp_path)
    frozen_now = datetime(2026, 5, 28, 10, 30, tzinfo=HALIFAX)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr("alfred.brief.daemon.datetime", _FrozenDT)

    async def _fake_generate_brief(cfg, state_mgr, refresh=False):
        state_mgr.state.add_run(BriefRun(
            date=frozen_now.date().isoformat(),
            generated_at=frozen_now.isoformat(),
            vault_path="run/morning-briefs/test.md",
            sections=["catch-up"],
            success=True,
        ))
        state_mgr.save()
        return "run/morning-briefs/test.md"

    monkeypatch.setattr(
        "alfred.brief.daemon.generate_brief", _fake_generate_brief,
    )

    with structlog.testing.capture_logs() as captured:
        await _run_with_timeout(run_daemon(config))

    catchup_events = [
        c for c in captured if c.get("event") == "brief.daemon.catchup_fired"
    ]
    assert len(catchup_events) == 1
    event = catchup_events[0]
    # Required fields per the dispatch contract.
    assert event["date"] == "2026-05-28"
    assert "intended_fire_time" in event
    assert "actual_fire_time" in event
    assert "delay_seconds" in event
    # delay_seconds is numeric (rounded to 1 decimal place in the
    # production code).
    assert isinstance(event["delay_seconds"], (int, float))
    # 4h30m = 16200s; tolerate rounding to 1 decimal.
    assert event["delay_seconds"] == pytest.approx(16200.0, abs=1.0)


# ---------------------------------------------------------------------------
# State-write parity — catch-up fire produces same state shape as scheduled
# ---------------------------------------------------------------------------


async def test_catchup_fire_updates_state_identically_to_scheduled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catch-up path MUST go through ``generate_brief`` (same code
    path as scheduled fire) so state updates land identically. Pin
    that after catch-up, ``has_brief_for_date(today)`` returns True
    and the runs list grew by one row with the same shape the
    scheduled fire produces."""
    config = _make_brief_config(tmp_path)
    frozen_now = datetime(2026, 5, 28, 10, 30, tzinfo=HALIFAX)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen_now if tz is None else frozen_now.astimezone(tz)

    monkeypatch.setattr("alfred.brief.daemon.datetime", _FrozenDT)

    async def _fake_generate_brief(cfg, state_mgr, refresh=False):
        # Exact same code as the real generate_brief's state-write
        # block (lines ~197-204 of brief/daemon.py). This is what we
        # mean by "state-write parity" — the catch-up doesn't shortcut.
        state_mgr.state.add_run(BriefRun(
            date=frozen_now.date().isoformat(),
            generated_at=frozen_now.isoformat(),
            vault_path="run/morning-briefs/test.md",
            sections=["weather"],
            success=True,
        ))
        state_mgr.save()
        return "run/morning-briefs/test.md"

    monkeypatch.setattr(
        "alfred.brief.daemon.generate_brief", _fake_generate_brief,
    )

    await _run_with_timeout(run_daemon(config))

    # Re-load state from disk to verify the on-disk shape matches
    # what a scheduled fire would produce.
    fresh = StateManager(config.state.path)
    fresh.load()
    assert fresh.state.has_brief_for_date("2026-05-28") is True
    assert len(fresh.state.runs) == 1
    run = fresh.state.runs[0]
    assert run.date == "2026-05-28"
    assert run.success is True
    assert run.vault_path == "run/morning-briefs/test.md"
