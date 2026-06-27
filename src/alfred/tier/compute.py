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

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

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
    """

    tier: int | None
    reason: str | None = None
    effective_due: date | None = None
    both_modes_conflict: bool = False


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
) -> RoutineItemClassification:
    """Classify one routine item into a tier placement (T1/T2/T3/none).

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
         the caller can emit the operator warn.
      3. **T3 self-care branch** (``due_pattern`` absent, ``self_care``
         true): the dedicated self-care lane (operator decision Q2,
         2026-06-26). An intrinsic classification — NOT deadline-driven,
         never escalates. Surfaces to T3 when NOT completed today (the
         daily self-care floor), so the operator's self-care item is
         deliberately included each day rather than skipped as "less
         necessary." Composes with ``target_cadence_days``: a self_care
         item ALSO carrying a cadence target surfaces if EITHER overdue
         against cadence OR not-done-today.
      4. **T3 soft-cadence branch** (``due_pattern`` absent,
         ``target_cadence_days`` present): surface (tier 3) when
         ``days_since_last_completed >= target_cadence_days`` (inclusive),
         OR when never completed (max overdue). Within window → no
         handoff (tier None).
      5. **T1/T2 deadline branch** (``due_pattern`` + ``escalate_at_days``
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

    # ---- Aspirational skip (T1/T2 only) --------------------------
    # An aspirational item with due_pattern does NOT take the T1/T2
    # handoff (operator semantic: T3 is for self-care intentions, not
    # deadline-driven work). It still falls through to the T3 branch
    # below if it carries target_cadence_days.
    aspirational = (priority or "").lower() == "aspirational"

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

    # ---- T1/T2 deadline branch -----------------------------------
    if due_pattern is None or escalate_at_days is None:
        return RoutineItemClassification(
            tier=None, both_modes_conflict=both_modes_conflict,
        )
    if aspirational:
        # Aspirational + deadline-bearing: never T1/T2. (No T3 either —
        # the T3 branch above only fires for target_cadence_days items;
        # an aspirational due_pattern item just renders in the routine
        # section.)
        return RoutineItemClassification(
            tier=None, both_modes_conflict=both_modes_conflict,
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
        )

    # Phase 2C C1 overdue retention: effective_due = prev_due when the
    # prior cycle lapsed unsatisfied → days_to_due negative → T1 admits.
    effective_due = overdue_effective_due(
        due_pattern, completion_log, item_text, today,
    )
    if effective_due is None:
        return RoutineItemClassification(
            tier=None, both_modes_conflict=both_modes_conflict,
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
        )
    return RoutineItemClassification(
        tier=None, both_modes_conflict=both_modes_conflict,
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
        if due == today_local:
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
        candidates.append(AutoT1Candidate(
            path=rel_path,
            name=name,
            due_iso=due.isoformat(),
            surface_reason=reason,
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

    Per ``feedback_intentionally_left_blank``: pure-compute path,
    no log emissions. Callers (Ship B brief render) emit the
    "ran, here's the count" log.
    """
    return _compute_auto_routine(vault_path, now, window="t1")


def compute_auto_routine_t2_candidates(
    vault_path: Path, now: datetime,
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
    """
    return _compute_auto_routine(vault_path, now, window="t2")


def _compute_auto_routine(
    vault_path: Path, now: datetime, *, window: str,
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
            )

            want_tier = 1 if window == "t1" else 2
            if classification.tier != want_tier:
                continue
            # T1/T2 classifications always carry a reason + effective_due
            # (the classifier only omits them for T3 / no-handoff).
            assert classification.reason is not None
            assert classification.effective_due is not None

            candidates.append(AutoT1Candidate(
                path=rel_path,
                name=item.text,
                due_iso=classification.effective_due.isoformat(),
                surface_reason=classification.reason,
                origin="routine",
                routine_record=record_name,
                item_text=item.text,
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


def compute_auto_t3_candidates(
    vault_path: Path, now: datetime,
) -> list[AutoT3Candidate]:
    """Walk ``vault/routine/*.md`` and return routine items
    auto-suggesting as T3 candidates (overdue against their soft
    cadence target).

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

            completion_dates = _parse_item_completion_dates(
                completion_log.get(item.text, [])
            )
            days_since_value: int | None
            if completion_dates:
                most_recent = max(completion_dates)
                days_since = (today_local - most_recent).days
                # Future-dated completion (operator hand-edit) → clamp.
                # (The classifier already clamps for its predicate; we
                # clamp here too so the ratio matches.)
                if days_since < 0:
                    days_since = 0
                days_since_value = days_since
                ratio = days_since / target
            else:
                # Never completed — treat as max overdue.
                days_since_value = None
                ratio = float("inf")

            candidates.append(AutoT3Candidate(
                path=rel_path,
                routine_record=record_name,
                item_text=item.text,
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
) -> list[AutoT1Candidate]:
    """Walk ``vault/routine/*.md`` and return ``self_care``-flagged items
    that surface to the T3 self-care lane (Q2, 2026-06-26).

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
        # Coerce self_care the same way Item.from_dict does (string-form
        # defensive).
        sc_raw = fm.get("self_care", False)
        if isinstance(sc_raw, str):
            self_care = sc_raw.strip().lower() in ("true", "yes", "1", "on")
        else:
            self_care = bool(sc_raw)
        if not self_care:
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
        ))

    candidates.sort(key=lambda c: c.name.lower())
    return candidates


# ---------------------------------------------------------------------------
# compute_today_view — the unified "today" view (Step 2b, 2026-06-26)
# ---------------------------------------------------------------------------
#
# The spec's keystone: ONE computed read over the substrate (tasks +
# behind-routines + imminent-events) that the voice/surfacing layer
# renders into channels. The brief's "Open Tasks by Tier" + "Today's
# Routines" become two RENDERINGS of this one object — collapsing the
# hand-mirrored two-pipeline math.
#
# Structural-dedup invariant: a routine item appears in EXACTLY ONE of
# {t1, t2, t3, routine_today}. Items that classify into a tier (via the
# single ``classify_routine_item`` predicate) land in t1/t2/t3; items
# that fired today but did NOT classify land in ``routine_today``. The
# two are complements by construction — no convention-enforced dedup.
#
# This commit (2b) builds the VIEW + the daily-goal. The render
# re-pointing (making ``render_tier_section`` / ``render_routine_section``
# consume this object) is Step 2c.


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


@dataclass
class RoutineLine:
    """One routine item that fired today but did NOT classify into any
    tier — the complement of {t1, t2, t3} for routine items.

    These render in the brief's "Today's Routines" section. The fields
    mirror what the aggregator's ``_collect_items_for_today`` already
    produces per item (text + priority + annotation + time), so Step 2c's
    render re-point is a straight read.
    """

    text: str
    priority: str
    annotation: str = ""
    time: str = ""


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

    Two renderings consume this:
      * brief "Open Tasks by Tier" ← ``t1`` / ``t2`` / ``t3`` + ``daily_goal``
      * brief "Today's Routines"   ← ``routine_today``

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


def _task_is_done_today(fm: dict, today: date) -> bool:
    """Return True iff a task record counts as completed for the daily
    goal: status is closed (done/cancelled) AND it was closed today
    (when a ``completed`` / ``done`` date is present), else just closed.

    Defensive: a closed task without a completion date still counts
    (operator marked it done; we can't prove it wasn't today, and
    counting it keeps the goal encouraging rather than pedantic).
    """
    status = str(fm.get("status") or "todo").lower()
    if status in OPEN_STATUSES:
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
) -> TodayView:
    """Build the unified today view over the substrate.

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
    auto_t1_routine = compute_auto_routine_candidates(vault_path, now)
    auto_t2_routine = compute_auto_routine_t2_candidates(vault_path, now)
    auto_t3_routine = compute_auto_t3_candidates(vault_path, now)
    # Q2 (2026-06-26): self_care-flagged items (no cadence target) →
    # the dedicated T3 self-care lane (daily floor). Both routine-item
    # and task origins.
    self_care_routine = compute_self_care_candidates(vault_path, now)
    self_care_task = compute_self_care_task_candidates(vault_path, now)

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

    def _task_key(name: str) -> str:
        return f"task::{name.lower()}"

    def _routine_key(record: str | None, text: str | None) -> str:
        return f"routine::{(record or '').lower()}::{(text or '').lower()}"

    # 1. Curated entries first (authoritative).
    if curation is not None:
        for e in curation.t1:
            entry = _curated_to_tier_entry(e, tier=1)
            if entry is not None:
                t1.append(entry)
                t1_keys.add(_entry_key(entry, _task_key, _routine_key))
        for e in curation.t2:
            entry = _curated_to_tier_entry(e, tier=2)
            if entry is not None:
                t2.append(entry)
                t2_keys.add(_entry_key(entry, _task_key, _routine_key))
        for e in curation.t3:
            entry = _curated_t3_to_tier_entry(e)
            if entry is not None:
                t3.append(entry)
                t3_keys.add(_routine_key(None, entry.item_text or entry.name))

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
        ))
        t1_keys.add(key)

    # 3. Auto-T1 routine candidates.
    for c in auto_t1_routine:
        key = _routine_key(c.routine_record, c.item_text)
        if key in t1_keys:
            continue
        t1.append(TierEntry(
            tier=1, origin="routine_item", name=c.name, path=c.path,
            due_iso=c.due_iso, surface_reason=c.surface_reason,
            source="auto-due-routine", confirmed=False,
            routine_record=c.routine_record, item_text=c.item_text,
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
        ))
        t2_keys.add(key)

    # 5. Auto-T3 routine (soft-cadence) candidates.
    for c in auto_t3_routine:
        key = _routine_key(c.routine_record, c.item_text)
        if key in t3_keys:
            continue
        t3.append(TierEntry(
            tier=3, origin="routine_item", name=c.item_text, path=c.path,
            source="auto-cadence-routine",
            routine_record=c.routine_record, item_text=c.item_text,
        ))
        t3_keys.add(key)

    # 6. Self-care T3 candidates (Q2 — the dedicated self-care lane).
    # self_care-flagged items with no cadence target surface here as the
    # daily floor. Dedup against curated + cadence T3 by the same key.
    for c in self_care_routine:
        key = _routine_key(c.routine_record, c.item_text)
        if key in t3_keys:
            continue
        t3.append(TierEntry(
            tier=3, origin="routine_item", name=c.item_text, path=c.path,
            surface_reason="self-care",
            source="self-care",
            routine_record=c.routine_record, item_text=c.item_text,
        ))
        t3_keys.add(key)

    # 7. Self-care TASK candidates (Q2 — self_care tasks with no near
    # deadline; near-deadline self_care tasks live in T1). Dedup by task
    # name key.
    for c in self_care_task:
        key = _task_key(c.name)
        if key in t3_keys:
            continue
        t3.append(TierEntry(
            tier=3, origin="task", name=c.name, path=c.path,
            surface_reason="self-care",
            source="self-care",
        ))
        t3_keys.add(key)

    # --- routine_today: the complement (fired today, no handoff) ---
    routine_today = _collect_routine_today(vault_path, today)

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
    )

    return TodayView(
        t1=t1, t2=t2, t3=t3,
        routine_today=routine_today,
        daily_goal=daily_goal,
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
    TierEntry."""
    text = getattr(entry, "item", None)
    if not text:
        return None
    return TierEntry(
        tier=3, origin="routine_item", name=str(text),
        path="routine/",
        source=getattr(entry, "source", "operator") or "operator",
        item_text=str(text),
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
) -> list[RoutineLine]:
    """Return routine items that fired today but did NOT hand off to any
    tier — the structural complement of {t1, t2, t3} for routine items.

    Delegates to the aggregator's ``_collect_items_for_today`` (the
    existing single source for "what fired today and stays in the
    routine section") so this view never re-derives the cadence + handoff
    logic. Returns the per-item shape the render layer needs.
    """
    from alfred.routine.aggregator import (
        _collect_items_for_today,
        _iter_routine_records,
    )

    records = _iter_routine_records(vault_path)
    items, _contributing, _critical = _collect_items_for_today(
        records, today,
    )
    out: list[RoutineLine] = []
    for it in items:
        out.append(RoutineLine(
            text=str(it.get("text") or ""),
            priority=str(it.get("priority") or "tracked"),
            annotation=str(it.get("annotation") or ""),
            time=str(it.get("time") or ""),
        ))
    return out


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
    import frontmatter  # type: ignore[import-untyped]

    # Cache task frontmatter by name + routine completion logs by record.
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

    def _entry_done(entry: TierEntry) -> bool:
        if entry.origin == "task":
            fm = task_fm_by_name.get(entry.name)
            if fm is None:
                return False
            return _task_is_done_today(fm, today)
        # routine_item: completed today (a date in completion_log[text]
        # equal to today). The classifier already excludes
        # completed-this-cycle items from the lanes, so this catches the
        # operator completing a curated routine item during the day.
        text_key = entry.item_text or entry.name
        if entry.routine_record:
            # Record-anchored entry (auto-surfaced or curated routine_item
            # with a record): look up that record's completion log.
            cl = completion_by_record.get(entry.routine_record, {})
            dates = _parse_item_completion_dates(cl.get(text_key, []))
            return today in dates
        # Free-text T3 entry (curated ``item:`` with no record anchor —
        # "Meditate", "Read for an hour"). Scan ALL routine completion
        # logs for a same-text completion today. This is the honest
        # "did the operator complete this intention today" signal when
        # the free-text maps to a routine item somewhere.
        for cl in completion_by_record.values():
            dates = _parse_item_completion_dates(cl.get(text_key, []))
            if today in dates:
                return True
        return False

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
    "classify_routine_item",
    "coerce_due_date",
    "compute_auto_routine_candidates",
    "compute_auto_routine_t2_candidates",
    "compute_auto_t1_candidates",
    "compute_auto_t3_candidates",
    "compute_self_care_candidates",
    "compute_self_care_task_candidates",
    "compute_today_view",
]
