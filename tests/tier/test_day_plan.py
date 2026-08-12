"""Pins for the shared slot projection (``alfred.tier.day_plan``, Phase C).

The projection is the spine BOTH morning renders format — the brief's
"Today's Plan" section and the briefing player's spoken ``day_plan`` segment.
What is pinned here is what those two renders are entitled to assume:

  * the operator-facing slot LABELS agree with the FE's, cross-language;
  * "carryover" means the SERVER rollover (and matches the way that
    computation actually behaves — task-origin, by record name);
  * carryover outranks candidate, so a row is never counted twice;
  * the honest residue is never folded into Duty to fill the board;
  * ``rows_in_tier`` preserves the view's lane order — the guarantee that let
    the narration re-point without changing a byte of its output;
  * the arrangement never becomes the target: ``daily_goal`` rides through
    verbatim and tier-based.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from alfred.tier import slots
from alfred.tier.compute import (
    DailyGoalState,
    RoutineLine,
    TierEntry,
    TodayView,
)
from alfred.tier.day_plan import (
    SLOT_LABELS,
    UNSLOTTED_LABEL,
    DayPlan,
    RolloverRef,
    build_day_plan,
    slot_label,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _entry(name, *, tier=1, slot="duty", origin="task", source="operator", **kw):
    return TierEntry(
        tier=tier, origin=origin, name=name,
        path=f"{origin}/{name}.md", slot=slot, source=source, **kw,
    )


def _plan(view, *, rollover=(), done=()):
    done_names = set(done)
    return build_day_plan(
        view, rollover=rollover, is_done=lambda e: e.name in done_names,
    )


# --- cross-language label parity --------------------------------------------


def test_slot_labels_match_the_frontend() -> None:
    """The server and the PWA name the slots identically.

    Neither language can import the other, so the labels are duplicated by
    necessity — which is exactly why this reads the TS source instead of
    trusting that a future edit remembers the twin. A rename on one side with
    no rename on the other is the operator seeing "Fuel" in the brief and
    something else on the board for the same three rows.
    """
    ts = (REPO_ROOT / "web" / "lib" / "algernon" / "feedConstants.ts").read_text(
        encoding="utf-8",
    )
    block = re.search(
        r"export const SLOT_LABELS[^{]*\{(.*?)\}", ts, re.S,
    )
    assert block is not None, "SLOT_LABELS not found in feedConstants.ts"
    fe_labels = dict(re.findall(r"(\w+):\s*'([^']*)'", block.group(1)))
    assert fe_labels == SLOT_LABELS

    board = (REPO_ROOT / "web" / "lib" / "algernon" / "board.ts").read_text(
        encoding="utf-8",
    )
    fe_residue = re.search(
        r"export const UNSLOTTED_LABEL\s*=\s*'([^']*)'", board,
    )
    assert fe_residue is not None
    assert fe_residue.group(1) == UNSLOTTED_LABEL


def test_slot_label_falls_back_to_the_residue_label() -> None:
    """Anything that is not one of the three canonical slots is labelled as
    residue — including ``unslotted`` itself and any value a future classifier
    adds without telling this module. Never a crash, never an invented name."""
    assert slot_label(slots.SLOT_DUTY) == "Duty"
    assert slot_label(slots.SLOT_UNSLOTTED) == UNSLOTTED_LABEL
    assert slot_label("something_new") == UNSLOTTED_LABEL


# --- carryover: the SERVER rollover, matched the way rollover computes -------


def test_carryover_marks_the_matching_task_row() -> None:
    """A rollover ref matches today's row by task RECORD NAME.

    Positive + negative in one test: ``Pay rent`` is in the rollover and is
    marked; ``Buy milk`` — its nearest admissible neighbour, same slot, same
    tier, same origin — is NOT, which is what proves the marking is driven by
    the rollover and not by everything-gets-marked.
    """
    view = TodayView(t1=[_entry("Pay rent"), _entry("Buy milk")])
    plan = _plan(view, rollover=[
        RolloverRef(tier_label="T1", wikilink="[[task/Pay rent]]",
                    record_name="Pay rent"),
    ])
    duty = plan.groups[0]
    assert [r.name for r in duty.carryover] == ["Pay rent"]
    assert [r.name for r in duty.committed] == ["Buy milk"]
    assert plan.unplaced_carryover == []


def test_routine_origin_rows_never_match_rollover() -> None:
    """Rollover skips routine-origin entries by design (the next cycle
    resolves them through the routine's own ``due_pattern``), so a
    same-NAMED routine row must not absorb a task's rollover ref.

    The task row in the same plan is the positive control — it proves the
    matcher can fire at all.
    """
    view = TodayView(t1=[
        _entry("Water plants", origin="routine_item"),
        _entry("Pay rent"),
    ])
    plan = _plan(view, rollover=[
        RolloverRef("T1", "[[task/Water plants]]", "Water plants"),
        RolloverRef("T1", "[[task/Pay rent]]", "Pay rent"),
    ])
    marked = {r.name for g in plan.groups for r in g.carryover}
    assert marked == {"Pay rent"}
    # The unmatched ref is not dropped — it surfaces as unplaced.
    assert [r.record_name for r in plan.unplaced_carryover] == ["Water plants"]


def test_carryover_outranks_candidate() -> None:
    """A row that is both carried over AND an auto-surfaced candidate lands in
    ``carryover`` only — listing it in both would double-count the day."""
    view = TodayView(t1=[_entry("Pay rent", source="auto-due", confirmed=False)])
    plan = _plan(view, rollover=[
        RolloverRef("T1", "[[task/Pay rent]]", "Pay rent"),
    ])
    duty = plan.groups[0]
    assert [r.name for r in duty.carryover] == ["Pay rent"]
    assert duty.suggestions == []
    assert duty.carryover[0].candidate is False
    # Sanity that ``candidate`` is reachable at all: the same entry without a
    # rollover ref IS a suggestion.
    assert [r.name for r in _plan(view).groups[0].suggestions] == ["Pay rent"]


def test_unplaced_carryover_keeps_yesterdays_tier_and_wikilink() -> None:
    """A commitment that fell off today's board entirely is not dropped — it
    keeps yesterday's tier label and the wikilink as yesterday wrote it,
    because that is the only place the operator can still click through."""
    plan = _plan(TodayView(), rollover=[
        RolloverRef("T2", "[[task/Connect QBO API]]", "Connect QBO API"),
    ])
    assert len(plan.unplaced_carryover) == 1
    ref = plan.unplaced_carryover[0]
    assert (ref.tier_label, ref.wikilink) == ("T2", "[[task/Connect QBO API]]")


# --- the honest residue ------------------------------------------------------


def test_residue_group_absent_when_nothing_is_unslotted() -> None:
    view = TodayView(t1=[_entry("Pay rent", slot="duty")])
    assert [g.slot for g in _plan(view).groups] == list(slots.CANONICAL_SLOTS)


def test_residue_is_surfaced_never_folded_into_duty() -> None:
    """An item the classifier could not answer for renders as unanswered. It
    is the confident-wrong-answer this design refuses to give — and the Duty
    stack must not absorb it to look full."""
    view = TodayView(t1=[
        _entry("Pay rent", slot="duty"),
        _entry("Someday thing", slot=slots.SLOT_UNSLOTTED),
    ])
    plan = _plan(view)
    assert [g.slot for g in plan.groups][-1] == slots.SLOT_UNSLOTTED
    duty = plan.groups[0]
    assert [r.name for r in duty.rows] == ["Pay rent"]
    assert [r.name for r in plan.groups[-1].rows] == ["Someday thing"]


def test_unknown_slot_value_degrades_into_the_residue() -> None:
    """A slot key this module has never heard of is residue, not a crash and
    not a fourth stack — the same direction ``normalize_slot`` takes."""
    view = TodayView(t1=[_entry("Odd one", slot="sideways")])
    plan = _plan(view)
    assert [r.name for r in plan.groups[-1].rows] == ["Odd one"]
    assert plan.groups[-1].slot == slots.SLOT_UNSLOTTED


# --- the tier axis survives the slot arrangement ----------------------------


def test_rows_in_tier_preserves_the_views_lane_order() -> None:
    """THE guarantee that let the narration re-point without changing a byte.

    Grouping by slot reorders the board; a reader on the tier axis must not
    inherit that reordering. Here the two T1 rows are in DIFFERENT slots, so
    a group-order read would return them Duty-first — the view's order is
    Fuel-first, and that is what must come back.
    """
    view = TodayView(
        t1=[_entry("Fuel first", slot="fuel"), _entry("Duty second", slot="duty")],
        t2=[_entry("A t2", tier=2, slot="duty")],
    )
    plan = _plan(view)
    assert [r.name for r in plan.rows_in_tier(1)] == ["Fuel first", "Duty second"]
    assert [r.name for r in plan.rows_in_tier(2)] == ["A t2"]
    assert plan.rows_in_tier(3) == []
    # The GROUP order genuinely differs — otherwise this pin proves nothing.
    assert [r.name for r in plan.rows][0] == "Duty second"


def test_daily_goal_rides_through_verbatim_and_tier_based() -> None:
    """The arrangement must never become the target. The projection carries
    the view's own ``DailyGoalState`` object through untouched and computes no
    slot-based goal of its own — there is no balanced-by-slot number here for
    a render to accidentally start quoting."""
    goal = DailyGoalState(t1_available=2, t1_done=1, balanced_day=False)
    plan = _plan(TodayView(daily_goal=goal))
    assert plan.daily_goal is goal
    assert not hasattr(plan, "balanced_day")
    assert not any(hasattr(g, "balanced") for g in plan.groups)


# --- routines (§4's dissolution) --------------------------------------------


def test_routine_lines_land_in_their_own_slot() -> None:
    view = TodayView(routine_today=[
        RoutineLine(text="Morning pages", priority="tracked", slot="rhythm"),
        RoutineLine(text="Sit outside", priority="aspirational", slot="fuel"),
    ])
    plan = _plan(view)
    by_slot = {g.slot: [ln.text for ln in g.routines] for g in plan.groups}
    assert by_slot["rhythm"] == ["Morning pages"]
    assert by_slot["fuel"] == ["Sit outside"]
    assert by_slot["duty"] == []


def test_a_plan_with_only_routines_is_not_empty() -> None:
    """A day of nothing but habit anchors is a real day. ``is_empty`` drives
    the section-level "nothing at all" sentinel, so a false positive here
    would erase today's routines behind a "nothing on today's plan" line."""
    view = TodayView(routine_today=[
        RoutineLine(text="Morning pages", priority="tracked", slot="rhythm"),
    ])
    assert _plan(view).is_empty is False
    assert DayPlan().is_empty is True


# --- done axis ---------------------------------------------------------------


def test_done_is_read_through_the_supplied_predicate() -> None:
    """``is_done`` is required, not defaulted — a default would leave the done
    axis dead everywhere it was not threaded while every pin that passed its
    own callable stayed green."""
    view = TodayView(t1=[_entry("Pay rent"), _entry("Buy milk")])
    plan = _plan(view, done=["Pay rent"])
    assert {r.name: r.done for r in plan.rows} == {
        "Pay rent": True, "Buy milk": False,
    }


# --- observability -----------------------------------------------------------


def test_projection_logs_on_an_empty_day() -> None:
    """Intentionally-left-blank: a projection that arranged nothing and one
    that stopped running are otherwise identical in the log."""
    with structlog.testing.capture_logs() as captured:
        _plan(TodayView())
    events = [c for c in captured if c.get("event") == "tier.day_plan.projected"]
    assert len(events) == 1
    e = events[0]
    assert e["total_rows"] == 0
    assert e["carryover"] == 0
    assert e["unplaced_carryover"] == 0
    assert e["slots_present"] == []


def test_projection_log_reports_the_arrangement() -> None:
    view = TodayView(
        t1=[_entry("Pay rent"), _entry("Odd one", slot=slots.SLOT_UNSLOTTED)],
        t3=[_entry("Walk", tier=3, slot="fuel", source="auto-cadence-routine")],
        routine_today=[
            RoutineLine(text="Morning pages", priority="tracked", slot="rhythm"),
        ],
    )
    with structlog.testing.capture_logs() as captured:
        _plan(view, rollover=[RolloverRef("T1", "[[task/Pay rent]]", "Pay rent")])
    e = [c for c in captured if c.get("event") == "tier.day_plan.projected"][0]
    assert e["total_rows"] == 3
    assert e["carryover"] == 1
    assert e["suggestions"] == 1
    assert e["routines"] == 1
    assert e["unslotted_rows"] == 1
    assert set(e["slots_present"]) == {"duty", "rhythm", "fuel", "unslotted"}
