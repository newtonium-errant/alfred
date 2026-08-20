"""``backdate_credit_window`` — THE bound for backdated completions.

The operator's 2026-08-20 report: a duty done yesterday but never logged
comes back due today, and completing it today would write a FALSE date. His
ruling: yesterday is the default "previously done" option, further backdating
as needed. The BOUND this module pins: a backdate may claim exactly the dates
that pay the debt being surfaced — ±half-cycle around the item's CURRENT
effective due (the same window the classifier credits by), strictly before
today. Beyond that window, "previously done" is a claim about a different
cycle, and the honest answer is a refusal, not a write.

Every case asserts the EXACT endpoints (never just non-None): the window IS
the contract the dispatcher enforces and the producer serves rungs from, so
an off-by-one here is an off-by-one in what the operator is offered.
"""

from __future__ import annotations

from datetime import date, timedelta

from alfred.routine.recurrence import Recurrence, backdate_credit_window

# Wednesday 2026-07-22 — the reference "today" every case computes against.
# Weekly-Tuesday shapes then put "due yesterday" one day back (the operator's
# Garbage-Day scenario exactly).
TODAY = date(2026, 7, 22)
YESTERDAY = TODAY - timedelta(days=1)


def _weekly_tue() -> dict:
    return {"type": "weekly", "day": "tue"}


# ---------------------------------------------------------------------------
# The operator's scenario — overdue by one day, nothing logged
# ---------------------------------------------------------------------------


def test_overdue_by_one_weekly_window_reaches_back_half_cycle() -> None:
    """Garbage Day: weekly Tuesday, today Wednesday, no completion. The
    effective due is YESTERDAY (overdue retention), the weekly half-cycle is
    3, so the honest claim range is [today-4 .. yesterday]."""
    window = backdate_credit_window(_weekly_tue(), {}, "Garbage Day", TODAY)
    assert window == (YESTERDAY - timedelta(days=3), YESTERDAY)


def test_yesterday_is_always_inside_an_overdue_window() -> None:
    """The default rung. If this fails, the ruling's centrepiece option is
    being refused on the exact shape it was ruled for."""
    window = backdate_credit_window(_weekly_tue(), {}, "Garbage Day", TODAY)
    assert window is not None
    start, end = window
    assert start <= YESTERDAY <= end


def test_accepts_raw_dict_and_recurrence_object_identically() -> None:
    """Both caller spellings (producer holds a DuePattern-derived dict, the
    dispatcher may hold a normalized Recurrence) get one answer."""
    raw = backdate_credit_window(_weekly_tue(), {}, "x", TODAY)
    rec = backdate_credit_window(Recurrence.from_dict(_weekly_tue()), {}, "x", TODAY)
    assert raw == rec is not None


# ---------------------------------------------------------------------------
# Due today / early completion
# ---------------------------------------------------------------------------


def test_due_today_admits_the_trailing_half_cycle() -> None:
    """Weekly due TODAY (Wednesday pattern): eff = today, so the window is
    [today-3 .. yesterday] — did-it-early is a claim the credit math itself
    honours, and 'today' stays the plain done verb's territory (end < today)."""
    window = backdate_credit_window({"type": "weekly", "day": "wed"}, {}, "x", TODAY)
    assert window == (TODAY - timedelta(days=3), YESTERDAY)


def test_due_in_two_days_admits_only_yesterday() -> None:
    """Weekly due Friday (today Wednesday, prior cycle long lapsed): eff is
    the UPCOMING due, and only yesterday sits within ±half-cycle of it and
    before today. The early-completion rule, at its edge."""
    window = backdate_credit_window({"type": "weekly", "day": "fri"}, {}, "x", TODAY)
    assert window == (YESTERDAY, YESTERDAY)


def test_due_in_three_days_admits_nothing() -> None:
    """Weekly due Saturday: eff - half = today, and a backdate must be
    strictly before today — empty range, None. Mid-window items offer no
    honest 'previously done'."""
    assert backdate_credit_window({"type": "weekly", "day": "sat"}, {}, "x", TODAY) is None


# ---------------------------------------------------------------------------
# The paid cycle — a satisfied debt offers no backdate
# ---------------------------------------------------------------------------


def test_prev_cycle_satisfied_offers_no_overdue_window() -> None:
    """Same Garbage-Day shape, but yesterday's completion IS logged: the
    effective due flips to next Tuesday (+6), whose credit window opens no
    earlier than +3 — nothing before today qualifies. A paid debt cannot be
    re-paid into the past."""
    log = {"Garbage Day": [YESTERDAY.isoformat()]}
    assert backdate_credit_window(_weekly_tue(), log, "Garbage Day", TODAY) is None


def test_long_lapsed_cycle_offers_only_the_upcoming_dues_window() -> None:
    """Weekly Friday, today Wednesday, never completed: the prior Friday
    lapsed 5 days ago — beyond the half-cycle retention — so eff is the
    UPCOMING Friday and the window is yesterday alone (early-completion),
    never a reach back into the expired cycle."""
    window = backdate_credit_window({"type": "weekly", "day": "fri"}, {}, "x", TODAY)
    assert window == (YESTERDAY, YESTERDAY)


# ---------------------------------------------------------------------------
# Other shapes — the half-cycle scales with the grammar
# ---------------------------------------------------------------------------


def test_every_n_days_short_cycle_narrow_window() -> None:
    """every_n_days n=2 overdue by one (anchor puts a due at yesterday):
    half-cycle 1, window [today-2 .. yesterday]. The narrowest real shape —
    the dispatcher's beyond-window refusal case drives this exact grammar."""
    pattern = {
        "type": "every_n_days",
        "n": 2,
        "anchor": (YESTERDAY - timedelta(days=14)).isoformat(),
    }
    window = backdate_credit_window(pattern, {}, "x", TODAY)
    assert window == (TODAY - timedelta(days=2), YESTERDAY)


def test_monthly_overdue_window_reaches_half_a_month() -> None:
    """monthly day=20, today the 22nd, nothing logged: eff = the 20th
    (overdue retention), cycle June-20 -> July-20 is 30 days, half 15."""
    window = backdate_credit_window({"type": "monthly", "day": 20}, {}, "x", TODAY)
    due = date(2026, 7, 20)
    assert window == (due - timedelta(days=15), YESTERDAY)


# ---------------------------------------------------------------------------
# No grammar, no bound
# ---------------------------------------------------------------------------


def test_none_and_malformed_patterns_yield_none() -> None:
    assert backdate_credit_window(None, {}, "x", TODAY) is None
    assert backdate_credit_window({"type": "no_such_shape"}, {}, "x", TODAY) is None
    assert backdate_credit_window("not-a-dict", {}, "x", TODAY) is None
    # Valid type, malformed aux (weekly with no day) — the catching path.
    assert backdate_credit_window({"type": "weekly"}, {}, "x", TODAY) is None
