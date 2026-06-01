"""Due-date resolution for routine items with recurring deadlines.

Operates on :class:`alfred.routine.config.DuePattern` instances and
returns the NEXT upcoming due date relative to a reference ``today``.
Companion to :mod:`alfred.routine.cadence` (which answers "is this
ROUTINE firing today?"); this module answers "what's the next deadline
for THIS ITEM within the routine?"

Two public functions:

  * :func:`resolve_due_date` — given a pattern + today, return the
    next due date (date object) or ``None`` for malformed patterns.
  * :func:`is_done_in_current_cycle` — given a pattern + completion
    log + today, return ``True`` if any completion lands inside the
    current cycle window (used by tier compute to skip items the
    operator has already completed this cycle).

Pattern dispatch (six types, mirroring cadence dispatcher's vocabulary):

  * ``weekly`` — fires once per ISO week on the configured weekday.
    Next due = the next occurrence of ``day`` on-or-after today (if
    today IS the weekday, today is returned — the operator hasn't
    missed the deadline yet).
  * ``biweekly`` — fires every 14 days. The ``anchor`` ISO date pins
    the cycle (anchor + 14k for non-negative integer k yields the
    valid due dates). Next due = the smallest anchor + 14k that is
    >= today.
  * ``monthly`` — fires on day-of-month ``day`` (1-31 or ``"last"``).
    Next due = ``day`` this month if not yet passed, else ``day``
    next month. ``day > current_month_length`` clamps to last day.
  * ``every_n_days`` — fires every ``n`` days starting from
    ``anchor``. Next due = anchor + n * ceil((today - anchor) / n).
  * ``monthly_nth_weekday`` — fires on the n-th specified weekday
    each month (e.g. 2nd Tuesday). Next due = this month's
    occurrence if not yet passed, else next month's.
  * ``weekly_soft`` — soft weekly cadence; due = end of current ISO
    week (Sunday). No anchor required.

DST/leap-year handling delegates to :mod:`alfred.routine.cadence`'s
helpers (``_weekday_index``, ``_parse_anchor``,
``_last_day_of_month``, ``_nth_weekday_of_month``) — single source
of truth across both modules.

Cycle-window logic for :func:`is_done_in_current_cycle`:

  * ``weekly`` / ``biweekly`` hard day: window = [due - 7d, due]
    (resp. 14d). Completion anywhere in the window counts.
  * ``monthly`` / ``monthly_nth_weekday``: window = the calendar
    month containing ``due``. Completion in that month counts.
  * ``every_n_days``: window = [due - n + 1, due]. The cycle is the
    ``n``-day period leading up to (and including) the due date.
  * ``weekly_soft``: window = current ISO week (Mon-Sun containing
    today).

Per ``feedback_intentionally_left_blank.md`` the resolver functions
emit a structured warn log on malformed input rather than raising —
the caller (tier compute) treats a None return as "skip this item"
and the operator sees the malformed-pattern signal in the log.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import structlog
from dateutil.relativedelta import relativedelta

from .cadence import (
    _last_day_of_month,
    _nth_weekday_of_month,
    _parse_anchor,
    _weekday_index,
    CadenceError,
)
from .config import DuePattern

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# resolve_due_date
# ---------------------------------------------------------------------------


def resolve_due_date(due_pattern: DuePattern, today: date) -> date | None:
    """Return the next upcoming due date for the pattern.

    Returns ``None`` for malformed patterns (missing required
    auxiliary fields, unknown type via DuePattern.from_dict). Each
    branch emits a ``routine.due.malformed`` structured log on the
    None path so operators see which item dropped + why.

    Per the module docstring's pattern dispatch table.
    """
    if due_pattern is None:
        return None

    pattern_type = due_pattern.type

    try:
        if pattern_type == "weekly" or pattern_type == "weekly_soft":
            return _resolve_weekly(due_pattern, today)
        if pattern_type == "biweekly":
            return _resolve_biweekly(due_pattern, today)
        if pattern_type == "monthly":
            return _resolve_monthly(due_pattern, today)
        if pattern_type == "every_n_days":
            return _resolve_every_n_days(due_pattern, today)
        if pattern_type == "monthly_nth_weekday":
            return _resolve_monthly_nth_weekday(due_pattern, today)
    except CadenceError as exc:
        log.warning(
            "routine.due.malformed",
            type=pattern_type,
            error=str(exc),
            detail=(
                "malformed due_pattern — item will not auto-surface "
                "in tier; check operator YAML for missing or invalid "
                "auxiliary fields."
            ),
        )
        return None
    except (TypeError, ValueError) as exc:
        log.warning(
            "routine.due.malformed",
            type=pattern_type,
            error=str(exc),
        )
        return None

    log.warning(
        "routine.due.unknown_type",
        type=pattern_type,
        detail=(
            "DuePattern.type fell through resolver dispatch — "
            "DUE_PATTERN_TYPES filter and resolver branches drifted."
        ),
    )
    return None


def _resolve_weekly(due_pattern: DuePattern, today: date) -> date | None:
    """Resolve weekly / weekly_soft patterns.

    For ``weekly``: ``day`` MUST be a weekday name. Next due = today
    if today is that weekday, else the next occurrence (1-6 days
    out).

    For ``weekly_soft``: no ``day`` required. The soft cadence has
    no hard weekday anchor — due = Sunday of the current ISO week
    (the end-of-week sentinel). If today is Sunday, today is the
    due date.
    """
    if due_pattern.type == "weekly_soft" or due_pattern.soft:
        # End of current ISO week (Sunday — Python's weekday() = 6).
        days_until_sunday = (6 - today.weekday()) % 7
        return today + timedelta(days=days_until_sunday)

    if due_pattern.day is None:
        raise CadenceError("weekly due_pattern requires 'day'")
    if not isinstance(due_pattern.day, str):
        raise CadenceError(
            f"weekly 'day' must be weekday name string, got "
            f"{type(due_pattern.day).__name__}"
        )
    target_weekday = _weekday_index(due_pattern.day)
    days_ahead = (target_weekday - today.weekday()) % 7
    return today + timedelta(days=days_ahead)


def _resolve_biweekly(due_pattern: DuePattern, today: date) -> date | None:
    """Resolve biweekly patterns.

    ``day`` = weekday name, ``anchor`` = ISO date of a reference
    occurrence. The cycle is 14 days; valid due dates are
    ``anchor + 14k`` for k >= 0 where the weekday matches.

    The anchor date's weekday must match the configured ``day``
    (validation) — otherwise the operator's intent is unresolvable.
    Next due = smallest ``anchor + 14k >= today``.
    """
    if due_pattern.day is None or due_pattern.anchor is None:
        raise CadenceError(
            "biweekly due_pattern requires 'day' and 'anchor'"
        )
    if not isinstance(due_pattern.day, str):
        raise CadenceError(
            f"biweekly 'day' must be weekday name string, got "
            f"{type(due_pattern.day).__name__}"
        )
    target_weekday = _weekday_index(due_pattern.day)
    anchor = _parse_anchor(due_pattern.anchor, "biweekly anchor")
    if anchor.weekday() != target_weekday:
        raise CadenceError(
            f"biweekly anchor weekday ({anchor.weekday()}) does not "
            f"match configured day ({target_weekday}); operator YAML "
            f"is inconsistent."
        )
    # Compute next anchor + 14k >= today.
    if today <= anchor:
        return anchor
    delta_days = (today - anchor).days
    # Ceiling division: smallest k such that anchor + 14k >= today.
    k = (delta_days + 13) // 14
    return anchor + timedelta(days=14 * k)


def _resolve_monthly(due_pattern: DuePattern, today: date) -> date | None:
    """Resolve monthly patterns by day-of-month.

    ``day`` is 1-31 or ``"last"``. Next due = ``day`` this month if
    not yet passed (or = today), else ``day`` next month.

    ``day`` > current month's length clamps to month's last day
    (matches operator intent: "day 31" in Feb = last day of Feb).
    """
    day_raw = due_pattern.day
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
                f"monthly 'day' must be int 1-31 or 'last', got "
                f"{day_raw!r}"
            ) from exc
        if d < 1 or d > 31:
            raise CadenceError(
                f"monthly 'day' out of range 1-31: {d}"
            )
        return min(d, last)

    this_month_day = _day_in_month(today.year, today.month)
    candidate = date(today.year, today.month, this_month_day)
    if candidate >= today:
        return candidate
    # Otherwise next month.
    next_month_date = today + relativedelta(months=1)
    next_day = _day_in_month(next_month_date.year, next_month_date.month)
    return date(next_month_date.year, next_month_date.month, next_day)


def _resolve_every_n_days(due_pattern: DuePattern, today: date) -> date | None:
    """Resolve every-N-days patterns.

    ``n`` = positive int, ``anchor`` = ISO date the cycle counts
    from. Valid due dates = ``anchor + n*k`` for k >= 0. Next due =
    smallest such date >= today.
    """
    if due_pattern.n is None or due_pattern.n < 1:
        raise CadenceError(
            f"every_n_days requires positive int 'n', got "
            f"{due_pattern.n!r}"
        )
    if due_pattern.anchor is None:
        raise CadenceError("every_n_days requires 'anchor'")
    anchor = _parse_anchor(due_pattern.anchor, "every_n_days anchor")
    n = due_pattern.n
    if today <= anchor:
        return anchor
    delta_days = (today - anchor).days
    k = (delta_days + n - 1) // n  # ceiling division
    return anchor + timedelta(days=n * k)


def _resolve_monthly_nth_weekday(
    due_pattern: DuePattern, today: date,
) -> date | None:
    """Resolve monthly-nth-weekday patterns.

    ``n`` = 1, 2, 3, 4 or -1 (last). ``weekday`` = weekday name.
    Next due = this month's nth weekday occurrence if not yet
    passed, else next month's.
    """
    if due_pattern.n is None or due_pattern.weekday is None:
        raise CadenceError(
            "monthly_nth_weekday requires 'n' and 'weekday'"
        )
    weekday_idx = _weekday_index(due_pattern.weekday)
    try:
        this_month_match = _nth_weekday_of_month(
            today.year, today.month, due_pattern.n, weekday_idx,
        )
    except CadenceError:
        # This month doesn't have an nth weekday (e.g. 5th Friday in
        # a month with only 4 Fridays). Try next month.
        this_month_match = None
    if this_month_match is not None and this_month_match >= today:
        return this_month_match
    # Try next month — and any month after that.
    cursor = today + relativedelta(months=1)
    for _ in range(12):  # safety bound; should resolve within 1-2 iterations
        try:
            return _nth_weekday_of_month(
                cursor.year, cursor.month, due_pattern.n, weekday_idx,
            )
        except CadenceError:
            cursor = cursor + relativedelta(months=1)
    raise CadenceError(
        f"could not resolve monthly_nth_weekday(n={due_pattern.n}, "
        f"weekday={due_pattern.weekday}) within 12 months of {today}"
    )


# ---------------------------------------------------------------------------
# is_done_in_current_cycle
# ---------------------------------------------------------------------------


def is_done_in_current_cycle(
    due_pattern: DuePattern,
    completion_dates: list[date],
    today: date,
) -> bool:
    """Return True iff any completion lands inside the current cycle.

    The cycle window is per-pattern (see module docstring):

      * ``weekly`` / ``weekly_soft``: ISO week containing today
      * ``biweekly``: 14-day window ending at due
      * ``monthly`` / ``monthly_nth_weekday``: calendar month
        containing due
      * ``every_n_days``: N-day window ending at due

    Used by tier compute to skip items the operator has already
    completed in the current cycle (e.g. clinic rent paid on the
    27th doesn't surface again until the next cycle's window
    begins).

    Returns ``False`` if the pattern can't be resolved (delegates
    to :func:`resolve_due_date`'s log signal) — caller treats the
    item as "not done; check surface windows."
    """
    if due_pattern is None:
        return False
    if not completion_dates:
        return False

    pattern_type = due_pattern.type

    # weekly_soft + weekly use the SAME window semantics (current
    # ISO week containing today). Pattern type differentiates only
    # the due date resolution — completion ANY day this week counts.
    if pattern_type in ("weekly", "weekly_soft"):
        return _has_completion_in_iso_week(completion_dates, today)

    # All other patterns key the window off the resolved due date.
    due = resolve_due_date(due_pattern, today)
    if due is None:
        return False

    if pattern_type == "biweekly":
        window_start = due - timedelta(days=13)
        return _has_completion_in_window(
            completion_dates, window_start, due,
        )

    if pattern_type == "monthly" or pattern_type == "monthly_nth_weekday":
        return any(
            c.year == due.year and c.month == due.month
            for c in completion_dates
        )

    if pattern_type == "every_n_days":
        n = due_pattern.n or 1
        window_start = due - timedelta(days=n - 1)
        return _has_completion_in_window(
            completion_dates, window_start, due,
        )

    # Unknown type — defer to no-cycle interpretation. log signal
    # already fired via resolve_due_date if it dispatched.
    return False


def _has_completion_in_window(
    completion_dates: list[date],
    window_start: date,
    window_end: date,
) -> bool:
    """Inclusive window check — completions on either endpoint count."""
    return any(window_start <= c <= window_end for c in completion_dates)


def _has_completion_in_iso_week(
    completion_dates: list[date], today: date,
) -> bool:
    """Return True if any completion lands in today's ISO week
    (Monday-Sunday containing today)."""
    weekday = today.weekday()  # Mon=0, Sun=6
    week_start = today - timedelta(days=weekday)
    week_end = week_start + timedelta(days=6)
    return _has_completion_in_window(
        completion_dates, week_start, week_end,
    )


# ---------------------------------------------------------------------------
# Phase 2C C1 (2026-06-01) — completion-aware auto-surface helpers
# ---------------------------------------------------------------------------
#
# Operator bug 2026-06-01: Pay Clinic Rental (monthly, day=1) marked
# complete May 29 via ``routine_done``, but still auto-surfaced as T1
# on June 1's brief. The aggregator's ``_decide_tier_handoff`` predicate
# and tier/compute.py's ``_compute_auto_routine`` consult window math
# but NOT completion_log → a completed item keeps surfacing.
#
# Mirror gap: the missed-cycle retention case (today=June 17, monthly
# day=15, no completion). resolve_due_date rolls forward to July 15;
# days_to_due=28 falls out of T1 window; item silently drops instead
# of staying in T1 with an overdue annotation.
#
# Both halves are fixed by completion-aware predicates that consult
# the completion_log:
#
#   * :func:`completion_satisfies_current_cycle` — nearest-cycle
#     ±half-cycle heuristic. A completion within half a cycle-length
#     of the upcoming due date "covers" that cycle.
#   * :func:`effective_due_for_predicate` — returns prev_due when
#     prev cycle is unsatisfied AND has passed, else current_due.
#     Lets the window math accept negative days_to_due as
#     "overdue, retain in T1."
#
# Distinct from :func:`is_done_in_current_cycle` (the calendar-window
# helper used by the render-layer cycle-aware annotation). Calendar-
# window is correct for "did the operator complete within the current
# calendar period?" The nearest-cycle helper is correct for "does the
# completion COVER the upcoming/current due date?" — different
# question; the calendar-window helper misses cross-boundary cases
# like Pay Clinic (completion May 29 doesn't fall in "calendar month
# of June" but DOES cover the June 1 cycle).


def _previous_cycle_due_date(
    due_pattern: DuePattern,
    current_due: date,
) -> date | None:
    """Compute one cycle back from ``current_due``.

    Returns ``None`` for malformed patterns (mirrors
    :func:`resolve_due_date`'s None semantic). Used by the
    completion-aware predicates to define the cycle window for the
    nearest-cycle heuristic + the overdue-retention check.

    Per-pattern arithmetic:

      * ``weekly`` / ``weekly_soft``: subtract 7 days.
      * ``biweekly``: subtract 14 days.
      * ``monthly``: previous calendar month, same day-of-month
        (clamped to last day if needed — mirrors
        :func:`_resolve_monthly`'s clamp semantics).
      * ``every_n_days``: subtract ``n`` days.
      * ``monthly_nth_weekday``: previous month's nth weekday
        occurrence (delegates to ``_nth_weekday_of_month`` for
        DST/leap-safe lookup).
    """
    pattern_type = due_pattern.type

    if pattern_type in ("weekly", "weekly_soft"):
        return current_due - timedelta(days=7)

    if pattern_type == "biweekly":
        return current_due - timedelta(days=14)

    if pattern_type == "monthly":
        # Previous calendar month, same day (clamped).
        prev_month = current_due - relativedelta(months=1)
        day_raw = due_pattern.day
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
        return date(
            prev_month.year, prev_month.month, min(d, last_in_prev),
        )

    if pattern_type == "every_n_days":
        n = due_pattern.n or 1
        if n < 1:
            return None
        return current_due - timedelta(days=n)

    if pattern_type == "monthly_nth_weekday":
        if due_pattern.n is None or due_pattern.weekday is None:
            return None
        weekday_idx = _weekday_index(due_pattern.weekday)
        prev_month = current_due - relativedelta(months=1)
        try:
            return _nth_weekday_of_month(
                prev_month.year, prev_month.month,
                due_pattern.n, weekday_idx,
            )
        except CadenceError:
            # Previous month doesn't have an nth occurrence (e.g. 5th
            # Friday in a month with 4 Fridays). Cascade further back
            # — bounded loop, like _resolve_monthly_nth_weekday does.
            cursor = prev_month - relativedelta(months=1)
            for _ in range(11):
                try:
                    return _nth_weekday_of_month(
                        cursor.year, cursor.month,
                        due_pattern.n, weekday_idx,
                    )
                except CadenceError:
                    cursor = cursor - relativedelta(months=1)
            return None

    return None


def completion_satisfies_current_cycle(
    item_text: str,
    completion_log: dict | None,
    due_pattern: DuePattern | None,
    today: date,
) -> bool:
    """Return True if a completion covers the current/upcoming cycle.

    Nearest-cycle ±half-cycle heuristic: a completion C "covers" the
    cycle ending at ``current_due`` when
    ``abs((current_due - C).days) <= cycle_length / 2``.

    For each pattern type, half_cycle works out to:
      * ``weekly`` / ``weekly_soft``: ±3 days (cycle_length=7, //2=3)
      * ``biweekly``: ±7 days (14 // 2)
      * ``monthly``: ±15 days (cycle_length is the prev-cycle span;
        ~28-31 days; //2 = ~14-15)
      * ``every_n_days``: ±n//2
      * ``monthly_nth_weekday``: ±cycle_length//2 (cycle_length
        computed dynamically from current_due - prev_due)

    Returns False when:
      * ``due_pattern`` is None or malformed (resolver fails)
      * ``completion_log`` is None / empty / has no entry for item_text
      * No completion lands within ±half_cycle of current_due

    Operator bug surfacing: Pay Clinic monthly day=1, completion May
    29, today June 1. current_due = June 1; prev_due = May 1;
    cycle_length = 31; half_cycle = 15. |June 1 - May 29| = 3 ≤ 15
    → True (suppress). Existing :func:`is_done_in_current_cycle`
    uses calendar-month window (June) which misses May 29 → False
    (incorrectly surfaces).

    Use by :func:`alfred.routine.aggregator._decide_tier_handoff`
    and :func:`alfred.tier.compute._compute_auto_routine` — both
    layers must invoke this with the SAME args for the mirror
    contract (``feedback_two_layer_window_math_mirror``). Cycle
    semantic is the load-bearing operator decision; window-math
    drift either double-suppresses or double-surfaces.
    """
    if due_pattern is None:
        return False
    if not isinstance(completion_log, dict) or not completion_log:
        return False
    entries = completion_log.get(item_text)
    if not isinstance(entries, list) or not entries:
        return False

    current_due = resolve_due_date(due_pattern, today)
    if current_due is None:
        return False
    prev_due = _previous_cycle_due_date(due_pattern, current_due)
    if prev_due is None:
        return False

    cycle_length = (current_due - prev_due).days
    if cycle_length <= 0:
        # Defensive — prev_due should be strictly before current_due
        # for every pattern; a zero or negative gap means the helper
        # is broken. Don't suppress.
        return False
    # //2 yields the integer half-cycle. For weekly (7), half=3; for
    # biweekly (14), half=7; for monthly (28-31), half=14-15. Always
    # at least 1 so even pathological tiny cycles don't divide-by-
    # zero out of the check.
    half_cycle = max(1, cycle_length // 2)

    # Parse completion entries — operator YAML may carry ISO strings
    # OR date objects (PyYAML's date parser fires inconsistently).
    completion_dates: list[date] = []
    for v in entries:
        if isinstance(v, date) and not isinstance(v, datetime):
            completion_dates.append(v)
        elif isinstance(v, datetime):
            completion_dates.append(v.date())
        elif isinstance(v, str):
            try:
                completion_dates.append(date.fromisoformat(v.strip()))
            except (TypeError, ValueError):
                continue
        # Other types silently dropped (defensive against operator
        # hand-edit garbage; matches the existing
        # ``_parse_item_completion_dates`` pattern in tier/compute.py).

    if not completion_dates:
        return False

    return any(
        abs((current_due - c).days) <= half_cycle for c in completion_dates
    )


def effective_due_for_predicate(
    due_pattern: DuePattern | None,
    completion_log: dict | None,
    item_text: str,
    today: date,
) -> date | None:
    """Return the due date the window-math predicate should compare against.

    Default path: returns the resolver's next-upcoming due date
    (``current_due``). This is the pre-Phase-2C-C1 behavior.

    Overdue-retention path (new 2026-06-01): when ``prev_due`` has
    passed (``prev_due < today``) AND no completion satisfies the
    prev cycle (per :func:`completion_satisfies_current_cycle`
    applied against the prev cycle window), returns ``prev_due``
    instead. This makes ``days_to_due = (prev_due - today).days``
    NEGATIVE, which the post-C1 T1 window math accepts as
    "overdue, retain in T1."

    Example: monthly day=15, today=June 17, completion_log empty.
      * resolve_due_date → July 15 (rolls forward)
      * prev_due → June 15
      * prev_due (June 15) < today (June 17) → True
      * no completion satisfies prev cycle → True
      * returns June 15 → days_to_due = -2 → T1 with overdue
        annotation

    Without this override, days_to_due = (July 15 - June 17) = 28,
    fails the T1 window check (even with escalate_at_days = 7), item
    silently drops.

    Returns ``None`` when the resolver returns None (malformed
    pattern) — caller treats as "no tier handoff."
    """
    if due_pattern is None:
        return None
    current_due = resolve_due_date(due_pattern, today)
    if current_due is None:
        return None
    if current_due >= today:
        # Current cycle hasn't passed yet — use current_due. The
        # completion-suppression check (separately) handles "did
        # the operator already cover this cycle?"
        return current_due
    # current_due < today is the pathological case (resolver should
    # always return >= today). If it does happen, treat as overdue.
    return current_due


def _completion_satisfies_prev_cycle(
    item_text: str,
    completion_log: dict | None,
    due_pattern: DuePattern,
    prev_due: date,
    cycle_length: int,
) -> bool:
    """Internal: did a completion cover the PREV cycle (ending at prev_due)?

    Same nearest-cycle ±half-cycle logic as
    :func:`completion_satisfies_current_cycle`, but with prev_due as
    the reference. Used by :func:`effective_due_for_predicate` to
    decide whether to fall back to prev_due as the effective due
    date (overdue retention) vs. the default current_due.
    """
    if not isinstance(completion_log, dict) or not completion_log:
        return False
    entries = completion_log.get(item_text)
    if not isinstance(entries, list) or not entries:
        return False
    half_cycle = max(1, cycle_length // 2)
    completion_dates: list[date] = []
    for v in entries:
        if isinstance(v, date) and not isinstance(v, datetime):
            completion_dates.append(v)
        elif isinstance(v, datetime):
            completion_dates.append(v.date())
        elif isinstance(v, str):
            try:
                completion_dates.append(date.fromisoformat(v.strip()))
            except (TypeError, ValueError):
                continue
    return any(
        abs((prev_due - c).days) <= half_cycle for c in completion_dates
    )


def overdue_effective_due(
    due_pattern: DuePattern | None,
    completion_log: dict | None,
    item_text: str,
    today: date,
) -> date | None:
    """Return effective_due that admits overdue retention.

    The full overdue-retention helper — distinct from
    :func:`effective_due_for_predicate` above which only returns
    current_due (the resolver's roll-forward result). This helper
    detects "prev cycle passed without completion → retain at prev_due
    as effective_due" and returns prev_due in that case.

    Used by the T1/T2 predicate to make the "missed deadline" case
    show up in the operator's tier section instead of silently
    rolling forward to next cycle.

    Returns:
      * ``None`` when pattern malformed or completion_log can't be
        interpreted (caller treats as "no tier handoff").
      * ``prev_due`` when ``prev_due < today`` AND no completion
        satisfies the prev cycle — overdue retention.
      * ``current_due`` otherwise (default; caller's window math
        proceeds as usual with non-negative days_to_due).

    Note (2026-06-01): the dispatch's separate-section "missed-cycle
    retention" framing is folded into this single helper rather than
    a parallel branch in each layer. Both halves of the
    completion-aware gap (suppress completed + retain overdue) go
    through the same two helpers
    (:func:`completion_satisfies_current_cycle` for suppress;
    this for retain). Layers call both in the same order, identical
    args.
    """
    import datetime as _dt
    _ = _dt  # silence unused-import warning; imported for clarity

    if due_pattern is None:
        return None
    current_due = resolve_due_date(due_pattern, today)
    if current_due is None:
        return None

    # The resolver returns >= today by design. The overdue case is
    # when the PREVIOUS cycle's due date has passed AND no completion
    # satisfies it.
    prev_due = _previous_cycle_due_date(due_pattern, current_due)
    if prev_due is None:
        return current_due
    if prev_due >= today:
        # prev_due is in the future — pathological for non-rolling
        # patterns. Treat as "no overdue retention needed."
        return current_due

    cycle_length = (current_due - prev_due).days
    if cycle_length <= 0:
        return current_due

    if _completion_satisfies_prev_cycle(
        item_text, completion_log, due_pattern, prev_due, cycle_length,
    ):
        # Prev cycle was completed → use current_due (default).
        return current_due

    # Prev cycle passed without completion → retain at prev_due.
    return prev_due


__all__ = [
    "completion_satisfies_current_cycle",
    "effective_due_for_predicate",
    "is_done_in_current_cycle",
    "overdue_effective_due",
    "resolve_due_date",
]
