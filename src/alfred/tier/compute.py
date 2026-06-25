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

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


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

    # Lazy imports — avoid the top-level circular hazard between
    # ``alfred.tier.compute`` and ``alfred.routine.due`` / config.
    # Both modules import from alfred.tier.compute (via cadence
    # symbol re-use) on the routine side; the lazy import here
    # keeps the tier-compute load order clean.
    from alfred.routine.config import Item
    from alfred.routine.due import (
        completion_satisfies_current_cycle,
        overdue_effective_due,
    )
    # Phase 2C C1 (2026-06-01) — ``is_done_in_current_cycle`` +
    # ``resolve_due_date`` no longer imported here. The new helpers
    # encapsulate both: ``completion_satisfies_current_cycle``
    # supersedes ``is_done_in_current_cycle`` (more permissive,
    # catches cross-month-boundary completions); ``overdue_effective_
    # due`` calls ``resolve_due_date`` internally + handles overdue
    # retention. Render-layer annotation in aggregator.py still uses
    # ``is_done_in_current_cycle`` for the "*(done this cycle)*"
    # text — different semantic (calendar-window question), kept
    # for that use.

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
            # MIRRORS ``_collect_items_for_today`` in
            # ``routine/aggregator.py`` (around line 530, the
            # ``should_check_handoff`` precondition):
            # aspirational-priority items skip the hard-deadline
            # T1/T2 handoff even when they accidentally carry
            # ``due_pattern`` + ``escalate_at_days``. The
            # operator-stated semantic (Phase 2A ratification): T3
            # is for self-care intentions, not deadline-driven work;
            # the soft-cadence T3 surface (``target_cadence_days``,
            # ``compute_auto_t3_candidates``) is the legitimate
            # aspirational path.
            #
            # Drift between the two layers either double-renders
            # items (compute permissive, aggregator strict — pre-fix
            # state) or silently loses them (reverse). Pinned by
            # ``test_mirror_aspirational_t1_predicate_matches_aggregator``
            # in ``tests/tier/test_compute.py`` per
            # ``feedback_two_layer_window_math_mirror``.
            #
            # Latent in production until this fix (2026-05-31): no
            # real records combined ``priority: aspirational`` with
            # deadline-bearing fields, so the double-render never
            # surfaced. The gate ships before a future record shape
            # exercises the bug.
            if item.priority == "aspirational":
                continue
            if item.due_pattern is None:
                continue
            if item.escalate_at_days is None:
                continue

            # Phase 2C C1 (2026-06-01) completion-aware suppression.
            # MIRROR of the same gate in
            # ``alfred.routine.aggregator._decide_tier_handoff``.
            # Operator bug surfacing: Pay Clinic Rental (monthly
            # day=1) completed May 29, still auto-surfaced T1 on
            # June 1's brief because the old ``is_done_in_current_
            # cycle`` predicate uses calendar-month windows and
            # May 29 doesn't fall in "calendar month of June 1 due."
            # The new nearest-cycle ±half-cycle helper correctly
            # treats May 29 as covering the June 1 cycle.
            #
            # Drift between this gate and the aggregator's identical
            # call would either double-surface completed items
            # (both layers permissive) or double-suppress real T1
            # work (both layers strict). Pinned by
            # ``test_mirror_completion_predicate_aggregator_matches_
            # compute`` in tests/tier/test_compute.py per
            # ``feedback_two_layer_window_math_mirror``.
            if completion_satisfies_current_cycle(
                item.text, completion_log, item.due_pattern,
                today_local,
            ):
                continue

            # Phase 2C C1 overdue retention. ``overdue_effective_due``
            # returns prev_due when prev cycle is unsatisfied AND has
            # passed → makes days_to_due negative → T1 window admits.
            # Without this, monthly day=15, today=June 17, no
            # completion → current_due=July 15, days_to_due=28, item
            # silently drops out of T1 instead of staying with the
            # overdue annotation operator expects.
            #
            # MIRROR with aggregator's _decide_tier_handoff use of
            # the same helper.
            effective_due = overdue_effective_due(
                item.due_pattern, completion_log, item.text, today_local,
            )
            if effective_due is None:
                continue
            due = effective_due
            days_to_due = (due - today_local).days

            reason: str | None = None
            if window == "t1":
                # Phase 2C C1: T1 admits non-positive days_to_due
                # (overdue retention). The upper bound stays
                # ``escalate_at_days``.
                if days_to_due <= item.escalate_at_days:
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
                        reason = (
                            f"escalate window "
                            f"({item.escalate_at_days}d before due)"
                        )
            else:  # window == "t2"
                surface = item.surface_at_days
                if (
                    surface is not None
                    and surface > item.escalate_at_days
                    and item.escalate_at_days < days_to_due <= surface
                ):
                    reason = f"surface window ({days_to_due}d before due)"

            if reason is None:
                continue

            candidates.append(AutoT1Candidate(
                path=rel_path,
                name=item.text,
                due_iso=due.isoformat(),
                surface_reason=reason,
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
# Mirror with :func:`alfred.routine.aggregator._decide_tier_handoff`:
# the predicate ``days_since >= target_cadence_days`` is computed in
# BOTH layers. The aggregator uses it to suppress the routine-section
# render (the item handed off to T3); this compute uses it to surface
# the item AS the T3 candidate. The two outcomes are two sides of one
# decision; rename the predicate here = update the aggregator in
# lockstep. Per ``feedback_two_layer_window_math_mirror`` — the
# regression-pin lives in ``tests/tier/test_compute.py``
# (``test_mirror_decide_tier_handoff_t3_matches_compute_auto_t3``).
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
            if item.target_cadence_days is None:
                continue
            # Mutually-exclusive precedence: due_pattern wins. The
            # aggregator's _decide_tier_handoff emits the warn log
            # naming the record + item text; here we defensively
            # match the same outcome silently (compute paths run per
            # /today + per brief fire — log emission lives at the
            # write/aggregate layer to avoid spam).
            if item.due_pattern is not None:
                continue
            target = item.target_cadence_days
            if not isinstance(target, int) or target <= 0:
                # Defensive: zero / negative target has undefined
                # overdue semantics. Operator hand-edit corruption.
                continue

            completion_dates = _parse_item_completion_dates(
                completion_log.get(item.text, [])
            )
            days_since_value: int | None
            if completion_dates:
                most_recent = max(completion_dates)
                days_since = (today_local - most_recent).days
                # Defensive: a future-dated completion (operator hand-
                # edit producing tomorrow's date) yields negative
                # days_since. Treat as "done today" — clamp to 0,
                # which produces overdue_ratio = 0 → skip.
                if days_since < 0:
                    days_since = 0
                days_since_value = days_since
                # Predicate mirror with aggregator._decide_tier_handoff
                # T3 branch: days_since >= target → surface.
                if days_since < target:
                    continue
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


__all__ = [
    "AutoT1Candidate",
    "AutoT3Candidate",
    "OPEN_STATUSES",
    "coerce_due_date",
    "compute_auto_routine_candidates",
    "compute_auto_routine_t2_candidates",
    "compute_auto_t1_candidates",
    "compute_auto_t3_candidates",
]
