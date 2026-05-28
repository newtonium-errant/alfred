"""Shared clock-aligned scheduling primitive.

Problem this solves
-------------------
Rolling-interval scheduling (``last_run + Nh``) drifts with daemon
restarts. Every restart during development shifts the next fire time
forward, which meant heavy daily passes (janitor deep sweep, distiller
deep extraction + consolidation) could land during the user's working
hours instead of overnight. The brief daemon already uses clock-aligned
scheduling ("next 06:00 America/Halifax"); this module generalises that
pattern so the other tools can adopt it.

Scope
-----
- Daily schedules: fire at ``time`` in ``timezone`` every day.
- Weekly schedules: fire at ``time`` in ``timezone`` on ``day_of_week``
  only (``"monday"`` ... ``"sunday"``).
- DST-aware via ``zoneinfo.ZoneInfo`` — construct target datetimes in
  the zone so spring-forward / fall-back resolve correctly.

Non-goals
---------
- Sub-daily intervals (those stay rolling — e.g. janitor structural
  sweep every hour).
- Multiple fire times per day. One ``time`` per schedule.
- Cron expressions. The two shapes (daily / weekly) cover every daily
  operational pass alfred runs; add more shapes only when a concrete
  use case demands it.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


_DAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass
class ScheduleConfig:
    """A clock-aligned schedule definition.

    Attributes
    ----------
    time:
        ``"HH:MM"`` 24-hour wall-clock time in ``timezone``.
    timezone:
        IANA timezone name (e.g. ``"America/Halifax"``).
    day_of_week:
        ``None`` for daily; one of ``"monday"``..``"sunday"`` (case
        insensitive) for a weekly gate — fires only on that weekday.
    """

    time: str = "06:00"
    timezone: str = "America/Halifax"
    day_of_week: str | None = None


def parse_day_of_week(s: str) -> int:
    """Map a day name to Python's ``weekday()`` index (Monday = 0).

    Accepts any case; rejects everything else with ``ValueError``.
    """
    if not isinstance(s, str):
        raise ValueError(f"day_of_week must be a string, got {type(s).__name__}")
    key = s.strip().lower()
    if key not in _DAYS:
        raise ValueError(
            f"invalid day_of_week: {s!r} "
            f"(expected one of: {', '.join(_DAYS)})"
        )
    return _DAYS[key]


def _parse_hhmm(s: str) -> tuple[int, int]:
    if not isinstance(s, str) or ":" not in s:
        raise ValueError(f"invalid time format: {s!r} (expected 'HH:MM')")
    try:
        hour_s, minute_s = s.split(":", 1)
        hour = int(hour_s)
        minute = int(minute_s)
    except ValueError as exc:
        raise ValueError(f"invalid time format: {s!r} (expected 'HH:MM')") from exc
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(
            f"invalid time: {s!r} (hour must be 0-23, minute 0-59)"
        )
    return hour, minute


def compute_next_fire(config: ScheduleConfig, now: datetime) -> datetime:
    """Return the next wall-clock fire time *after* ``now`` (tz-aware).

    ``now`` MUST be timezone-aware — this is a pure function, callers
    are expected to pass ``datetime.now(tz)`` or equivalent. Accepting
    naive ``now`` would silently drift on DST boundaries.

    Returns the target datetime in ``config.timezone`` (the caller can
    convert to UTC via ``.astimezone(timezone.utc)`` if needed).

    Daily (``day_of_week is None``):
        Returns today's ``time`` in ``timezone`` if it is strictly
        after ``now``, otherwise tomorrow's.

    Weekly (``day_of_week`` set):
        Returns the next occurrence of ``day_of_week`` at ``time``. If
        today is ``day_of_week`` and ``time`` is still ahead of
        ``now``, returns today. Otherwise, the next matching weekday
        within the following 7 days.

    DST notes:
        Spring-forward: the missing hour is handled by ``zoneinfo``;
        if ``time`` lands in the skipped hour, ``zoneinfo`` shifts it
        forward. Fall-back: the first (pre-transition) occurrence is
        returned — stable for scheduling because the daemon loop
        reschedules after firing.
    """
    if now.tzinfo is None:
        raise ValueError(
            "compute_next_fire requires tz-aware 'now'; "
            "pass datetime.now(ZoneInfo(...)) or similar"
        )

    tz = ZoneInfo(config.timezone)
    hour, minute = _parse_hhmm(config.time)
    local_now = now.astimezone(tz)

    target_today = local_now.replace(
        hour=hour, minute=minute, second=0, microsecond=0,
    )

    if config.day_of_week is None:
        # Daily schedule.
        if target_today > local_now:
            return target_today
        return target_today + timedelta(days=1)

    # Weekly schedule — find the next occurrence of day_of_week.
    target_weekday = parse_day_of_week(config.day_of_week)
    # How many days ahead is the target weekday?
    days_ahead = (target_weekday - local_now.weekday()) % 7
    if days_ahead == 0 and target_today > local_now:
        return target_today
    if days_ahead == 0:
        days_ahead = 7
    # Construct the target date then materialise the wall-clock time
    # inside the tz so DST transitions resolve correctly.
    target_date = (local_now + timedelta(days=days_ahead)).date()
    return datetime(
        target_date.year,
        target_date.month,
        target_date.day,
        hour,
        minute,
        tzinfo=tz,
    )


def compute_today_fire(
    config: ScheduleConfig, now: datetime,
) -> datetime:
    """Return TODAY's fire time in the configured timezone, regardless
    of whether ``now`` is before or after it.

    Distinct from :func:`compute_next_fire`, which always returns a
    fire time *after* ``now`` (so a post-fire-window ``now`` returns
    tomorrow's target). This helper returns today's target unconditionally
    — needed for the daemon-catch-up shape where a daemon boots after
    today's window has already passed and needs to check "should I
    catch up?"

    Weekly schedules: returns today's target if today matches
    ``day_of_week`` (whether or not the time has passed); otherwise
    returns the next matching weekday's target. The catch-up shape
    only fires when today is the scheduled day, so a non-matching
    weekday returns a future target the caller will ignore.

    ``now`` MUST be timezone-aware — same contract as
    :func:`compute_next_fire`.
    """
    if now.tzinfo is None:
        raise ValueError(
            "compute_today_fire requires tz-aware 'now'; "
            "pass datetime.now(ZoneInfo(...)) or similar"
        )

    tz = ZoneInfo(config.timezone)
    hour, minute = _parse_hhmm(config.time)
    local_now = now.astimezone(tz)

    if config.day_of_week is None:
        # Daily schedule — today's target regardless of past/future.
        return local_now.replace(
            hour=hour, minute=minute, second=0, microsecond=0,
        )

    # Weekly schedule — if today matches day_of_week, return today's
    # target; otherwise delegate to compute_next_fire (the catch-up
    # caller only acts when today's target is in the past, which
    # implies a same-day match — non-matching weekdays return a future
    # target the caller won't act on).
    target_weekday = parse_day_of_week(config.day_of_week)
    if local_now.weekday() == target_weekday:
        return local_now.replace(
            hour=hour, minute=minute, second=0, microsecond=0,
        )
    return compute_next_fire(config, now)


def should_catchup_today(
    config: ScheduleConfig,
    now: datetime,
    already_fired_today: bool,
) -> tuple[bool, datetime, float]:
    """Decide whether a daemon should fire immediately on boot to
    catch up on a missed scheduled fire.

    Returns ``(should_catchup, today_target, delay_seconds)``:
      * ``should_catchup`` — True iff today's scheduled fire window
        has passed AND the daemon's state shows no successful fire
        for today.
      * ``today_target`` — today's scheduled fire time in the
        configured timezone (the "intended_fire_time" the daemon
        will log).
      * ``delay_seconds`` — non-negative; how late ``now`` is vs.
        ``today_target``. 0.0 if ``now`` is at-or-before the target.

    The caller is responsible for the actual fire — this is a pure
    decision helper. Two callers in V1:

      * ``brief.daemon.run_daemon`` — checks
        ``state_mgr.state.has_brief_for_date(today_iso)`` to derive
        ``already_fired_today``.
      * ``daily_sync.daemon.run_daemon`` — checks
        ``state.get("last_fired_date") == today_iso``.

    Per ``feedback_intentionally_left_blank.md``: the catch-up path
    is observability-load-bearing. Each daemon's catch-up log event
    (``brief.daemon.catchup_fired`` / ``daily_sync.daemon.catchup_fired``)
    surfaces ``delay_seconds`` as a top-level field so operators can
    count incidents and characterise their lateness distribution
    via grep.

    Shipped 2026-05-28 after a host restart mid-morning caused the
    brief + daily_sync daemons to miss their windows; both probes
    reported FAIL even though the daemons were alive and would have
    fired had the host been up at 06:00 / 09:00 ADT. The catch-up
    closes the false-FAIL class.
    """
    today_target = compute_today_fire(config, now)
    # Convert now to the same tz for clean subtraction (both
    # tz-aware ⇒ subtraction is straightforward).
    local_now = now.astimezone(today_target.tzinfo)
    delay = (local_now - today_target).total_seconds()
    delay_seconds = max(0.0, delay)
    window_passed = local_now > today_target
    should = window_passed and not already_fired_today
    return should, today_target, delay_seconds


# Default re-check cadence for ``sleep_until``. Each chunk costs one
# wakeup + wall-clock read; 60s is a negligible per-day overhead
# (~1440 chunks) and keeps early/late fires bounded to <= one chunk
# regardless of how badly the monotonic clock drifts mid-sleep.
_SLEEP_CHUNK_SECONDS = 60.0


async def sleep_until(
    target: datetime,
    *,
    chunk_seconds: float = _SLEEP_CHUNK_SECONDS,
    sleeper = None,
    clock = None,
) -> float:
    """Sleep until wall-clock ``target`` (tz-aware) is reached.

    Why this exists
    ---------------
    A single ``await asyncio.sleep(N)`` over a long horizon (hours)
    drifts when the underlying monotonic clock gets out of sync with
    wall-clock time. On WSL2, Windows host suspend / resume and NTP
    adjustments can push the monotonic clock forward or backward
    relative to wall time, causing the daemon to fire many minutes
    early or late. Observed drift in production: brief daemon fired
    14-40 min early on consecutive days during Apr 16-21 2026, despite
    ``compute_next_fire`` returning the correct target.

    The fix is to treat the long sleep as a series of capped chunks,
    re-reading the wall clock between each chunk. Even if one chunk's
    monotonic duration is wildly off, the wall-clock check catches up
    on the next iteration — so the maximum drift is bounded to roughly
    one chunk (default 60s) regardless of suspend/resume behavior.

    Parameters
    ----------
    target:
        The tz-aware wall-clock time to wake at.
    chunk_seconds:
        Max per-iteration sleep. Shorter = tighter wall-clock bound
        but more wakeups. 60s is the default (≈1440 wakeups/day,
        negligible).
    sleeper:
        Injectable async sleep function for tests. Defaults to
        ``asyncio.sleep``.
    clock:
        Injectable clock function for tests. Called with no args,
        must return a tz-aware ``datetime``. Defaults to reading
        ``datetime.now(target.tzinfo)``.

    Returns
    -------
    Actual elapsed wall-clock seconds (measured by ``clock``) — caller
    can log this and compare against the intended sleep for drift
    diagnostics.
    """
    if target.tzinfo is None:
        raise ValueError(
            "sleep_until requires tz-aware 'target'; pass a datetime "
            "with tzinfo set (e.g. from compute_next_fire)"
        )
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds must be positive")

    if sleeper is None:
        sleeper = asyncio.sleep
    if clock is None:
        def clock() -> datetime:
            return datetime.now(target.tzinfo)

    start = clock()
    while True:
        now = clock()
        remaining = (target - now).total_seconds()
        if remaining <= 0:
            break
        await sleeper(min(remaining, chunk_seconds))
    end = clock()
    return (end - start).total_seconds()
