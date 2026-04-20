"""Tests for janitor's clock-aligned deep sweep scheduling.

The c3 migration replaces a rolling ``last_run + 24h`` gate with a
clock-aligned ``compute_next_fire`` check against a
``deep_sweep_schedule: ScheduleConfig``. These tests exercise the
scheduling *policy* directly — we don't spin up the full daemon loop
because the sweep itself does heavy I/O.

Policy under test:
    1. On first boot with no persisted ``last_deep_sweep``, we seed
       ``last_deep`` = ``now`` so we don't re-fire immediately.
    2. The deep gate fires only when ``now >=
       compute_next_fire(schedule, last_deep)``.
    3. A restart inside the same scheduled window (after last_deep was
       set) does not re-fire.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alfred.common.schedule import ScheduleConfig, compute_next_fire
from alfred.janitor.config import JanitorConfig, load_from_unified


def test_deep_sweep_schedule_defaults_to_0230_halifax() -> None:
    cfg = JanitorConfig()
    assert cfg.sweep.deep_sweep_schedule.time == "02:30"
    assert cfg.sweep.deep_sweep_schedule.timezone == "America/Halifax"
    assert cfg.sweep.deep_sweep_schedule.day_of_week is None


def test_load_from_unified_reads_deep_sweep_schedule(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "janitor": {
            "sweep": {
                "deep_sweep_schedule": {
                    "time": "03:15",
                    "timezone": "America/New_York",
                },
            },
            "state": {"path": str(tmp_path / "state.json")},
        },
    }
    cfg = load_from_unified(raw)
    assert isinstance(cfg.sweep.deep_sweep_schedule, ScheduleConfig)
    assert cfg.sweep.deep_sweep_schedule.time == "03:15"
    assert cfg.sweep.deep_sweep_schedule.timezone == "America/New_York"


def test_load_from_unified_fallback_defaults_when_schedule_missing(
    tmp_path: Path,
) -> None:
    """Omitting the schedule block falls back to built-in defaults."""
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "janitor": {
            "sweep": {"interval_seconds": 3600},
            "state": {"path": str(tmp_path / "state.json")},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.sweep.deep_sweep_schedule.time == "02:30"
    assert cfg.sweep.deep_sweep_schedule.timezone == "America/Halifax"


# ---------------------------------------------------------------------------
# Scheduling policy — simulate the watch loop's deep-sweep gate.
# ---------------------------------------------------------------------------


def _would_fire(
    schedule: ScheduleConfig, last_deep: datetime, now: datetime,
) -> bool:
    """Mirror the daemon's gate: ``now >= compute_next_fire(schedule, last_deep)``."""
    return now >= compute_next_fire(schedule, last_deep)


def test_fresh_state_seeded_to_now_does_not_fire_immediately() -> None:
    """On first boot we seed ``last_deep = now`` — the very next loop
    iteration must NOT re-fire (else we'd always deep-sweep on boot)."""
    hfx = ZoneInfo("America/Halifax")
    schedule = ScheduleConfig(time="02:30", timezone="America/Halifax")

    # Boot at 09:00 Halifax; seeded last_deep = now.
    boot = datetime(2026, 4, 21, 9, 0, tzinfo=hfx)
    last_deep = boot

    # Immediately next loop iteration a few seconds later — must not fire.
    now = boot + timedelta(seconds=5)
    assert not _would_fire(schedule, last_deep, now)


def test_fires_once_when_clock_rolls_past_0230_halifax() -> None:
    hfx = ZoneInfo("America/Halifax")
    schedule = ScheduleConfig(time="02:30", timezone="America/Halifax")

    # Boot at 23:00 on 2026-04-20; next scheduled fire is 02:30 on 04-21.
    last_deep = datetime(2026, 4, 20, 23, 0, tzinfo=hfx)

    # Just before 02:30 — don't fire.
    now_before = datetime(2026, 4, 21, 2, 29, tzinfo=hfx)
    assert not _would_fire(schedule, last_deep, now_before)

    # Exactly 02:30 — fire.
    now_at = datetime(2026, 4, 21, 2, 30, tzinfo=hfx)
    assert _would_fire(schedule, last_deep, now_at)

    # After 02:30 — fire.
    now_after = datetime(2026, 4, 21, 3, 0, tzinfo=hfx)
    assert _would_fire(schedule, last_deep, now_after)


def test_restart_within_same_window_does_not_refire() -> None:
    """After we fire at 02:30, ``last_deep`` advances. Any restart
    before the NEXT 02:30 must not re-fire the deep sweep."""
    hfx = ZoneInfo("America/Halifax")
    schedule = ScheduleConfig(time="02:30", timezone="America/Halifax")

    # Simulate a successful fire: last_deep bumped to 02:31 Halifax.
    last_deep_utc = datetime(2026, 4, 21, 2, 31, tzinfo=hfx).astimezone(
        timezone.utc,
    )

    # Restart at 11:00 Halifax same day — should NOT re-fire (next
    # scheduled fire is 02:30 on 04-22).
    now_restart = datetime(2026, 4, 21, 11, 0, tzinfo=hfx)
    assert not _would_fire(schedule, last_deep_utc, now_restart)

    # Restart just before next 02:30 — still not due.
    now_still = datetime(2026, 4, 22, 2, 29, tzinfo=hfx)
    assert not _would_fire(schedule, last_deep_utc, now_still)

    # Now at 02:30 next morning — due.
    now_next_window = datetime(2026, 4, 22, 2, 31, tzinfo=hfx)
    assert _would_fire(schedule, last_deep_utc, now_next_window)


def test_mixed_tz_now_and_last_deep_compare_correctly() -> None:
    """``now`` is UTC inside the daemon; ``last_deep`` may be UTC or
    Halifax. Both are tz-aware so comparison handles offsets."""
    schedule = ScheduleConfig(time="02:30", timezone="America/Halifax")

    last_deep_utc = datetime(2026, 4, 20, 23, 0, tzinfo=timezone.utc)
    # 2026-04-21 05:35 UTC == 02:35 Halifax (ADT, UTC-3) — past fire.
    now_utc = datetime(2026, 4, 21, 5, 35, tzinfo=timezone.utc)
    assert _would_fire(schedule, last_deep_utc, now_utc)
