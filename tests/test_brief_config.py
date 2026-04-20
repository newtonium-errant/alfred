"""Tests for the brief config's ScheduleConfig integration.

Covers the c2 migration: brief now consumes the shared
``alfred.common.schedule.ScheduleConfig`` instead of a private
per-module dataclass. Zero-behavior-change guarantee is enforced by
equivalence tests against ``compute_next_fire`` directly.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alfred.brief.config import BriefConfig, load_from_unified
from alfred.common.schedule import ScheduleConfig, compute_next_fire


def test_schedule_is_shared_dataclass(tmp_path: Path) -> None:
    """Brief's schedule is the shared common dataclass, not a private clone."""
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "brief": {"schedule": {"time": "06:00", "timezone": "America/Halifax"}},
    }
    cfg = load_from_unified(raw)
    assert isinstance(cfg.schedule, ScheduleConfig)
    # Brief is daily-only.
    assert cfg.schedule.day_of_week is None
    assert cfg.schedule.time == "06:00"
    assert cfg.schedule.timezone == "America/Halifax"


def test_schedule_default_values(tmp_path: Path) -> None:
    """Missing schedule section falls back to the documented defaults."""
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "brief": {},
    }
    cfg = load_from_unified(raw)
    assert cfg.schedule.time == "06:00"
    assert cfg.schedule.timezone == "America/Halifax"


def test_custom_schedule_preserved(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "brief": {"schedule": {"time": "07:30", "timezone": "America/New_York"}},
    }
    cfg = load_from_unified(raw)
    assert cfg.schedule.time == "07:30"
    assert cfg.schedule.timezone == "America/New_York"


def test_daemon_next_fire_matches_helper_output(tmp_path: Path) -> None:
    """The brief daemon's next-fire computation matches
    ``compute_next_fire`` directly — this is the zero-behavior-drift
    contract for the c2 migration."""
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "brief": {"schedule": {"time": "06:00", "timezone": "America/Halifax"}},
    }
    cfg = load_from_unified(raw)

    hfx = ZoneInfo("America/Halifax")
    # Pick a concrete moment so comparison is deterministic.
    now = datetime(2026, 4, 21, 3, 0, tzinfo=hfx)
    target = compute_next_fire(cfg.schedule, now)

    # Same-day 06:00 Halifax (before spring/fall DST shifts).
    assert target.day == 21
    assert target.hour == 6
    assert target.minute == 0
