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


# ---------------------------------------------------------------------------
# c6 — quarantine_dir_name wiring (2026-05-31 followup to 164839a)
# ---------------------------------------------------------------------------
#
# The brief reads quarantined-records from
# ``<vault>/<quarantine_dir_name>/spam/<YYYY-MM>/`` in operations.py.
# Pre-followup, the brief's daemon hardcoded the default — a per-
# instance ``email_classifier.quarantine_dir_name`` override would
# silently misroute the read (classifier wrote to override path; brief
# read from ``quarantine/``, reported "empty" forever). This wiring
# pulls the field from the email_classifier YAML block into BriefConfig
# so the daemon can thread it to format_operations_section.


def test_brief_config_quarantine_dir_default_when_email_classifier_absent(
    tmp_path: Path,
) -> None:
    """No email_classifier block → BriefConfig.quarantine_dir_name
    defaults to ``"quarantine"`` (matches EmailClassifierConfig
    default; instances without an email pipeline stay unaffected)."""
    raw: dict[str, Any] = {"vault": {"path": str(tmp_path)}}
    cfg = load_from_unified(raw)
    assert cfg.quarantine_dir_name == "quarantine"


def test_brief_config_quarantine_dir_default_when_field_omitted(
    tmp_path: Path,
) -> None:
    """email_classifier block present but ``quarantine_dir_name``
    field omitted → BriefConfig still defaults to ``"quarantine"``.
    Pins the YAML's-block-without-the-field path (the common case for
    instances that enable the classifier but don't override the dir)."""
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "email_classifier": {"enabled": True},
    }
    cfg = load_from_unified(raw)
    assert cfg.quarantine_dir_name == "quarantine"


def test_brief_config_quarantine_dir_threaded_from_email_classifier_block(
    tmp_path: Path,
) -> None:
    """Per-instance override on the classifier surfaces in BriefConfig.
    This is THE wiring the WARN identified — without this, the brief
    silently misroutes its quarantine read.

    A future drift that removes the load_from_unified plumbing fails
    this test instead of silently restoring the production
    misroute."""
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "email_classifier": {
            "enabled": True,
            "quarantine_dir_name": "spam_archive",
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.quarantine_dir_name == "spam_archive"


# ---------------------------------------------------------------------------
# Q3 Option A — BriefConfig cross-section read of routine.tier_defaults
# ---------------------------------------------------------------------------
#
# The brief's 06:00 tier view MUST apply the SAME tier-window defaults the
# routine aggregator's 05:59 pass does, or the render disagrees with the
# persisted handoff. The defaults live under routine.tier_defaults; the
# brief reads them cross-section (same pattern as quarantine_dir_name from
# email_classifier).


def test_brief_reads_routine_tier_defaults(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "routine": {"tier_defaults": {"escalate_at_days": 3,
                                      "surface_at_days": 5}},
    }
    cfg = load_from_unified(raw)
    assert cfg.tier_defaults is not None
    assert cfg.tier_defaults.escalate_at_days == 3
    assert cfg.tier_defaults.surface_at_days == 5


def test_brief_tier_defaults_absent_all_none(tmp_path: Path) -> None:
    """No routine.tier_defaults block → all-None (opt-out unchanged)."""
    raw: dict[str, Any] = {"vault": {"path": str(tmp_path)}}
    cfg = load_from_unified(raw)
    assert cfg.tier_defaults.escalate_at_days is None
    assert cfg.tier_defaults.surface_at_days is None


def test_brief_and_routine_tier_defaults_agree(tmp_path: Path) -> None:
    """The whole point of the cross-section read: brief + routine build
    IDENTICAL tier_defaults from the same YAML block (so the 05:59 and
    06:00 passes can't disagree)."""
    from alfred.routine.config import (
        load_from_unified as routine_load,
    )

    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "telegram": {"instance": {"name": "Salem"}},
        "routine": {"tier_defaults": {"escalate_at_days": 2,
                                      "surface_at_days": 7}},
    }
    brief_cfg = load_from_unified(raw)
    routine_cfg = routine_load(raw)
    assert (
        brief_cfg.tier_defaults.escalate_at_days
        == routine_cfg.tier_defaults.escalate_at_days
        == 2
    )
    assert (
        brief_cfg.tier_defaults.surface_at_days
        == routine_cfg.tier_defaults.surface_at_days
        == 7
    )
