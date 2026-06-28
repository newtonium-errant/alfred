"""Differential golden-master — the behavior-preservation LOCK for the
recurrence-DSL consolidation (Step 3).

Asserts the LIVE unified surface (``alfred.routine.cadence`` +
``alfred.routine.due`` shims over ``alfred.routine.recurrence``) reproduces the
FROZEN pre-consolidation behavior (``_recurrence_oracle``) EXACTLY, for every
shape, across a multi-year DAILY date range that hits: Feb leap (2028),
28/29/30/31 month boundaries, 5th-weekday months, ``"last"`` day, and the
malformed-biweekly anchor-mismatch path. DST transitions are included in the
swept range (the date layer is DST-insensitive by design).

Unconditional pins — no importorskip (pure date math; no optional deps). Per
``feedback_regression_pin_unconditional``.

The oracle is frozen; if any assertion here fails, the consolidation changed a
routine's firing day or due/behind result — a behavior regression, NOT an
expected diff.
"""
from __future__ import annotations

from datetime import date, timedelta

import structlog

from alfred.routine import cadence as live_cadence
from alfred.routine import due as live_due
from alfred.routine.config import DuePattern
from alfred.routine.recurrence import Recurrence
from tests.routine import _recurrence_oracle as oracle


# Multi-year daily sweep (spans leap-2028 + every month length + 5th weekdays).
_START = date(2026, 1, 1)
_END = date(2029, 12, 31)


def _date_range(start: date = _START, end: date = _END):
    d = start
    one = timedelta(days=1)
    while d <= end:
        yield d
        d += one


def _outcome(fn):
    """Return ('val', value) or ('raise', ExceptionTypeName) — lets the
    differential compare raise-vs-return symmetrically."""
    try:
        return ("val", fn())
    except Exception as exc:  # noqa: BLE001 — differential captures the class
        return ("raise", type(exc).__name__)


# ---------------------------------------------------------------------------
# Shape corpora
# ---------------------------------------------------------------------------

# Cadence dicts (routine-level) for the fires_on differential — the six
# cadence shapes + malformed cases.
_CADENCE_SHAPES: list[dict] = [
    {"type": "daily"},
    {"type": "weekly", "days": ["Wed"]},
    {"type": "weekly", "days": ["Mon", "Thu", "Sat"]},
    {"type": "weekly", "days": ["sunday"]},                 # long-form alias
    {"type": "every_n_days", "n": 1, "anchor": "2026-01-06"},
    {"type": "every_n_days", "n": 3, "anchor": "2026-01-06"},
    {"type": "every_n_days", "n": 14, "anchor": "2026-02-28"},
    {"type": "monthly", "day": 1},
    {"type": "monthly", "day": 15},
    {"type": "monthly", "day": 29},                          # Feb clamp
    {"type": "monthly", "day": 31},                          # 30/31 clamp
    {"type": "monthly", "day": "last"},
    {"type": "monthly", "nth_weekday": [1, "Mon"]},
    {"type": "monthly", "nth_weekday": [3, "Tue"]},
    {"type": "monthly", "nth_weekday": [5, "Fri"]},          # 5th-weekday months
    {"type": "monthly", "nth_weekday": [-1, "Fri"]},
    {"type": "every_n_months", "n": 2, "day": 15, "anchor": "2026-01-15"},
    {"type": "every_n_months", "n": 3, "day": "last", "anchor": "2026-01-31"},
    {"type": "every_n_months", "n": 1, "day": 31, "anchor": "2026-01-31"},
    # DUE-ONLY types fed to the CADENCE query (trap 3): cadence.is_due never
    # accepted these — old raised "unknown cadence type"; the unified fires_on
    # must STILL raise (one shared shape vocabulary, two distinct query
    # surfaces). The _outcome differential asserts both raise.
    {"type": "biweekly", "day": "thu", "anchor": "2026-05-28"},
    {"type": "weekly_soft"},
    # malformed → both must raise CadenceError
    {"type": "weekly", "days": []},
    {"type": "weekly"},
    {"type": "monthly"},
    {"type": "every_n_days", "n": 0, "anchor": "2026-01-01"},
    {"type": "bogus"},
    {"type": "monthly", "nth_weekday": [0, "Mon"]},          # n=0 invalid
]

# Due patterns (item-level) for the next_due + cycle differential — the six
# due shapes + malformed cases.
_DUE_PATTERNS: list[DuePattern] = [
    DuePattern(type="weekly", day="thu"),
    DuePattern(type="weekly", day="monday"),
    DuePattern(type="weekly_soft"),
    DuePattern(type="weekly", soft=True),
    DuePattern(type="biweekly", day="thu", anchor="2026-05-28"),   # valid Thu
    DuePattern(type="monthly", day=1),
    DuePattern(type="monthly", day=15),
    DuePattern(type="monthly", day=31),
    DuePattern(type="monthly", day="last"),
    DuePattern(type="every_n_days", n=10, anchor="2026-01-01"),
    DuePattern(type="every_n_days", n=7, anchor="2026-03-15"),
    DuePattern(type="monthly_nth_weekday", n=2, weekday="tue"),
    DuePattern(type="monthly_nth_weekday", n=5, weekday="fri"),
    DuePattern(type="monthly_nth_weekday", n=-1, weekday="fri"),
    # malformed → both resolve to None
    DuePattern(type="weekly"),                                     # no day
    DuePattern(type="biweekly", day="thu", anchor="2026-05-27"),   # anchor=Wed
    DuePattern(type="biweekly", day="thu"),                        # no anchor
    DuePattern(type="every_n_days", n=None, anchor="2026-01-01"),
    DuePattern(type="monthly"),                                    # no day
    DuePattern(type="monthly_nth_weekday", n=2),                   # no weekday
]


# ---------------------------------------------------------------------------
# fires_on differential (cadence.is_due)
# ---------------------------------------------------------------------------


def test_fires_on_matches_oracle_every_shape_every_day():
    for shape in _CADENCE_SHAPES:
        for d in _date_range():
            old = _outcome(lambda: oracle.cadence_is_due(shape, d))
            new = _outcome(lambda: live_cadence.is_due(shape, d))
            assert old == new, (shape, d.isoformat(), old, new)


# ---------------------------------------------------------------------------
# next_due differential (due.resolve_due_date) — both catch → compare values
# ---------------------------------------------------------------------------


def test_resolve_due_date_matches_oracle_every_shape_every_day():
    for dp in _DUE_PATTERNS:
        for d in _date_range():
            old = oracle.resolve_due_date(dp, d)
            new = live_due.resolve_due_date(dp, d)
            assert old == new, (dp, d.isoformat(), old, new)


# ---------------------------------------------------------------------------
# Cycle-helper differentials — per shape × per day × per completion scenario
# ---------------------------------------------------------------------------


def _completion_scenarios(today: date) -> list[list[date]]:
    """Completion-date sets exercising window boundaries relative to today."""
    return [
        [],
        [today],
        [today - timedelta(days=1)],
        [today - timedelta(days=7)],
        [today - timedelta(days=15)],
        [today - timedelta(days=30)],
        [today + timedelta(days=1)],
        [today - timedelta(days=3), today - timedelta(days=33)],
    ]


# A 2-year sub-range keeps the (shape × day × scenario × 3-helper) product
# fast while still covering every month length + boundary. fires_on/next_due
# above carry the full 4-year leap sweep.
_CYCLE_START = date(2026, 1, 1)
_CYCLE_END = date(2027, 12, 31)


def test_is_done_in_current_cycle_matches_oracle():
    for dp in _DUE_PATTERNS:
        for d in _date_range(_CYCLE_START, _CYCLE_END):
            for comp in _completion_scenarios(d):
                old = oracle.is_done_in_current_cycle(dp, comp, d)
                new = live_due.is_done_in_current_cycle(dp, comp, d)
                assert old == new, (dp, d.isoformat(), comp, old, new)


def test_completion_satisfies_current_cycle_matches_oracle():
    item = "X"
    for dp in _DUE_PATTERNS:
        for d in _date_range(_CYCLE_START, _CYCLE_END):
            for comp in _completion_scenarios(d):
                log_dict = {item: [c.isoformat() for c in comp]}
                old = oracle.completion_satisfies_current_cycle(
                    item, log_dict, dp, d,
                )
                new = live_due.completion_satisfies_current_cycle(
                    item, log_dict, dp, d,
                )
                assert old == new, (dp, d.isoformat(), comp, old, new)


def test_overdue_effective_due_matches_oracle():
    item = "X"
    for dp in _DUE_PATTERNS:
        for d in _date_range(_CYCLE_START, _CYCLE_END):
            for comp in _completion_scenarios(d):
                log_dict = {item: [c.isoformat() for c in comp]}
                old = oracle.overdue_effective_due(dp, log_dict, item, d)
                new = live_due.overdue_effective_due(dp, log_dict, item, d)
                assert old == new, (dp, d.isoformat(), comp, old, new)


# ---------------------------------------------------------------------------
# Malformed-biweekly path (operator decision Q2 — preserve, do NOT silently fire)
# ---------------------------------------------------------------------------


def test_malformed_biweekly_resolves_none_both():
    """Anchor weekday != configured day → unresolvable. Oracle returns None;
    the live shim returns None (must NOT silently fire a different cadence)."""
    dp = DuePattern(type="biweekly", day="thu", anchor="2026-05-27")  # Wed
    for d in _date_range(date(2026, 5, 1), date(2026, 8, 1)):
        assert oracle.resolve_due_date(dp, d) is None
        assert live_due.resolve_due_date(dp, d) is None


def test_malformed_biweekly_logs_routine_due_malformed():
    """The live shim preserves the ``routine.due.malformed`` warn on the
    anchor-mismatch path (log-emission discipline)."""
    dp = DuePattern(type="biweekly", day="thu", anchor="2026-05-27")
    with structlog.testing.capture_logs() as cap:
        result = live_due.resolve_due_date(dp, date(2026, 6, 1))
    assert result is None
    warns = [c for c in cap if c.get("event") == "routine.due.malformed"]
    assert len(warns) == 1
    assert warns[0]["type"] == "biweekly"


# ---------------------------------------------------------------------------
# from_dict normalization pins (the actual vocabulary unification)
# ---------------------------------------------------------------------------


def test_from_dict_weekly_singular_day_folds_to_days_list():
    rec = Recurrence.from_dict({"type": "weekly", "day": "thu"})
    assert rec is not None
    assert rec.type == "weekly"
    assert rec.days == ["thu"]


def test_from_dict_weekly_days_list_preserved():
    rec = Recurrence.from_dict({"type": "weekly", "days": ["Mon", "Wed"]})
    assert rec.type == "weekly" and rec.days == ["Mon", "Wed"]


def test_from_dict_soft_folds_to_weekly_soft():
    assert Recurrence.from_dict({"type": "weekly", "soft": True}).type == "weekly_soft"
    assert Recurrence.from_dict({"type": "weekly_soft"}).type == "weekly_soft"


def test_from_dict_monthly_nth_weekday_nesting_folds():
    rec = Recurrence.from_dict({"type": "monthly", "nth_weekday": [1, "Mon"]})
    assert rec.type == "monthly_nth_weekday"
    assert rec.n == 1 and rec.weekday == "Mon"


def test_from_dict_monthly_day_stays_monthly():
    rec = Recurrence.from_dict({"type": "monthly", "day": 15})
    assert rec.type == "monthly" and rec.day == 15


def test_from_dict_biweekly_preserved():
    rec = Recurrence.from_dict(
        {"type": "biweekly", "day": "thu", "anchor": "2026-05-28"})
    assert rec.type == "biweekly" and rec.day == "thu"
    assert rec.anchor == "2026-05-28"


def test_from_dict_every_n_months_preserved():
    rec = Recurrence.from_dict(
        {"type": "every_n_months", "n": 2, "day": 15, "anchor": "2026-01-15"})
    assert rec.type == "every_n_months" and rec.n == 2 and rec.day == 15


def test_from_dict_unknown_type_returns_none():
    assert Recurrence.from_dict({"type": "bogus"}) is None
    assert Recurrence.from_dict({"no": "type"}) is None


def test_from_dict_non_dict_returns_none():
    assert Recurrence.from_dict("weekly") is None
    assert Recurrence.from_dict(None) is None
    assert Recurrence.from_dict(42) is None


# ---------------------------------------------------------------------------
# Consumer integration pin — tier classify_routine_item is driven by the
# unified surface and tracks the oracle's deadline math day-by-day.
# ---------------------------------------------------------------------------


def test_classify_routine_item_tracks_oracle_deadline_math():
    """The tier classifier (LIVE consumer) on a monthly deadline item must
    produce the SAME tier the oracle's overdue/completion math implies, every
    day across a multi-month sweep. Ties the consumer to the frozen baseline:
    if the unified recurrence drifted, the classifier output would diverge."""
    from alfred.tier.compute import classify_routine_item

    item = "Pay Clinic Rental"
    dp = DuePattern(type="monthly", day=1)
    escalate_at_days = 0     # T1 only on/after the due day (clamped overdue)
    surface_at_days = 5      # T2 ramp 5 days out
    # A completion log so suppression + overdue-retention both get exercised.
    completion_log = {item: ["2026-05-29", "2026-07-01"]}

    for d in _date_range(date(2026, 5, 1), date(2026, 9, 30)):
        result = classify_routine_item(
            priority=None,
            due_pattern=dp,
            surface_at_days=surface_at_days,
            escalate_at_days=escalate_at_days,
            target_cadence_days=None,
            completion_log=completion_log,
            item_text=item,
            today=d,
            self_care=False,
            default_escalate_at_days=None,
            default_surface_at_days=None,
        )

        # Independent expectation via the FROZEN oracle, replicating the
        # classifier's window formula (completion-suppress → effective_due →
        # days_to_due → T1/T2 windows).
        if oracle.completion_satisfies_current_cycle(
            item, completion_log, dp, d,
        ):
            expected = None
        else:
            effective = oracle.overdue_effective_due(
                dp, completion_log, item, d,
            )
            if effective is None:
                expected = None
            else:
                days_to_due = (effective - d).days
                if days_to_due <= escalate_at_days:
                    expected = 1
                elif (
                    surface_at_days > escalate_at_days
                    and escalate_at_days < days_to_due <= surface_at_days
                ):
                    expected = 2
                else:
                    expected = None

        assert result.tier == expected, (d.isoformat(), result.tier, expected)


# ---------------------------------------------------------------------------
# Malformed-handling MODE per query (trap 1) — the two sides have DIFFERENT
# contracts and the unification must preserve each separately:
#   * cadence.is_due  → RAISES CadenceError (no log here; the aggregator logs)
#   * due.resolve_due_date → RETURNS None + emits routine.due.malformed (no raise)
# ---------------------------------------------------------------------------

# Malformed cadence dicts (the cadence query must RAISE on these).
_MALFORMED_CADENCE: list[dict] = [
    {"type": "weekly", "days": []},
    {"type": "weekly"},
    {"type": "monthly"},
    {"type": "every_n_days", "n": 0, "anchor": "2026-01-01"},
    {"type": "every_n_days", "n": 5},                 # missing anchor
    {"type": "bogus"},
    {"type": "monthly", "nth_weekday": [0, "Mon"]},
    {"no": "type"},
    "not-a-dict",
]

# Malformed due patterns (the due query must RETURN None + log, NOT raise).
_MALFORMED_DUE: list[DuePattern] = [
    DuePattern(type="weekly"),                                    # no day
    DuePattern(type="biweekly", day="thu", anchor="2026-05-27"),  # anchor=Wed
    DuePattern(type="biweekly", day="thu"),                       # no anchor
    DuePattern(type="every_n_days", n=None, anchor="2026-01-01"),
    DuePattern(type="every_n_days", n=5),                         # no anchor
    DuePattern(type="monthly"),                                   # no day
    DuePattern(type="monthly_nth_weekday", n=2),                  # no weekday
]


def test_cadence_query_malformed_mode_is_raise_no_log():
    """cadence.is_due RAISES CadenceError on malformed input AND does not emit
    the due-side ``routine.due.malformed`` log (mode = raise, not None+log)."""
    from alfred.routine.recurrence import CadenceError

    for shape in _MALFORMED_CADENCE:
        with structlog.testing.capture_logs() as cap:
            raised = False
            try:
                live_cadence.is_due(shape, date(2026, 6, 1))
            except CadenceError:
                raised = True
            assert raised, ("cadence.is_due must RAISE on malformed", shape)
        assert not [
            c for c in cap if c.get("event") == "routine.due.malformed"
        ], shape


def test_due_query_malformed_mode_is_none_plus_log_no_raise():
    """due.resolve_due_date RETURNS None + emits ``routine.due.malformed`` on
    malformed input and does NOT raise (mode = None+log, not raise)."""
    for dp in _MALFORMED_DUE:
        with structlog.testing.capture_logs() as cap:
            # Must not raise.
            result = live_due.resolve_due_date(dp, date(2026, 6, 1))
        assert result is None, (dp,)
        warns = [c for c in cap if c.get("event") == "routine.due.malformed"]
        assert len(warns) == 1, (dp, warns)
        assert warns[0]["type"] == dp.type


# ---------------------------------------------------------------------------
# Distinct query vocabularies (trap 3) — one shared shape, two query surfaces.
# ---------------------------------------------------------------------------


def test_fires_on_raises_on_due_only_types_matching_oracle():
    """``fires_on`` must RAISE on the due-only shapes (biweekly, weekly_soft) —
    exactly as the frozen oracle's cadence_is_due raised 'unknown cadence
    type'. The unified shape must NOT make the cadence query start accepting
    them."""
    from alfred.routine.recurrence import CadenceError, fires_on

    for shape in (
        {"type": "biweekly", "day": "thu", "anchor": "2026-05-28"},
        {"type": "weekly_soft"},
        {"type": "weekly", "soft": True},
    ):
        d = date(2026, 6, 1)
        # Oracle raises on these (verifies the baseline contract). The oracle
        # has its own CadenceError class (frozen copy) — catch THAT for the
        # old side, the live CadenceError for the new side.
        oracle_raised = False
        try:
            oracle.cadence_is_due(shape, d)
        except oracle.CadenceError:
            oracle_raised = True
        new_raised = False
        try:
            fires_on(shape, d)
        except CadenceError:
            new_raised = True
        assert oracle_raised and new_raised, shape


def test_next_due_fail_loud_on_cadence_only_types():
    """``next_due_on_or_after`` has no item-level resolver for the cadence-only
    shapes (daily, every_n_months) — it fails loud (Q5). Unreachable via the
    due shim because ``DuePattern`` cannot hold those types (see the companion
    pin), but the raise guards the substrate against a future caller."""
    from alfred.routine.recurrence import (
        CadenceError,
        Recurrence,
        next_due_on_or_after,
    )

    for rec in (
        Recurrence(type="daily"),
        Recurrence(type="every_n_months", n=2, day=15, anchor="2026-01-15"),
    ):
        raised = False
        try:
            next_due_on_or_after(rec, date(2026, 6, 1))
        except CadenceError:
            raised = True
        assert raised, rec.type


def test_duepattern_cannot_hold_cadence_only_types():
    """The item-parse gate (``DUE_PATTERN_TYPES`` via ``DuePattern.from_dict``)
    excludes daily / every_n_months, so the due query NEVER sees them in
    production — the fail-loud above is a defensive guard, not a regression."""
    assert DuePattern.from_dict({"type": "daily"}) is None
    assert DuePattern.from_dict(
        {"type": "every_n_months", "n": 2, "day": 15, "anchor": "2026-01-15"}
    ) is None


# ---------------------------------------------------------------------------
# THE ONE DOCUMENTED DIVERGENCE (reader-first widening) — pins NOTE-2.
# A routine-level cadence written with due.py's SINGULAR `day` spelling
# (``{type: weekly, day: "mon"}``) used to RAISE in cadence.is_due (cadence
# weekly required a `days:[list]`). The unified from_dict FOLDS singular
# `day` → `days`, so fires_on now FIRES on that weekday instead of raising.
# This is the intended reader-first widening (operator-approved) — the single
# old≠new case, kept honest by an explicit pin (NOT in the old==new sweep).
# ---------------------------------------------------------------------------


def test_singular_day_cadence_is_the_documented_reader_first_divergence():
    shape = {"type": "weekly", "day": "mon"}
    # OLD: cadence.is_due raised (no singular-day support at the routine level).
    a_monday = date(2026, 6, 1)   # Monday
    a_tuesday = date(2026, 6, 2)  # Tuesday
    for d in (a_monday, a_tuesday):
        old_raised = False
        try:
            oracle.cadence_is_due(shape, d)
        except oracle.CadenceError:
            old_raised = True
        assert old_raised, ("old cadence.is_due must raise on singular day", d)
    # NEW: fires_on folds day→days and fires on the named weekday.
    assert live_cadence.is_due(shape, a_monday) is True
    assert live_cadence.is_due(shape, a_tuesday) is False
