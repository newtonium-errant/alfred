"""Day plan — the ONE slot projection both morning renders read (Phase C, C2+C3).

The brief's day section and the briefing player's ``day_plan`` segment used to
read :class:`~alfred.tier.compute.TodayView` independently, each re-deciding
what a row was and how to group it. This module is the shared spine: one pure
projection over the view, consumed by both. Neither render makes a surface
decision of its own any more — they are formatters of this object's WHAT.

Arrangement, not target
-----------------------
The board arranges the day by SLOT (Duty / Rhythm / Fuel). The daily GOAL is
still measured by TIER — ``DailyGoalState.balanced_day`` counts one-done in each
of T1/T2/T3, and the stage-3 flip of that metric to the slot axis is a separate,
separately-gated lane. So this projection carries both axes on every row and the
renders group by slot while the goal line keeps speaking tiers.

**No copy anywhere may claim a slot-based goal while the metric is tier-based.**

That sentence is the whole constraint. A render that says "one in every slot" —
or a helper that quietly starts counting ``groups`` to answer "balanced?" — has
made the arrangement into the target, and the number underneath it will not
agree. Group by slot; measure by tier; say so in the copy.

The word "carryover" has two correct meanings here
--------------------------------------------------
Two surfaces answer "is this carried over?" differently, ON PURPOSE, because
they can see different things:

* **Server ROLLOVER** (``brief.tier_section``): yesterday's ``tier_curation``
  T1+T2 entries that are still open today. It reads yesterday's curation block,
  so it knows what the operator actually committed to yesterday. T3 is
  deliberately excluded — self-care intentions are picked fresh each day, not
  carried. A missing yesterday-file SUPPRESSES the section; a yesterday-file
  with everything completed emits a SENTINEL, so "no data" and "all clear" stay
  distinguishable.
* **Board ``boardIsCarryover``** (``web/lib/algernon/board.ts``): the feed
  episode's ``created_at`` is not today. Episode age is the only carry signal
  the feed has — it never sees a curation block — and it is exact for what it
  measures, because the store preserves first-seen across syncs.

This projection consumes the EXISTING server rollover; it does not invent a
third definition, and it does not adopt the board's rule server-side. Both
definitions stay right for their own surface. If you are here to "unify" them,
the thing to unify is the vocabulary in the copy, not the two computations.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from . import slots

if TYPE_CHECKING:  # avoid a heavy import at module load
    from .compute import DailyGoalState, RoutineLine, TierEntry, TodayView

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Operator-facing slot labels — the SERVER half of the G9 rename.
#
# Ratification 4 (2026-08-12): "Rename is UI + model labels only — Duty/Rhythm/
# Fuel everywhere operator-facing; storage keys/CLI stay t1/t2/t3." These are
# the labels; ``slots.CANONICAL_SLOTS`` stays the key set.
#
# Deliberately mirrors ``SLOT_LABELS`` / ``UNSLOTTED_LABEL`` in
# ``web/lib/algernon/feedConstants.ts`` + ``board.ts`` — the two renders are in
# different languages and neither can import the other, so the pin in
# ``tests/tier/test_day_plan.py`` asserts the two agree rather than trusting
# that a future edit remembers the twin.
# ---------------------------------------------------------------------------

SLOT_LABELS: dict[str, str] = {
    slots.SLOT_DUTY: "Duty",
    slots.SLOT_RHYTHM: "Rhythm",
    slots.SLOT_FUEL: "Fuel",
}

#: What the honest residue is called out loud. ``unslotted`` is NOT a slot — it
#: is the classifier's admission that no rule fired, and it is never defaulted
#: into Duty to make the stacks look full.
UNSLOTTED_LABEL = "Not sorted yet"


def slot_label(slot: str) -> str:
    """The operator-facing name of a slot key; the residue label for anything
    else (including ``unslotted`` itself, and any value a future classifier
    might add without telling this module)."""
    return SLOT_LABELS.get(slot, UNSLOTTED_LABEL)


# ---------------------------------------------------------------------------
# Rollover — the EXISTING server definition, factored out of its renderer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RolloverRef:
    """One of yesterday's committed items that is still open today.

    ``tier_label`` is ``"T1"`` / ``"T2"`` (the tier it was committed AT
    yesterday — rollover is a fact about yesterday's curation, so it carries
    yesterday's tier, not today's). ``wikilink`` is the raw ``[[task/Name]]``
    exactly as yesterday's block wrote it; ``record_name`` is the parsed name,
    which is what matches against a row on today's board.
    """

    tier_label: str
    wikilink: str
    record_name: str


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanRow:
    """One row of the day, carrying BOTH axes plus its resolved state.

    ``entry`` is the underlying :class:`~alfred.tier.compute.TierEntry` — tier,
    slot, origin, name, path, due, source, all of it. Nothing is copied out of
    it and re-derived here; the row adds only the three facts the renders would
    otherwise each have had to work out for themselves.
    """

    entry: "TierEntry"
    #: Completed today, per ``compute.entry_is_done`` — the SAME predicate the
    #: daily-goal count and the feed producer's ``done`` flag use. Never
    #: re-derived: a done/undone disagreement between two surfaces is the exact
    #: gaslighting Phase C exists to close.
    done: bool = False
    #: Present in the server rollover — i.e. committed yesterday, still open.
    carryover: bool = False
    #: Auto-surfaced and not yet committed — the yes/no candidates.
    candidate: bool = False

    @property
    def slot(self) -> str:
        return getattr(self.entry, "slot", slots.SLOT_UNSLOTTED)

    @property
    def tier(self) -> int:
        return getattr(self.entry, "tier", 0)

    @property
    def name(self) -> str:
        return getattr(self.entry, "name", "")


@dataclass(frozen=True)
class SlotGroup:
    """One slot's stack, in the order a morning reader wants it.

    ``carryover`` first (it is the thing that has already cost the operator a
    day), then ``committed`` (what today is actually for), then ``suggestions``
    (the yes/nos), then ``routines`` (today's habit anchors that never escalated
    into a tier — §4's dissolution).

    A row that is BOTH carryover and candidate lands in ``carryover``: the
    age claim outranks the offer, and listing it twice would double-count the
    day.
    """

    slot: str
    label: str
    carryover: list[PlanRow] = field(default_factory=list)
    committed: list[PlanRow] = field(default_factory=list)
    suggestions: list[PlanRow] = field(default_factory=list)
    routines: list["RoutineLine"] = field(default_factory=list)

    @property
    def rows(self) -> list[PlanRow]:
        """Every tier-lane row in this slot, in render order."""
        return [*self.carryover, *self.committed, *self.suggestions]

    @property
    def is_empty(self) -> bool:
        return not self.rows and not self.routines

    @property
    def done_count(self) -> int:
        return sum(1 for r in self.rows if r.done)


@dataclass(frozen=True)
class DayPlan:
    """The whole day, arranged by slot — what both morning renders format.

    ``groups`` is always the three canonical slots in ring order, plus the
    ``unslotted`` residue group IFF it holds anything. The residue is never
    silently dropped and never folded into Duty (``slots.py``'s discipline,
    carried through the render layer): an item the classifier could not answer
    for is shown as unanswered.

    ``unplaced_carryover`` is the honest remainder of the rollover — yesterday's
    still-open commitments that are NOT on today's board at all, so there is no
    row to attach them to and no slot to put them in without guessing. They are
    surfaced as their own short list rather than dropped, because "it fell off
    the board while still open" is exactly the thing a morning reader needs to
    see.

    ``daily_goal`` rides along verbatim from the view. It is TIER-based — see
    the module docstring's constraint before writing any copy against it.
    """

    groups: list[SlotGroup] = field(default_factory=list)
    unplaced_carryover: list[RolloverRef] = field(default_factory=list)
    daily_goal: "DailyGoalState | None" = None
    coverage: slots.SlotCoverage = field(default_factory=slots.SlotCoverage)
    #: Every row in the ORIGINAL view lane order (t1, then t2, then t3).
    #: Grouping by slot reorders ``groups``; a reader on the TIER axis must not
    #: inherit that reordering, so it reads this instead. Underscore-prefixed
    #: because :meth:`rows_in_tier` is the supported way in.
    _ordered_rows: list[PlanRow] = field(default_factory=list)

    @property
    def rows(self) -> list[PlanRow]:
        """Every tier-lane row across every group, in group order."""
        return [r for g in self.groups for r in g.rows]

    def rows_in_tier(self, tier: int) -> list[PlanRow]:
        """Rows in one TIER lane, in the view's own lane order.

        The tier axis survives the slot arrangement — this is how a render that
        still speaks tiers (the narration's ``day_plan`` segment, until the
        voice pass re-voices it) reads the projection without the grouping
        changing a word of its output.
        """
        return [r for r in self._ordered_rows if r.tier == tier]

    @property
    def total_rows(self) -> int:
        return len(self._ordered_rows)

    @property
    def is_empty(self) -> bool:
        return not self._ordered_rows and not self.unplaced_carryover and not any(
            g.routines for g in self.groups
        )


def _is_candidate(entry: "TierEntry") -> bool:
    """Whether a row is an auto-surfaced yes/no rather than a commitment.

    Delegates to the feed producer's predicate so the brief, the board and the
    player agree on what "candidate" means by IDENTITY rather than by two
    look-alike implementations drifting apart. That module owns the accept
    provenance-guard; this is the same question.
    """
    from alfred.brief.feed_producer import _slot_is_candidate

    return _slot_is_candidate(
        getattr(entry, "source", "") or "", getattr(entry, "confirmed", None),
    )


def build_day_plan(
    view: "TodayView",
    *,
    rollover: Sequence[RolloverRef],
    is_done: Callable[["TierEntry"], bool],
) -> DayPlan:
    """Project a :class:`TodayView` + the rollover material into a day plan.

    PURE — no I/O, no clock. ``is_done`` is REQUIRED rather than defaulted:
    a default would make the done axis dead everywhere it was not threaded
    while every unit pin that passed its own callable stayed green, which is
    the optional-gate trap this codebase has paid for before.

    Rollover matching is by task RECORD NAME. Only task-origin rows can match,
    which mirrors the rollover computation itself (routine-origin entries are
    skipped there — the next cycle resolves them via the routine's own
    ``due_pattern``, so they are not "carried").
    """
    rollover_names = {r.record_name for r in rollover if r.record_name}
    matched: set[str] = set()

    ordered: list[PlanRow] = []
    for entry in (*view.t1, *view.t2, *view.t3):
        name = getattr(entry, "name", "")
        is_carry = (
            getattr(entry, "origin", "") == "task"
            and name in rollover_names
        )
        if is_carry:
            matched.add(name)
        ordered.append(PlanRow(
            entry=entry,
            done=bool(is_done(entry)),
            carryover=is_carry,
            # carryover outranks candidate — see SlotGroup's docstring.
            candidate=(not is_carry) and _is_candidate(entry),
        ))

    # Bucket into slots. ``unslotted`` gets a group too, built last and kept
    # only if it holds something.
    buckets: dict[str, dict[str, list]] = {
        key: {"carryover": [], "committed": [], "suggestions": [], "routines": []}
        for key in (*slots.CANONICAL_SLOTS, slots.SLOT_UNSLOTTED)
    }
    for row in ordered:
        bucket = buckets.get(row.slot) or buckets[slots.SLOT_UNSLOTTED]
        if row.carryover:
            bucket["carryover"].append(row)
        elif row.candidate:
            bucket["suggestions"].append(row)
        else:
            bucket["committed"].append(row)

    for line in getattr(view, "routine_today", []) or []:
        key = getattr(line, "slot", slots.SLOT_UNSLOTTED)
        bucket = buckets.get(key) or buckets[slots.SLOT_UNSLOTTED]
        bucket["routines"].append(line)

    groups: list[SlotGroup] = []
    for key in slots.CANONICAL_SLOTS:
        b = buckets[key]
        groups.append(SlotGroup(
            slot=key, label=slot_label(key),
            carryover=b["carryover"], committed=b["committed"],
            suggestions=b["suggestions"], routines=b["routines"],
        ))
    residue = buckets[slots.SLOT_UNSLOTTED]
    if any(residue.values()):
        groups.append(SlotGroup(
            slot=slots.SLOT_UNSLOTTED, label=UNSLOTTED_LABEL,
            carryover=residue["carryover"], committed=residue["committed"],
            suggestions=residue["suggestions"], routines=residue["routines"],
        ))

    unplaced = [r for r in rollover if r.record_name not in matched]

    plan = DayPlan(
        groups=groups,
        unplaced_carryover=unplaced,
        daily_goal=getattr(view, "daily_goal", None),
        coverage=getattr(view, "slot_coverage", slots.SlotCoverage()),
        _ordered_rows=ordered,
    )
    log.info(
        "tier.day_plan.projected",
        # Emitted on EVERY projection including the empty day, per
        # intentionally-left-blank: a plan that arranged nothing and a
        # projection that stopped running are otherwise identical.
        total_rows=len(ordered),
        carryover=sum(len(g.carryover) for g in groups),
        committed=sum(len(g.committed) for g in groups),
        suggestions=sum(len(g.suggestions) for g in groups),
        routines=sum(len(g.routines) for g in groups),
        unplaced_carryover=len(unplaced),
        slots_present=[g.slot for g in groups if not g.is_empty],
        unslotted_rows=sum(
            1 for r in ordered if r.slot == slots.SLOT_UNSLOTTED
        ),
    )
    return plan


def build_day_plan_for_vault(
    vault_path: Path,
    view: "TodayView",
    today: date,
    *,
    rollover: Sequence[RolloverRef] = (),
) -> DayPlan:
    """Gatherer: build the done-caches this vault needs, then project.

    The thin I/O wrapper around :func:`build_day_plan`, so every production
    caller gets the done axis threaded without each of them having to know
    about ``build_done_caches``. Cache-build failure degrades to an
    all-not-done plan rather than killing the render — a morning brief that
    renders without strike-throughs is far better than one that does not render.
    """
    from .compute import build_done_caches, entry_is_done

    try:
        task_fm_by_name, completion_by_record = build_done_caches(vault_path)
    except Exception as exc:  # noqa: BLE001 — a bad cache degrades, never kills
        log.warning(
            "tier.day_plan.done_caches_failed",
            error=str(exc),
            error_type=exc.__class__.__name__,
            detail="day plan renders with no completion state this run.",
        )
        return build_day_plan(view, rollover=rollover, is_done=lambda _e: False)

    def _done(entry: "TierEntry") -> bool:
        return entry_is_done(
            entry,
            task_fm_by_name=task_fm_by_name,
            completion_by_record=completion_by_record,
            today=today,
        )

    return build_day_plan(view, rollover=rollover, is_done=_done)


def iter_rows(plan: DayPlan) -> Iterable[PlanRow]:
    """Every row in group order — convenience for consumers that do not care
    about the grouping (kept here so they do not reach into ``_ordered_rows``)."""
    return plan.rows


__all__ = [
    "SLOT_LABELS",
    "UNSLOTTED_LABEL",
    "slot_label",
    "RolloverRef",
    "PlanRow",
    "SlotGroup",
    "DayPlan",
    "build_day_plan",
    "build_day_plan_for_vault",
    "iter_rows",
]
