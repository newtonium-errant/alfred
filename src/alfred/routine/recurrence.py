"""Unified recurrence grammar — one shape, one kernel, two queries.

Routine consolidation Step 3 (2026-06-28). Merges the two recurrence DSLs —
``cadence.py`` ("is this routine firing TODAY?") and ``due.py`` ("what's the
next deadline for THIS ITEM + am I behind this cycle?") — into ONE per-item
grammar. ``cadence.py`` and ``due.py`` are now thin compat shims over this
module; the frontmatter keys are UNCHANGED (``cadence:`` at the routine level,
``due_pattern:`` at the item level) — two names, one grammar (operator
decision Q1 = A: unify the vocabulary, keep the structure).

The two questions are ORTHOGONAL and stay distinct queries over the same
shape:

  * :func:`fires_on` — boolean "does this recurrence land on ``today``?"
    Replaces ``cadence.is_due``. Cadence error policy: RAISES
    :class:`CadenceError` on a malformed/unknown shape (the routine
    aggregator catches + logs + skips one bad routine).
  * :func:`next_due_on_or_after` — the next occurrence date >= ``today``.
    Replaces ``due.resolve_due_date``'s core. Also RAISES
    :class:`CadenceError` on malformed aux fields; the ``due`` shim catches
    it and converts to ``None`` + a ``routine.due.malformed`` log, preserving
    the historical due error policy exactly.

Plus the behind/due substrate helpers, relocated verbatim (behavior-identical)
to operate on the unified shape:
:func:`is_done_in_current_cycle`, :func:`completion_satisfies_current_cycle`,
:func:`overdue_effective_due`.

The unified type set (the union of both DSLs' shapes):

    daily                 — fires every day                         (cadence)
    weekly                — days: ["Mon","Wed"]                     (cadence + due)
                            due's singular ``day: "thu"`` folds to ``days: ["thu"]``
    weekly_soft           — due = end of current ISO week (Sunday)  (due)
    biweekly              — day + anchor; every 14 days             (due)
                            ALIAS: shares the every_n_days math (n=14) but keeps
                            its own anchor-weekday validation (do NOT silently
                            fire a mismatch — preserves the malformed→raise path)
    every_n_days          — n + anchor                             (cadence + due)
    monthly               — day: 1..31 | "last"                    (cadence + due)
    monthly_nth_weekday   — n + weekday (1st Mon, last Fri)        (cadence + due)
                            cadence's nested ``monthly {nth_weekday:[n,w]}`` folds
                            to the top-level ``monthly_nth_weekday {n, weekday}``
    every_n_months        — n + day + anchor                       (cadence only)

Query type-scoping (each query accepts exactly the shapes its predecessor
accepted, so the differential golden-master against the frozen old behavior
holds with ZERO divergence):

  * ``fires_on`` handles the SIX cadence shapes (daily, weekly, every_n_days,
    monthly, monthly_nth_weekday, every_n_months) and RAISES for the due-only
    shapes (biweekly, weekly_soft) + unknown — exactly as ``cadence.is_due``
    raised "unknown cadence type" for them. (Routine-level ``cadence:`` dicts
    only ever carry the cadence shapes, so this is never hit in production.)
  * ``next_due_on_or_after`` handles the SIX due shapes (weekly, weekly_soft,
    biweekly, monthly, every_n_days, monthly_nth_weekday). ``daily`` /
    ``every_n_months`` have no item-level next-due semantics today (Q5:
    leave unimplemented, fail-loud) and RAISE — unreachable in production
    because ``DUE_PATTERN_TYPES`` (the item-parse gate) excludes them.

All shapes evaluate in the operator's local timezone via a plain ``date``
(no clock-time at this layer). Anchors are inclusive lower bounds. DST/leap
notes carry over from the old modules: ``every_n_days`` is plain day
subtraction (DST-insensitive); ``monthly day:31`` clamps to the month's last
day; ``every_n_months`` uses ``relativedelta`` for leap-safe month math;
``nth_weekday:[-1, ...]`` walks backward from the month's last day.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from dateutil.relativedelta import relativedelta


# ---------------------------------------------------------------------------
# Shared date-math kernel (the de-facto shared base of both old DSLs —
# cadence.py owned it; due.py imported it. Now it lives here, one copy.)
# ---------------------------------------------------------------------------


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
    """Raised when a recurrence shape is malformed or names an unknown type.

    Name retained from the pre-consolidation ``cadence.py`` (re-exported by
    the ``cadence`` + ``due`` shims) so existing ``except CadenceError``
    catches in the aggregator + tests keep working unchanged. Distinct from
    ``ValueError`` so the aggregator can catch + log + skip one bad routine
    without crashing the sweep.
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
    """Parse an ``anchor`` value — either ``date`` (PyYAML datetime) or
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

    ``n`` is 1-based forward (1 = first occurrence) or ``-1`` for "last"
    (other negatives follow: ``-2`` = second-to-last). Out-of-range positive
    ``n`` (e.g. the 5th Friday in a 4-Friday month) raises
    :class:`CadenceError` — the cadence is unresolvable, surfaced rather than
    silently firing on a different week.
    """
    if n == 0:
        raise CadenceError("nth_weekday n must be non-zero")

    last_day = _last_day_of_month(year, month)

    if n > 0:
        first = date(year, month, 1)
        offset = (weekday - first.weekday()) % 7
        candidate_day = 1 + offset + (n - 1) * 7
        if candidate_day > last_day:
            raise CadenceError(
                f"month {year:04d}-{month:02d} has no {n}-th "
                f"weekday-index-{weekday}"
            )
        return date(year, month, candidate_day)

    # n < 0 — walk backward from the last day.
    last = date(year, month, last_day)
    offset_back = (last.weekday() - weekday) % 7
    candidate_day = last_day - offset_back + (n + 1) * 7
    if candidate_day < 1:
        raise CadenceError(
            f"month {year:04d}-{month:02d} has no {n}-th "
            f"weekday-index-{weekday} (counting from end)"
        )
    return date(year, month, candidate_day)


# ---------------------------------------------------------------------------
# The unified shape + normalization
# ---------------------------------------------------------------------------


_CANONICAL_TYPES: frozenset[str] = frozenset({
    "daily", "weekly", "weekly_soft", "biweekly", "every_n_days",
    "monthly", "monthly_nth_weekday", "every_n_months",
})


@dataclass
class Recurrence:
    """One normalized recurrence shape (the union of both old DSLs).

    Built via :meth:`from_dict` (the reader-first normalizer — accepts both
    old cadence spellings and old due_pattern spellings). Field meanings are
    type-scoped:

      * ``days``    — list of weekday names (``weekly``)
      * ``day``     — day-of-month 1..31 or ``"last"`` (``monthly`` /
                      ``every_n_months``) OR the weekday name for ``biweekly``
      * ``anchor``  — ISO date the cycle counts from (``every_n_days`` /
                      ``every_n_months`` / ``biweekly``)
      * ``n``       — positive int (``every_n_days`` / ``every_n_months``) or
                      the occurrence index (``monthly_nth_weekday``: 1..4/-1)
      * ``weekday`` — single weekday name (``monthly_nth_weekday``)

    Aux-field VALIDATION is deferred to the query functions (``fires_on`` /
    ``next_due_on_or_after``) — exactly like the old modules validated at
    evaluate-time, not parse-time. :meth:`from_dict` only gates structure +
    type-known-ness, so a valid-type-but-bad-aux shape (e.g. ``biweekly``
    with a mismatched anchor weekday) still builds, then RAISES at resolve —
    preserving the historical ``routine.due.malformed`` path.
    """

    type: str
    days: list[str] | None = None
    day: str | int | None = None
    anchor: str | None = None
    n: int | None = None
    weekday: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> "Recurrence | None":
        """Normalize a recurrence dict (cadence OR due_pattern spelling).

        Returns ``None`` when ``raw`` is not a dict or ``type`` is missing /
        not a recognized canonical type (the defensive parse contract shared
        with the old ``DuePattern.from_dict``). Unknown auxiliary fields are
        silently dropped (load-time schema-tolerance contract).

        Normalizations applied (the actual vocabulary unification):
          * ``weekly`` singular ``day: "thu"`` → ``days: ["thu"]``
          * ``weekly`` with ``soft: true`` (or ``type: weekly_soft``) →
            canonical ``weekly_soft``
          * ``monthly`` nested ``nth_weekday: [n, w]`` → canonical
            ``monthly_nth_weekday`` with ``n`` + ``weekday``
        """
        try:
            return _normalize(raw)
        except CadenceError:
            # type unknown / not-a-dict → defensive None (no log; matches the
            # old DuePattern.from_dict + cadence's caller-side handling).
            return None


def _normalize(raw: Any) -> Recurrence:
    """Structural normalizer — RAISES :class:`CadenceError` on a non-dict,
    a missing ``type``, or an unknown type. Aux validation stays at eval.

    This is the raising sibling of :meth:`Recurrence.from_dict`. ``fires_on``
    uses it directly so a malformed routine-level ``cadence:`` dict raises
    (matching the old ``cadence.is_due``); ``from_dict`` wraps it to return
    ``None`` for the defensive item-parse path.
    """
    if isinstance(raw, Recurrence):
        return raw
    if not isinstance(raw, dict):
        raise CadenceError(
            f"recurrence must be a dict, got {type(raw).__name__}"
        )
    rtype = raw.get("type")
    if not isinstance(rtype, str):
        raise CadenceError(
            f"recurrence missing 'type' (got keys: {sorted(raw.keys())})"
        )

    soft = bool(raw.get("soft"))
    if rtype == "weekly_soft" or (rtype == "weekly" and soft):
        return Recurrence(type="weekly_soft")

    if rtype == "weekly":
        # Accept ``days: [...]`` (cadence) OR singular ``day: "thu"`` (due).
        raw_days = raw.get("days")
        if isinstance(raw_days, list):
            days = list(raw_days)
        else:
            day = raw.get("day")
            days = [day] if isinstance(day, str) else None
        return Recurrence(type="weekly", days=days)

    if rtype == "monthly":
        if "nth_weekday" in raw:
            nw = raw.get("nth_weekday")
            if isinstance(nw, (list, tuple)) and len(nw) == 2:
                n_raw, weekday_raw = nw
                return Recurrence(
                    type="monthly_nth_weekday",
                    n=n_raw if isinstance(n_raw, int) else None,
                    weekday=(
                        str(weekday_raw) if weekday_raw is not None else None
                    ),
                )
            # Malformed nth_weekday → carry as monthly_nth_weekday with empty
            # aux so the eval-time validator raises (CadenceError type
            # preserved; message differs from the old inline check, which is
            # fine — the aggregator catches on type, not text).
            return Recurrence(type="monthly_nth_weekday")
        return Recurrence(type="monthly", day=raw.get("day"))

    if rtype == "monthly_nth_weekday":
        return Recurrence(
            type="monthly_nth_weekday",
            n=raw.get("n") if isinstance(raw.get("n"), int) else None,
            weekday=(
                str(raw.get("weekday"))
                if isinstance(raw.get("weekday"), str) else None
            ),
        )

    if rtype == "biweekly":
        day = raw.get("day")
        return Recurrence(
            type="biweekly",
            day=day if isinstance(day, (str, int)) else None,
            anchor=str(raw["anchor"]) if raw.get("anchor") is not None else None,
        )

    if rtype == "every_n_days":
        return Recurrence(
            type="every_n_days",
            n=raw.get("n") if isinstance(raw.get("n"), int) else None,
            anchor=str(raw["anchor"]) if raw.get("anchor") is not None else None,
        )

    if rtype == "every_n_months":
        return Recurrence(
            type="every_n_months",
            n=raw.get("n") if isinstance(raw.get("n"), int) else None,
            day=raw.get("day"),
            anchor=str(raw["anchor"]) if raw.get("anchor") is not None else None,
        )

    if rtype == "daily":
        return Recurrence(type="daily")

    raise CadenceError(
        f"unknown recurrence type: {rtype!r} (expected one of "
        f"{sorted(_CANONICAL_TYPES)})"
    )


# ---------------------------------------------------------------------------
# Query 1 — fires_on (replaces cadence.is_due; cadence error policy = raise)
# ---------------------------------------------------------------------------


def fires_on(cadence: Any, today: date) -> bool:
    """Return True iff the recurrence fires on ``today`` (cadence semantics).

    Accepts a raw cadence dict (the routine-level ``cadence:`` shape) or a
    :class:`Recurrence`. RAISES :class:`CadenceError` on a malformed/unknown
    shape — the routine aggregator catches it and skips the one bad routine.

    Handles the SIX cadence shapes; the due-only shapes (biweekly,
    weekly_soft) raise "unsupported" exactly as the old ``cadence.is_due``
    raised "unknown cadence type" for them (routine ``cadence:`` dicts never
    carry those — never hit in production, pinned by the differential).
    """
    rec = _normalize(cadence)
    rtype = rec.type

    if rtype == "daily":
        return True

    if rtype == "weekly":
        if not rec.days:
            raise CadenceError(
                "weekly cadence requires non-empty 'days' list "
                "(e.g. ['Mon', 'Wed', 'Fri'])"
            )
        indices = {_weekday_index(d) for d in rec.days}
        return today.weekday() in indices

    if rtype == "every_n_days":
        n = rec.n
        if not isinstance(n, int) or n < 1:
            raise CadenceError(
                f"every_n_days requires positive integer 'n', got {n!r}"
            )
        anchor = _parse_anchor(rec.anchor, "every_n_days anchor")
        if today < anchor:
            return False
        return (today - anchor).days % n == 0

    if rtype == "monthly":
        day = rec.day
        if day == "last":
            return today.day == _last_day_of_month(today.year, today.month)
        if isinstance(day, int) and 1 <= day <= 31:
            last_day = _last_day_of_month(today.year, today.month)
            return today.day == min(day, last_day)
        raise CadenceError(
            f"monthly requires 'day' (1..31 or 'last'), got day={day!r}"
        )

    if rtype == "monthly_nth_weekday":
        if not isinstance(rec.n, int):
            raise CadenceError(
                f"monthly_nth_weekday requires int 'n', got {rec.n!r}"
            )
        weekday = _weekday_index(rec.weekday)
        target = _nth_weekday_of_month(today.year, today.month, rec.n, weekday)
        return today == target

    if rtype == "every_n_months":
        n = rec.n
        if not isinstance(n, int) or n < 1:
            raise CadenceError(
                f"every_n_months requires positive integer 'n', got {n!r}"
            )
        anchor = _parse_anchor(rec.anchor, "every_n_months anchor")
        if today < anchor:
            return False
        day = rec.day if rec.day is not None else anchor.day
        months_between = (today.year - anchor.year) * 12 + (
            today.month - anchor.month
        )
        if months_between < 0:
            return False
        if months_between % n != 0:
            return False
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

    # biweekly / weekly_soft / anything else — not a cadence shape.
    raise CadenceError(
        f"unsupported cadence type: {rtype!r} "
        f"(expected daily/weekly/every_n_days/monthly/"
        f"monthly_nth_weekday/every_n_months)"
    )


# ---------------------------------------------------------------------------
# Query 2 — next_due_on_or_after (replaces due.resolve_due_date core)
# ---------------------------------------------------------------------------


def next_due_on_or_after(due: Any, today: date) -> date | None:
    """Return the next upcoming due date >= ``today`` (due semantics).

    Accepts a :class:`Recurrence`, a due_pattern dict, or ``None`` (→ None).
    RAISES :class:`CadenceError` on a valid-type-but-malformed-aux shape; the
    ``due`` shim catches it → ``None`` + a ``routine.due.malformed`` log,
    preserving the historical error policy.

    Handles the SIX due shapes. ``daily`` / ``every_n_months`` have no
    item-level next-due semantics (Q5) and RAISE — unreachable via the item
    parse (``DUE_PATTERN_TYPES`` excludes them).
    """
    if due is None:
        return None
    rec = due if isinstance(due, Recurrence) else Recurrence.from_dict(due)
    if rec is None:
        return None
    rtype = rec.type

    if rtype == "weekly_soft":
        # End of the current ISO week (Sunday — Python weekday() == 6).
        days_until_sunday = (6 - today.weekday()) % 7
        return today + timedelta(days=days_until_sunday)

    if rtype == "weekly":
        if not rec.days:
            raise CadenceError("weekly due_pattern requires 'day'/'days'")
        # Nearest upcoming occurrence of ANY configured weekday (a single-
        # element list reduces to the old singular-day behaviour exactly).
        candidates = []
        for d in rec.days:
            target_weekday = _weekday_index(d)
            days_ahead = (target_weekday - today.weekday()) % 7
            candidates.append(today + timedelta(days=days_ahead))
        return min(candidates)

    if rtype == "biweekly":
        # Alias of every_n_days n=14, BUT keep the anchor-weekday validation:
        # a mismatch is unresolvable and must RAISE (never silently fire a
        # different cadence) — operator decision Q2.
        if rec.day is None or rec.anchor is None:
            raise CadenceError("biweekly due_pattern requires 'day' and 'anchor'")
        if not isinstance(rec.day, str):
            raise CadenceError(
                f"biweekly 'day' must be a weekday name string, got "
                f"{type(rec.day).__name__}"
            )
        target_weekday = _weekday_index(rec.day)
        anchor = _parse_anchor(rec.anchor, "biweekly anchor")
        if anchor.weekday() != target_weekday:
            raise CadenceError(
                f"biweekly anchor weekday ({anchor.weekday()}) does not match "
                f"configured day ({target_weekday}); operator YAML is "
                f"inconsistent."
            )
        return _next_every_n_days(anchor, 14, today)

    if rtype == "monthly":
        return _next_monthly(rec.day, today)

    if rtype == "every_n_days":
        if rec.n is None or rec.n < 1:
            raise CadenceError(
                f"every_n_days requires positive int 'n', got {rec.n!r}"
            )
        if rec.anchor is None:
            raise CadenceError("every_n_days requires 'anchor'")
        anchor = _parse_anchor(rec.anchor, "every_n_days anchor")
        return _next_every_n_days(anchor, rec.n, today)

    if rtype == "monthly_nth_weekday":
        if rec.n is None or rec.weekday is None:
            raise CadenceError("monthly_nth_weekday requires 'n' and 'weekday'")
        weekday_idx = _weekday_index(rec.weekday)
        try:
            this_month_match = _nth_weekday_of_month(
                today.year, today.month, rec.n, weekday_idx,
            )
        except CadenceError:
            this_month_match = None
        if this_month_match is not None and this_month_match >= today:
            return this_month_match
        cursor = today + relativedelta(months=1)
        for _ in range(12):
            try:
                return _nth_weekday_of_month(
                    cursor.year, cursor.month, rec.n, weekday_idx,
                )
            except CadenceError:
                cursor = cursor + relativedelta(months=1)
        raise CadenceError(
            f"could not resolve monthly_nth_weekday(n={rec.n}, "
            f"weekday={rec.weekday}) within 12 months of {today}"
        )

    # daily / every_n_months — no item-level next-due semantics (Q5).
    raise CadenceError(
        f"recurrence type {rtype!r} has no item-level next-due resolver "
        f"(daily/every_n_months are routine-cadence-only)"
    )


def _safe_next_due(rec: "Recurrence", today: date) -> date | None:
    """Catching wrapper over :func:`next_due_on_or_after` → ``None`` on a
    malformed shape (no raise).

    The behind/due substrate helpers below historically resolved via the
    catching ``resolve_due_date`` (malformed → ``None``), NOT the raising
    core. This preserves that exact value behavior: a malformed pattern makes
    a cycle helper return its not-resolvable answer (False / None) rather than
    propagating. Logging of the malformed shape stays at the ``due`` shim's
    ``resolve_due_date`` (the operator-facing entry point), keeping the
    tier-compute path's no-logs invariant intact.
    """
    try:
        return next_due_on_or_after(rec, today)
    except CadenceError:
        return None


def _next_every_n_days(anchor: date, n: int, today: date) -> date:
    """Smallest ``anchor + n*k`` that is >= ``today`` (k >= 0)."""
    if today <= anchor:
        return anchor
    delta_days = (today - anchor).days
    k = (delta_days + n - 1) // n  # ceiling division
    return anchor + timedelta(days=n * k)


def _next_monthly(day_raw: Any, today: date) -> date:
    """Next day-of-month occurrence >= today (``day`` 1..31 or 'last';
    ``day`` > month length clamps to the month's last day)."""
    if day_raw is None:
        raise CadenceError("monthly due_pattern requires 'day'")

    def _day_in_month(year: int, month: int) -> int:
        last = _last_day_of_month(year, month)
        if isinstance(day_raw, str) and day_raw.strip().lower() == "last":
            return last
        try:
            d = int(day_raw)
        except (TypeError, ValueError) as exc:
            raise CadenceError(
                f"monthly 'day' must be int 1-31 or 'last', got {day_raw!r}"
            ) from exc
        if d < 1 or d > 31:
            raise CadenceError(f"monthly 'day' out of range 1-31: {d}")
        return min(d, last)

    this_month_day = _day_in_month(today.year, today.month)
    candidate = date(today.year, today.month, this_month_day)
    if candidate >= today:
        return candidate
    next_month_date = today + relativedelta(months=1)
    next_day = _day_in_month(next_month_date.year, next_month_date.month)
    return date(next_month_date.year, next_month_date.month, next_day)


# ---------------------------------------------------------------------------
# Behind/due substrate — completion-window helpers (relocated verbatim)
# ---------------------------------------------------------------------------


def is_done_in_current_cycle(
    recurrence: Any,
    completion_dates: list[date],
    today: date,
) -> bool:
    """True iff any completion lands inside the current cycle window.

    Per-shape window (unchanged from the old ``due`` module):
      * ``weekly`` / ``weekly_soft``: ISO week containing today
      * ``biweekly``: 14-day window ending at due
      * ``monthly`` / ``monthly_nth_weekday``: calendar month of due
      * ``every_n_days``: N-day window ending at due
    """
    if recurrence is None:
        return False
    rec = recurrence if isinstance(recurrence, Recurrence) else Recurrence.from_dict(recurrence)
    if rec is None:
        return False
    if not completion_dates:
        return False

    rtype = rec.type

    if rtype in ("weekly", "weekly_soft"):
        return _has_completion_in_iso_week(completion_dates, today)

    due = _safe_next_due(rec, today)
    if due is None:
        return False

    if rtype == "biweekly":
        return _has_completion_in_window(
            completion_dates, due - timedelta(days=13), due,
        )

    if rtype in ("monthly", "monthly_nth_weekday"):
        return any(
            c.year == due.year and c.month == due.month
            for c in completion_dates
        )

    if rtype == "every_n_days":
        n = rec.n or 1
        return _has_completion_in_window(
            completion_dates, due - timedelta(days=n - 1), due,
        )

    return False


def _has_completion_in_window(
    completion_dates: list[date], window_start: date, window_end: date,
) -> bool:
    """Inclusive window check — completions on either endpoint count."""
    return any(window_start <= c <= window_end for c in completion_dates)


def _has_completion_in_iso_week(
    completion_dates: list[date], today: date,
) -> bool:
    """True if any completion lands in today's ISO week (Mon-Sun)."""
    week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)
    return _has_completion_in_window(completion_dates, week_start, week_end)


def _previous_cycle_due_date(
    rec: Recurrence, current_due: date,
) -> date | None:
    """Compute one cycle back from ``current_due`` (None for malformed).

    Per-shape arithmetic unchanged from the old ``due`` module.
    """
    rtype = rec.type

    if rtype in ("weekly", "weekly_soft"):
        return current_due - timedelta(days=7)

    if rtype == "biweekly":
        return current_due - timedelta(days=14)

    if rtype == "monthly":
        prev_month = current_due - relativedelta(months=1)
        day_raw = rec.day
        if isinstance(day_raw, str) and day_raw.strip().lower() == "last":
            return date(
                prev_month.year, prev_month.month,
                _last_day_of_month(prev_month.year, prev_month.month),
            )
        try:
            d = int(day_raw) if day_raw is not None else current_due.day
        except (TypeError, ValueError):
            return None
        last_in_prev = _last_day_of_month(prev_month.year, prev_month.month)
        return date(prev_month.year, prev_month.month, min(d, last_in_prev))

    if rtype == "every_n_days":
        n = rec.n or 1
        if n < 1:
            return None
        return current_due - timedelta(days=n)

    if rtype == "monthly_nth_weekday":
        if rec.n is None or rec.weekday is None:
            return None
        weekday_idx = _weekday_index(rec.weekday)
        prev_month = current_due - relativedelta(months=1)
        try:
            return _nth_weekday_of_month(
                prev_month.year, prev_month.month, rec.n, weekday_idx,
            )
        except CadenceError:
            cursor = prev_month - relativedelta(months=1)
            for _ in range(11):
                try:
                    return _nth_weekday_of_month(
                        cursor.year, cursor.month, rec.n, weekday_idx,
                    )
                except CadenceError:
                    cursor = cursor - relativedelta(months=1)
            return None

    return None


def _parse_completion_entries(entries: Any) -> list[date]:
    """Coerce completion-log entries (ISO strings / date / datetime) to
    ``date`` objects, silently dropping garbage (operator hand-edit
    defensive — matches the old ``due`` parse + tier compute's parser)."""
    out: list[date] = []
    if not isinstance(entries, list):
        return out
    for v in entries:
        if isinstance(v, datetime):
            out.append(v.date())
        elif isinstance(v, date):
            out.append(v)
        elif isinstance(v, str):
            try:
                out.append(date.fromisoformat(v.strip()))
            except (TypeError, ValueError):
                continue
    return out


def completion_satisfies_current_cycle(
    item_text: str,
    completion_log: dict | None,
    recurrence: Any,
    today: date,
) -> bool:
    """True if a completion covers the current/upcoming cycle.

    Nearest-cycle ±half-cycle heuristic (unchanged): a completion C "covers"
    the cycle ending at ``current_due`` when
    ``abs((current_due - C).days) <= cycle_length // 2``. Half-cycle works out
    to ±3d (weekly), ±7d (biweekly), ±14-15d (monthly), ±n//2 (every_n_days),
    ±cycle//2 (monthly_nth_weekday). Returns False for malformed pattern /
    empty-or-missing log entry / no covering completion.
    """
    if recurrence is None:
        return False
    rec = recurrence if isinstance(recurrence, Recurrence) else Recurrence.from_dict(recurrence)
    if rec is None:
        return False
    if not isinstance(completion_log, dict) or not completion_log:
        return False
    entries = completion_log.get(item_text)
    if not isinstance(entries, list) or not entries:
        return False

    current_due = _safe_next_due(rec, today)
    if current_due is None:
        return False
    prev_due = _previous_cycle_due_date(rec, current_due)
    if prev_due is None:
        return False

    cycle_length = (current_due - prev_due).days
    if cycle_length <= 0:
        return False
    half_cycle = max(1, cycle_length // 2)

    completion_dates = _parse_completion_entries(entries)
    if not completion_dates:
        return False

    return any(
        abs((current_due - c).days) <= half_cycle for c in completion_dates
    )


def _completion_satisfies_prev_cycle(
    item_text: str,
    completion_log: dict | None,
    rec: Recurrence,
    prev_due: date,
    cycle_length: int,
) -> bool:
    """Did a completion cover the PREV cycle (ending at ``prev_due``)?

    Same ±half-cycle logic as :func:`completion_satisfies_current_cycle`, with
    ``prev_due`` as the reference. Used by :func:`overdue_effective_due`.
    """
    if not isinstance(completion_log, dict) or not completion_log:
        return False
    entries = completion_log.get(item_text)
    if not isinstance(entries, list) or not entries:
        return False
    half_cycle = max(1, cycle_length // 2)
    completion_dates = _parse_completion_entries(entries)
    return any(
        abs((prev_due - c).days) <= half_cycle for c in completion_dates
    )


def overdue_effective_due(
    recurrence: Any,
    completion_log: dict | None,
    item_text: str,
    today: date,
) -> date | None:
    """Effective due that admits overdue retention.

    Returns ``prev_due`` when the previous cycle lapsed RECENTLY (within
    half a cycle) without a covering completion → the caller's
    ``days_to_due = (prev_due - today).days`` is negative and the T1 window
    admits it as overdue retention. Otherwise returns ``current_due`` (the
    next-upcoming due). ``None`` for malformed / uninterpretable input.
    Logic unchanged from the old ``due`` module (the recency gate prevents a
    full-cycle-back retention on the due date itself).
    """
    if recurrence is None:
        return None
    rec = recurrence if isinstance(recurrence, Recurrence) else Recurrence.from_dict(recurrence)
    if rec is None:
        return None
    current_due = _safe_next_due(rec, today)
    if current_due is None:
        return None

    prev_due = _previous_cycle_due_date(rec, current_due)
    if prev_due is None:
        return current_due
    if prev_due >= today:
        return current_due

    cycle_length = (current_due - prev_due).days
    if cycle_length <= 0:
        return current_due

    days_since_prev_due = (today - prev_due).days
    half_cycle = max(1, cycle_length // 2)
    if days_since_prev_due > half_cycle:
        return current_due

    if _completion_satisfies_prev_cycle(
        item_text, completion_log, rec, prev_due, cycle_length,
    ):
        return current_due

    return prev_due


__all__ = [
    "CadenceError",
    "Recurrence",
    "completion_satisfies_current_cycle",
    "fires_on",
    "is_done_in_current_cycle",
    "next_due_on_or_after",
    "overdue_effective_due",
]
