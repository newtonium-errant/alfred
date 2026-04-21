"""Tests for the shared ``alfred.common.schedule`` primitive."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from alfred.common.schedule import (
    ScheduleConfig,
    compute_next_fire,
    parse_day_of_week,
    sleep_until,
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


# ---------------------------------------------------------------------------
# sleep_until — wall-clock-checked chunked sleep
#
# Regression coverage for the brief daemon firing 14-40 min early on
# 2026-04-16..21. Root cause: a single ``await asyncio.sleep(N)`` over
# a ~10h horizon drifted relative to wall-clock time (WSL2 host
# suspend/resume; NTP adjustments). The fix re-checks the wall clock
# between capped chunks so drift is bounded to one chunk.
#
# The tests below drive ``sleep_until`` with injected sleeper/clock so
# we can simulate monotonic-vs-wall-clock skew deterministically.
# ---------------------------------------------------------------------------


class _FakeClock:
    """Deterministic tz-aware clock for sleep_until tests.

    ``advance_on_sleep`` controls how fast wall-clock time advances
    per sleep call relative to the requested duration. 1.0 = perfect
    sync; 0.5 = monotonic clock runs 2x faster than wall clock (early
    fires — the observed brief bug); 2.0 = wall clock runs 2x faster
    (late fires).

    Tail-progress note: at ``advance_on_sleep < 1.0`` the production
    code's ``min(remaining, chunk_seconds)`` shrinks each tail chunk,
    and a strict ``advance = seconds * factor`` model makes ``remaining``
    decay geometrically toward subnormal floats — an infinite loop that
    doesn't exist in production (real wall time advances independently
    of the sleep request, and ``asyncio.sleep`` has an event-loop tick
    floor of ~1 ms). We model that floor here: a per-call ``advance``
    is at least 1 ms, so the loop terminates after a bounded number of
    iterations exactly as it does in production.
    """

    # Minimum wall-clock advance per sleep call. Mirrors the real
    # asyncio event-loop tick floor; without it the geometric tail-chunk
    # shrinkage at ``advance_on_sleep < 1`` never reaches zero.
    _MIN_ADVANCE_S = 0.001

    def __init__(
        self,
        start: datetime,
        advance_on_sleep: float = 1.0,
    ) -> None:
        self.t = start
        self.advance_on_sleep = advance_on_sleep
        self.sleep_calls: list[float] = []

    def now(self) -> datetime:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.sleep_calls.append(seconds)
        advance = max(seconds * self.advance_on_sleep, self._MIN_ADVANCE_S)
        self.t = self.t + timedelta(seconds=advance)


async def test_sleep_until_requires_tz_aware_target() -> None:
    with pytest.raises(ValueError):
        await sleep_until(datetime(2026, 4, 21, 9, 0))  # no tzinfo


async def test_sleep_until_rejects_nonpositive_chunk() -> None:
    tz = timezone.utc
    with pytest.raises(ValueError):
        await sleep_until(datetime(2026, 4, 21, 9, 0, tzinfo=tz), chunk_seconds=0)
    with pytest.raises(ValueError):
        await sleep_until(datetime(2026, 4, 21, 9, 0, tzinfo=tz), chunk_seconds=-5)


async def test_sleep_until_returns_immediately_if_target_in_past() -> None:
    tz = timezone.utc
    start = datetime(2026, 4, 21, 10, 0, tzinfo=tz)
    target = datetime(2026, 4, 21, 9, 0, tzinfo=tz)  # already passed
    fake = _FakeClock(start)
    elapsed = await sleep_until(
        target,
        sleeper=fake.sleep,
        clock=fake.now,
    )
    assert elapsed == 0.0
    assert fake.sleep_calls == []


async def test_sleep_until_chunks_long_sleep_into_capped_pieces() -> None:
    tz = timezone.utc
    start = datetime(2026, 4, 20, 23, 0, tzinfo=tz)
    target = datetime(2026, 4, 21, 9, 0, tzinfo=tz)  # 10h ahead
    fake = _FakeClock(start, advance_on_sleep=1.0)

    elapsed = await sleep_until(
        target,
        chunk_seconds=60.0,
        sleeper=fake.sleep,
        clock=fake.now,
    )

    # 10h = 36000s / 60s per chunk = 600 chunks, all at 60s.
    assert len(fake.sleep_calls) == 600
    assert all(c == 60.0 for c in fake.sleep_calls)
    assert elapsed == pytest.approx(10 * 3600, abs=0.1)


async def test_sleep_until_bounded_when_monotonic_clock_runs_fast() -> None:
    """Regression: brief fired early because monotonic clock advanced
    faster than wall clock during long sleep. With chunked checks, the
    early-fire window is bounded to roughly one chunk."""
    tz = timezone.utc
    start = datetime(2026, 4, 20, 23, 0, tzinfo=tz)
    target = datetime(2026, 4, 21, 9, 0, tzinfo=tz)  # 10h ahead

    # Wall clock runs at half the speed that asyncio.sleep(N) thinks
    # it does — every "N-second" sleep actually consumes 0.5*N wall
    # seconds. Without chunked re-checks this would "fire" at 5h wall
    # time even though 10h of wall time were requested.
    fake = _FakeClock(start, advance_on_sleep=0.5)

    elapsed = await sleep_until(
        target,
        chunk_seconds=60.0,
        sleeper=fake.sleep,
        clock=fake.now,
    )

    # Wall-clock target must be reached (remaining <= 0 break
    # condition) — the loop keeps re-checking and issuing more chunks
    # until real wall time catches up.
    assert fake.now() >= target
    # Elapsed wall-clock time is close to the requested 10h (within a
    # single chunk), not half of it. The pre-fix behavior would have
    # returned after ~5h of wall time.
    assert elapsed == pytest.approx(10 * 3600, abs=60.0)


async def test_sleep_until_bounded_when_monotonic_clock_runs_slow() -> None:
    """Symmetric case: if wall clock jumps forward during sleep (host
    resume from suspend), sleep_until still completes — it doesn't
    oversleep by hours waiting for a monotonic count that no longer
    matters."""
    tz = timezone.utc
    start = datetime(2026, 4, 20, 23, 0, tzinfo=tz)
    target = datetime(2026, 4, 21, 9, 0, tzinfo=tz)  # 10h ahead

    # Wall clock runs 4x faster than asyncio.sleep's monotonic view —
    # simulates a sudden NTP jump or host clock sync.
    fake = _FakeClock(start, advance_on_sleep=4.0)

    elapsed = await sleep_until(
        target,
        chunk_seconds=60.0,
        sleeper=fake.sleep,
        clock=fake.now,
    )

    # Wall clock target reached (possibly slightly overshot within one
    # chunk) — the loop exits as soon as remaining <= 0.
    assert fake.now() >= target
    # With 4x speedup, each 60s chunk advances wall clock by 240s. The
    # loop stops as soon as wall clock >= target, so it fires close to
    # the intended time (not 40h late).
    assert elapsed == pytest.approx(10 * 3600, abs=240.0)


async def test_sleep_until_honors_chunk_seconds_bound() -> None:
    """Each sleep chunk must be no larger than ``chunk_seconds``.

    This is the anti-drift invariant: if any single chunk could be
    arbitrarily long, we'd be back to the original bug where a long
    asyncio.sleep drifts from wall time."""
    tz = timezone.utc
    start = datetime(2026, 4, 20, 23, 0, tzinfo=tz)
    target = datetime(2026, 4, 21, 9, 0, tzinfo=tz)
    fake = _FakeClock(start, advance_on_sleep=1.0)

    await sleep_until(
        target,
        chunk_seconds=30.0,
        sleeper=fake.sleep,
        clock=fake.now,
    )

    assert all(c <= 30.0 + 1e-9 for c in fake.sleep_calls)


async def test_sleep_until_final_chunk_shorter_than_cap() -> None:
    """When remaining < chunk_seconds, we only sleep for remaining —
    not the full chunk. Prevents firing up to one chunk late."""
    tz = timezone.utc
    start = datetime(2026, 4, 21, 8, 59, 30, tzinfo=tz)
    target = datetime(2026, 4, 21, 9, 0, tzinfo=tz)  # 30s ahead
    fake = _FakeClock(start, advance_on_sleep=1.0)

    await sleep_until(
        target,
        chunk_seconds=60.0,
        sleeper=fake.sleep,
        clock=fake.now,
    )

    assert fake.sleep_calls == [30.0]


async def test_sleep_until_tz_aware_target_in_halifax() -> None:
    """The brief daemon's actual use case: 06:00 Halifax target
    starting from 23:02 UTC the night before."""
    hfx = ZoneInfo("America/Halifax")
    start = datetime(2026, 4, 20, 23, 2, 15, tzinfo=timezone.utc).astimezone(hfx)
    target = datetime(2026, 4, 21, 6, 0, tzinfo=hfx)  # 06:00 ADT = 09:00 UTC
    fake = _FakeClock(start, advance_on_sleep=0.97)  # 3% drift (observed)

    elapsed = await sleep_until(
        target,
        chunk_seconds=60.0,
        sleeper=fake.sleep,
        clock=fake.now,
    )

    # Wall clock target reached. Pre-fix: a single asyncio.sleep at
    # 3% drift would fire ~18 min early on a 10h sleep.
    assert fake.now() >= target
    expected_s = (target - start).total_seconds()
    assert elapsed == pytest.approx(expected_s, abs=60.0)
