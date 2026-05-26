"""Cadence dispatcher — "is the routine due today?" for six recurrence shapes.

Hand-rolled rather than ``dateutil.rrule``: the six shapes below cover every
operator template captured during Plan ratification (daily, weekly,
every-N-days, monthly-by-day, monthly-by-nth-weekday, every-N-months) and
the implementation is short, deterministic, and dependency-thin. Add a new
shape only when a concrete use case demands it — symmetric to the
``common/schedule.py`` "two-shapes-or-add-by-need" stance.

Shape reference (cadence dict on each ``routine`` record):

    {type: daily}
    {type: weekly, days: ["Mon", "Wed", "Fri"]}
    {type: every_n_days, n: 14, anchor: "2026-01-06"}
    {type: monthly, day: 1}                  # 1..31 or "last"
    {type: monthly, nth_weekday: [1, "Mon"]} # 1st Mon; [-1, "Fri"] = last Fri
    {type: every_n_months, n: 2, day: 15, anchor: "2026-01-15"}

All shapes are evaluated in the operator's local timezone via a plain
``date`` (no clock-time involved at this layer — clock-time gating is the
daemon's job). Anchors are inclusive lower bounds: a routine with an
``anchor`` in the future is NOT due today; an anchor equal to today IS
due (modulo the per-shape rule).

DST + leap-year notes:
  - ``every_n_days`` arithmetic is plain ``date`` subtraction in days; DST
    transitions don't affect calendar-day counts.
  - ``monthly day: 31`` in February clamps to the month's last day
    (matches operator intent — "the latest day of the month I can get").
  - ``monthly day: 'last'`` always returns True on the last day of the
    current month regardless of length.
  - ``every_n_months`` arithmetic uses ``dateutil.relativedelta`` so
    "anchor 2026-01-31 + 1 month" maps to 2026-02-28 (or 2026-02-29 on
    leap years) rather than crashing.
  - ``monthly nth_weekday: [-1, ...]`` requires walking from the LAST day
    of the month backwards, distinct from "nth from start." Test fixture
    pins both branches.
"""

from __future__ import annotations

import calendar
from datetime import date
from typing import Any

from dateutil.relativedelta import relativedelta


# Three-letter day abbreviations, lowercased for case-insensitive parsing.
# Maps to Python's ``date.weekday()`` index (Monday = 0).
_WEEKDAY_INDEX: dict[str, int] = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3,
    "fri": 4, "sat": 5, "sun": 6,
    # Long-form aliases — operators may write "Monday" instead of "Mon".
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


class CadenceError(ValueError):
    """Raised when a cadence dict is malformed or names an unknown shape.

    Distinct from ``ValueError`` so the aggregator can catch + log + skip
    one bad routine without crashing the whole sweep. Per
    ``feedback_intentionally_left_blank.md`` the aggregator emits a
    structured ``routine.aggregator.malformed_cadence`` event when it
    catches one rather than silently dropping the routine.
    """


def _weekday_index(token: str) -> int:
    """Map a weekday token (any case) to its ``date.weekday()`` index."""
    if not isinstance(token, str):
        raise CadenceError(
            f"weekday token must be a string, got {type(token).__name__}"
        )
    key = token.strip().lower()
    if key not in _WEEKDAY_INDEX:
        raise CadenceError(
            f"unknown weekday: {token!r} (expected Mon..Sun or full names)"
        )
    return _WEEKDAY_INDEX[key]


def _parse_anchor(anchor: Any, field_name: str) -> date:
    """Parse an ``anchor`` value — either ``date`` (Python YAML datetime) or
    ISO string ``YYYY-MM-DD``."""
    if isinstance(anchor, date):
        return anchor
    if isinstance(anchor, str):
        try:
            return date.fromisoformat(anchor)
        except ValueError as exc:
            raise CadenceError(
                f"{field_name}: invalid ISO date {anchor!r}"
            ) from exc
    raise CadenceError(
        f"{field_name}: expected date or YYYY-MM-DD string, "
        f"got {type(anchor).__name__}"
    )


def _last_day_of_month(year: int, month: int) -> int:
    """Calendar last day for the given month (handles Feb leap-year)."""
    return calendar.monthrange(year, month)[1]


def _nth_weekday_of_month(year: int, month: int, n: int, weekday: int) -> date:
    """Return the date of the n-th ``weekday`` in (year, month).

    ``n`` is 1-based for forward indexing (1 = first occurrence) or
    ``-1`` for "last occurrence." Other negative values follow the same
    pattern (``-2`` = second-to-last). Out-of-range positive ``n``
    (e.g. asking for the 5th Friday in a month with only 4) raises
    ``CadenceError`` — the operator's cadence is unresolvable, and we
    surface that rather than silently fire on a different week.
    """
    if n == 0:
        raise CadenceError("nth_weekday n must be non-zero")

    last_day = _last_day_of_month(year, month)

    if n > 0:
        # Walk forward from day 1, find matches.
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        candidate_day = 1 + offset + (n - 1) * 7
        if candidate_day > last_day:
            raise CadenceError(
                f"month {year:04d}-{month:02d} has no {n}-th "
                f"weekday-index-{weekday}"
            )
        return date(year, month, candidate_day)

    # n < 0 — walk backward from last day.
    last = date(year, month, last_day)
    offset_back = (last.weekday() - weekday) % 7
    candidate_day = last_day - offset_back + (n + 1) * 7
    if candidate_day < 1:
        raise CadenceError(
            f"month {year:04d}-{month:02d} has no {n}-th "
            f"weekday-index-{weekday} (counting from end)"
        )
    return date(year, month, candidate_day)


def is_due(cadence: Any, today: date) -> bool:
    """Return True iff the cadence fires on ``today``.

    ``cadence`` should be a dict with a ``type`` key. Anything else
    raises ``CadenceError``. Six known shapes:

      - ``{type: daily}``                          — fires every day.
      - ``{type: weekly, days: [...]}``            — fires on listed weekdays.
      - ``{type: every_n_days, n: N, anchor: D}``  — fires when (today-anchor) %% N == 0.
      - ``{type: monthly, day: D}``                — fires on day D of the month;
                                                     ``D = "last"`` ⇒ last day; D > month's
                                                     length clamps to last day.
      - ``{type: monthly, nth_weekday: [N, W]}``   — fires on the N-th W weekday of the month;
                                                     N may be negative (count from end).
      - ``{type: every_n_months, n: N, day: D, anchor: A}``
                                                   — fires when (today is exactly anchor + k*N
                                                     months for some k >= 0) AND the day-of-
                                                     month rule resolves to today.

    All shapes return False for ``today`` strictly before the anchor (when
    an anchor is required); a future anchor means the routine hasn't started
    yet. Anchors equal to ``today`` ARE due (the routine starts today).
    """
    if not isinstance(cadence, dict):
        raise CadenceError(
            f"cadence must be a dict, got {type(cadence).__name__}"
        )

    cadence_type = cadence.get("type")
    if not isinstance(cadence_type, str):
        raise CadenceError(
            f"cadence missing 'type' key (got keys: {sorted(cadence.keys())})"
        )

    if cadence_type == "daily":
        return True

    if cadence_type == "weekly":
        raw_days = cadence.get("days")
        if not isinstance(raw_days, list) or not raw_days:
            raise CadenceError(
                "weekly cadence requires non-empty 'days' list "
                "(e.g. ['Mon', 'Wed', 'Fri'])"
            )
        indices = {_weekday_index(d) for d in raw_days}
        return today.weekday() in indices

    if cadence_type == "every_n_days":
        n = cadence.get("n")
        if not isinstance(n, int) or n < 1:
            raise CadenceError(
                f"every_n_days requires positive integer 'n', got {n!r}"
            )
        anchor = _parse_anchor(cadence.get("anchor"), "every_n_days anchor")
        if today < anchor:
            return False
        delta_days = (today - anchor).days
        return delta_days % n == 0

    if cadence_type == "monthly":
        # Two sub-shapes: ``day: ...`` OR ``nth_weekday: [n, weekday]``.
        if "nth_weekday" in cadence:
            nw = cadence["nth_weekday"]
            if not (isinstance(nw, (list, tuple)) and len(nw) == 2):
                raise CadenceError(
                    f"monthly.nth_weekday must be [n, weekday], got {nw!r}"
                )
            n_raw, weekday_raw = nw
            if not isinstance(n_raw, int):
                raise CadenceError(
                    f"monthly.nth_weekday first element must be int, "
                    f"got {n_raw!r}"
                )
            weekday = _weekday_index(weekday_raw)
            target = _nth_weekday_of_month(today.year, today.month, n_raw, weekday)
            return today == target

        day = cadence.get("day")
        if day == "last":
            return today.day == _last_day_of_month(today.year, today.month)
        if isinstance(day, int) and 1 <= day <= 31:
            # Clamp day > month-length to the month's actual last day.
            last_day = _last_day_of_month(today.year, today.month)
            target_day = min(day, last_day)
            return today.day == target_day
        raise CadenceError(
            f"monthly requires 'day' (1..31 or 'last') OR 'nth_weekday', "
            f"got cadence={cadence!r}"
        )

    if cadence_type == "every_n_months":
        n = cadence.get("n")
        if not isinstance(n, int) or n < 1:
            raise CadenceError(
                f"every_n_months requires positive integer 'n', got {n!r}"
            )
        anchor = _parse_anchor(cadence.get("anchor"), "every_n_months anchor")
        if today < anchor:
            return False
        day = cadence.get("day", anchor.day)
        # Walk forward from anchor by ``n``-month steps; check whether
        # ``today`` is one of them. Bound the loop to keep it
        # deterministic — months_between covers ~166 years at n=1.
        months_between = (today.year - anchor.year) * 12 + (today.month - anchor.month)
        if months_between < 0:
            return False
        if months_between % n != 0:
            return False
        # Resolve the day-of-month rule for the target month.
        candidate_month_start = anchor + relativedelta(months=months_between)
        year, month = candidate_month_start.year, candidate_month_start.month
        last_day = _last_day_of_month(year, month)
        if day == "last":
            target_day = last_day
        elif isinstance(day, int) and 1 <= day <= 31:
            target_day = min(day, last_day)
        else:
            raise CadenceError(
                f"every_n_months.day must be 1..31 or 'last', got {day!r}"
            )
        return today == date(year, month, target_day)

    raise CadenceError(
        f"unknown cadence type: {cadence_type!r} "
        f"(expected daily/weekly/every_n_days/monthly/every_n_months)"
    )


__all__ = ["is_due", "CadenceError"]
