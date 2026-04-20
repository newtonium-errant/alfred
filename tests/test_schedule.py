"""Tests for the shared ``alfred.common.schedule`` primitive."""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from alfred.common.schedule import (
    ScheduleConfig,
    compute_next_fire,
    parse_day_of_week,
)


# ---------------------------------------------------------------------------
# parse_day_of_week
# ---------------------------------------------------------------------------


def test_parse_day_of_week_all_valid() -> None:
    expected = {
        "monday": 0, "tuesday": 1, "wednesday": 2,
        "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    }
    for name, idx in expected.items():
        assert parse_day_of_week(name) == idx


def test_parse_day_of_week_case_insensitive() -> None:
    assert parse_day_of_week("Monday") == 0
    assert parse_day_of_week("SUNDAY") == 6
    assert parse_day_of_week("Wednesday") == 2


def test_parse_day_of_week_whitespace_tolerated() -> None:
    assert parse_day_of_week("  tuesday ") == 1


def test_parse_day_of_week_invalid_raises() -> None:
    with pytest.raises(ValueError):
        parse_day_of_week("funday")
    with pytest.raises(ValueError):
        parse_day_of_week("")
    with pytest.raises(ValueError):
        parse_day_of_week(3)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Daily schedules
# ---------------------------------------------------------------------------


def test_daily_fires_today_when_now_is_before_time() -> None:
    hfx = ZoneInfo("America/Halifax")
    cfg = ScheduleConfig(time="06:00", timezone="America/Halifax")
    # 2026-04-21 03:00 Halifax — before 06:00
    now = datetime(2026, 4, 21, 3, 0, tzinfo=hfx)

    fire = compute_next_fire(cfg, now)

    assert fire.year == 2026 and fire.month == 4 and fire.day == 21
    assert fire.hour == 6 and fire.minute == 0
    assert fire.tzinfo is not None


def test_daily_fires_tomorrow_when_now_is_after_time() -> None:
    hfx = ZoneInfo("America/Halifax")
    cfg = ScheduleConfig(time="06:00", timezone="America/Halifax")
    # 2026-04-21 09:00 Halifax — already past 06:00
    now = datetime(2026, 4, 21, 9, 0, tzinfo=hfx)

    fire = compute_next_fire(cfg, now)

    assert fire.year == 2026 and fire.month == 4 and fire.day == 22
    assert fire.hour == 6 and fire.minute == 0


def test_daily_edge_now_equals_target_pushes_to_next_day() -> None:
    """If now == target, return tomorrow (strict-after semantics)."""
    hfx = ZoneInfo("America/Halifax")
    cfg = ScheduleConfig(time="06:00", timezone="America/Halifax")
    now = datetime(2026, 4, 21, 6, 0, 0, tzinfo=hfx)

    fire = compute_next_fire(cfg, now)

    assert fire.day == 22


def test_daily_respects_utc_input_converted_to_target_tz() -> None:
    """UTC ``now`` is converted into the target tz before comparison."""
    cfg = ScheduleConfig(time="02:30", timezone="America/Halifax")
    # 2026-04-21 08:00 UTC == 2026-04-21 05:00 Halifax (ADT, UTC-3)
    now_utc = datetime(2026, 4, 21, 8, 0, tzinfo=timezone.utc)

    fire = compute_next_fire(cfg, now_utc)
    # 05:00 Halifax is after 02:30 Halifax, so next fire is tomorrow.
    hfx = ZoneInfo("America/Halifax")
    fire_local = fire.astimezone(hfx)
    assert fire_local.day == 22
    assert fire_local.hour == 2 and fire_local.minute == 30


# ---------------------------------------------------------------------------
# Weekly schedules
# ---------------------------------------------------------------------------


def test_weekly_from_wednesday_picks_next_sunday() -> None:
    hfx = ZoneInfo("America/Halifax")
    cfg = ScheduleConfig(
        time="04:00", timezone="America/Halifax", day_of_week="sunday",
    )
    # 2026-04-22 is a Wednesday.
    now = datetime(2026, 4, 22, 12, 0, tzinfo=hfx)

    fire = compute_next_fire(cfg, now)

    # Next Sunday is 2026-04-26.
    assert fire.year == 2026 and fire.month == 4 and fire.day == 26
    assert fire.weekday() == 6  # sunday
    assert fire.hour == 4 and fire.minute == 0


def test_weekly_fires_today_when_today_is_target_and_time_is_ahead() -> None:
    hfx = ZoneInfo("America/Halifax")
    cfg = ScheduleConfig(
        time="04:00", timezone="America/Halifax", day_of_week="sunday",
    )
    # 2026-04-26 is a Sunday; 02:00 is before the 04:00 target.
    now = datetime(2026, 4, 26, 2, 0, tzinfo=hfx)

    fire = compute_next_fire(cfg, now)

    assert fire.day == 26 and fire.hour == 4


def test_weekly_fires_next_week_when_today_is_target_but_time_passed() -> None:
    hfx = ZoneInfo("America/Halifax")
    cfg = ScheduleConfig(
        time="04:00", timezone="America/Halifax", day_of_week="sunday",
    )
    # 2026-04-26 is a Sunday; 09:00 is after 04:00.
    now = datetime(2026, 4, 26, 9, 0, tzinfo=hfx)

    fire = compute_next_fire(cfg, now)

    # Next Sunday: 2026-05-03.
    assert fire.month == 5 and fire.day == 3
    assert fire.weekday() == 6


def test_weekly_from_every_starting_weekday() -> None:
    """Starting from any weekday, we always land on the next target weekday."""
    hfx = ZoneInfo("America/Halifax")
    cfg = ScheduleConfig(
        time="04:00", timezone="America/Halifax", day_of_week="sunday",
    )
    # 2026-04-20 is a Monday (weekday 0).
    base = datetime(2026, 4, 20, 12, 0, tzinfo=hfx)
    for offset in range(7):
        now = base.replace(day=20 + offset)
        fire = compute_next_fire(cfg, now)
        assert fire.weekday() == 6
        # All fires should be within 7 days of ``now``.
        delta = (fire - now).total_seconds()
        assert 0 < delta <= 7 * 24 * 3600


# ---------------------------------------------------------------------------
# DST transitions — Halifax observes DST.
# Spring forward 2026: 2026-03-08 02:00 ADT → 03:00 (02:00-02:59 skipped)
# Fall back 2026: 2026-11-01 02:00 ADT → 01:00 AST (01:00-01:59 repeats)
# ---------------------------------------------------------------------------


def test_dst_spring_forward_target_after_transition_ok() -> None:
    hfx = ZoneInfo("America/Halifax")
    cfg = ScheduleConfig(time="06:00", timezone="America/Halifax")
    # 2026-03-07 23:00 Halifax — before spring forward (03-08 02:00).
    now = datetime(2026, 3, 7, 23, 0, tzinfo=hfx)

    fire = compute_next_fire(cfg, now)

    # Next 06:00 Halifax is 2026-03-08 06:00, which is after the transition.
    assert fire.day == 8 and fire.hour == 6
    assert fire.tzinfo is not None


def test_dst_fall_back_target_after_transition_ok() -> None:
    hfx = ZoneInfo("America/Halifax")
    cfg = ScheduleConfig(time="06:00", timezone="America/Halifax")
    # 2026-10-31 23:00 Halifax — before fall back (11-01 02:00).
    now = datetime(2026, 10, 31, 23, 0, tzinfo=hfx)

    fire = compute_next_fire(cfg, now)

    # Target 06:00 on 11-01 is unambiguous (only occurs once after fallback).
    assert fire.month == 11 and fire.day == 1 and fire.hour == 6


def test_dst_deep_sweep_time_during_fall_back_window_resolved() -> None:
    """Deep sweep at 02:30 Halifax during fall-back — the 02:30 slot exists
    only once on fallback day (01:00-01:59 repeats, 02:00 is unambiguous)."""
    hfx = ZoneInfo("America/Halifax")
    cfg = ScheduleConfig(time="02:30", timezone="America/Halifax")
    now = datetime(2026, 10, 31, 23, 0, tzinfo=hfx)

    fire = compute_next_fire(cfg, now)

    assert fire.month == 11 and fire.day == 1
    assert fire.hour == 2 and fire.minute == 30


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_naive_now_raises() -> None:
    cfg = ScheduleConfig(time="06:00", timezone="America/Halifax")
    with pytest.raises(ValueError):
        compute_next_fire(cfg, datetime(2026, 4, 21, 3, 0))  # no tzinfo


def test_invalid_time_format_raises() -> None:
    cfg = ScheduleConfig(time="6am", timezone="America/Halifax")
    now = datetime.now(ZoneInfo("America/Halifax"))
    with pytest.raises(ValueError):
        compute_next_fire(cfg, now)


def test_invalid_time_range_raises() -> None:
    cfg = ScheduleConfig(time="25:00", timezone="America/Halifax")
    now = datetime.now(ZoneInfo("America/Halifax"))
    with pytest.raises(ValueError):
        compute_next_fire(cfg, now)


def test_invalid_day_of_week_raises() -> None:
    cfg = ScheduleConfig(
        time="04:00", timezone="America/Halifax", day_of_week="funday",
    )
    now = datetime.now(ZoneInfo("America/Halifax"))
    with pytest.raises(ValueError):
        compute_next_fire(cfg, now)
