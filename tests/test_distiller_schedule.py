"""Tests for distiller's clock-aligned deep extraction and (c5)
consolidation scheduling.

The c4/c5 migrations replace rolling ``hours_since`` gates with
``compute_next_fire`` checks. These tests exercise the scheduling
policy directly — we don't spin up the heavy extraction pipeline
because that's orthogonal to the gating decision.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alfred.common.schedule import ScheduleConfig, compute_next_fire
from alfred.distiller.config import DistillerConfig, load_from_unified


# ---------------------------------------------------------------------------
# Config — c4 deep extraction schedule
# ---------------------------------------------------------------------------


def test_deep_extraction_schedule_defaults_to_0330_halifax() -> None:
    cfg = DistillerConfig()
    sched = cfg.extraction.deep_extraction_schedule
    assert sched.time == "03:30"
    assert sched.timezone == "America/Halifax"
    assert sched.day_of_week is None


def test_load_from_unified_reads_deep_extraction_schedule(
    tmp_path: Path,
) -> None:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "distiller": {
            "extraction": {
                "deep_extraction_schedule": {
                    "time": "04:15",
                    "timezone": "America/New_York",
                },
            },
            "state": {"path": str(tmp_path / "state.json")},
        },
    }
    cfg = load_from_unified(raw)
    sched = cfg.extraction.deep_extraction_schedule
    assert isinstance(sched, ScheduleConfig)
    assert sched.time == "04:15"
    assert sched.timezone == "America/New_York"


def test_load_from_unified_deep_extraction_fallback_to_defaults(
    tmp_path: Path,
) -> None:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "distiller": {
            "extraction": {"interval_seconds": 3600},
            "state": {"path": str(tmp_path / "state.json")},
        },
    }
    cfg = load_from_unified(raw)
    sched = cfg.extraction.deep_extraction_schedule
    assert sched.time == "03:30"
    assert sched.timezone == "America/Halifax"


# ---------------------------------------------------------------------------
# Scheduling policy — c4 deep extraction
# ---------------------------------------------------------------------------


def _would_fire(
    schedule: ScheduleConfig, last_fire: datetime, now: datetime,
) -> bool:
    return now >= compute_next_fire(schedule, last_fire)


def test_deep_extraction_fires_once_at_0330_halifax() -> None:
    hfx = ZoneInfo("America/Halifax")
    schedule = ScheduleConfig(time="03:30", timezone="America/Halifax")

    # Boot at 23:00 on 2026-04-20; next scheduled fire is 03:30 on 04-21.
    last_deep = datetime(2026, 4, 20, 23, 0, tzinfo=hfx)

    assert not _would_fire(
        schedule, last_deep,
        datetime(2026, 4, 21, 3, 29, tzinfo=hfx),
    )
    assert _would_fire(
        schedule, last_deep,
        datetime(2026, 4, 21, 3, 30, tzinfo=hfx),
    )
    assert _would_fire(
        schedule, last_deep,
        datetime(2026, 4, 21, 4, 0, tzinfo=hfx),
    )


def test_deep_extraction_restart_within_window_does_not_refire() -> None:
    hfx = ZoneInfo("America/Halifax")
    schedule = ScheduleConfig(time="03:30", timezone="America/Halifax")
    last_deep = datetime(2026, 4, 21, 3, 31, tzinfo=hfx).astimezone(
        timezone.utc,
    )

    # Restart later same day — not yet due.
    assert not _would_fire(
        schedule, last_deep,
        datetime(2026, 4, 21, 12, 0, tzinfo=hfx),
    )
    # Next morning at 03:31 — due again.
    assert _would_fire(
        schedule, last_deep,
        datetime(2026, 4, 22, 3, 31, tzinfo=hfx),
    )


def test_deep_extraction_fresh_state_seed_does_not_fire_immediately() -> None:
    hfx = ZoneInfo("America/Halifax")
    schedule = ScheduleConfig(time="03:30", timezone="America/Halifax")
    boot = datetime(2026, 4, 21, 9, 0, tzinfo=hfx)
    last_deep = boot  # seeded to now
    assert not _would_fire(schedule, last_deep, boot + timedelta(seconds=5))
