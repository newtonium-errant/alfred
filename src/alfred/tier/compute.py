"""Tier computation — pure projection over task + routine frontmatter (V2).

Tier-V2 reframes tier as a **daily curation ritual** stored in
``vault/daily/<date>.md`` rather than persistent per-task attributes.
See :mod:`alfred.tier.daily_curation` for the data layer + Ship 2's
``alfred.brief.tier_section`` for the render that consumes both this
auto-T1 surface and the operator-curated shortlists.

Compute primitives for V2 (three auto-surfaces):

  * :func:`compute_auto_t1_candidates` — open ``task/*.md`` records
    whose ``due`` is today/tomorrow OR inside the
    ``escalate_at_days`` window.
  * :func:`compute_auto_routine_candidates` (Phase 2A Ship A,
    2026-05-29) — items inside ``routine/*.md`` records whose
    ``due_pattern`` resolves to a date inside the T1 window
    ``[0, escalate_at_days]``. The Pay-Clinic-Rental shape:
    ``surface_at_days: 5`` + ``escalate_at_days: 0`` puts the
    item in T2 on the 27th through 31st, then T1 on the 1st.
  * :func:`compute_auto_routine_t2_candidates` (Ship A) — same scan,
    but the T2 window ``(escalate_at_days, surface_at_days]``.

The brief renderer (Ship B) reads all three and merges them with
operator-curated shortlists; the operator confirms-or-drops via
talker (Ship D SKILL).

Auto-surface criteria for tasks (in priority order —
short-circuits on first match):

  * ``due`` is today → reason ``"due today"``
  * ``due`` is tomorrow → reason ``"due tomorrow"``
  * ``escalate_at_days`` is set + ``due`` is within that window (more
    than 1 day out — the 0/1-day cases above subsume the rest) →
    reason ``"escalate window (Nd before due)"``

Defensive filters: parse failures, non-task ``type:``, closed
``status:``, ``alfred_triage: True`` (janitor-generated records that
go to the Daily Sync Triage Queue, not the tier section).

Auto-surface criteria for routine items (Phase 2A Ship A):

  * Item has ``due_pattern`` (recurring deadline) AND
    ``escalate_at_days`` set (absent ``escalate_at_days`` means the
    item is daily-routine surface only — Walk Fergus shape, never
    auto-tiered).
  * ``resolve_due_date(pattern, today)`` succeeds.
  * The item is NOT done in the current cycle (per
    :func:`alfred.routine.due.is_done_in_current_cycle`).

  T1 window: ``[0, escalate_at_days]`` (days_to_due in inclusive range)
  T2 window: ``(escalate_at_days, surface_at_days]`` (strictly above
             escalate, inclusive of surface) — only fires when
             ``surface_at_days > escalate_at_days``.

  Reason strings for routine items:
    * ``"due today"`` (days_to_due == 0)
    * ``"due tomorrow"`` (days_to_due == 1)
    * ``"escalate window (Nd before due)"`` (T1, days > 1)
    * ``"surface window (Nd before due)"`` (T2 candidate)

V1 retired (2026-05-29 Ship 3). The per-task ``base_tier`` /
``escalate_to`` / priority-fallback projection through the prior
``compute_effective_tier`` function is gone, along with the
``PRIORITY_TO_BASE_TIER`` constant, ``derive_base_tier_from_priority``
helper, ``TierResult`` namedtuple, and ``DEFAULT_ESCALATION_GAP``
constant. The ``base_tier`` / ``escalate_to`` fields were also removed
from the schema surface 2026-06-25 (routine-systems consolidation
Step 1); the once-deferred "Ship 5 backfill" is moot — those fields
are being stripped from the ~24 stale records, not backfilled.
``escalate_at_days`` is the sole surviving tier field (the live V2
due-window knob; see :func:`compute_auto_t1_candidates`).

Reason strings (``"due today"`` / ``"due tomorrow"`` / ``"escalate
window (Nd before due)"`` / ``"surface window (Nd before due)"``) are
stable contract surface for Ship 2's brief render + Ship D's SKILL
(SKILL quotes the strings verbatim so the talker recognises operator
replies). Change the strings here = update Ship B + Ship D in
lockstep.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from alfred.tier import slots

# Module logger. NOTE: the pure-compute predicates in this module
# (``classify_routine_item``, ``compute_auto_*``) deliberately emit NO
# logs (callers own logging — see their docstrings + the
# ``capture_logs`` no-log pins). The ONE logging call-site is
# ``compute_today_view``, the aggregation entry point, which emits a
# single "ran, here's the view" signal per ``feedback_intentionally_
# left_blank`` so an empty day is distinguishable from a broken render.
log = structlog.get_logger(__name__)


# Task statuses considered "open" — surfaced in the tier section /
# selection pool. Per dispatch ratification: blocked tasks still
# surface (operator needs to see them in the queue). Done / cancelled
# are excluded.
OPEN_STATUSES: frozenset[str] = frozenset({"todo", "active", "blocked"})

# The "moot" disposition (#103) — closed, but NOT an achievement. Named here
# because three things in this module now turn on it and a re-localized string
# comparison is how the health-status ``skip`` miss happened three times in one
# day. NOT imported from ``tier.task_cancel``: that module is the WRITER, and a
# reader taking its vocabulary from the writer would make the two agree by
# construction even if both were wrong about the schema. Both are pinned
# against ``vault.schema.STATUS_BY_TYPE["task"]``, which is the source.
CANCELLED_STATUS = "cancelled"


def coerce_due_date(value: Any) -> date | None:
    """Coerce a frontmatter ``due`` value to a ``date``.

    PyYAML parses ``due: 2026-05-28`` as a ``date`` object directly;
    the isoformat-string branch handles operator-edited records where
    the field came in as a quoted string (``due: '2026-05-28'``).
    datetime instances are normalised to their date component.

    Public API: the V2 brief render layer in
    :mod:`alfred.brief.tier_section` parses ``due`` for distance
    formatting + sort keying; a future tier-CLI surface or related
    render path has the same need. One canonical helper > N copies of
    the parser threaded through inline calls.
    """
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Routine-item tier classification — THE SINGLE SOURCE OF TRUTH
# ---------------------------------------------------------------------------
#
# Routine-systems consolidation Step 2 (2026-06-26): the T1/T2/T3
# window math used to be hand-mirrored in TWO places —
# ``_decide_tier_handoff`` (aggregator, used at 05:59 to SUPPRESS
# handed-off items from the routine section) and ``_compute_auto_routine``
# / ``compute_auto_t3_candidates`` (this module, used at 06:00 to
# SURFACE them in the tier section). The two ran identical predicates
# over the same records one minute apart, kept in sync only by
# convention + the three ``test_mirror_*`` regression pins.
#
# :func:`classify_routine_item` collapses that mirror into ONE predicate.
# Both former call sites now delegate here. The ``test_mirror_*`` pins
# stay green and now prove "both callers route through one function"
# rather than "two hand-written copies happen to agree." Per
# ``feedback_two_layer_window_math_mirror`` — the duplication dissolves
# once the decision has a single home.


@dataclass
class RoutineItemClassification:
    """The tier decision for one routine item — what both the aggregator
    (suppress-from-routine-section) and the tier render (surface) read.

    Fields:
      * ``tier`` — ``1`` / ``2`` / ``3`` for a T1/T2/T3 placement, or
        ``None`` when the item is OUTSIDE all tier windows (and so
        renders normally in the routine section). This is exactly the
        value the aggregator's ``_decide_tier_handoff`` returned.
      * ``reason`` — the canonical operator-facing reason string Ship 2's
        brief renders inline (``"due today"`` / ``"due tomorrow"`` /
        ``"escalate window (Nd before due)"`` / ``"surface window (Nd
        before due)"`` / ``"overdue by Nd (no completion this cycle)"``).
        ``None`` for a T3 placement (T3 is cadence-ranked, not
        reason-stringed) and for ``tier is None``.
      * ``effective_due`` — the due date the decision was made against
        (overdue-retention-aware: prev_due when the prior cycle lapsed
        unsatisfied). ``None`` for T3 and for ``tier is None``.
      * ``both_modes_conflict`` — ``True`` when the item carries BOTH
        ``due_pattern`` and ``target_cadence_days`` (mutually-exclusive
        semantics; ``due_pattern`` wins). The aggregator emits the
        once-per-pass ``routine.item_both_cadence_modes`` warn on this
        flag; the compute/render path reads it silently (it runs
        per-brief-fire + per-/today and would spam the log). The
        WARN-VOICING lives at the caller; the DECISION lives here.
      * ``gap_escalated`` — ``True`` iff this T1 placement came from the
        neglect-gap escalation (FUEL-ESCALATION, 2026-08-20) rather than
        a due window. The slot classifier reads it (rule 0 → Duty visit);
        the T1 surfaces read it to pick the ``auto-gap-escalated``
        source and to accept the absent ``effective_due`` (a gap
        escalation has no due date). Always ``False`` on every other
        placement.
      * ``gap_escalation_conflict`` — ``True`` when the item carries BOTH
        ``due_pattern`` and a per-item ``escalate_after_gap_days`` (a
        completion GAP is undefined semantics under cycle-based
        doneness; ``due_pattern`` wins, the gap field is ignored). The
        aggregator voices the once-per-pass
        ``routine.item_gap_escalation_with_due_pattern`` warn on this
        flag; compute/render paths read it silently — the exact
        ``both_modes_conflict`` pattern, second instance.
    """

    tier: int | None
    reason: str | None = None
    effective_due: date | None = None
    both_modes_conflict: bool = False
    gap_escalated: bool = False
    gap_escalation_conflict: bool = False


def _tier_default_values(
    tier_defaults: Any,
) -> tuple[int | None, int | None, int | None]:
    """Extract ``(default_escalate_at_days, default_surface_at_days,
    default_fuel_escalate_after_gap_days)`` from a
    ``TierDefaultsConfig``-like object (or ``None``).

    Duck-typed (``getattr``) so the compute layer doesn't import the
    routine config dataclass — ``None`` → all-``None`` (no defaults).
    """
    if tier_defaults is None:
        return None, None, None
    return (
        getattr(tier_defaults, "escalate_at_days", None),
        getattr(tier_defaults, "surface_at_days", None),
        getattr(tier_defaults, "fuel_escalate_after_gap_days", None),
    )


def classify_routine_item(
    *,
    priority: str | None,
    due_pattern: Any,
    surface_at_days: int | None,
    escalate_at_days: int | None,
    target_cadence_days: int | None,
    completion_log: dict | None,
    item_text: str,
    today: date,
    self_care: bool = False,
    default_escalate_at_days: int | None = None,
    default_surface_at_days: int | None = None,
    escalate_after_gap_days: int | None = None,
    explicit_slot: Any = None,
    default_fuel_escalate_after_gap_days: int | None = None,
) -> RoutineItemClassification:
    """Classify one routine item into a tier placement (T1/T2/T3/none).

    Field-combination table (FUEL-ESCALATION, 2026-08-20 — scope-first;
    every combination's behaviour, stated before the mechanism):

    =============================================  ==============================
    Fields on the item                             Behaviour
    =============================================  ==============================
    ``due_pattern`` + ``escalate_at_days`` (/       Deadline windows, days-BEFORE-
    ``surface_at_days``)                            due: T1 ``[0, esc]`` (negative
                                                    admitted), T2 ``(esc, surf]``.
                                                    UNTOUCHED by this extension.
    ``target_cadence_days`` only                    Soft cadence: T3 at gap >=
                                                    target; never T1/T2 by itself.
    ``warn_after_gap_days``                         Routine-section ANNOTATION
                                                    threshold only (aggregator
                                                    render); independent of every
                                                    escalation axis, both ways.
    ``escalate_after_gap_days`` (per-item)          Neglect-gap escalation on
                                                    NO-``due_pattern`` items:
                                                    gap >= M → tier 1 +
                                                    ``gap_escalated=True`` (slot
                                                    classifier rule 0 → Duty
                                                    visit). Below M → the item's
                                                    quiet behaviour, unchanged.
    config ``fuel_escalate_after_gap_days``         Default M for items whose
    (via ``default_fuel_escalate_after_gap_days``)  EXPLICIT ``slot:`` is fuel.
                                                    Per-item value always wins;
                                                    ``None`` = no default (zero
                                                    change off-instance). Never
                                                    applied to a derived slot.
    ``escalate_after_gap_days`` + ``due_pattern``   INVALID combo: a completion
                                                    gap is undefined under
                                                    cycle-based doneness.
                                                    ``due_pattern`` wins, gap
                                                    field ignored,
                                                    ``gap_escalation_conflict``
                                                    flagged (aggregator voices
                                                    the warn).
    never-completed item + gap threshold            NO escalation (a gap needs a
                                                    completion to measure from;
                                                    a freshly-added fuel item
                                                    must not open in Duty). The
                                                    cadence branch still ranks
                                                    it max-overdue in T3.
    aspirational + gap threshold                    ESCALATES. The aspirational
                                                    skip is about deadline
                                                    pressure; the gap ruling is
                                                    the operator's explicit word
                                                    that neglected fuel becomes
                                                    Duty, and fuel items are
                                                    commonly aspirational —
                                                    gating would kill the
                                                    feature on its own targets.
    =============================================  ==============================

    THE single window-math predicate for routine items. Encapsulates,
    in this order:

      1. **Aspirational skip (T1/T2 only).** A ``priority ==
         "aspirational"`` item never takes the hard-deadline T1/T2
         handoff even if it carries ``due_pattern`` + ``escalate_at_days``
         (T3 is the legitimate aspirational surface). It CAN still take
         the T3 soft-cadence handoff via ``target_cadence_days``. This
         used to live split across two layers: the aggregator gated it
         at the ``should_check_handoff`` call site; the compute path
         gated it inline in ``_compute_auto_routine``. Now it's one
         rule, enforced once.
      2. **Both-modes precedence.** ``due_pattern`` + ``target_cadence_days``
         both set → ``due_pattern`` wins; ``both_modes_conflict=True`` so
         the caller can emit the operator warn. Likewise
         ``due_pattern`` + ``escalate_after_gap_days`` →
         ``gap_escalation_conflict=True``, gap field ignored.
      3. **Neglect-gap escalation** (``due_pattern`` absent, effective
         gap threshold present): FUEL-ESCALATION, 2026-08-20. The
         effective threshold is the per-item ``escalate_after_gap_days``,
         else the config ``fuel_escalate_after_gap_days`` when the
         item's EXPLICIT ``slot:`` normalises to fuel. When the item has
         at least one completion and ``days_since >= threshold`` →
         tier 1, ``gap_escalated=True``, reason
         ``"neglected Nd (escalates at Md gap)"``. Fires BEFORE the
         self-care and cadence branches so a neglected fuel item
         escalates instead of surfacing quietly in T3; NOT gated by the
         aspirational skip (see the field table). Never-completed items
         do not escalate.
      4. **T3 self-care branch** (``due_pattern`` absent, ``self_care``
         true): the dedicated self-care lane (operator decision Q2,
         2026-06-26). An intrinsic classification — NOT deadline-driven,
         never escalates. Surfaces to T3 when NOT completed today (the
         daily self-care floor), so the operator's self-care item is
         deliberately included each day rather than skipped as "less
         necessary." Composes with ``target_cadence_days``: a self_care
         item ALSO carrying a cadence target surfaces if EITHER overdue
         against cadence OR not-done-today.
      5. **T3 soft-cadence branch** (``due_pattern`` absent,
         ``target_cadence_days`` present): surface (tier 3) when
         ``days_since_last_completed >= target_cadence_days`` (inclusive),
         OR when never completed (max overdue). Within window → no
         handoff (tier None).
      6. **T1/T2 deadline branch** (``due_pattern`` + ``escalate_at_days``
         present): completion-aware suppression
         (``completion_satisfies_current_cycle``) → tier None; else
         overdue-retention-aware effective due → T1 when
         ``days_to_due <= escalate_at_days`` (admits negative/overdue),
         T2 when ``escalate_at_days < days_to_due <= surface_at_days``.

    ``self_care`` is intrinsic to the item (an item-level classification,
    not deadline-driven). A self_care item that ALSO carries a real
    deadline (``due_pattern`` + ``escalate_at_days``) still classifies
    T1/T2 on the deadline — deadline pressure is real and wins over the
    self-care floor (the spec frames T3 as "no external deadline
    pressure"; a deadline-bearing item isn't pure self-care). The
    self-care floor only applies to non-deadline items.

    Reason strings are stable contract (the SKILL quotes them verbatim
    so the talker recognises operator replies). Change a string here =
    update Ship B render + Ship D SKILL in lockstep.

    Per ``feedback_intentionally_left_blank``: this is a pure-compute
    predicate; it emits NO log lines (callers own the "ran, here's the
    decision" log + the both-modes warn). Tests assert the no-logs
    invariant via ``capture_logs``.
    """
    # Lazy import — avoid the top-level circular hazard between
    # ``alfred.tier.compute`` and ``alfred.routine.due``.
    from alfred.routine.due import (
        completion_satisfies_current_cycle,
        overdue_effective_due,
    )

    both_modes_conflict = (
        due_pattern is not None and target_cadence_days is not None
    )
    # FUEL-ESCALATION (2026-08-20): a completion GAP is undefined semantics
    # under cycle-based doneness (completion_satisfies_current_cycle owns
    # "done" for deadline items), so a per-item gap threshold on a
    # deadline-bearing item is operator confusion. due_pattern wins; the
    # aggregator voices the warn on this flag (same pattern as
    # both_modes_conflict, one line up).
    gap_escalation_conflict = (
        due_pattern is not None and escalate_after_gap_days is not None
    )

    # ---- Aspirational skip (T1/T2 only) --------------------------
    # An aspirational item with due_pattern does NOT take the T1/T2
    # handoff (operator semantic: T3 is for self-care intentions, not
    # deadline-driven work). It still falls through to the T3 branch
    # below if it carries target_cadence_days.
    aspirational = (priority or "").lower() == "aspirational"

    # ---- Neglect-gap escalation (FUEL-ESCALATION, 2026-08-20) ----
    # The operator's model, his words: "Fuel and rhythm are equals, and
    # Duty is mostly for anything that is escalated from either of those"
    # + "Not adding that fuel three days in a row becomes a more critical
    # issue." Mechanism: on a NO-deadline item, when the completion gap
    # reaches the effective threshold, the item classifies T1 and carries
    # ``gap_escalated=True`` — the slot classifier's rule 0 then slots it
    # Duty for the day (a VISIT: overlay only, recomputed every
    # projection; the record's own slot is untouched and the item goes
    # home the moment the gap closes).
    #
    # Placed BEFORE the self-care and cadence branches on purpose: both
    # would otherwise answer T3 first and the escalation would be
    # unreachable. NOT gated by the aspirational skip — that skip is
    # about deadline pressure, and fuel items are commonly aspirational
    # (gating here would kill the feature on exactly its targets; see
    # the field table). Never-completed items never escalate: a gap
    # needs a completion to measure from, and a freshly-added fuel item
    # must not open in Duty (the cadence branch still ranks it
    # max-overdue in T3).
    if due_pattern is None:
        gap_threshold = escalate_after_gap_days
        if (
            gap_threshold is None
            and default_fuel_escalate_after_gap_days is not None
            and slots.normalize_slot(explicit_slot, warn_unrecognized=False)
            == slots.SLOT_FUEL
        ):
            # The config default reaches ONLY items the operator
            # explicitly marked ``slot: fuel`` — never a derived slot
            # (a guess must not conscript an item into escalation).
            # Quiet normalisation: this predicate is contractually
            # no-log; the SAME raw value is read loudly by
            # ``classify_slot`` (rule 1) in the same projection, so a
            # typo still warns exactly once.
            gap_threshold = default_fuel_escalate_after_gap_days
        if isinstance(gap_threshold, int) and gap_threshold > 0:
            log_dict = (
                completion_log if isinstance(completion_log, dict) else {}
            )
            gap_completion_dates = _parse_item_completion_dates(
                log_dict.get(item_text, [])
            )
            if gap_completion_dates:
                days_since = (today - max(gap_completion_dates)).days
                if days_since < 0:
                    # Future-dated completion (operator hand-edit) → clamp,
                    # mirroring the cadence branch below.
                    days_since = 0
                if days_since >= gap_threshold:
                    return RoutineItemClassification(
                        tier=1,
                        # Stable contract string (new member of the reason
                        # vocabulary): change here = update the brief render
                        # + SKILL quoting in lockstep, per the module rule.
                        reason=(
                            f"neglected {days_since}d "
                            f"(escalates at {gap_threshold}d gap)"
                        ),
                        both_modes_conflict=both_modes_conflict,
                        gap_escalated=True,
                    )

    # ---- T3 self-care branch (Q2, 2026-06-26) --------------------
    # The dedicated self-care lane: a non-deadline item flagged
    # ``self_care: true`` surfaces to T3 when not completed today, so
    # it's deliberately included in the day rather than skipped. Fires
    # before the soft-cadence branch so a self_care item without a
    # cadence target still surfaces (the daily floor). A self_care item
    # WITH a cadence target also surfaces here if not-done-today even
    # when within its cadence window (self-care broadens, never narrows).
    if due_pattern is None and self_care:
        log_dict = (
            completion_log if isinstance(completion_log, dict) else {}
        )
        completion_dates = _parse_item_completion_dates(
            log_dict.get(item_text, [])
        )
        if today not in completion_dates:
            # Not done today → surface in the self-care lane.
            return RoutineItemClassification(
                tier=3, both_modes_conflict=both_modes_conflict,
            )
        # Done today: fall through to the cadence branch (a cadence
        # target may still surface it as overdue against a longer
        # window); otherwise it renders in the routine section.

    # ---- T3 soft-cadence branch ----------------------------------
    # Fires when due_pattern is absent (both-modes precedence: due_pattern
    # wins when both set). Predicate: days_since >= target (inclusive).
    if due_pattern is None and target_cadence_days is not None:
        if (
            not isinstance(target_cadence_days, int)
            or target_cadence_days <= 0
        ):
            # Defensive: zero/negative target → undefined semantics →
            # no handoff. Item renders in the routine section.
            return RoutineItemClassification(
                tier=None, both_modes_conflict=both_modes_conflict,
            )
        log_dict = (
            completion_log if isinstance(completion_log, dict) else {}
        )
        completion_dates = _parse_item_completion_dates(
            log_dict.get(item_text, [])
        )
        if not completion_dates:
            # Never completed → max overdue → SURFACE in T3.
            return RoutineItemClassification(
                tier=3, both_modes_conflict=both_modes_conflict,
            )
        days_since = (today - max(completion_dates)).days
        if days_since < 0:
            # Future-dated completion (operator hand-edit) → clamp.
            days_since = 0
        if days_since >= target_cadence_days:
            return RoutineItemClassification(
                tier=3, both_modes_conflict=both_modes_conflict,
            )
        # Within soft cadence window → render in routine section.
        return RoutineItemClassification(
            tier=None, both_modes_conflict=both_modes_conflict,
        )

    # ---- Global default resolution (Q3 Option A, 2026-06-26) ------
    # The spec's "global default + per-item override" for the tier
    # windows, WITHOUT breaking the load-bearing opt-out: an item that
    # has a ``due_pattern`` but NEITHER tier field is opted-OUT
    # (the Walk-Fergus "absent = never auto-tier" shape) — defaults do
    # NOT apply, it stays opted-out. Defaults apply ONLY to an item that
    # has ALREADY opted in (has ≥1 of escalate_at_days / surface_at_days)
    # but omits the SPECIFIC field. A per-item value always wins (it's
    # only substituted when the per-item value is None). This yields ZERO
    # behaviour change for existing records (which carry an explicit
    # escalate_at_days when they tier, or neither field when they don't).
    has_tier_signal = (
        due_pattern is not None
        and (escalate_at_days is not None or surface_at_days is not None)
    )
    if has_tier_signal:
        if escalate_at_days is None and default_escalate_at_days is not None:
            escalate_at_days = default_escalate_at_days
        if surface_at_days is None and default_surface_at_days is not None:
            surface_at_days = default_surface_at_days

    # ---- T1/T2 deadline branch -----------------------------------
    # From here down every return threads ``gap_escalation_conflict``:
    # the flag requires ``due_pattern``, so the T3-region returns above
    # provably carry its default False, while any deadline-region exit
    # may be the one the aggregator reads the warn flag from.
    if due_pattern is None or escalate_at_days is None:
        return RoutineItemClassification(
            tier=None, both_modes_conflict=both_modes_conflict,
            gap_escalation_conflict=gap_escalation_conflict,
        )
    if aspirational:
        # Aspirational + deadline-bearing: never T1/T2. (No T3 either —
        # the T3 branch above only fires for target_cadence_days items;
        # an aspirational due_pattern item just renders in the routine
        # section.)
        return RoutineItemClassification(
            tier=None, both_modes_conflict=both_modes_conflict,
            gap_escalation_conflict=gap_escalation_conflict,
        )

    # Phase 2C C1 completion-aware suppression: a completion covering
    # the current/upcoming cycle (nearest-cycle ±half-cycle heuristic)
    # → no handoff; the routine section's "*(done this cycle)*"
    # annotation is the right surface.
    if completion_satisfies_current_cycle(
        item_text, completion_log, due_pattern, today,
    ):
        return RoutineItemClassification(
            tier=None, both_modes_conflict=both_modes_conflict,
            gap_escalation_conflict=gap_escalation_conflict,
        )

    # Phase 2C C1 overdue retention: effective_due = prev_due when the
    # prior cycle lapsed unsatisfied → days_to_due negative → T1 admits.
    effective_due = overdue_effective_due(
        due_pattern, completion_log, item_text, today,
    )
    if effective_due is None:
        return RoutineItemClassification(
            tier=None, both_modes_conflict=both_modes_conflict,
            gap_escalation_conflict=gap_escalation_conflict,
        )
    days_to_due = (effective_due - today).days

    if days_to_due <= escalate_at_days:
        if days_to_due < 0:
            reason = (
                f"overdue by {abs(days_to_due)}d "
                f"(no completion this cycle)"
            )
        elif days_to_due == 0:
            reason = "due today"
        elif days_to_due == 1:
            reason = "due tomorrow"
        else:
            reason = f"escalate window ({escalate_at_days}d before due)"
        return RoutineItemClassification(
            tier=1,
            reason=reason,
            effective_due=effective_due,
            both_modes_conflict=both_modes_conflict,
            gap_escalation_conflict=gap_escalation_conflict,
        )
    if (
        surface_at_days is not None
        and surface_at_days > escalate_at_days
        and escalate_at_days < days_to_due <= surface_at_days
    ):
        return RoutineItemClassification(
            tier=2,
            reason=f"surface window ({days_to_due}d before due)",
            effective_due=effective_due,
            both_modes_conflict=both_modes_conflict,
            gap_escalation_conflict=gap_escalation_conflict,
        )
    return RoutineItemClassification(
        tier=None, both_modes_conflict=both_modes_conflict,
        gap_escalation_conflict=gap_escalation_conflict,
    )


# ---------------------------------------------------------------------------
# Tier-V2 surface — auto-T1 candidate discovery
# ---------------------------------------------------------------------------


@dataclass
class AutoT1Candidate:
    """One auto-surfaced T1 (or T2 ramp) candidate this morning.

    ``path`` is the vault-relative path — Ship 2's brief uses this
    to construct the wikilink. For tasks: ``"task/RRTS Payroll.md"``;
    for routine items: ``"routine/Recurring Bills + Admin.md"``
    (the routine record itself; the item is identified by the
    ``item_text`` field).

    ``name`` is the operator-facing display string. For tasks: the
    task record's ``name`` (or file stem). For routine items: the
    item's ``text`` field (e.g. ``"Pay Clinic Rental ..."``).

    ``due_iso`` is the deadline as an ISO date string (always present
    — a candidate without a resolvable due date wouldn't have
    triggered the auto-surface).

    ``surface_reason`` is the canonical reason string Ship 2 renders
    inline:

      * ``"due today"``
      * ``"due tomorrow"``
      * ``"escalate window (Nd before due)"`` — N is the
        ``escalate_at_days`` value (e.g. ``"escalate window (3d
        before due)"`` for a task with ``escalate_at_days: 3``).
      * ``"surface window (Nd before due)"`` — Phase 2A Ship A T2
        ramp. N is the days-to-due when the item entered the T2
        window (NOT the ``surface_at_days`` value — the latter is
        the window's outer bound, the former is "how close are we
        right now").

    Phase 2A Ship A discriminated-union fields:

      * ``origin`` — ``"task"`` (default, backward-compatible) or
        ``"routine"``. Ship B's brief uses this to pick the right
        wikilink + item-text rendering path.
      * ``routine_record`` — populated only when ``origin == "routine"``;
        the routine record's name (e.g. ``"Recurring Bills + Admin"``).
        Allows the brief to render ``[[routine/<record>]]`` and the
        item text together.
      * ``item_text`` — populated only when ``origin == "routine"``;
        the item's ``text`` field. The brief renders this as the
        operator-facing line item (the routine record name + item
        text together identify a specific completion target).

    Reason strings are stable contract per the module docstring.
    Ship B + Ship D both depend on these field names — rename here
    = update both in lockstep.
    """

    path: str
    name: str
    due_iso: str
    surface_reason: str
    origin: str = "task"
    routine_record: str | None = None
    item_text: str | None = None
    # --- slot-classifier inputs (#18 slice 1) --------------------------------
    # Carried so the slot classifier reads OPERATOR INTENT rather than
    # re-deriving it from surface shape. Each is a field the operator set by
    # hand; the classifier is a reader, not an opinion.
    #
    # ``self_care`` is the one that must not be dropped: this dataclass already
    # backs the self-care lanes, and a self_care item outranks every structural
    # rule (self_care → Fuel beats cadence → Rhythm). Defaults keep every
    # existing construction site valid per the dataclass-default extension
    # contract.
    self_care: bool = False
    explicit_slot: str | None = None
    has_due_pattern: bool = False
    # --- backdated completion (2026-08-20) -----------------------------------
    # How many days BACK a "previously done" completion may honestly reach for
    # this item — ``(today - window_start).days`` over
    # ``routine.recurrence.backdate_credit_window`` (±half-cycle around the
    # item's current effective due), computed HERE because this scan is the
    # one place the pattern + completion_log + today are all in hand. 0 =
    # no admissible backdate (no due_pattern, mid-window, paid cycle). The
    # producer stamps it into slot evidence; the serve side filters the
    # ``done_Nd`` rungs by it so no offered rung can refuse when pressed.
    backdate_limit_days: int = 0
    # --- neglect-gap escalation (FUEL-ESCALATION, 2026-08-20) ----------------
    # ``True`` iff this T1 candidacy came from ``classify_routine_item``'s
    # gap branch (no due date — ``due_iso`` is ""). Threaded onto the
    # TierEntry so the slot classifier's rule 0 sends the item visiting
    # Duty and the view stamps the ``auto-gap-escalated`` source.
    gap_escalated: bool = False


def compute_auto_t1_candidates(
    vault_path: Path, now: datetime,
) -> list[AutoT1Candidate]:
    """Walk ``vault/task/*.md`` and return tasks auto-surfacing as T1.

    Filter logic (in this exact order — short-circuits on first
    rejection):

      1. Frontmatter parse failure → skip silently. Ship 2's brief
         renders parse failures separately; this compute path is
         "what auto-surfaces" and a broken record can't.
      2. ``type != "task"`` → skip. Defensive against stray files.
      3. ``status`` NOT in :data:`OPEN_STATUSES` → skip. Done /
         cancelled tasks aren't tier-rankable.
      4. ``alfred_triage is True`` → skip. Janitor triage records go
         to the Daily Sync Triage Queue (Ship 3 section provider),
         not the tier section. Per the operator-stated semantics
         2026-05-29.
      5. ``due`` missing or unparseable → skip. No deadline → can't
         auto-surface.
      6. ``due`` is today → surface with reason ``"due today"``.
      7. ``due`` is tomorrow → surface with reason ``"due tomorrow"``.
      8. ``due`` is more than 1 day out BUT inside the
         ``escalate_at_days`` window → surface with reason
         ``"escalate window (Nd before due)"``.
      9. Otherwise → skip (deadline too far out).

    ``now`` is the caller-supplied reference instant. The function
    uses only ``now.date()`` for date math; the time component is
    irrelevant here (the brief daemon passes ``datetime.now(tz)``).

    Returns the candidate list sorted by ``due_iso`` ascending then
    by ``name`` — deterministic order so Ship 2's brief render stays
    stable across consecutive aggregator runs on the same morning.

    Per ``feedback_intentionally_left_blank``: this function emits no
    log lines itself (compute path is pure); each call-site that
    uses the result is responsible for the "ran, here's the count"
    log. Tests assert the no-logs invariant via ``capture_logs``.
    """
    import frontmatter  # type: ignore[import-untyped]

    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        return []

    today_local = now.date()
    tomorrow_local = today_local + timedelta(days=1)

    candidates: list[AutoT1Candidate] = []
    for path in sorted(task_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "task":
            continue
        status = str(fm.get("status") or "todo").lower()
        if status not in OPEN_STATUSES:
            continue
        if fm.get("alfred_triage") is True:
            continue
        due = coerce_due_date(fm.get("due"))
        if due is None:
            continue

        reason: str | None = None
        if due < today_local:
            # OVERDUE RETENTION — the asymmetry this closes. Every other
            # branch below asks "is the deadline near?", and a deadline in
            # the PAST answers no to all of them: not today, not tomorrow,
            # and ``2 <= days_to_due`` is false for a negative number. So an
            # open task fell to ``reason = None`` and vanished from the
            # producer THE DAY AFTER its due date — it surfaced while there
            # was still time to do it, then went quiet once it was late.
            #
            # The routine branch has had this retention since Phase 2C C1
            # (``classify_routine_item`` via ``overdue_effective_due``);
            # tasks were the half that never got it. Same shape, minus the
            # cycle-completion clause, which is routine-only.
            #
            # Made "Submit Documents to Clutch" (accepted, never done)
            # invisible, and set up the Pete Tong auto-retirement.
            reason = f"overdue by {(today_local - due).days}d"
        elif due == today_local:
            reason = "due today"
        elif due == tomorrow_local:
            reason = "due tomorrow"
        else:
            # Check the escalate_at_days window.
            escalate_at_days_raw = fm.get("escalate_at_days")
            try:
                escalate_at_days = (
                    int(escalate_at_days_raw)
                    if escalate_at_days_raw is not None
                    else None
                )
            except (TypeError, ValueError):
                escalate_at_days = None
            if escalate_at_days is not None and escalate_at_days > 0:
                days_to_due = (due - today_local).days
                # The 0-day + 1-day cases were caught above. The
                # escalate window is "more than 1 day but within the
                # window" — gate on ``2 <= days_to_due <=
                # escalate_at_days``. (A task ``escalate_at_days: 1``
                # is already covered by the tomorrow-branch; only
                # ``escalate_at_days >= 2`` produces NEW surfacings
                # here.)
                if 2 <= days_to_due <= escalate_at_days:
                    reason = (
                        f"escalate window ({escalate_at_days}d before due)"
                    )

        if reason is None:
            continue

        name = str(fm.get("name") or path.stem)
        rel_path = f"task/{path.name}"
        from alfred.routine.config import _coerce_self_care

        candidates.append(AutoT1Candidate(
            path=rel_path,
            name=name,
            due_iso=due.isoformat(),
            surface_reason=reason,
            # A self_care task with a NEAR deadline surfaces in T1 rather than
            # the self-care lane, so this branch is the only place that signal
            # survives for such a task. Under the ratified precedence
            # (rule 3 > rule 6) it is Fuel that happens to be dated, not a Duty
            # — which is the whole point of slot being orthogonal to tier.
            # Shared coercion helper, no fourth reader of the raw field.
            self_care=_coerce_self_care(fm.get("self_care", False)),
            explicit_slot=(
                str(fm["slot"]).strip() if fm.get("slot") is not None else None
            ),
        ))

    candidates.sort(key=lambda c: (c.due_iso, c.name.lower()))
    return candidates


# ---------------------------------------------------------------------------
# Phase 2A Ship A — auto-surface for routine items with due_pattern
# ---------------------------------------------------------------------------
#
# Routine items can carry a recurring deadline via ``due_pattern`` +
# ``escalate_at_days`` + (optionally) ``surface_at_days``. The two
# functions below scan ``vault/routine/*.md``, iterate each record's
# items, resolve the next due date via :func:`alfred.routine.due.
# resolve_due_date`, and emit AutoT1Candidates for items inside the
# respective T1 or T2 window.
#
# Window math (operator-stated, Plan-ratified):
#   T1 window: ``[0, escalate_at_days]`` (inclusive)
#   T2 window: ``(escalate_at_days, surface_at_days]`` (strict-above
#              escalate, inclusive of surface) — only when
#              ``surface_at_days > escalate_at_days``.
#
# The Pay-Clinic-Rental shape (surface_at_days=5, escalate_at_days=0,
# monthly day=1) yields:
#   * days_to_due = 0  → T1 ("due today")
#   * days_to_due 1..5 → T2 ("surface window (Nd before due)")
#   * days_to_due > 5  → no surface
#
# The Garbage-Day shape (escalate_at_days=1, weekly day=thu) yields:
#   * days_to_due = 0 (Thu) → T1 ("due today")
#   * days_to_due = 1 (Wed) → T1 ("due tomorrow")
#   * days_to_due > 1       → no surface (no T2 ramp configured)
#
# Items with no ``escalate_at_days`` (Walk Fergus, daily-routine
# items) NEVER auto-surface in tier; they live in the routines
# section of the brief.


def compute_auto_routine_candidates(
    vault_path: Path, now: datetime,
    tier_defaults: Any = None,
) -> list[AutoT1Candidate]:
    """Walk ``vault/routine/*.md`` and return routine items
    auto-surfacing in the T1 window.

    Filter logic (in this exact order — short-circuits on first
    rejection per item):

      1. Frontmatter parse failure → skip the whole record silently.
      2. ``type != "routine"`` → skip (defensive against stray files).
      3. ``status`` archived → skip the whole record (the routine is
         retired; items don't surface).
      4. ``alfred_triage is True`` on the routine record → skip
         (defense-in-depth — routines shouldn't be triage-flagged
         in practice, but mirror the task-path defensive filter).
      5. For each item in ``items``:
         a. Item missing ``due_pattern`` → skip (not deadline-bearing).
         b. Item missing ``escalate_at_days`` → skip (Walk-Fergus
            shape: surface-by-cadence in routines section, never
            auto-tier).
         c. ``resolve_due_date`` returns None → skip (malformed
            pattern; log already emitted by resolver).
         d. ``days_to_due`` not in T1 window ``[0,
            escalate_at_days]`` → skip.
         e. ``is_done_in_current_cycle`` → skip (operator has
            already completed this cycle's instance).

    Returns AutoT1Candidates with ``origin="routine"``,
    ``routine_record`` + ``item_text`` populated, ``path`` set to
    the routine record's vault-relative path.

    Sorted by ``due_iso`` ascending then ``name`` (item text)
    case-insensitive — deterministic order for Ship B's brief
    render.

    ``tier_defaults`` (Q3 Option A): optional global window defaults
    (a ``TierDefaultsConfig`` or ``None``) applied to an item that has
    a ``due_pattern`` + ≥1 tier field but omits the specific field. See
    :func:`classify_routine_item`.

    Per ``feedback_intentionally_left_blank``: pure-compute path,
    no log emissions. Callers (Ship B brief render) emit the
    "ran, here's the count" log.
    """
    return _compute_auto_routine(
        vault_path, now, window="t1", tier_defaults=tier_defaults,
    )


def compute_auto_routine_t2_candidates(
    vault_path: Path, now: datetime,
    tier_defaults: Any = None,
) -> list[AutoT1Candidate]:
    """Walk ``vault/routine/*.md`` and return routine items
    auto-surfacing in the T2 ramp window.

    Same filter logic as :func:`compute_auto_routine_candidates`,
    but the window check is:

      ``escalate_at_days < days_to_due <= surface_at_days``

    AND the item must satisfy ``surface_at_days > escalate_at_days``
    (otherwise it's a T1-only item per ratified semantics).

    Reason string: ``"surface window (Nd before due)"`` where N is
    the current ``days_to_due`` value (NOT ``surface_at_days``).
    The brief renders this so the operator sees how close the
    deadline is right now, not the window's outer bound.

    Returns AutoT1Candidates with the same shape as
    :func:`compute_auto_routine_candidates`. The discriminator is
    ``surface_reason`` (``"surface window ..."`` vs
    ``"escalate window ..."`` / ``"due today"`` / ``"due tomorrow"``).

    ``tier_defaults`` (Q3 Option A): see
    :func:`compute_auto_routine_candidates`.
    """
    return _compute_auto_routine(
        vault_path, now, window="t2", tier_defaults=tier_defaults,
    )


def _compute_auto_routine(
    vault_path: Path, now: datetime, *, window: str,
    tier_defaults: Any = None,
) -> list[AutoT1Candidate]:
    """Shared scan + filter for T1 / T2 routine surfaces.

    ``window`` is ``"t1"`` or ``"t2"`` — selects the days-to-due
    window check + reason-string format.

    Implementation note: split into a private helper rather than
    inlining in each public function so the routine-record scan +
    item filter + completion-cycle check stay in one place. Tests
    invoke the public functions; future ships that need a new
    surface (e.g. "next-week preview") would add a third public
    function reusing the same scan.
    """
    import frontmatter  # type: ignore[import-untyped]

    # Lazy import — avoid the top-level circular hazard between
    # ``alfred.tier.compute`` and ``alfred.routine.config``. The
    # window-math helpers (``completion_satisfies_current_cycle`` /
    # ``overdue_effective_due``) are no longer imported here — they live
    # inside ``classify_routine_item`` now (Step 2 single-source
    # collapse). This scan only needs ``Item`` to parse the raw items.
    from alfred.routine.config import Item

    routine_dir = vault_path / "routine"
    if not routine_dir.is_dir():
        return []

    today_local = now.date()

    candidates: list[AutoT1Candidate] = []
    for record_path in sorted(routine_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(record_path))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "routine":
            continue
        # Status filter — archived routines don't surface. Other
        # statuses (active, or unset which defaults to active) are
        # in-scope.
        status = str(fm.get("status") or "active").lower()
        if status == "archived":
            continue
        if fm.get("alfred_triage") is True:
            continue
        record_name = str(fm.get("name") or record_path.stem)
        raw_items = fm.get("items") or []
        if not isinstance(raw_items, list):
            continue

        # Completion log: dict mapping item text → list of date
        # values (operator's hand-edits sometimes use ISO strings;
        # the parser normalises both).
        completion_log = fm.get("completion_log") or {}
        if not isinstance(completion_log, dict):
            completion_log = {}

        rel_path = f"routine/{record_path.name}"

        for raw_item in raw_items:
            item = Item.from_dict(raw_item)
            if item is None:
                continue

            # Single source of truth (Step 2, 2026-06-26). The
            # aspirational skip, the completion-aware suppression, the
            # overdue-retention effective-due, and the T1/T2 window math
            # all live in ``classify_routine_item`` — the SAME predicate
            # the aggregator's ``_decide_tier_handoff`` now delegates to.
            # No hand-mirrored copy here; the two layers cannot drift.
            _def_esc, _def_surf, _def_fuel_gap = _tier_default_values(
                tier_defaults,
            )
            classification = classify_routine_item(
                priority=item.priority,
                due_pattern=item.due_pattern,
                surface_at_days=item.surface_at_days,
                escalate_at_days=item.escalate_at_days,
                target_cadence_days=item.target_cadence_days,
                completion_log=completion_log,
                item_text=item.text,
                today=today_local,
                self_care=item.self_care,
                default_escalate_at_days=_def_esc,
                default_surface_at_days=_def_surf,
                escalate_after_gap_days=item.escalate_after_gap_days,
                explicit_slot=item.slot,
                default_fuel_escalate_after_gap_days=_def_fuel_gap,
            )

            want_tier = 1 if window == "t1" else 2
            if classification.tier != want_tier:
                continue
            # T1/T2 classifications always carry a reason; a due date is
            # guaranteed except for the one T1 shape that HAS none — the
            # neglect-gap escalation (FUEL-ESCALATION, 2026-08-20), which
            # surfaces with ``due_iso=""`` like the self-care lane.
            assert classification.reason is not None
            assert (
                classification.effective_due is not None
                or classification.gap_escalated
            )

            # Backdated-completion depth (2026-08-20): the credit window's
            # reach, from the SAME recurrence arithmetic the classifier
            # credits by (one owner — alfred.routine.recurrence). Lazy import
            # beside ``Item`` for the same circular-hazard reason. A
            # gap-escalated item has no due_pattern → window None → 0
            # (measured: ``backdate_credit_window(None, ...) is None``).
            from alfred.routine.due import backdate_credit_window

            credit_window = backdate_credit_window(
                item.due_pattern, completion_log, item.text, today_local,
            )
            backdate_limit = (
                (today_local - credit_window[0]).days if credit_window else 0
            )

            candidates.append(AutoT1Candidate(
                path=rel_path,
                name=item.text,
                due_iso=(
                    classification.effective_due.isoformat()
                    if classification.effective_due is not None
                    else ""
                ),
                surface_reason=classification.reason,
                origin="routine",
                routine_record=record_name,
                item_text=item.text,
                # Slot inputs — read the item's own fields, never the lane it
                # landed in. This is the branch where ``has_due_pattern`` is
                # genuinely True, which is what makes rule 4 (Duty) fire.
                self_care=item.self_care,
                explicit_slot=item.slot,
                has_due_pattern=item.due_pattern is not None,
                backdate_limit_days=backdate_limit,
                gap_escalated=classification.gap_escalated,
            ))

    candidates.sort(key=lambda c: (c.due_iso, c.name.lower()))
    return candidates


def _parse_item_completion_dates(raw: Any) -> list[date]:
    """Parse a completion_log entry value into a list of dates.

    Operator YAML carries completion_log values as ISO strings OR
    date objects depending on whether PyYAML's date parser fired.
    Both forms accepted. Malformed entries silently dropped (the
    cycle check just sees a shorter list).
    """
    if not isinstance(raw, list):
        return []
    out: list[date] = []
    for v in raw:
        if isinstance(v, datetime):
            out.append(v.date())
            continue
        if isinstance(v, date):
            out.append(v)
            continue
        if isinstance(v, str):
            try:
                out.append(date.fromisoformat(v.strip()[:10]))
                continue
            except ValueError:
                pass
        # Silently skip — defensive against operator hand-edit
        # corruption.
    return out


def _cadence_metadata(
    completion_dates: list[date], *, target: int, today: date,
) -> tuple[int | None, float]:
    """``(days_since_last_completed, overdue_ratio)`` for a soft-cadence item.

    ONE owner for the pair, lifted here the moment a SECOND consumer appeared
    (``_hydrate_curated_entries``' routine branch) rather than after the two
    spellings had a chance to disagree. The auto-T3 surface and the hydrated
    curated copy of the SAME item must produce the same two numbers, or the
    brief annotates an accepted item differently from the card that was
    accepted.

    Never-completed is ``(None, inf)`` — max overdue, and the render's "never
    done" wording (``T3_AUTO_DAYS_SINCE_NEVER_LABEL``) is TRUE for exactly that
    shape and false for every other. A future-dated completion (operator
    hand-edit) clamps to 0, matching ``classify_routine_item``'s own clamp so
    the ratio and the surface predicate agree.

    ``target`` is required to be a positive int by both callers (the classifier
    rejects zero/negative before either reaches here).
    """
    if not completion_dates:
        return None, float("inf")
    days_since = (today - max(completion_dates)).days
    if days_since < 0:
        days_since = 0
    return days_since, days_since / target


# ---------------------------------------------------------------------------
# Phase 2A-soft-cadence — auto-T3 surface for routine items with
# ``target_cadence_days`` (2026-05-30)
# ---------------------------------------------------------------------------
#
# The T3 self-care surface is NOT deadline-driven (unlike T1/T2 which scan
# ``due_pattern`` + tier windows). Instead, it ranks routine items by
# **days-since-last-completed** vs a soft cadence target carried on the
# item itself (``target_cadence_days``). Operator framing: "walk the dog
# at least every 3 days" — surface in T3 when overdue.
#
# Auto-T3 criteria:
#   * Item carries ``target_cadence_days: <int>``.
#   * Item does NOT carry ``due_pattern`` (mutually exclusive; the
#     deadline-bearing item's already handled by T1/T2 surfaces). If
#     BOTH are set on the same item, the routine aggregator's
#     ``_decide_tier_handoff`` emits a warn log + prefers ``due_pattern``;
#     the auto-T3 compute path defensively skips items with both set
#     so the precedence-rule outcome is identical regardless of which
#     consumer reads first. The aggregator owns the operator-facing
#     warn; this compute layer just enforces the same precedence
#     silently (no log spam from the compute path which runs every
#     /today + every brief fire).
#   * Days-since-last-completed (from ``completion_log[item.text]``,
#     parsed via :func:`_parse_item_completion_dates`) is GREATER than
#     OR EQUAL to ``target_cadence_days`` — the threshold is INCLUSIVE
#     at the boundary (a 3-day target with the last completion 3 days
#     ago is overdue per the operator-stated "at least every Nd"
#     framing; "at least every 3 days" includes the 3rd day as the
#     when-you-should-have-done-it boundary).
#   * Never-completed items (``completion_log[text]`` missing or empty)
#     are treated as MAXIMUM overdue — they rank first in the
#     sort-by-overdue-ratio output. Operator hasn't started yet; the
#     item should surface most prominently.
#
# Single source of truth (Step 2, 2026-06-26): the T3 surface predicate
# (``days_since >= target_cadence_days``, never-completed = max overdue)
# lives in :func:`classify_routine_item`. Both this surface (which reads
# ``tier == 3`` to emit the candidate) AND the aggregator's
# ``_decide_tier_handoff`` (which reads the same value to SUPPRESS the
# routine-section render) delegate to that one function — the two
# outcomes are two reads of one decision, not a hand-mirror. The
# ``overdue_ratio`` / ``days_since_last_completed`` SORT metadata below
# is computed here (it isn't part of the handoff decision). Per
# ``feedback_two_layer_window_math_mirror`` — the regression-pin lives
# in ``tests/tier/test_compute.py``
# (``test_mirror_decide_tier_handoff_t3_matches_compute_auto_t3``), now
# proving "both callers route through one function."
#
# Companion talker grammar (``T3 confirm <item>`` + voice-completion
# of soft-cadence items) shipped 2026-05-30 (Phase 2B B1) — the
# ``routine_done`` tool path in :mod:`alfred.telegram.conversation`.
# The operator-facing ILB acknowledgement that used to surface in the
# brief (:data:`alfred.brief.tier_section.T3_AUTO_TALKER_DEFERRED_NOTE`)
# was retired in that same ship; the constant is preserved for
# backwards-compat but the brief render loop no longer emits it.


@dataclass
class AutoT3Candidate:
    """One auto-suggested T3 (self-care) candidate this morning.

    Discriminated-union sibling to :class:`AutoT1Candidate`. The two
    aren't unified because the surface semantics differ:
      * AutoT1Candidate: deadline-driven (``due_iso`` + ``surface_reason``
        anchored to a due date).
      * AutoT3Candidate: cadence-driven (``days_since_last_completed``
        + ``overdue_ratio`` ranked against a soft target).

    Fields:
      * ``path`` — routine record vault-relative path (e.g.
        ``"routine/Self Care.md"``).
      * ``routine_record`` — routine record's name (e.g.
        ``"Self Care"``); brief renders the wikilink
        ``[[routine/<record>]]``.
      * ``item_text`` — operator-facing line (e.g. ``"Walk Fergus"``).
      * ``target_cadence_days`` — the soft cadence target carried on
        the item (e.g. ``3`` for "every 3 days").
      * ``days_since_last_completed`` — int days since most-recent
        completion log entry; ``None`` when the completion log is
        empty for this item (never completed).
      * ``overdue_ratio`` — ``days_since / target_cadence_days`` when
        completed at least once; ``float('inf')`` when never
        completed. Used purely for sort-order (descending — most
        overdue surfaces first).

    Brief renders this via :func:`alfred.brief.tier_section.
    _render_auto_t3_routine_entry`. Talker recognition pattern
    (``T3 confirm <item>``) shipped 2026-05-30 (Phase 2B B1).

    Cross-Ship contract: field names are stable; rename = update
    brief render + Phase 2B SKILL in lockstep.
    """

    path: str
    routine_record: str
    item_text: str
    target_cadence_days: int
    days_since_last_completed: int | None
    overdue_ratio: float
    # --- slot-classifier inputs (#18 slice 1) --------------------------------
    # THE non-obvious plumbing cost the design flagged, located precisely.
    # ``compute_auto_t3_candidates`` already READS ``item.self_care`` (it passes
    # it to ``classify_routine_item``) and then discards it — so a self-care
    # item that ALSO carries ``target_cadence_days`` arrives at the classifier
    # indistinguishable from a plain cadence practice, and would classify Rhythm
    # when the ratified precedence says Fuel.
    #
    # That is the one gap in this arc that produces a WRONG answer rather than
    # an honest ``unslotted``, and it lands on exactly the category the feature
    # exists to protect (restorative activity). Hence the field.
    self_care: bool = False
    explicit_slot: str | None = None


def compute_auto_t3_candidates(
    vault_path: Path, now: datetime,
    tier_defaults: Any = None,
) -> list[AutoT3Candidate]:
    """Walk ``vault/routine/*.md`` and return routine items
    auto-suggesting as T3 candidates (overdue against their soft
    cadence target).

    ``tier_defaults`` (FUEL-ESCALATION, 2026-08-20): the SAME defaults
    bundle the T1/T2 surfaces read, threaded here so this surface sees
    the SAME classification. A fuel item at/past its escalation gap
    classifies tier 1 in the shared predicate, and this surface's
    ``tier != 3`` gate then excludes it — without the threading it
    would classify 3 HERE and 1 THERE, double-rendering the item and
    breaking the exactly-one-lane invariant. (The due-window defaults
    in the bundle remain inert on this surface: the cadence branch
    never reads them.)

    Filter logic (in this exact order — short-circuits on first
    rejection per item):

      1. Frontmatter parse failure → skip the whole record silently
         (mirrors the T1/T2 routine scan).
      2. ``type != "routine"`` → skip (defensive against stray files).
      3. ``status`` archived → skip the whole record.
      4. ``alfred_triage is True`` on the routine record → skip
         (defense-in-depth — mirror of the T1/T2 scan).
      5. For each item in ``items``:
         a. Item missing ``target_cadence_days`` → skip (not a
            soft-cadence item; T1/T2 handle deadline-bearing items).
         b. Item ALSO carries ``due_pattern`` → skip (precedence
            rule: the aggregator's warn-log path owns the operator-
            facing signal; this compute path defensively enforces the
            same outcome). Mutually-exclusive semantics per
            :class:`alfred.routine.config.Item`.
         c. ``target_cadence_days`` is not a positive int → skip
            (zero / negative would produce undefined overdue
            semantics; defensive against operator hand-edit).
         d. Resolve days_since_last_completed:
              - completion_log empty / missing key → ``None`` →
                overdue_ratio = ``inf`` → SURFACE (treat as max
                overdue; never-completed items rank first).
              - completion_log populated → max(parsed dates) → today
                delta in days → if ``days_since >= target`` →
                SURFACE; otherwise SKIP (item is within its soft
                cadence window).

    Returns AutoT3Candidates sorted by ``overdue_ratio`` DESCENDING
    (most overdue first), ties broken by ``item_text`` case-
    insensitive ascending. ``float('inf')`` ranks above any finite
    ratio so never-completed items always lead.

    Per ``feedback_intentionally_left_blank``: pure-compute path, no
    log emissions. Callers (brief render layer) emit the "ran, here's
    the count" log.
    """
    import frontmatter  # type: ignore[import-untyped]

    # Lazy imports to avoid the top-level circular hazard between
    # ``alfred.tier.compute`` and ``alfred.routine.config``. Mirrors
    # the pattern in ``_compute_auto_routine``.
    from alfred.routine.config import Item

    routine_dir = vault_path / "routine"
    if not routine_dir.is_dir():
        return []

    today_local = now.date()

    candidates: list[AutoT3Candidate] = []
    for record_path in sorted(routine_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(record_path))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "routine":
            continue
        status = str(fm.get("status") or "active").lower()
        if status == "archived":
            continue
        if fm.get("alfred_triage") is True:
            continue
        record_name = str(fm.get("name") or record_path.stem)
        raw_items = fm.get("items") or []
        if not isinstance(raw_items, list):
            continue

        completion_log = fm.get("completion_log") or {}
        if not isinstance(completion_log, dict):
            completion_log = {}

        rel_path = f"routine/{record_path.name}"

        for raw_item in raw_items:
            item = Item.from_dict(raw_item)
            if item is None:
                continue

            # Single source of truth (Step 2, 2026-06-26). The T3 SURFACE
            # decision (target present, due_pattern absent, target
            # positive, days_since >= target OR never-completed) lives in
            # ``classify_routine_item`` — the SAME predicate the
            # aggregator delegates to. We read ``tier == 3`` for the
            # surface gate, then compute the sort-only metadata
            # (``days_since`` / ``overdue_ratio``) locally; those fields
            # aren't part of the handoff decision so they stay here.
            _def_esc, _def_surf, _def_fuel_gap = _tier_default_values(
                tier_defaults,
            )
            classification = classify_routine_item(
                priority=item.priority,
                due_pattern=item.due_pattern,
                surface_at_days=item.surface_at_days,
                escalate_at_days=item.escalate_at_days,
                target_cadence_days=item.target_cadence_days,
                completion_log=completion_log,
                item_text=item.text,
                today=today_local,
                self_care=item.self_care,
                default_escalate_at_days=_def_esc,
                default_surface_at_days=_def_surf,
                escalate_after_gap_days=item.escalate_after_gap_days,
                explicit_slot=item.slot,
                default_fuel_escalate_after_gap_days=_def_fuel_gap,
            )
            if classification.tier != 3:
                continue
            # This surface is CADENCE-driven (its AutoT3Candidate carries
            # overdue_ratio / target_cadence_days). A pure ``self_care``
            # item (Q2) with NO cadence target can also classify tier 3,
            # but it has no ratio to rank — it's surfaced separately by
            # ``compute_today_view``'s self-care pass. Skip it here so the
            # cadence-shaped dataclass stays well-defined.
            if item.target_cadence_days is None:
                continue

            # tier == 3 + a cadence target guarantees target_cadence_days
            # is a positive int and due_pattern is absent (the
            # classifier's T3 cadence branch preconditions). Recompute
            # the sort metadata.
            target = item.target_cadence_days
            assert isinstance(target, int) and target > 0

            # Sort metadata from the ONE owner (``_cadence_metadata``) — the
            # hydrator computes the same pair for the curated copy of this
            # item, and two spellings of "days since / overdue ratio" would
            # drift silently.
            days_since_value, ratio = _cadence_metadata(
                _parse_item_completion_dates(
                    completion_log.get(item.text, [])
                ),
                target=target,
                today=today_local,
            )

            candidates.append(AutoT3Candidate(
                path=rel_path,
                routine_record=record_name,
                item_text=item.text,
                # THE gap the design flagged: self_care was read a few lines
                # above (passed to classify_routine_item) and then dropped.
                # Without it a cadence-bearing self-care item classifies Rhythm
                # instead of Fuel.
                self_care=item.self_care,
                explicit_slot=item.slot,
                target_cadence_days=target,
                days_since_last_completed=days_since_value,
                overdue_ratio=ratio,
            ))

    # Sort by overdue_ratio DESCENDING (most overdue first), ties
    # broken by item_text case-insensitive. ``float('inf')`` ranks
    # above any finite ratio naturally — Python's sort handles inf
    # correctly without special-casing.
    candidates.sort(
        key=lambda c: (-c.overdue_ratio, c.item_text.lower()),
    )
    return candidates


def compute_self_care_candidates(
    vault_path: Path, now: datetime,
    tier_defaults: Any = None,
) -> list[AutoT1Candidate]:
    """Walk ``vault/routine/*.md`` and return ``self_care``-flagged items
    that surface to the T3 self-care lane (Q2, 2026-06-26).

    ``tier_defaults`` (FUEL-ESCALATION, 2026-08-20): threaded for the
    same exactly-one-lane reason as
    :func:`compute_auto_t3_candidates` — a self-care fuel item at/past
    its escalation gap classifies tier 1 in the shared predicate, and
    this surface's ``tier != 3`` gate excludes it rather than
    double-rendering it as the daily floor.

    The dedicated self-care lane: items flagged ``self_care: true`` (no
    ``due_pattern``) surface to T3 when not completed today — the daily
    self-care floor, deliberately included rather than skipped. This is
    the surface for self_care items WITHOUT a ``target_cadence_days``
    (cadence-driven self-care is already covered by
    :func:`compute_auto_t3_candidates`; including it here too would
    double-render, so this surface SKIPS items that carry a cadence
    target — they belong to the cadence surface).

    Returns :class:`AutoT1Candidate` (the shared routine-origin shape;
    ``origin="routine"``, ``surface_reason="self-care"``, no ``due_iso``).
    Surfacing decision is the single ``classify_routine_item`` predicate
    (``tier == 3``); this just filters to the self_care-only subset +
    builds the candidate. Sorted by item text case-insensitive.

    Per ``feedback_intentionally_left_blank``: pure-compute, no logs.
    """
    import frontmatter  # type: ignore[import-untyped]

    from alfred.routine.config import Item

    routine_dir = vault_path / "routine"
    if not routine_dir.is_dir():
        return []

    today_local = now.date()

    candidates: list[AutoT1Candidate] = []
    for record_path in sorted(routine_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(record_path))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "routine":
            continue
        status = str(fm.get("status") or "active").lower()
        if status == "archived":
            continue
        if fm.get("alfred_triage") is True:
            continue
        record_name = str(fm.get("name") or record_path.stem)
        raw_items = fm.get("items") or []
        if not isinstance(raw_items, list):
            continue
        completion_log = fm.get("completion_log") or {}
        if not isinstance(completion_log, dict):
            completion_log = {}
        rel_path = f"routine/{record_path.name}"

        for raw_item in raw_items:
            item = Item.from_dict(raw_item)
            if item is None:
                continue
            if not item.self_care:
                continue
            # Cadence-driven self_care is the cadence surface's job;
            # this surface is the self_care-ONLY (no-cadence) floor.
            if item.target_cadence_days is not None:
                continue
            _def_esc, _def_surf, _def_fuel_gap = _tier_default_values(
                tier_defaults,
            )
            classification = classify_routine_item(
                priority=item.priority,
                due_pattern=item.due_pattern,
                surface_at_days=item.surface_at_days,
                escalate_at_days=item.escalate_at_days,
                target_cadence_days=item.target_cadence_days,
                completion_log=completion_log,
                item_text=item.text,
                today=today_local,
                self_care=item.self_care,
                default_escalate_at_days=_def_esc,
                default_surface_at_days=_def_surf,
                escalate_after_gap_days=item.escalate_after_gap_days,
                explicit_slot=item.slot,
                default_fuel_escalate_after_gap_days=_def_fuel_gap,
            )
            if classification.tier != 3:
                continue
            candidates.append(AutoT1Candidate(
                path=rel_path,
                name=item.text,
                due_iso="",
                surface_reason="self-care",
                origin="routine",
                routine_record=record_name,
                item_text=item.text,
                # This lane is self_care-BY-FILTER, but read the field rather
                # than hardcoding True — a pin that asserts Fuel here should be
                # measuring the item, not this branch's name.
                self_care=item.self_care,
                explicit_slot=item.slot,
                has_due_pattern=item.due_pattern is not None,
            ))

    candidates.sort(key=lambda c: c.name.lower())
    return candidates


def compute_self_care_task_candidates(
    vault_path: Path, now: datetime,
) -> list[AutoT1Candidate]:
    """Walk ``vault/task/*.md`` and return open tasks flagged
    ``self_care: true`` that surface to the T3 self-care lane (Q2,
    2026-06-26 — the spec routes ``self_care`` on routines AND tasks to
    T3).

    A self_care task surfaces to T3 when it is OPEN and NOT already an
    auto-T1 candidate (a near-deadline self_care task surfaces in T1 —
    deadline pressure wins over the self-care floor, per the spec). So
    this surface is "self_care tasks with no near deadline" — the daily
    self-care floor for one-off tasks (e.g. a ``task`` record "book a
    massage" flagged self_care, no due date).

    Returns :class:`AutoT1Candidate` with ``origin="task"``,
    ``surface_reason="self-care"``, no ``due_iso``. Sorted by name.
    Defensive filters mirror the auto-T1 task scan (parse failure,
    non-task type, closed status, ``alfred_triage``).

    Per ``feedback_intentionally_left_blank``: pure-compute, no logs.
    """
    import frontmatter  # type: ignore[import-untyped]

    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        return []

    # Tasks already surfacing in auto-T1 (by name) are excluded — a
    # near-deadline self_care task lives in T1, not the T3 floor.
    auto_t1_names = {c.name for c in compute_auto_t1_candidates(vault_path, now)}

    candidates: list[AutoT1Candidate] = []
    for path in sorted(task_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "task":
            continue
        status = str(fm.get("status") or "todo").lower()
        if status not in OPEN_STATUSES:
            continue
        if fm.get("alfred_triage") is True:
            continue
        # Coerce self_care via the shared helper (no drift across the
        # three readers of the field — reviewer NOTE 2026-06-27).
        from alfred.routine.config import _coerce_self_care
        if not _coerce_self_care(fm.get("self_care", False)):
            continue
        name = str(fm.get("name") or path.stem)
        if name in auto_t1_names:
            # Already surfacing in T1 (deadline pressure wins).
            continue
        candidates.append(AutoT1Candidate(
            path=f"task/{path.name}",
            name=name,
            due_iso="",
            surface_reason="self-care",
            origin="task",
            # Reached only past the ``_coerce_self_care`` filter above.
            self_care=True,
            explicit_slot=(
                str(fm["slot"]).strip() if fm.get("slot") is not None else None
            ),
        ))

    candidates.sort(key=lambda c: c.name.lower())
    return candidates


def compute_returned_task_candidates(
    vault_path: Path,
    now: datetime,
) -> list[AutoT1Candidate]:
    """Tasks whose reminder has returned and has not been acted on.

    This is the READ half of the snooze-return contract. The transport
    scheduler fires a reminder and writes the ruled ``slot:`` onto the
    record; without this function nothing ever looks, and the return is
    invisible on every surface. Measured before it existed: no module
    outside ``transport/`` read ``remind_at``/``reminded_at`` at all,
    and an undated task could not become a candidate by any other path
    (``compute_auto_t1_candidates`` requires a due date;
    ``compute_self_care_task_candidates`` requires ``self_care``). Four
    of the six live carriers have no due date, so their returns had
    nowhere to land.

    **Predicate (ratified, deliberately minimal):** ``status: todo``
    AND ``reminded_at`` set AND ``remind_at`` absent.

    The state it describes is "came back, still open, not re-armed".
    Every exit from that state runs through a verb that already exists,
    which is why this needs no new dismissal store:

      * completing it changes ``status`` — falls out of the predicate;
      * re-snoozing sets ``remind_at`` — falls out, and the scheduler's
        re-snooze signal fires on the next return;
      * a board-side snooze suppresses at the surface layer, above this.

    Nothing here prejudges the rotation lane's ack model; when that
    lane ships shelve/ack it inherits and refines this predicate.

    Slot and escalation come from the shared
    :func:`alfred.tier.slots.resolve_effective_slot`, so a boundary that
    passes AFTER the fire still escalates — the septic case, which fires
    2029-06-01 as rhythm and escalates three months later while sitting
    in returned-state.

    Pure, per this module's contract: no logging here, the call site
    reports the count.
    """
    import frontmatter  # type: ignore[import-untyped]
    from alfred.routine.config import _coerce_self_care

    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        return []

    today_local = now.date()
    candidates: list[AutoT1Candidate] = []
    for path in sorted(task_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "task":
            continue
        if str(fm.get("status") or "todo").lower() != "todo":
            continue
        if not str(fm.get("reminded_at") or "").strip():
            continue
        # Re-armed — it is a pending reminder again, not a return.
        if str(fm.get("remind_at") or "").strip():
            continue

        waiting_on = str(fm.get("waiting_on") or "").strip()
        slot, _rule = slots.resolve_effective_slot(
            return_slot=fm.get("return_slot"),
            escalate_on=fm.get("escalate_on"),
            escalate_to=fm.get("escalate_to"),
            today=today_local,
        )
        candidates.append(AutoT1Candidate(
            path=f"task/{path.name}",
            name=str(fm.get("name") or path.stem),
            due_iso=str(fm.get("due") or ""),
            # A waiting item is not a snooze that came back — it is
            # blocked on somebody. The framing says what the next
            # physical action is.
            surface_reason=slots.chase_phrase(waiting_on) or "returned",
            origin="task",
            self_care=_coerce_self_care(fm.get("self_care", False)),
            # Feeds rule 1 of the slot classifier — the operator's own
            # word, highest precedence.
            explicit_slot=slot,
        ))

    candidates.sort(key=lambda c: c.name.lower())
    return candidates


# ---------------------------------------------------------------------------
# compute_today_view — the unified "today" view (Step 2b, 2026-06-26)
# ---------------------------------------------------------------------------
#
# The spec's keystone: ONE computed read over the substrate (tasks +
# behind-routines + imminent-events) that the voice/surfacing layer
# renders into channels. The brief's day section and its routines section
# became two RENDERINGS of this one object — collapsing the hand-mirrored
# two-pipeline math. Phase C (2026-08-12) finished the collapse: the two
# renderings merged into ONE section ("Today's Plan"), projected through
# ``tier/day_plan.py`` and shared with the briefing player's spoken segment.
#
# Structural-dedup invariant: a routine item appears in EXACTLY ONE of
# {t1, t2, t3, routine_today}. Items that classify into a tier (via the
# single ``classify_routine_item`` predicate) land in t1/t2/t3; items
# that fired today but did NOT classify land in ``routine_today``. The
# two are complements by construction — no convention-enforced dedup.
#
# This commit (2b) builds the VIEW + the daily-goal. The render
# re-pointing (making ``render_tier_section`` consume this object) was
# Step 2c; Phase C re-pointed the briefing player's narration at the same
# object too, through the shared ``tier/day_plan.py`` projection.


@dataclass
class TierEntry:
    """One entry in a tier lane (T1/T2/T3) of the unified today view.

    Discriminated union by ``origin``:
      * ``"task"`` — a ``task/*.md`` record. ``name`` = the task name;
        ``path`` = ``"task/<file>.md"``; ``routine_record`` / ``item_text``
        are ``None``.
      * ``"routine_item"`` — a recurring item inside a ``routine/*.md``
        record. ``name`` = the item text; ``routine_record`` = the
        record name; ``item_text`` = the item text; ``path`` =
        ``"routine/<file>.md"``.

    Fields:
      * ``tier`` — 1 / 2 / 3 (which lane this entry is in).
      * ``origin`` — ``"task"`` or ``"routine_item"``.
      * ``name`` — operator-facing display string.
      * ``path`` — vault-relative path to the owning record.
      * ``due_iso`` — ISO due date when deadline-bearing; ``None`` for
        T3 self-care (cadence-ranked, not deadline-anchored).
      * ``surface_reason`` — the canonical reason string (``"due today"``
        etc.) for deadline-bearing entries; ``None`` for T3.
      * ``source`` — provenance enum (``"auto-due"`` / ``"auto-escalate"``
        / ``"auto-due-routine"`` / ``"auto-surface-routine"`` /
        ``"auto-cadence-routine"`` / ``"operator"`` / ``"rollover"``).
        Mirrors the ``daily_curation`` source enum + adds the
        cadence-routine T3 source.
      * ``confirmed`` — T1-only; ``True`` once the operator confirms an
        auto-surfaced candidate (curated entries are confirmed; fresh
        auto candidates are not). ``None`` for T2/T3.
      * ``escalation_state`` — optional human string describing the
        climb dynamic (e.g. ``"T2→T1 in 2d"`` for an item ramping toward
        T1). ``None`` when not escalating / not applicable.
      * ``routine_record`` / ``item_text`` — populated only for
        ``origin == "routine_item"``.
    """

    tier: int
    origin: str
    name: str
    path: str
    due_iso: str | None = None
    surface_reason: str | None = None
    source: str = "operator"
    confirmed: bool | None = None
    escalation_state: str | None = None
    routine_record: str | None = None
    item_text: str | None = None
    # Cadence metadata — populated only for T3 cadence-driven entries
    # (source ``auto-cadence-routine``). Carried so the brief's T3
    # render is a pure read of the view (Step 2c): the annotation
    # "Nd days since last; target every Md" needs both. ``None`` for
    # every non-cadence entry.
    target_cadence_days: int | None = None
    days_since_last_completed: int | None = None
    # --- snooze breakthrough (#14) -------------------------------------------
    # WHY this row is back before its snooze expired: ``crossed_due`` or
    # ``moved_earlier``. Empty for every row that was never snoozed, and for a
    # snoozed row returning because its duration simply ran out (the clock is
    # not a delta — a return the operator can predict needs no explanation).
    #
    # Carried on the ENTRY rather than logged and dropped: the reason was
    # already computed in the projection, and a card that returns early without
    # saying why is the mysterious-resurrection shape the delta rule exists to
    # avoid. The producer copies it into evidence so the face can answer it.
    snooze_breakthrough: str = ""
    # --- slot axis (#18 slice 1) ---------------------------------------------
    # Slot is ORTHOGONAL to tier: tier answers "when does this press", slot
    # answers "what does this do for the day". Both survive; neither replaces
    # the other, and the T1/T2/T3 machinery is untouched.
    #
    # INPUTS to the classifier (operator intent, read not inferred):
    #   ``explicit_slot``    — the operator's own ``slot:`` frontmatter (rule 1)
    #   ``self_care``        — already means "intrinsic" here (rule 3)
    #   ``has_due_pattern``  — a recurring hard deadline (rule 4)
    #   ``target_cadence_days`` (above) — a soft cadence (rule 5)
    #
    # OUTPUT, stamped by the projection:
    #   ``slot``      — ``duty`` / ``rhythm`` / ``fuel`` / ``unslotted``
    #   ``slot_rule`` — WHICH rule fired, so "Duty because the operator said so"
    #                   and "Duty because it's a dated task" stay distinguishable
    #                   in the coverage telemetry.
    #
    # ``slot`` is a producer-time OVERLAY — recomputed every projection, never
    # written to a record (the ``candidate``/``done``-derived vs
    # ``confirmed``-persisted law). It is deliberately SEPARATE from
    # ``explicit_slot`` so a re-projection can never read the classifier's own
    # previous answer back in as operator intent.
    explicit_slot: str | None = None
    self_care: bool = False
    has_due_pattern: bool = False
    slot: str = "unslotted"
    slot_rule: str = "no_signal"
    overdue_ratio: float | None = None
    # Arc #20 (2026-07-22) — free-text T3 ad-hoc done-state. Carried
    # ONLY for curated free-text T3 entries (``_curated_t3_to_tier_entry``);
    # ``None`` for every task-origin / routine-origin entry (those resolve
    # done-ness through their backing task status / completion_log). ISO
    # ``YYYY-MM-DD`` when the operator checked the item off via
    # ``tier_done``. Lets ``_entry_done`` count it toward the daily goal
    # and the render layer strike / drop it.
    done_at: str | None = None
    # --- backdated completion (2026-08-20) -----------------------------------
    # How many days back a "previously done" completion may honestly reach —
    # threaded from ``AutoT1Candidate.backdate_limit_days`` (see there for the
    # derivation) on routine due-pattern entries; 0 everywhere else. A FACT
    # about the item's recurrence state, stamped onto the curated copy of the
    # same item too (the operator's accept-then-backdate flow rides the
    # curated entry). The feed producer copies it into slot evidence; the
    # serve side filters the ``done_Nd`` rungs by it.
    backdate_limit_days: int = 0
    # --- neglect-gap escalation (FUEL-ESCALATION, 2026-08-20) ----------------
    # ``True`` iff this row's T1 placement came from the neglect-gap
    # escalation. The slot classifier's rule 0 reads it (Duty visit,
    # outranking the record's own ``slot: fuel`` — see tier/slots.py's
    # precedence note). Like ``backdate_limit_days`` it is a FACT about the
    # item's current completion state, so the view stamps it onto the curated
    # copy of the same item too — an accepted escalation stays in Duty while
    # the gap persists and goes home when it closes.
    gap_escalated: bool = False


@dataclass
class RoutineLine:
    """One routine item that fired today but did NOT classify into any
    tier — the complement of {t1, t2, t3} for routine items.

    These are today's habit anchors that never escalated. Phase C dissolved
    the brief's standalone "Today's Routines" section INTO the slot board, so
    they now render inside their slot alongside the tier rows — which is why
    they carry the slot axis below. The text + priority + annotation + time
    fields still mirror what the aggregator's ``_collect_items_for_today``
    produces per item, so the render re-point stays a straight read.
    """

    text: str
    priority: str
    annotation: str = ""
    time: str = ""
    # --- slot axis (Phase C, §4 dissolution) ---------------------------------
    # A routine item that fired today but stayed OUT of every tier still has a
    # slot: "water the plants" is Rhythm whether or not it escalated. Stamped
    # by ``_collect_routine_today`` through ``slots.classify_slot`` — the SAME
    # classifier the tier lanes use, never a second look-alike rule — over the
    # signals the aggregator now emits alongside each item.
    #
    # ``origin`` is fixed at ``"routine_item"`` so the classifier's rule 6
    # (dated TASK ⟹ Duty) cannot fire on these: a routine item is not a task
    # record, and letting rule 6 reach it would put habit anchors under Duty on
    # the strength of a field they do not have.
    explicit_slot: str | None = None
    self_care: bool = False
    has_due_pattern: bool = False
    target_cadence_days: int | None = None
    origin: str = "routine_item"
    slot: str = slots.SLOT_UNSLOTTED
    slot_rule: str = slots.RULE_NONE


@dataclass
class DailyGoalState:
    """The one-of-each-tier daily goal — the PURPOSE of tiering.

    The spec's success criterion: finish AT LEAST one item from each of
    T1, T2, T3 every day (ideal = all T1 done + one each of T2/T3). This
    is a balanced day (urgent + medium + self-care), not "clear the
    urgent." The voice layer renders/encourages off this state.

    Fields (per-tier available + done counts, plus the rollups):
      * ``t1_available`` / ``t2_available`` / ``t3_available`` — how many
        items are in each lane today.
      * ``t1_done`` / ``t2_done`` / ``t3_done`` — how many of each lane's
        items are completed today (task status closed, or routine item
        completed in the current cycle / today).
      * ``balanced_day`` — ``True`` iff at least one item is done in EACH
        of the three lanes (the daily goal met).
      * ``all_t1_done`` — ``True`` iff every available T1 item is done
        (the "ideal" T1 component; ``True`` vacuously when no T1 items).
    """

    t1_available: int = 0
    t2_available: int = 0
    t3_available: int = 0
    t1_done: int = 0
    t2_done: int = 0
    t3_done: int = 0
    balanced_day: bool = False
    all_t1_done: bool = False


@dataclass
class TodayView:
    """The unified today view — ONE computed read the voice layer renders.

    Consumed through ``tier/day_plan.build_day_plan``, which projects it into
    the slot arrangement BOTH morning renders format:
      * brief "Today's Plan"           ← ``t1`` / ``t2`` / ``t3`` (slot-grouped)
                                         + ``routine_today`` (§4's dissolution)
                                         + ``daily_goal`` (tier-based, unchanged)
      * the briefing player's spoken ``day_plan`` segment ← the same projection

    The ``t1`` / ``t2`` / ``t3`` lanes hold :class:`TierEntry` (tasks +
    routine-items + curated). ``routine_today`` holds :class:`RoutineLine`
    (routine items that fired but didn't escalate — the structural
    complement). An item is in exactly one of the four by construction.
    """

    t1: list[TierEntry] = field(default_factory=list)
    t2: list[TierEntry] = field(default_factory=list)
    t3: list[TierEntry] = field(default_factory=list)
    routine_today: list[RoutineLine] = field(default_factory=list)
    daily_goal: DailyGoalState = field(default_factory=DailyGoalState)
    # #18 slice 1 — how much of today the slot classifier could answer for.
    # The rollout's convergence metric and the number that gates the stage-2
    # rings swap. Carried on the view (rather than only logged) so the brief
    # and the board read the SAME figure the projection computed, never a
    # re-derived one.
    slot_coverage: slots.SlotCoverage = field(
        default_factory=slots.SlotCoverage
    )


def _task_is_done_today(fm: dict, today: date) -> bool:
    """Return True iff a task record counts as completed for the daily
    goal: status is ``done`` AND it was closed today (when a ``completed`` /
    ``done`` date is present), else just done.

    Defensive: a done task without a completion date still counts
    (operator marked it done; we can't prove it wasn't today, and
    counting it keeps the goal encouraging rather than pedantic).

    CANCELLED IS NOT DONE (#103), and this docstring used to say it was
    ("status is closed (done/cancelled)") — with the code agreeing. That was
    never merely theoretical: ``vault-talker/SKILL.md`` names
    ``set_fields {"status": "cancelled"}`` as the RECOMMENDED DEFAULT when the
    operator asks Salem to remove a task, and ``cancelled_at`` is not among the
    completion-date keys below, so every such task fell through to the
    unconditional ``return True`` and was counted toward the daily goal as
    something he had COMPLETED. The board verb makes that path one tap instead of
    one conversation; it did not create it. Either way it is the operator's own
    complaint — *"I don't want to mark it done"* — implemented as a feature.
    A cancellation is the ABSENCE of an achievement, not a cheap one.

    REACHABLE THROUGH ``entry_is_done``, AND PINNED THERE. ``compute_today_view``
    drops cancelled task entries before they reach the lanes, which SHADOWS this
    correction on that one path — a mutation removing the ``CANCELLED_STATUS``
    clause leaves the curated-goal pin green, because the entry never arrives.
    That shadow is why the fix needs its own driver: ``entry_is_done`` is called
    directly by ``day_plan`` and by the feed producer over their OWN caches, and
    those callers do not go through ``compute_today_view``'s drop. A cancelled
    record genuinely reaches this predicate there.

    So the pin that makes this line load-bearing drives ``entry_is_done`` — the
    named consumer — not just this private function
    (``test_cancelled_task_is_not_done_via_the_shared_predicate``). Without it
    the correction was defended by exactly one direct-call assertion, and the
    consumers this docstring cites as the REASON for the fix had nothing
    covering them.
    """
    status = str(fm.get("status") or "todo").lower()
    if status in OPEN_STATUSES or status == CANCELLED_STATUS:
        return False
    # Closed. If a completion date is present, only count today's.
    for key in ("completed", "done", "completed_at", "closed"):
        raw = fm.get(key)
        d = coerce_due_date(raw)
        if d is not None:
            return d == today
    return True


def compute_today_view(
    vault_path: Path, now: datetime,
    tier_defaults: Any = None,
    snooze_path: str | Path | None = None,
) -> TodayView:
    """Build the unified today view over the substrate.

    ``tier_defaults`` (Q3 Option A, 2026-06-26): optional global
    window defaults (a ``TierDefaultsConfig`` or ``None``) threaded into
    the routine T1/T2 surfaces so the brief's 06:00 view applies the SAME
    defaults the aggregator's 05:59 pass does. Passed by the brief daemon
    from its loaded config; ``None`` → no defaults (opt-out unchanged).

    Gathers, in one pass:
      * task-origin auto-T1 (``compute_auto_t1_candidates``),
      * routine-origin T1 / T2 / T3 (the ``classify_routine_item``-backed
        ``compute_auto_routine_candidates`` / ``_t2`` / ``compute_auto_t3``),
      * operator-curated T1/T2/T3 shortlists (``load_daily_curation``),
      * the routine items that fired today but did NOT classify into a
        tier (the aggregator's ``_collect_items_for_today`` complement),

    partitions them into the T1/T2/T3 lanes + ``routine_today``, and
    computes the one-of-each ``daily_goal``.

    Curated + auto entries are merged per lane with dedup:
      * task-origin key = record name (from the wikilink),
      * routine-origin key = ``(routine_record, item_text)``.
    A curated entry wins over an auto candidate for the same key (the
    operator's confirmation is authoritative); auto candidates not in
    the curated set are appended as ``confirmed=False`` (T1) entries.

    Per ``feedback_intentionally_left_blank``: emits ONE structured
    ``brief.today_view.computed`` log with the per-lane counts + the
    daily-goal rollup so a stable "ran, here's the view" signal is
    grep-able even on an empty day. Tests pin the emission +
    field shape via ``capture_logs``.
    """
    import frontmatter  # type: ignore[import-untyped]

    from alfred.tier.daily_curation import load_daily_curation

    today = now.date()

    # --- substrate: auto candidates (all routed through the single
    # classify_routine_item predicate on the routine side) ----------
    auto_t1_task = compute_auto_t1_candidates(vault_path, now)
    auto_t1_routine = compute_auto_routine_candidates(
        vault_path, now, tier_defaults,
    )
    auto_t2_routine = compute_auto_routine_t2_candidates(
        vault_path, now, tier_defaults,
    )
    # ``tier_defaults`` threaded (FUEL-ESCALATION, 2026-08-20) so the T3 +
    # self-care surfaces see the SAME classification the T1 surface does —
    # an escalated fuel item excludes itself from T3 here rather than
    # double-rendering (exactly-one-lane invariant).
    auto_t3_routine = compute_auto_t3_candidates(vault_path, now, tier_defaults)
    # Q2 (2026-06-26): self_care-flagged items (no cadence target) →
    # the dedicated T3 self-care lane (daily floor). Both routine-item
    # and task origins. (Gap escalation never fires for TASK origins:
    # completion gaps are measured from a routine record's
    # ``completion_log``, which tasks do not have.)
    self_care_routine = compute_self_care_candidates(
        vault_path, now, tier_defaults,
    )
    self_care_task = compute_self_care_task_candidates(vault_path, now)
    # Phase 2c+h: snooze returns and waiting-chases whose reminder has
    # fired and not been acted on. Without this THREADING the reader
    # exists but nothing calls it, which is the same write-live/read-
    # dead failure the reader was built to fix — one layer up.
    returned_task = compute_returned_task_candidates(vault_path, now)

    # --- operator curation ----------------------------------------
    curation = load_daily_curation(vault_path, today)

    # --- build the lanes ------------------------------------------
    t1: list[TierEntry] = []
    t2: list[TierEntry] = []
    t3: list[TierEntry] = []

    # Track keys present in each lane so auto candidates don't double
    # an operator-curated entry (curated wins).
    t1_keys: set[str] = set()
    t2_keys: set[str] = set()
    t3_keys: set[str] = set()
    # Lowercased texts of curated free-text T3 entries — used to
    # suppress a record-anchored auto-T3 duplicate of the same item
    # (reviewer NOTE-2). Free-text + record-anchored keys differ, so
    # text-match is the cross-shape dedup.
    curated_t3_texts: set[str] = set()

    def _task_key(name: str) -> str:
        return f"task::{name.lower()}"

    def _routine_key(record: str | None, text: str | None) -> str:
        return f"routine::{(record or '').lower()}::{(text or '').lower()}"

    # Auto reason/due lookups by key — so a CURATED entry that also
    # auto-surfaces carries the auto reason + due (the brief annotates a
    # confirmed curated entry with "due today" etc.; that metadata comes
    # from the auto candidate, which dedup would otherwise discard).
    # Keyed the same way the lanes dedup.
    auto_reason_by_t1_key: dict[str, str] = {}
    auto_due_by_t1_key: dict[str, str] = {}
    for c in auto_t1_task:
        k = _task_key(c.name)
        auto_reason_by_t1_key[k] = c.surface_reason
        auto_due_by_t1_key[k] = c.due_iso
    auto_backdate_by_t1_key: dict[str, int] = {}
    auto_gap_escalated_t1_keys: set[str] = set()
    for c in auto_t1_routine:
        k = _routine_key(c.routine_record, c.item_text)
        auto_reason_by_t1_key[k] = c.surface_reason
        auto_due_by_t1_key[k] = c.due_iso
        auto_backdate_by_t1_key[k] = c.backdate_limit_days
        if c.gap_escalated:
            auto_gap_escalated_t1_keys.add(k)

    # CANCELLED TASKS LEAVE THE PLAN ENTIRELY (#103).
    #
    # Every AUTO producer above already drops them for free — each walks the
    # task dir behind ``status not in OPEN_STATUSES`` (or, for the returned
    # candidates, ``status != "todo"``), and ``cancelled`` is in neither set. A
    # CURATED entry is the one shape that does not, because curation is read
    # from the daily file and is authoritative by design: the operator put it on
    # today's list, so nothing re-asks the record whether it still wants to be
    # there.
    #
    # THAT ASYMMETRY IS A LIVE DEFECT, NOT A LATENT ONE, and this comment said
    # the opposite until it was checked. The first draft read "harmless while
    # nothing wrote ``cancelled`` at runtime" — false: ``vault-talker/SKILL.md``
    # instructs Salem that ``set_fields {"status": "cancelled"}`` is the
    # RECOMMENDED DEFAULT when the operator asks to remove a task, so every task
    # he has ever asked Salem to cancel has been counted toward his daily goal as
    # something he COMPLETED. The board verb below does not introduce this bug; it
    # makes it frequent. Corrected here rather than only in commit history,
    # because this comment is where the next reader meets the claim.
    #
    # MEASURED at c552fa09 on a curated T1 whose record was cancelled,
    # ``compute_today_view``
    # reported ``t1_count=1 t1_done=1 all_t1_done=True`` — because
    # ``_task_is_done_today`` treats every closed status as done, and
    # ``cancelled_at`` is not one of the completion-date keys it checks. So a
    # cancellation counted as an ACHIEVEMENT and the brief would have told the
    # operator he finished his T1. That is the precise falsification the
    # operator refused when he said *"I don't want to mark it done"* — shipping
    # the cancel verb without this would have handed his own complaint back to
    # him in the daily goal.
    #
    # Dropping the entry (rather than merely making the predicate answer False)
    # is the fix, and the alternative is worse: a cancelled task left in the
    # lane with ``done=False`` renders as an open, actionable card whose ✓ would
    # flip ``cancelled`` straight to ``done``. Neither a to-do nor an
    # achievement is what *moot* means, and neither is what it should look like.
    # ``_task_is_done_today`` is corrected in the same commit as defence in
    # depth for any future surface that reaches it another way.
    # SCANNED ONLY WHEN THERE IS SOMETHING TO FILTER. The set is consumed by
    # curated entries alone (every auto producer already drops cancelled records
    # on its own walk), so on an un-curated day this was a second full-directory
    # frontmatter walk whose result nothing read.
    cancelled_task_names: set[str] = set()
    task_dir = vault_path / "task"
    _curated_task_entries = bool(
        curation is not None and (curation.t1 or curation.t2)
    )
    if _curated_task_entries and task_dir.is_dir():
        import frontmatter as _fm_mod

        for _p in task_dir.glob("*.md"):
            try:
                _meta = dict(_fm_mod.load(str(_p)).metadata or {})
            except Exception:  # noqa: BLE001 — an unreadable record is not a cancel
                continue
            if _meta.get("type") != "task":
                continue
            if str(_meta.get("status") or "").strip().lower() == CANCELLED_STATUS:
                cancelled_task_names.add(str(_meta.get("name") or _p.stem).lower())

    # The names ACTUALLY dropped from a lane — reported after the build, not
    # before it. The first version of this logged the cancelled records FOUND in
    # the vault under the event name ``..._excluded``, which is a different set:
    # it fired ``count=2`` on a vault with two cancelled tasks and zero curated
    # entries, where nothing was excluded at all, and it dumped every cancelled
    # task name on every today-view forever. An observability line whose name
    # does not describe its number is worse than none — and the pin asserted the
    # wrong behaviour, encoding the overstatement as spec.
    _excluded_names: list[str] = []

    def _is_cancelled_task_entry(entry: TierEntry) -> bool:
        hit = (
            entry.origin == "task"
            and (entry.name or "").strip().lower() in cancelled_task_names
        )
        if hit:
            _excluded_names.append(entry.name)
        return hit

    # 1. Curated entries first (authoritative). Annotate with the auto
    # reason/due when the same item also auto-surfaces (so the render is
    # a pure read of the lane, curated entries included).
    if curation is not None:
        for e in curation.t1:
            entry = _curated_to_tier_entry(e, tier=1)
            if entry is not None and _is_cancelled_task_entry(entry):
                continue
            if entry is not None:
                k = _entry_key(entry, _task_key, _routine_key)
                if entry.surface_reason is None and k in auto_reason_by_t1_key:
                    entry.surface_reason = auto_reason_by_t1_key[k]
                    entry.due_iso = auto_due_by_t1_key.get(k)
                # UNCONDITIONAL (unlike the reason/due annotation above): the
                # backdate depth is a fact about the item's recurrence state,
                # not a presentation the curated copy may already carry — and
                # the operator's accept-then-backdate flow lives on exactly
                # this entry (an accepted candidate re-projects as curated).
                entry.backdate_limit_days = auto_backdate_by_t1_key.get(k, 0)
                # Same unconditional stamp for the neglect-escalation fact
                # (FUEL-ESCALATION): an ACCEPTED escalated item re-projects
                # as curated, and without this it would fall back to its
                # home slot mid-neglect. False the moment the gap closes.
                entry.gap_escalated = k in auto_gap_escalated_t1_keys
                t1.append(entry)
                t1_keys.add(k)
        for e in curation.t2:
            entry = _curated_to_tier_entry(e, tier=2)
            if entry is not None and _is_cancelled_task_entry(entry):
                continue
            if entry is not None:
                t2.append(entry)
                t2_keys.add(_entry_key(entry, _task_key, _routine_key))
        for e in curation.t3:
            entry = _curated_t3_to_tier_entry(e)
            if entry is not None:
                t3.append(entry)
                t3_keys.add(_routine_key(None, entry.item_text or entry.name))
                # Reviewer NOTE-2 (Step 2c): a curated free-text T3 entry
                # ("Walk Fergus") keys as ``routine::::<text>`` while an
                # auto-cadence/self-care entry for the same item keys as
                # ``routine::<record>::<text>`` — different keys, so both
                # would render. Track the lowercased text so the auto T3
                # steps below can suppress the record-anchored duplicate.
                # (Pre-existing brief behaviour; closing it here is the
                # cheap win — the full close is the deferred talker
                # "anchor free-text back to a routine record" path.)
                curated_t3_texts.add(
                    (entry.item_text or entry.name).strip().lower()
                )

    # ILB: a card vanishing without a trace is exactly the silent absence this
    # rule exists to close — "where did that go?" must be greppable. Fires only
    # when something WAS dropped, and names only those entries, so the count and
    # the event name describe the same set.
    if _excluded_names:
        log.info(
            "tier.today_view.cancelled_tasks_excluded",
            count=len(_excluded_names),
            names=sorted(_excluded_names),
            detail=(
                "curated entries whose task record is cancelled — dropped from "
                "today's lanes; a cancellation is not a to-do and not an "
                "achievement"
            ),
        )

    # 2. Auto-T1 task candidates (append if not already curated).
    for c in auto_t1_task:
        key = _task_key(c.name)
        if key in t1_keys:
            continue
        source = (
            "auto-due"
            if c.surface_reason in ("due today", "due tomorrow")
            else "auto-escalate"
        )
        t1.append(TierEntry(
            tier=1, origin="task", name=c.name, path=c.path,
            due_iso=c.due_iso, surface_reason=c.surface_reason,
            source=source, confirmed=False,
            self_care=c.self_care, explicit_slot=c.explicit_slot,
            has_due_pattern=c.has_due_pattern,
        ))
        t1_keys.add(key)

    # 2b. Returned snoozes and waiting-chases (Phase 2c+h).
    #
    # T1 because the operator picked the date: a snooze returning today
    # is pressing today by his own choice, which is exactly what T1
    # means. The SLOT it lands in is separate and comes from his ruling
    # via ``explicit_slot`` — tier and slot are orthogonal axes.
    #
    # Runs after the auto-due block and dedups against it, so a returned
    # task that ALSO has a due date today surfaces once, keeping the
    # deadline framing rather than being doubled.
    for c in returned_task:
        key = _task_key(c.name)
        if key in t1_keys:
            continue
        t1.append(TierEntry(
            tier=1, origin="task", name=c.name, path=c.path,
            due_iso=c.due_iso, surface_reason=c.surface_reason,
            source="auto-returned", confirmed=False,
            self_care=c.self_care, explicit_slot=c.explicit_slot,
            has_due_pattern=c.has_due_pattern,
        ))
        t1_keys.add(key)

    # 3. Auto-T1 routine candidates. A neglect-gap escalation carries its
    # own source (distinct provenance: "Duty because neglected" and "Duty
    # because due" are different claims — the #102 distinct-ids rule) and
    # the ``gap_escalated`` fact the slot classifier's rule 0 reads.
    for c in auto_t1_routine:
        key = _routine_key(c.routine_record, c.item_text)
        if key in t1_keys:
            continue
        t1.append(TierEntry(
            tier=1, origin="routine_item", name=c.name, path=c.path,
            due_iso=c.due_iso, surface_reason=c.surface_reason,
            source=(
                "auto-gap-escalated" if c.gap_escalated
                else "auto-due-routine"
            ),
            confirmed=False,
            routine_record=c.routine_record, item_text=c.item_text,
            self_care=c.self_care, explicit_slot=c.explicit_slot,
            has_due_pattern=c.has_due_pattern,
            backdate_limit_days=c.backdate_limit_days,
            gap_escalated=c.gap_escalated,
        ))
        t1_keys.add(key)

    # 4. Auto-T2 routine candidates (suppress if already in curated
    # T1 OR T2 — an item confirmed up to T1 shouldn't also show as a
    # T2 ramp suggestion).
    for c in auto_t2_routine:
        key = _routine_key(c.routine_record, c.item_text)
        if key in t1_keys or key in t2_keys:
            continue
        t2.append(TierEntry(
            tier=2, origin="routine_item", name=c.name, path=c.path,
            due_iso=c.due_iso, surface_reason=c.surface_reason,
            source="auto-surface-routine",
            escalation_state=_escalation_state_from_reason(c.surface_reason),
            routine_record=c.routine_record, item_text=c.item_text,
            self_care=c.self_care, explicit_slot=c.explicit_slot,
            has_due_pattern=c.has_due_pattern,
            backdate_limit_days=c.backdate_limit_days,
        ))
        t2_keys.add(key)

    # 5. Auto-T3 routine (soft-cadence) candidates. Carry the cadence
    # metadata so the brief's T3 render reads the view (Step 2c).
    for c in auto_t3_routine:
        key = _routine_key(c.routine_record, c.item_text)
        if key in t3_keys:
            continue
        # NOTE-2 cross-shape dedup: skip if the operator already curated
        # this item as free-text T3 (different key, same item).
        if (c.item_text or "").strip().lower() in curated_t3_texts:
            continue
        t3.append(TierEntry(
            tier=3, origin="routine_item", name=c.item_text, path=c.path,
            source="auto-cadence-routine",
            routine_record=c.routine_record, item_text=c.item_text,
            target_cadence_days=c.target_cadence_days,
            days_since_last_completed=c.days_since_last_completed,
            overdue_ratio=c.overdue_ratio,
            self_care=c.self_care, explicit_slot=c.explicit_slot,
        ))
        t3_keys.add(key)

    # 6. Self-care T3 candidates (Q2 — the dedicated self-care lane).
    # self_care-flagged items with no cadence target surface here as the
    # daily floor. Dedup against curated + cadence T3 by the same key.
    for c in self_care_routine:
        key = _routine_key(c.routine_record, c.item_text)
        if key in t3_keys:
            continue
        if (c.item_text or "").strip().lower() in curated_t3_texts:
            continue  # NOTE-2 cross-shape dedup
        t3.append(TierEntry(
            tier=3, origin="routine_item", name=c.item_text, path=c.path,
            surface_reason="self-care",
            source="self-care",
            routine_record=c.routine_record, item_text=c.item_text,
            self_care=c.self_care, explicit_slot=c.explicit_slot,
            has_due_pattern=c.has_due_pattern,
        ))
        t3_keys.add(key)

    # 7. Self-care TASK candidates (Q2 — self_care tasks with no near
    # deadline; near-deadline self_care tasks live in T1). Dedup by task
    # name key.
    for c in self_care_task:
        key = _task_key(c.name)
        if key in t3_keys:
            continue
        if c.name.strip().lower() in curated_t3_texts:
            continue  # NOTE-2 cross-shape dedup
        t3.append(TierEntry(
            tier=3, origin="task", name=c.name, path=c.path,
            surface_reason="self-care",
            source="self-care",
            self_care=c.self_care, explicit_slot=c.explicit_slot,
        ))
        t3_keys.add(key)

    # --- board snooze ---------------------------------------------
    # Drop rows the operator parked, BEFORE the daily goal is computed — a
    # hidden row must not count toward the day's target. ``snooze_path=None``
    # (the default) leaves the feature entirely inert: no store, no filter,
    # byte-identical view. Applied HERE, in the one projection the board and
    # the brief's tier section both read, so the two can never disagree.
    # The path normally arrives on ``tier_defaults`` (stamped once at brief
    # config load), which is why no production caller passes it explicitly —
    # an explicit argument is the test/override path. Reading it from the
    # already-threaded bundle is what makes the read side reachable from all
    # three callers without a 4th parameter any of them could forget.
    if snooze_path is None:
        snooze_path = getattr(tier_defaults, "snooze_path", "") or None
    snooze_suppressed = 0
    if snooze_path is not None:
        from alfred.tier.snooze import filter_snoozed_entries, load_snoozes

        _snoozes = load_snoozes(snooze_path)
        if _snoozes:
            for _lane_name, _lane in (("t1", t1), ("t2", t2), ("t3", t3)):
                _kept, _stats = filter_snoozed_entries(
                    _lane, _snoozes, today=today,
                )
                _lane[:] = _kept
                snooze_suppressed += _stats.suppressed
                # STAMP, then log (#14). The log line alone left the reason
                # dying here: the producer never saw it, so the card could not
                # answer "why is this back?" — the one question an early return
                # provokes. Stamped onto the entry the projection already holds,
                # so it travels the normal path to evidence with no new plumbing.
                #
                # Matched by the SAME `slot_stable_key` the filter keyed on, so
                # a reason can never land on the wrong row (the key and the
                # card's feed id are already required to agree — see
                # feed_producer's slot_stable_key delegation).
                _reasons = dict(_stats.broke_through)
                if _reasons:
                    from alfred.tier.snooze import slot_stable_key as _stable_key

                    for _entry in _lane:
                        _r = _reasons.get(_stable_key(_entry))
                        if _r:
                            _entry.snooze_breakthrough = _r
                for _key, _reason in _stats.broke_through:
                    # A card returning EARLY must be explicable, never
                    # mysterious — name which delta fired.
                    log.info(
                        "board.snooze_breakthrough",
                        lane=_lane_name, key=_key, reason=_reason,
                    )

    # --- slot classification (#18 slice 1 — classify + observe) ---
    # ONE pass over the assembled lanes rather than a stamp at each of the six
    # construction sites: slot is a property of the item, not of the lane it
    # landed in, and six copies of the call is six chances for one to drift.
    #
    # Placed AFTER the snooze filter so the coverage number describes the board
    # the operator will actually see — a parked row is not part of today.
    # Placed BEFORE the daily goal because stage 3 flips that goal to read these
    # slots; keeping the order right now means that flip is a change of
    # definition, not a change of sequencing.
    #
    # STAGE 1 CONTRACT: this stamps and reports. It does NOT feed
    # ``_compute_daily_goal`` (still tier-based) and the rings still group by
    # tier. Nothing operator-visible changes except the coverage line.
    # HYDRATE CURATED ENTRIES FIRST — without this the classifier is blind on
    # every operator-curated row, and rule 1 ("the operator's word is final") is
    # DEAD for them.
    #
    # ``_curated_to_tier_entry`` / ``_curated_t3_to_tier_entry`` build from a
    # ``daily_curation`` entry, which carries only the item's identity (a
    # wikilink, a record+text pair, or — in the T3 lane — a bare string) — no
    # due date, no ``slot:``, no ``self_care``, no ``due_pattern``, no cadence,
    # because those live on the BACKING RECORD and the curation block never
    # copied them. Every auto lane reads them off the record; the three curated
    # converters are the construction sites that do not, so a curated entry
    # arrives here with its classifier inputs empty and classifies
    # ``unslotted / no_signal`` no matter what its record said.
    #
    # MEASURED, 2026-08-19 (task side): a task with ``due: 2026-08-21`` AND
    # ``slot: duty`` written on it, curated into T2, reported
    # ``slot='unslotted', slot_rule='no_signal'``. That is the operator's
    # screenshot — four dated tasks under "NOT SORTED YET" — and it is also why
    # writing a slot from the board would have appeared to do nothing.
    #
    # MEASURED, 2026-08-21 (routine side, the other half of the same bug):
    # ``Hot Tub Chemistry`` carries ``slot: rhythm`` AND
    # ``target_cadence_days: 1`` on ``routine/Core Daily.md``. Accepted into
    # today's board it reported ``unslotted / no_signal`` and landed in no ring
    # at all, while the SAME item un-accepted classified ``rhythm / explicit``.
    # Accepting a card was silently costing it its ring.
    #
    # Hydration is CONSERVATIVE: it fills only fields the curated entry left
    # empty, so a value the converter did set still wins, and it is a pure read
    # of the record. It cannot change any already-slotted row. ``today`` is
    # threaded for the cadence pair (days-since is a fact about TODAY, and the
    # brief's cadence annotation is false without it).
    _hydrate_curated_entries(vault_path, (t1, t2, t3), today=today)

    _slot_verdicts: list[slots.SlotVerdict] = []
    for _lane in (t1, t2, t3):
        for _entry in _lane:
            _verdict = slots.classify_slot(_entry, learned=slots.NoOverrides())
            _entry.slot = _verdict.slot
            _entry.slot_rule = _verdict.rule
            _slot_verdicts.append(_verdict)
    slot_coverage = slots.summarize_coverage(_slot_verdicts)
    slots.log_coverage(slot_coverage, where="compute_today_view")

    # --- routine_today: the complement (fired today, no handoff) ---
    routine_today = _collect_routine_today(vault_path, today, tier_defaults)

    # --- daily goal -----------------------------------------------
    daily_goal = _compute_daily_goal(vault_path, today, t1, t2, t3)

    log.info(
        "brief.today_view.computed",
        t1_count=len(t1),
        t2_count=len(t2),
        t3_count=len(t3),
        routine_today_count=len(routine_today),
        balanced_day=daily_goal.balanced_day,
        all_t1_done=daily_goal.all_t1_done,
        t1_done=daily_goal.t1_done,
        t2_done=daily_goal.t2_done,
        t3_done=daily_goal.t3_done,
        curation_loaded=curation is not None,
        # ILB: a shorter board must be explicable as "you parked these" rather
        # than looking like the projection lost them. Always emitted, including
        # 0 and including when the feature is inert.
        snooze_suppressed=snooze_suppressed,
    )

    return TodayView(
        t1=t1, t2=t2, t3=t3,
        routine_today=routine_today,
        daily_goal=daily_goal,
        slot_coverage=slot_coverage,
    )


@dataclass(frozen=True)
class _RoutineItemFacts:
    """What one routine ITEM says about itself — the hydration payload.

    Everything here is read off the item's own entry in its record's ``items``
    list (plus that record's ``completion_log`` for the cadence pair). Nothing
    is inherited from the RECORD and nothing is guessed from a sibling item;
    see :func:`_hydrate_curated_entries` on why that distinction is the whole
    argument for hydrating ``target_cadence_days`` here when the task path
    deliberately does not.
    """

    record_name: str
    explicit_slot: str | None
    self_care: bool
    has_due_pattern: bool
    target_cadence_days: int | None
    days_since_last_completed: int | None
    overdue_ratio: float | None


@dataclass(frozen=True)
class _RoutineItemIndex:
    """Routine items, keyed for the two curated shapes that need them.

    * ``by_record_and_text`` — the ANCHORED shape (a curated T1/T2 entry names
      ``routine_item: {record, text}``). Registered under the record's ``name``
      AND its filename stem, because the curated block carries whichever of the
      two the writer had in hand.
    * ``by_text`` — the FREE-TEXT shape (a curated T3 entry carries only
      ``item:``; :class:`alfred.tier.daily_curation.T3Entry` has no record
      field at all, so the anchor is gone by the time it is re-read). A list,
      not a single value: two records may legitimately carry an item of the
      same text, and that ambiguity is refused rather than resolved by a coin
      flip.

    ``records_scanned`` is reported on the miss signals so "found nothing" and
    "there was nothing to find" stay distinguishable.
    """

    by_record_and_text: dict[tuple[str, str], _RoutineItemFacts]
    by_text: dict[str, list[_RoutineItemFacts]]
    records_scanned: int


def _norm_key(raw: Any) -> str:
    """The identity spelling used for record names and item texts here.

    ``strip().lower()`` — the same normalisation ``_routine_key`` applies for
    lane dedup, plus a strip (``Item.from_dict`` and the confirm writer both
    strip, so a stored text and a parsed one can differ only in whitespace the
    operator hand-typed).
    """
    return str(raw or "").strip().lower()


def _build_routine_item_index(vault_path: Path, today: date) -> _RoutineItemIndex:
    """Read every routine record ONCE and index its items for hydration.

    The record-level filters are the SAME ones every auto surface applies —
    ``type: routine``, not ``status: archived``, not ``alfred_triage: true``.
    That is deliberate rather than defensive copying: hydration must see the
    vault the projection sees, so an archived record cannot silently supply a
    slot for a row no auto lane would ever have surfaced.
    """
    import frontmatter  # type: ignore[import-untyped]

    from alfred.routine.config import Item

    by_record_and_text: dict[tuple[str, str], _RoutineItemFacts] = {}
    by_text: dict[str, list[_RoutineItemFacts]] = {}
    scanned = 0

    routine_dir = vault_path / "routine"
    if not routine_dir.is_dir():
        return _RoutineItemIndex({}, {}, 0)

    for record_path in sorted(routine_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(record_path))
        except Exception:  # noqa: BLE001 — a hint is never worth the board
            continue
        fm = dict(post.metadata or {})
        if fm.get("type") != "routine":
            continue
        if str(fm.get("status") or "active").lower() == "archived":
            continue
        if fm.get("alfred_triage") is True:
            continue
        raw_items = fm.get("items") or []
        if not isinstance(raw_items, list):
            continue
        scanned += 1

        record_name = str(fm.get("name") or record_path.stem)
        completion_log = fm.get("completion_log") or {}
        if not isinstance(completion_log, dict):
            completion_log = {}

        for raw_item in raw_items:
            item = Item.from_dict(raw_item)
            if item is None:
                continue
            target = item.target_cadence_days
            if isinstance(target, int) and target > 0:
                days_since, ratio = _cadence_metadata(
                    _parse_item_completion_dates(
                        completion_log.get(item.text, [])
                    ),
                    target=target,
                    today=today,
                )
            else:
                # Not a soft-cadence item (or a zero/negative target, which
                # the classifier refuses too) — no cadence pair to carry.
                target, days_since, ratio = None, None, None

            facts = _RoutineItemFacts(
                record_name=record_name,
                explicit_slot=item.slot,
                self_care=item.self_care,
                has_due_pattern=item.due_pattern is not None,
                target_cadence_days=target,
                days_since_last_completed=days_since,
                overdue_ratio=ratio,
            )
            text_key = _norm_key(item.text)
            # Register under BOTH spellings of the record's identity; the
            # curated block may name either (the confirm writer stores the
            # record NAME, a hand-edit may use the filename).
            for record_key in {
                _norm_key(record_name), _norm_key(record_path.stem),
            }:
                by_record_and_text.setdefault((record_key, text_key), facts)
            by_text.setdefault(text_key, []).append(facts)

    return _RoutineItemIndex(by_record_and_text, by_text, scanned)


def _hydrate_curated_entries(vault_path: Path, lanes, *, today: date) -> None:
    """Fill a curated entry's missing classifier inputs from its backing record.

    In place, and only where the entry is EMPTY — an auto-built entry already
    read these off the record, and a curated entry that somehow carries a value
    keeps it. Reading, never writing: the slot axis is a producer-time overlay
    and this is part of computing it.

    **Two branches, because the two curated shapes lose different things.**

    *Task origin* — the entry names a ``task/`` record, so the fields are read
    from that record's frontmatter: ``explicit_slot`` (rule 1), ``self_care``
    (rule 3), ``has_due_pattern`` (rule 4) and ``due_iso`` (rule 6).
    ``target_cadence_days`` (rule 5) is deliberately NOT hydrated on this
    branch: it is a per-ITEM routine field rather than a record-level one, and
    guessing it from the record would place a habit anchor in Rhythm on the
    strength of a sibling item's cadence.

    *Routine origin* (2026-08-21) — the fields live on the item's own entry
    inside the record's ``items`` list, so this branch reads THE ITEM. It
    hydrates ``target_cadence_days``, and the objection quoted above does NOT
    transfer: that objection is about provenance — reading a RECORD-level value
    and attributing it to one item. Reading the item's own entry gives that
    item's cadence and no sibling's; when the matched item carries no cadence
    the field stays ``None`` and rule 5 does not fire, which is an absence, not
    a guess. The decisive argument is agreement: the auto-T3 lane already puts
    ``target_cadence_days`` on the TierEntry for this very item, so hydrating
    it makes the ACCEPTED copy classify the same as the card that was accepted.
    Accept must be slot-preserving; before this it was not (measured
    2026-08-21: ``Hot Tub Chemistry``, ``slot: rhythm`` +
    ``target_cadence_days: 1`` on the record, accepted into T3, reported
    ``unslotted / no_signal`` — landing in no ring at all).

    ``days_since_last_completed`` (and ``overdue_ratio``) ride along with the
    cadence value and are NOT optional extras: the brief renders a cadence row
    with no days-since as *"never done; target every Nd"*
    (``T3_AUTO_DAYS_SINCE_NEVER_LABEL``). Hydrating the target alone would put
    a confident false claim on the operator's morning for any item completed
    recently. The two travel together or the render lies.

    ``due_iso`` is NOT hydrated on the routine branch: rule 6 is task-origin
    only, so it is not a classifier input here, and a curated T1 routine row
    already receives the auto candidate's due via ``auto_due_by_t1_key``.

    **Identity is never hydrated.** A free-text T3 row whose text resolves to a
    routine item keeps ``routine_record=None``. Stamping the resolved record on
    it would move the row's dedup key AND move its done-state home from the
    entry's own ``done_at`` to that record's ``completion_log`` (see
    :class:`alfred.tier.daily_curation.T3Entry` — ``done_at`` is the ONLY
    done-state home for a free-text item). This function fills classifier
    inputs; it does not re-anchor rows.

    **The named failure mode, answered.** A renamed item resolves to nothing
    and the row stays unslotted — indistinguishable, from the outside, from a
    record that genuinely says nothing. Every unresolved routine-origin row
    therefore emits a signal, and the reasons are distinct because the causes
    are: ``unknown_record`` / ``no_such_item`` / ``ambiguous_text`` are WARNs
    (an entry that NAMES a record and misses it is anomalous; an ambiguous text
    is a refusal to guess), while a free-text row matching no routine item is
    the ORDINARY case for that lane — "Read for an hour" is a genuine ad-hoc
    intention, not a rename — and gets its own INFO event so it can never be
    read as the anomaly. One ``tier.hydrate.routine_summary`` per projection
    reports the rollup, including zero (intentionally-left-blank; the same
    unconditional-emission contract ``slots.log_coverage`` keeps one caller up).

    Failure is silent BY DESIGN for the RECORD read: an unreadable or absent
    record leaves the entry exactly as it was, which is the pre-existing
    behaviour. A projection that raised because one task file was mid-write
    would take the whole morning board down over a field that is, at worst, a
    missing hint. (Silent means "does not raise" — the unresolved row still
    announces itself per the paragraph above.)
    """
    import frontmatter  # type: ignore[import-untyped]

    from alfred.routine.config import _coerce_self_care

    cache: dict[str, dict] = {}

    def _fm_for(rel_path: str) -> dict:
        if rel_path not in cache:
            cache[rel_path] = {}
            try:
                post = frontmatter.load(str(vault_path / rel_path))
                cache[rel_path] = dict(post.metadata or {})
            except Exception:  # noqa: BLE001 — a hint is never worth the board
                pass
        return cache[rel_path]

    index: _RoutineItemIndex | None = None
    considered = hydrated = unmatched = ambiguous = 0

    for lane in lanes:
        for entry in lane:
            if entry.origin == "task":
                # Only CURATED entries reach here empty on all four; an auto
                # entry populated them at construction. Testing the fields
                # rather than the source string keeps this correct if a new
                # curated source appears.
                needs = (
                    entry.explicit_slot is None
                    or not entry.due_iso
                    or not entry.self_care
                    or not entry.has_due_pattern
                )
                if not needs:
                    continue
                rel = (entry.path or "").strip()
                if not rel:
                    continue
                fm = _fm_for(rel)
                if not fm:
                    continue
                if entry.explicit_slot is None and fm.get("slot") is not None:
                    entry.explicit_slot = str(fm["slot"]).strip()
                if not entry.due_iso and fm.get("due"):
                    entry.due_iso = str(fm["due"]).strip()
                if not entry.self_care:
                    entry.self_care = _coerce_self_care(
                        fm.get("self_care", False)
                    )
                if not entry.has_due_pattern and fm.get("due_pattern"):
                    entry.has_due_pattern = True
                continue

            if entry.origin != "routine_item":
                continue

            text = (entry.item_text or entry.name or "").strip()
            if not text:
                continue
            # ALL FOUR empty — the blind-construction signature, and an AND
            # rather than the task branch's OR on purpose. On the routine side
            # the OR is vacuous: ``not self_care`` and ``not has_due_pattern``
            # are true of most perfectly-hydrated auto rows, so an OR would
            # re-read the whole routine directory on every projection and count
            # rows that needed nothing. A row carrying ANY classifier input was
            # built by a lane that read the item; only the curated converters
            # produce one with none. (Testing the FIELDS, not the source string,
            # so a new curated source is covered the day it appears.)
            blind = (
                entry.explicit_slot is None
                and not entry.self_care
                and not entry.has_due_pattern
                and entry.target_cadence_days is None
            )
            if not blind:
                continue
            considered += 1

            if index is None:
                index = _build_routine_item_index(vault_path, today)

            record = (entry.routine_record or "").strip()
            text_key = _norm_key(text)
            facts: _RoutineItemFacts | None = None
            reason: str | None = None
            candidates: list[str] = []

            if record:
                facts = index.by_record_and_text.get((_norm_key(record), text_key))
                if facts is None:
                    # WHY it missed, because the two causes call for different
                    # operator actions: a record nobody can find vs. an item
                    # text that no longer matches anything inside a record that
                    # IS there (the rename).
                    reason = (
                        "no_such_item"
                        if any(
                            key[0] == _norm_key(record)
                            for key in index.by_record_and_text
                        )
                        else "unknown_record"
                    )
            else:
                matches = index.by_text.get(text_key, [])
                # Distinct RECORDS, not distinct entries: one record listing
                # the same text twice is a malformed record, not a genuine
                # ambiguity about whose cadence applies.
                by_record = {m.record_name: m for m in matches}
                if len(by_record) == 1:
                    facts = next(iter(by_record.values()))
                elif len(by_record) > 1:
                    reason = "ambiguous_text"
                    candidates = sorted(by_record)
                else:
                    reason = "no_routine_match"

            if facts is None:
                if reason == "ambiguous_text":
                    ambiguous += 1
                else:
                    unmatched += 1
                _log_unresolved_routine_entry(
                    entry, reason=reason or "no_routine_match",
                    text=text, candidates=candidates,
                    records_scanned=index.records_scanned,
                )
                continue

            if entry.explicit_slot is None and facts.explicit_slot is not None:
                entry.explicit_slot = facts.explicit_slot
            if not entry.self_care:
                entry.self_care = facts.self_care
            if not entry.has_due_pattern:
                entry.has_due_pattern = facts.has_due_pattern
            if entry.target_cadence_days is None:
                entry.target_cadence_days = facts.target_cadence_days
                # Paired — see the docstring. Never one without the other.
                if entry.days_since_last_completed is None:
                    entry.days_since_last_completed = (
                        facts.days_since_last_completed
                    )
                if entry.overdue_ratio is None:
                    entry.overdue_ratio = facts.overdue_ratio
            hydrated += 1

    # ILB: emitted every projection, including the all-zero one. "The hydrator
    # found nothing to do" and "the hydrator stopped running" are the same
    # picture without this line, and the rename signal below it is only
    # trustworthy if its absence means something.
    log.info(
        "tier.hydrate.routine_summary",
        considered=considered,
        hydrated=hydrated,
        unmatched=unmatched,
        ambiguous=ambiguous,
        records_scanned=(index.records_scanned if index is not None else 0),
    )


def _log_unresolved_routine_entry(
    entry: TierEntry, *, reason: str, text: str,
    candidates: list[str], records_scanned: int,
) -> None:
    """Announce a curated routine row that hydrated from nothing.

    Level is chosen by what the miss MEANS, not by convenience. An entry that
    names a record (``unknown_record`` / ``no_such_item``) or a text that two
    records both claim (``ambiguous_text``) is anomalous — WARN. A free-text T3
    row matching no routine item is the ordinary shape of that lane and gets an
    INFO under its OWN event name, so a WARN grep for renames is never diluted
    by every ad-hoc intention the operator types.
    """
    fields = dict(
        reason=reason,
        # ILB data shape: always present, empty for the free-text shape, so a
        # consumer can never mistake "no record named" for "field not emitted".
        record=(entry.routine_record or ""),
        item_text=text,
        tier=entry.tier,
        source=entry.source,
        records_scanned=records_scanned,
        candidates=candidates,
    )
    if reason == "no_routine_match":
        log.info(
            "tier.hydrate.free_text_no_routine_match",
            hint=(
                "free-text row matches no routine item — expected for an "
                "ad-hoc intention; it stays unslotted because there is "
                "genuinely nothing to read."
            ),
            **fields,
        )
        return
    log.warning(
        "tier.hydrate.routine_item_unresolved",
        hint=(
            "curated routine row could not be resolved to an item, so its "
            "slot inputs are unreadable and it will render unslotted. A "
            "renamed item or a renamed record is the usual cause; "
            "ambiguous_text means two records claim this text and the "
            "classifier refuses to guess which cadence applies."
        ),
        **fields,
    )


def _entry_key(entry: TierEntry, task_key, routine_key) -> str:
    if entry.origin == "task":
        return task_key(entry.name)
    return routine_key(entry.routine_record, entry.item_text)


def _escalation_state_from_reason(reason: str | None) -> str | None:
    """Translate a T2 ``"surface window (Nd before due)"`` reason into a
    climb hint ``"T2→T1 in ~Nd"``. Best-effort; returns None when the
    reason doesn't carry a parseable day count."""
    if not reason or "surface window" not in reason:
        return None
    import re

    m = re.search(r"\((\d+)d before due\)", reason)
    if not m:
        return None
    return f"T2 (escalates to T1 as due nears, ~{m.group(1)}d out)"


def _curated_to_tier_entry(entry: Any, *, tier: int) -> TierEntry | None:
    """Convert a ``daily_curation`` T1/T2 entry to a TierEntry. Returns
    None for an entry with neither task nor routine_item populated."""
    if getattr(entry, "task", None):
        name = _curated_task_display_name(entry.task)
        return TierEntry(
            tier=tier, origin="task", name=name,
            path=_curated_task_path(entry.task),
            source=getattr(entry, "source", "operator") or "operator",
            confirmed=(
                getattr(entry, "confirmed", None) if tier == 1 else None
            ),
        )
    ri = getattr(entry, "routine_item", None)
    if ri is not None:
        record = getattr(ri, "record", None) or (
            ri.get("record") if isinstance(ri, dict) else None
        )
        text = getattr(ri, "text", None) or (
            ri.get("text") if isinstance(ri, dict) else None
        )
        return TierEntry(
            tier=tier, origin="routine_item", name=str(text or ""),
            path=f"routine/{record}.md" if record else "routine/",
            source=getattr(entry, "source", "operator") or "operator",
            confirmed=(
                getattr(entry, "confirmed", None) if tier == 1 else None
            ),
            routine_record=str(record) if record else None,
            item_text=str(text) if text else None,
        )
    return None


def _curated_t3_to_tier_entry(entry: Any) -> TierEntry | None:
    """Convert a ``daily_curation`` T3 entry (free-text ``item:``) to a
    TierEntry.

    Arc #20: carries the entry's ``done_at`` (the free-text T3 done-state)
    onto the TierEntry so ``_entry_done`` can count it toward the daily
    goal. ``None`` for an unmarked item (the common case)."""
    text = getattr(entry, "item", None)
    if not text:
        return None
    return TierEntry(
        tier=3, origin="routine_item", name=str(text),
        path="routine/",
        source=getattr(entry, "source", "operator") or "operator",
        item_text=str(text),
        done_at=getattr(entry, "done_at", None),
    )


def _curated_task_display_name(wikilink: str) -> str:
    """Extract the display name from a ``[[task/Name]]`` wikilink (or a
    bare name). Mirrors the brief's ``_wikilink_to_record_name`` without
    importing the render layer."""
    s = (wikilink or "").strip().strip("[]").strip()
    if "/" in s:
        s = s.split("/", 1)[1]
    if "|" in s:
        s = s.split("|", 1)[1]
    return s.strip()


def _curated_task_path(wikilink: str) -> str:
    name = _curated_task_display_name(wikilink)
    return f"task/{name}.md"


def _collect_routine_today(
    vault_path: Path, today: date,
    tier_defaults: Any = None,
) -> list[RoutineLine]:
    """Return routine items that fired today but did NOT hand off to any
    tier — the structural complement of {t1, t2, t3} for routine items.

    Delegates to the aggregator's ``_collect_items_for_today`` (the
    existing single source for "what fired today and stays in the
    routine section") so this view never re-derives the cadence + handoff
    logic. Returns the per-item shape the render layer needs.

    ``quiet=True`` (Step 2c reviewer NOTE-1): the brief's tier view runs
    ~06:00, after the aggregate pass already wrote the daily file + logged
    each ``handed_off_to_tier`` at 05:59. This call is a derived READ over
    the same records; without ``quiet`` it would re-emit the same
    operator-facing handoff logs, duplicating them for every item. The
    aggregate pass owns that log; the view reads silently.

    ``tier_defaults`` (Q3 Option A): MUST be passed so the complement's
    handoff decision applies the SAME global defaults the tier lanes do —
    otherwise an item that the defaults push into a tier would still
    appear here (double-render, breaking the structural-complement
    invariant).
    """
    from alfred.routine.aggregator import (
        _collect_items_for_today,
        _iter_routine_records,
    )

    _def_esc, _def_surf, _def_fuel_gap = _tier_default_values(tier_defaults)
    records = _iter_routine_records(vault_path)
    items, _contributing, _critical = _collect_items_for_today(
        records, today, quiet=True,
        default_escalate_at_days=_def_esc,
        default_surface_at_days=_def_surf,
        default_fuel_escalate_after_gap_days=_def_fuel_gap,
    )
    out: list[RoutineLine] = []
    for it in items:
        line = RoutineLine(
            text=str(it.get("text") or ""),
            priority=str(it.get("priority") or "tracked"),
            annotation=str(it.get("annotation") or ""),
            time=str(it.get("time") or ""),
            explicit_slot=it.get("slot"),
            self_care=bool(it.get("self_care", False)),
            has_due_pattern=bool(it.get("has_due_pattern", False)),
            target_cadence_days=it.get("target_cadence_days"),
        )
        # Same classifier as the tier lanes (§4 dissolution) — a habit anchor
        # that never escalated is still Duty / Rhythm / Fuel, and answering
        # that question twice in two places is how the two answers drift.
        verdict = slots.classify_slot(line, learned=slots.NoOverrides())
        out.append(replace(line, slot=verdict.slot, slot_rule=verdict.rule))
    return out


def build_done_caches(vault_path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Load the substrate the done-predicate reads: task frontmatter by name +
    routine completion logs by record.

    Extracted from ``_compute_daily_goal`` so the SAME caches feed BOTH the
    daily-goal done-count AND the feed producer's per-item ``done`` flag — the
    producer must never re-derive doneness (ring/brief doneness disagreement is
    the gaslighting class Phase C slice 1 kills). Returns
    ``(task_fm_by_name, completion_by_record)``.
    """
    import frontmatter  # type: ignore[import-untyped]

    task_fm_by_name: dict[str, dict] = {}
    task_dir = vault_path / "task"
    if task_dir.is_dir():
        for p in sorted(task_dir.glob("*.md")):
            try:
                post = frontmatter.load(str(p))
            except Exception:  # noqa: BLE001
                continue
            fm = dict(post.metadata or {})
            if fm.get("type") != "task":
                continue
            name = str(fm.get("name") or p.stem)
            task_fm_by_name.setdefault(name, fm)

    completion_by_record: dict[str, dict] = {}
    routine_dir = vault_path / "routine"
    if routine_dir.is_dir():
        for p in sorted(routine_dir.glob("*.md")):
            try:
                post = frontmatter.load(str(p))
            except Exception:  # noqa: BLE001
                continue
            fm = dict(post.metadata or {})
            if fm.get("type") != "routine":
                continue
            rec = str(fm.get("name") or p.stem)
            cl = fm.get("completion_log") or {}
            completion_by_record[rec] = cl if isinstance(cl, dict) else {}

    return task_fm_by_name, completion_by_record


def entry_is_done(
    entry: TierEntry,
    *,
    task_fm_by_name: dict[str, dict],
    completion_by_record: dict[str, dict],
    today: date,
) -> bool:
    """THE done-predicate for a tier-lane entry — the SINGLE source of truth
    for "is this item completed today", reused by the daily-goal count AND the
    feed producer's ``done`` flag (identity-pinned: fork this → both reds).

    "Done" =
      * task-origin entry → the task record's status is closed today
        (``_task_is_done_today``),
      * routine-origin entry with a record → the item is completed today in
        that record's ``completion_log``,
      * free-text T3 entry → its ad-hoc ``done_at`` is today (Arc #20), else a
        same-text completion today in ANY routine record (the pre-Arc-#20
        fallback).

    ``task_fm_by_name`` / ``completion_by_record`` come from
    :func:`build_done_caches`.
    """
    if entry.origin == "task":
        fm = task_fm_by_name.get(entry.name)
        if fm is None:
            return False
        return _task_is_done_today(fm, today)
    # routine_item: completed today (a date in completion_log[text] equal to
    # today). The classifier already excludes completed-this-cycle items from
    # the lanes, so this catches the operator completing a curated routine item
    # during the day.
    text_key = entry.item_text or entry.name
    if entry.routine_record:
        # Record-anchored entry (auto-surfaced or curated routine_item with a
        # record): look up that record's completion log.
        cl = completion_by_record.get(entry.routine_record, {})
        dates = _parse_item_completion_dates(cl.get(text_key, []))
        return today in dates
    # Free-text T3 entry (curated ``item:`` with no record anchor —
    # "Meditate", "Read for an hour", "Rake leaves").
    #
    # Arc #20: the item's OWN ad-hoc done-state comes first — an operator
    # ``tier_done`` stamps ``done_at`` on the entry, and a free-text ad-hoc
    # intention ("rake leaves") has no routine to map to, so this is the ONLY
    # signal that will fire for it. Count it done when ``done_at`` is today (a
    # back-dated ``done_at`` from a prior day does NOT count toward today).
    if entry.done_at is not None and entry.done_at == today.isoformat():
        return True
    # Fallback for a free-text item that DOES map to a routine item somewhere:
    # scan ALL routine completion logs for a same-text completion today (the
    # honest "did the operator complete this intention today" signal,
    # pre-Arc-#20 behaviour, preserved).
    for cl in completion_by_record.values():
        dates = _parse_item_completion_dates(cl.get(text_key, []))
        if today in dates:
            return True
    return False


def _compute_daily_goal(
    vault_path: Path,
    today: date,
    t1: list[TierEntry],
    t2: list[TierEntry],
    t3: list[TierEntry],
) -> DailyGoalState:
    """Compute the one-of-each-tier daily goal over the assembled lanes.

    Counts available + done per lane. "Done" =
      * task-origin entry → the task record's status is closed
        (``_task_is_done_today``),
      * routine-origin entry → the item is completed in its current
        cycle (or today, for soft-cadence) per its routine record's
        ``completion_log``.

    Note: items that classified into a tier are by definition NOT
    completed-this-cycle (the classifier suppresses completed items via
    ``completion_satisfies_current_cycle``). So tier-lane ``*_done``
    counts come from CURATED entries the operator placed AND later
    completed (a curated T1 task the operator finished), plus any
    auto-task entry whose status flipped to done after surfacing. This
    is the honest "did you finish something in this lane today" signal.
    """
    # Per-entry doneness delegates to the module-level ``entry_is_done`` (the
    # SAME predicate the feed producer's ``done`` flag uses, over caches from
    # ``build_done_caches``) so the daily-goal count and the ring green-dots can
    # never disagree on "done".
    task_fm_by_name, completion_by_record = build_done_caches(vault_path)

    def _entry_done(entry: TierEntry) -> bool:
        return entry_is_done(
            entry,
            task_fm_by_name=task_fm_by_name,
            completion_by_record=completion_by_record,
            today=today,
        )

    t1_done = sum(1 for e in t1 if _entry_done(e))
    t2_done = sum(1 for e in t2 if _entry_done(e))
    t3_done = sum(1 for e in t3 if _entry_done(e))

    balanced_day = t1_done >= 1 and t2_done >= 1 and t3_done >= 1
    # all_t1_done is vacuously True when there are no T1 items.
    all_t1_done = (len(t1) == 0) or (t1_done == len(t1))

    return DailyGoalState(
        t1_available=len(t1),
        t2_available=len(t2),
        t3_available=len(t3),
        t1_done=t1_done,
        t2_done=t2_done,
        t3_done=t3_done,
        balanced_day=balanced_day,
        all_t1_done=all_t1_done,
    )


__all__ = [
    "AutoT1Candidate",
    "AutoT3Candidate",
    "DailyGoalState",
    "OPEN_STATUSES",
    "RoutineItemClassification",
    "RoutineLine",
    "TierEntry",
    "TodayView",
    "build_done_caches",
    "classify_routine_item",
    "coerce_due_date",
    "compute_auto_routine_candidates",
    "compute_auto_routine_t2_candidates",
    "compute_auto_t1_candidates",
    "compute_auto_t3_candidates",
    "compute_self_care_candidates",
    "compute_self_care_task_candidates",
    "compute_returned_task_candidates",
    "compute_today_view",
    "entry_is_done",
]
