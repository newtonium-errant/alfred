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
