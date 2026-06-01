"""Routine aggregator — scan active routine records, write the daily note.

Pure module — no daemon loop, no scheduling. The daemon calls
``run_aggregator_once(config, today)`` once per fire; the brief reads
the resulting file at 06:00. Same loose-coupling pattern as the BIT →
brief handoff: filesystem is the contract.

Output shape (``vault/daily/<date>.md``):

    ---
    type: daily
    date: 2026-05-26
    routines_contributing: [Core Daily, For Self Health, Mondays]
    critical_pending: [Kiki Insulin @ 12:00, ...]
    ---

    ## Critical
    - [ ] Kiki Insulin @ 12:00
    ...

    ## Tracked
    - [ ] Dog Walk *(last: 4 days ago — past 3-day threshold)*
    ...

    ## Aspirational
    - [ ] Reading for pleasure
    ...

Section headers are emitted UNCONDITIONALLY (intentionally-left-blank
principle): if no routines fire today, the file still has all three
headers + a "no routines due today" sentinel, so the operator can
distinguish "ran, nothing to do" from "broken."

Note: ``daily/`` is added to ``vault.dont_scan_dirs`` in the operator
config so the janitor skips this derivative file.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]
import structlog

from alfred.brief.renderer import serialize_record

from .cadence import CadenceError, is_due
from .config import DuePattern, RoutineConfig
from .due import (
    completion_satisfies_current_cycle,
    is_done_in_current_cycle,
    overdue_effective_due,
    resolve_due_date,
)
from .state import RoutineRun, StateManager

log = structlog.get_logger(__name__)


# Priority ordering — Critical surfaces first (medication, time-critical
# care), Tracked next (habits that should be done), Aspirational last
# (nice-to-have). Maps the operator-facing string to a sort key for
# deterministic section ordering.
_PRIORITY_ORDER = {"critical": 0, "tracked": 1, "aspirational": 2}

# Default gap threshold for tracked items when the record omits
# ``warn_after_gap_days``. 5 days is the dispatch-ratified default —
# tunable per-item via the frontmatter field.
DEFAULT_TRACKED_GAP_DAYS = 5


def _iter_routine_records(vault_path: Path) -> list[tuple[Path, dict, str]]:
    """Yield ``(path, frontmatter_dict, name)`` for every active routine.

    Walks ``<vault>/routine/`` (deterministic order via sorted iteration).
    Skips files that fail to parse — emits a single log line per failure
    so operators see the skip rather than a silent drop.

    Records with ``status: archived`` (or anything other than ``active``,
    or missing status — treated as active by default for forward compat
    with operator-authored files) are skipped if explicitly archived.
    """
    routine_dir = vault_path / "routine"
    if not routine_dir.is_dir():
        # Per feedback_intentionally_left_blank: emit signal so absence
        # is distinguishable from broken. ``no_routine_dir`` is what
        # the operator sees on a fresh install before any routines exist.
        log.info("routine.aggregator.no_routine_dir", path=str(routine_dir))
        return []

    out: list[tuple[Path, dict, str]] = []
    for path in sorted(routine_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "routine.aggregator.parse_failed",
                path=str(path),
                error=str(exc),
            )
            continue
        fm = dict(post.metadata or {})
        if fm.get("status") == "archived":
            continue
        name = str(fm.get("name") or path.stem)
        out.append((path, fm, name))
    return out


def _parse_log_dates(values: Any) -> list[date]:
    """Parse a list of YAML date / ISO-string values into date objects.

    Silently skips entries we can't parse — operator hand-edits sometimes
    introduce bad strings, and dropping just the bad entry is the
    forgiving choice. Each drop emits a debug-level log so the trail
    exists if needed.
    """
    out: list[date] = []
    if not isinstance(values, list):
        return out
    for v in values:
        if isinstance(v, date):
            out.append(v)
            continue
        if isinstance(v, str):
            try:
                out.append(date.fromisoformat(v))
                continue
            except ValueError:
                pass
        log.debug("routine.aggregator.skipping_bad_log_entry", value=repr(v))
    return out


def _format_tracked_annotation(
    item_text: str,
    completion_log: dict,
    warn_threshold: int,
    today: date,
) -> str | None:
    """Compose the gap-annotation string for a tracked item.

    Returns ``"*(last: N days ago — past T-day threshold)*"`` when the
    gap exceeds the threshold; ``"*(last: N days ago)*"`` when within
    threshold; ``"*(no completions yet)*"`` when the log is empty; or
    ``None`` when the threshold is non-positive (operator opted out).

    Annotation is always emitted for tracked items so the operator
    sees the recency state at a glance — per intentionally-left-blank,
    silent "no annotation" is ambiguous with "operator didn't check yet."
    """
    log_dates = _parse_log_dates(completion_log.get(item_text, []))
    if not log_dates:
        return "*(no completions yet)*"
    most_recent = max(log_dates)
    days_since = (today - most_recent).days
    if days_since == 0:
        return "*(done today)*"
    if warn_threshold <= 0:
        return f"*(last: {days_since} days ago)*"
    if days_since > warn_threshold:
        return (
            f"*(last: {days_since} days ago — past "
            f"{warn_threshold}-day threshold)*"
        )
    return f"*(last: {days_since} days ago)*"


def _format_cycle_aware_annotation(
    item_text: str,
    due_pattern: DuePattern,
    completion_log: dict,
    today: date,
) -> str | None:
    """Compose a cycle-aware annotation for an item with ``due_pattern``.

    Phase 2A Ship B (2026-05-29): items with ``due_pattern`` carry a
    semantic notion of "cycle" (the period containing the next due
    date). The gap-based annotation isn't meaningful for these items
    because their completion windows are tied to the recurrence, not
    elapsed days since last touch.

    Returns one of:
      * ``"*(done this cycle)*"`` — operator has completed in the
        current cycle (e.g. Garbage Day done Wed for Thu pickup).
      * ``"*(due in Nd)*"`` — not yet done; surfaces the time-to-due
        signal so the operator can plan ahead even when the item
        isn't yet inside the tier-surface windows.
      * ``"*(due today)*"`` / ``"*(due tomorrow)*"`` — surface for
        0 / 1 days to due (consistent with tier-section phrasing).
      * ``"*(overdue by Nd)*"`` — a corner case: items WITH
        ``due_pattern`` but no ``escalate_at_days`` never tier-surface,
        so they CAN go past due without escalation. The annotation
        flags it so the operator sees the miss.

        NB: ``resolve_due_date`` always returns the NEXT upcoming
        due date (today or later); past-due detection compares the
        most recent completion against the most-recent-passed cycle
        boundary.
      * ``None`` — pattern malformed (resolver returned None); caller
        falls back to the gap-based annotation. This preserves
        operator visibility when the pattern itself can't resolve.
    """
    due = resolve_due_date(due_pattern, today)
    if due is None:
        return None

    completion_dates = _parse_log_dates(completion_log.get(item_text, []))

    # Done in current cycle → terminal state for this cycle.
    if is_done_in_current_cycle(due_pattern, completion_dates, today):
        return "*(done this cycle)*"

    days_to_due = (due - today).days
    if days_to_due == 0:
        return "*(due today)*"
    if days_to_due == 1:
        return "*(due tomorrow)*"
    if days_to_due > 1:
        return f"*(due in {days_to_due}d)*"

    # days_to_due < 0 — should not normally happen because
    # resolve_due_date returns the next upcoming due. Defensive
    # fallback: present as overdue (operator can fix the pattern).
    return f"*(overdue by {abs(days_to_due)}d)*"


def _format_soft_cadence_annotation(
    item_text: str,
    completion_log: dict,
    target_cadence_days: int,
    today: date,
) -> str | None:
    """Compose a soft-cadence annotation string.

    Phase 2A-soft-cadence (2026-05-30): items with
    ``target_cadence_days`` that are NOT overdue (so they stay in the
    routine section — the T3 handoff would have intercepted overdue
    items) get a "Nd since last; target every Nd" annotation. Operator
    sees how close to the cadence boundary they are at a glance.

    Returns:
      * ``"*(Nd since last; target every Nd)*"`` — most recent
        completion N days ago, within target.
      * ``"*(done today; target every Nd)*"`` — completion today.
      * ``"*(no completions yet; target every Nd)*"`` — empty log
        (defensive: this state SHOULD have been intercepted by the
        T3 handoff which treats never-completed as max overdue, but
        if a future refactor changes that contract the annotation
        still works).
      * ``None`` — defensive against non-positive target (shouldn't
        reach this helper but mirrors the
        ``_format_tracked_annotation`` defensive return).
    """
    if not isinstance(target_cadence_days, int) or target_cadence_days <= 0:
        return None
    log_dates = _parse_log_dates(completion_log.get(item_text, []))
    if not log_dates:
        return (
            f"*(no completions yet; target every "
            f"{target_cadence_days}d)*"
        )
    most_recent = max(log_dates)
    days_since = (today - most_recent).days
    if days_since < 0:
        # Future-dated completion — clamp.
        days_since = 0
    if days_since == 0:
        return f"*(done today; target every {target_cadence_days}d)*"
    return (
        f"*({days_since}d since last; target every "
        f"{target_cadence_days}d)*"
    )


def _decide_tier_handoff(
    due_pattern: DuePattern | None,
    surface_at_days: int | None,
    escalate_at_days: int | None,
    today: date,
    *,
    target_cadence_days: int | None = None,
    completion_log: dict | None = None,
    item_text: str = "",
    routine_record: str = "",
) -> int | None:
    """Decide if the item should be handed off to the tier section.

    Returns:
      * ``1`` — item is in the T1 window
        (``days_to_due <= escalate_at_days``, including NEGATIVE days
        for overdue retention per Phase 2C C1 2026-06-01); tier
        section will surface it. Aggregator SKIPs the render.
      * ``2`` — item is in the T2 window
        (``(escalate_at_days, surface_at_days]``); tier section will
        surface it. Aggregator SKIPs the render.
      * ``3`` — item is overdue against its soft cadence
        (``target_cadence_days``); tier section's T3 auto-suggest
        subsection will surface it. Aggregator SKIPs the render.
        (Phase 2A-soft-cadence, 2026-05-30.)
      * ``None`` — item is OUTSIDE all windows OR has neither
        ``escalate_at_days`` nor ``target_cadence_days``; the routine
        section renders it normally (with cycle-aware annotation per
        Item 4, OR with soft-cadence annotation when the item carries
        ``target_cadence_days`` but is within its cadence window).

    Phase 2A-soft-cadence keyword args (all optional; backward-compat
    with existing call sites that only pass T1/T2 fields):
      * ``target_cadence_days`` — the item's soft cadence target.
        When set AND ``days_since_last_completed >= target``, returns
        ``3``. Mutually exclusive with ``due_pattern``: when BOTH are
        provided, ``due_pattern`` wins and a single warn log
        ``routine.item_both_cadence_modes`` fires naming the record
        + item text. Validator-level rule, NOT a load failure — the
        operator's record still parses and renders; the warn flags
        the configuration ambiguity so they can resolve it.
      * ``completion_log`` — record-level completion log dict (mapping
        item_text → list of dates). Used to compute days-since for
        the T3 predicate. Defensive default ``None`` → treated as
        empty dict → never-completed items still surface (the T3
        compute path treats never-completed as max overdue).
      * ``item_text`` — item's text (lookup key into completion_log).
      * ``routine_record`` — routine record name (operator-facing
        identifier for the warn log).

    Mirrors the window math in :mod:`alfred.tier.compute`'s
    ``_compute_auto_routine`` (T1/T2) and ``compute_auto_t3_candidates``
    (T3). Don't introduce variance here — the two layers must agree
    exactly on which items hand off vs. render-in-routine-section.
    Per ``feedback_two_layer_window_math_mirror``; regression-pin in
    ``tests/tier/test_compute.py`` (T3 mirror) and
    ``tests/routine/test_aggregator.py`` (T1/T2 mirror).
    """
    # Mutually-exclusive validator: both cadence modes set → warn +
    # prefer due_pattern. The warn fires HERE (not at the T3 compute
    # path) because this is the once-per-aggregate-pass call site;
    # the compute path runs per-brief-fire + per-/today and would
    # spam the log. Per the dispatch's "validator-level rule, not a
    # load-failure" framing — operator sees the signal but the record
    # still works.
    if (
        due_pattern is not None
        and target_cadence_days is not None
    ):
        log.warning(
            "routine.item_both_cadence_modes",
            routine_record=routine_record,
            item_text=item_text,
            due_pattern_type=getattr(due_pattern, "type", None),
            target_cadence_days=target_cadence_days,
            detail=(
                "item carries BOTH ``due_pattern`` (deadline-bearing) "
                "AND ``target_cadence_days`` (soft-cadence). These are "
                "mutually exclusive semantics; preferring ``due_pattern`` "
                "(deadline wins). Operator should remove "
                "``target_cadence_days`` from the item to resolve the "
                "ambiguity."
            ),
        )
        # due_pattern wins — fall through to the T1/T2 branches below;
        # target_cadence_days is ignored for this dispatch.

    # ---- Phase 2A-soft-cadence T3 branch -------------------------
    # Only fires when due_pattern is absent (the precedence rule
    # above means due_pattern wins when both are set). Predicate
    # mirror with tier.compute.compute_auto_t3_candidates:
    # ``days_since >= target_cadence_days`` (inclusive boundary).
    if due_pattern is None and target_cadence_days is not None:
        if (
            not isinstance(target_cadence_days, int)
            or target_cadence_days <= 0
        ):
            # Defensive: zero/negative target → undefined semantics →
            # don't hand off. Item renders in routine section.
            return None
        log_dict = (
            completion_log if isinstance(completion_log, dict) else {}
        )
        completion_dates = _parse_log_dates(log_dict.get(item_text, []))
        if not completion_dates:
            # Never completed → max overdue → SURFACE in T3.
            return 3
        days_since = (today - max(completion_dates)).days
        if days_since < 0:
            # Future-dated completion (operator hand-edit) → clamp.
            days_since = 0
        if days_since >= target_cadence_days:
            return 3
        # Within soft cadence window → render in routine section.
        return None

    # ---- T1/T2 branches (deadline-bearing) -----------------------
    if due_pattern is None or escalate_at_days is None:
        return None

    # Phase 2C C1 (2026-06-01) completion-aware suppression: if a
    # completion entry covers the current/upcoming cycle (per the
    # nearest-cycle ±half-cycle heuristic in
    # ``completion_satisfies_current_cycle``), don't hand off — the
    # operator has already completed this cycle's work, so the
    # routine section's "*(done this cycle)*" annotation is the
    # right surface, not T1/T2.
    #
    # Operator bug 2026-06-01 surfacing: Pay Clinic Rental (monthly
    # day=1) completed May 29 via routine_done, but still auto-
    # surfaced T1 on June 1's brief because this predicate didn't
    # consult completion_log. The new helper consults it.
    #
    # MIRROR with ``alfred.tier.compute._compute_auto_routine`` —
    # same call, same args, same result. Drift here either
    # double-surfaces (operator sees completed items in T1) or
    # double-suppresses (operator misses real T1 work). Pinned in
    # ``test_mirror_completion_predicate_aggregator_matches_compute``.
    if completion_satisfies_current_cycle(
        item_text, completion_log, due_pattern, today,
    ):
        return None

    # Phase 2C C1 — overdue retention. The default
    # ``resolve_due_date`` returns the next-upcoming due, which rolls
    # forward past today on a missed deadline. The
    # ``overdue_effective_due`` helper returns prev_due instead when
    # prev cycle has passed without completion → days_to_due becomes
    # negative → T1 window accepts as "overdue, retain."
    #
    # Without this: monthly day=15, today=June 17, no completion →
    # current_due=July 15, days_to_due=28, falls out of T1 window,
    # item silently drops. With this: effective_due=June 15,
    # days_to_due=-2, T1 window admits.
    effective_due = overdue_effective_due(
        due_pattern, completion_log, item_text, today,
    )
    if effective_due is None:
        return None
    days_to_due = (effective_due - today).days
    # Phase 2C C1: T1 admits non-positive days_to_due ("overdue,
    # retain"). The upper bound stays ``escalate_at_days``; the
    # lower bound moves from 0 to -∞ to capture the overdue case.
    # T2 stays bounded — operator wants overdue items in T1 (the
    # urgent surface), not T2 (the upcoming-soon surface).
    if days_to_due <= escalate_at_days:
        return 1
    if (
        surface_at_days is not None
        and surface_at_days > escalate_at_days
        and escalate_at_days < days_to_due <= surface_at_days
    ):
        return 2
    return None


def _collect_items_for_today(
    records: list[tuple[Path, dict, str]],
    today: date,
) -> tuple[list[dict], list[str], list[str]]:
    """Group items by priority for today.

    Returns ``(items, routines_contributing, critical_pending)``:
      - ``items``: list of dicts ``{text, priority, annotation, time}``
        — DEDUPLICATED by ``text`` (first occurrence wins; subsequent
        appearances are dropped, preserving the originating routine's
        priority). Same text appearing under different routines is
        common (operator splits a habit across daily + weekly routines).
      - ``routines_contributing``: routine names that fired today.
        Deterministic order — sorted alphabetically.
      - ``critical_pending``: list of "Kiki Insulin @ 12:00" formatted
        strings for the frontmatter ``critical_pending`` field. Sorted
        by time, then text.
    """
    items_by_text: dict[str, dict] = {}
    contributing: set[str] = set()

    for path, fm, name in records:
        cadence = fm.get("cadence")
        try:
            if not is_due(cadence, today):
                continue
        except CadenceError as exc:
            log.warning(
                "routine.aggregator.malformed_cadence",
                path=str(path),
                name=name,
                error=str(exc),
            )
            continue

        contributing.add(name)
        completion_log = fm.get("completion_log") or {}
        if not isinstance(completion_log, dict):
            completion_log = {}

        raw_items = fm.get("items") or []
        if not isinstance(raw_items, list):
            log.warning(
                "routine.aggregator.items_not_list",
                path=str(path),
                name=name,
                items_type=type(raw_items).__name__,
            )
            continue

        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                log.debug(
                    "routine.aggregator.skipping_non_dict_item",
                    path=str(path),
                    name=name,
                    item=repr(raw_item),
                )
                continue
            text = str(raw_item.get("text") or "").strip()
            if not text:
                continue
            if text in items_by_text:
                # First-occurrence-wins dedup; cite the duplicate so the
                # operator can resolve it if intentional.
                continue
            priority = str(raw_item.get("priority") or "tracked").lower()
            if priority not in _PRIORITY_ORDER:
                log.warning(
                    "routine.aggregator.unknown_priority",
                    path=str(path),
                    name=name,
                    priority=priority,
                    fallback="tracked",
                )
                priority = "tracked"

            # Phase 2A Ship B (2026-05-29): parse due_pattern + tier
            # window fields so we can (a) hand off to tier section + skip
            # rendering here, (b) swap to cycle-aware annotation when the
            # item has a due_pattern but stays in the routine section.
            #
            # Aspirational items are NEVER handed off via the deadline-
            # bearing T1/T2 path (operator-stated: T3 is for self-care
            # intentions, not deadline-driven work). Phase 2A-soft-cadence
            # (2026-05-30) DOES allow aspirational items to hand off to
            # T3 — that's exactly the new T3 surface (overdue self-care
            # items rank into the brief's auto-suggest subsection).
            due_pattern = DuePattern.from_dict(raw_item.get("due_pattern"))
            surface_raw = raw_item.get("surface_at_days")
            try:
                surface_at_days = (
                    int(surface_raw) if surface_raw is not None else None
                )
            except (TypeError, ValueError):
                surface_at_days = None
            escalate_raw = raw_item.get("escalate_at_days")
            try:
                escalate_at_days = (
                    int(escalate_raw) if escalate_raw is not None else None
                )
            except (TypeError, ValueError):
                escalate_at_days = None
            # Phase 2A-soft-cadence (2026-05-30): parse target_cadence_days
            # for the T3 auto-suggest surface.
            target_cadence_raw = raw_item.get("target_cadence_days")
            try:
                target_cadence_days = (
                    int(target_cadence_raw)
                    if target_cadence_raw is not None
                    else None
                )
            except (TypeError, ValueError):
                target_cadence_days = None

            # T1/T2 handoff path: only Critical/Tracked items with
            # due_pattern. T3 handoff path: any item with
            # target_cadence_days (aspirational included — that's the
            # whole point of the soft-cadence surface).
            #
            # MIRRORS the ``if item.priority == "aspirational": continue``
            # guard in ``_compute_auto_routine`` in ``tier/compute.py``.
            # Same gate, same reason, same operator-stated semantic:
            # aspirational items don't deadline-escalate to T1/T2 even
            # when they carry due_pattern + escalate_at_days. The
            # soft-cadence T3 path IS the legitimate aspirational
            # surface (target_cadence_days → auto-T3 candidates).
            #
            # Don't introduce variance here without matching the
            # compute site. Drift either double-renders items
            # (compute permissive, aggregator strict) or silently
            # loses them (reverse). Side-by-side regression-pin in
            # ``tests/tier/test_compute.py::test_mirror_aspirational_t1_predicate_matches_aggregator``
            # per ``feedback_two_layer_window_math_mirror``.
            should_check_handoff = (
                (due_pattern is not None and priority != "aspirational")
                or target_cadence_days is not None
            )
            if should_check_handoff:
                handoff_tier = _decide_tier_handoff(
                    due_pattern,
                    surface_at_days,
                    escalate_at_days,
                    today,
                    target_cadence_days=target_cadence_days,
                    completion_log=completion_log,
                    item_text=text,
                    routine_record=name,
                )
                if handoff_tier is not None:
                    # Compute days_to_due for the log only when
                    # due_pattern is present (T1/T2 path); T3 path
                    # uses days_since semantics, but logging it
                    # uniformly as days_to_due=None keeps the log
                    # event shape stable.
                    #
                    # Phase 2C C1 (2026-06-01): use overdue_effective_due
                    # instead of resolve_due_date so the log reflects
                    # the SAME effective due the predicate used. The
                    # predicate now admits overdue retention
                    # (effective_due=prev_due → days_to_due negative);
                    # the log field MUST mirror that so operator log
                    # review can grep for negative days_to_due as the
                    # overdue signal. Pre-C1 the log used the resolver
                    # directly which always returned >= today, so
                    # the field stayed non-negative even when the
                    # predicate was treating an item as overdue.
                    days_to_due = None
                    if due_pattern is not None:
                        effective = overdue_effective_due(
                            due_pattern, completion_log, text, today,
                        )
                        days_to_due = (
                            (effective - today).days
                            if effective is not None
                            else None
                        )
                    log.info(
                        "routine.aggregator.handed_off_to_tier",
                        item_text=text,
                        tier=handoff_tier,
                        days_to_due=days_to_due,
                        routine_record=name,
                        detail=(
                            "routine item handed off to tier section "
                            f"(T{handoff_tier}); routine-section "
                            "render suppressed for dedup. T1/T2 path: "
                            "due_pattern + tier window. T3 path: "
                            "target_cadence_days + overdue against "
                            "soft cadence target."
                        ),
                    )
                    continue

            time_str = ""
            if priority == "critical":
                raw_time = raw_item.get("time")
                if isinstance(raw_time, str) and raw_time.strip():
                    time_str = raw_time.strip()

            annotation: str | None = None
            if priority == "tracked":
                # Cycle-aware annotation when due_pattern present (Ship B
                # Item 4); fall back to gap-based annotation when no
                # pattern OR pattern fails to resolve.
                if due_pattern is not None:
                    annotation = _format_cycle_aware_annotation(
                        text, due_pattern, completion_log, today,
                    )
                if annotation is None:
                    gap_raw = raw_item.get(
                        "warn_after_gap_days", DEFAULT_TRACKED_GAP_DAYS,
                    )
                    try:
                        gap = int(gap_raw)
                    except (TypeError, ValueError):
                        gap = DEFAULT_TRACKED_GAP_DAYS
                    annotation = _format_tracked_annotation(
                        text, completion_log, gap, today,
                    )
            # Phase 2A-soft-cadence (2026-05-30): items with
            # ``target_cadence_days`` that AREN'T overdue (overdue
            # path was intercepted by the T3 handoff above) get a
            # soft-cadence annotation. Applies to ALL priorities —
            # operator framing is "self-care", commonly on
            # ``aspirational`` items but doesn't preclude ``tracked``
            # / ``critical`` use. Overrides the tracked-gap annotation
            # when both apply (target_cadence_days is the more
            # specific signal; gap-based is the generic fallback).
            if target_cadence_days is not None and due_pattern is None:
                soft_annotation = _format_soft_cadence_annotation(
                    text, completion_log, target_cadence_days, today,
                )
                if soft_annotation is not None:
                    annotation = soft_annotation

            items_by_text[text] = {
                "text": text,
                "priority": priority,
                "annotation": annotation,
                "time": time_str,
            }

    items = list(items_by_text.values())

    critical_pending: list[str] = []
    for item in items:
        if item["priority"] != "critical":
            continue
        if item["time"]:
            critical_pending.append(f"{item['text']} @ {item['time']}")
        else:
            critical_pending.append(item["text"])
    # Stable sort: time-bearing first (sorted by HH:MM string), then text.
    critical_pending.sort(key=lambda s: (0 if "@" in s else 1, s))

    return items, sorted(contributing), critical_pending


def _format_item_line(item: dict) -> str:
    """Render one ``- [ ] ...`` checklist line."""
    text = item["text"]
    suffix_parts: list[str] = []
    if item["priority"] == "critical" and item["time"]:
        suffix_parts.append(f"@ {item['time']}")
    line = f"- [ ] {text}"
    if suffix_parts:
        line += " " + " ".join(suffix_parts)
    if item["annotation"]:
        line += " " + item["annotation"]
    return line


def _render_section(items: list[dict], header: str) -> str:
    """Compose ``## {header}\n\n- [ ] ...`` for one priority bucket.

    Always emits the header — per intentionally-left-blank, the operator
    sees three section headers every day so absence-of-items is
    distinguishable from absence-of-section.
    """
    lines = [f"## {header}", ""]
    if not items:
        lines.append(f"*(no {header.lower()} routines today)*")
        lines.append("")
        return "\n".join(lines)
    for item in items:
        lines.append(_format_item_line(item))
    lines.append("")
    return "\n".join(lines)


def render_daily_body(
    items: list[dict],
    no_routines_overall: bool,
) -> str:
    """Render the body markdown — three sections (Critical / Tracked /
    Aspirational), header always emitted, sentinel when no routines
    are due at all."""
    if no_routines_overall:
        # Three empty section headers + top-level sentinel so the brief
        # reader sees "ran, nothing to do" rather than "broken."
        body = (
            "*(no routines due today)*\n\n"
            "## Critical\n\n"
            "*(no critical routines today)*\n\n"
            "## Tracked\n\n"
            "*(no tracked routines today)*\n\n"
            "## Aspirational\n\n"
            "*(no aspirational routines today)*\n"
        )
        return body

    critical = [i for i in items if i["priority"] == "critical"]
    tracked = [i for i in items if i["priority"] == "tracked"]
    aspirational = [i for i in items if i["priority"] == "aspirational"]
    sections = [
        _render_section(critical, "Critical"),
        _render_section(tracked, "Tracked"),
        _render_section(aspirational, "Aspirational"),
    ]
    return "\n".join(sections)


def _load_existing_tier_curation(file_path: Path) -> dict | None:
    """Preserve any pre-existing ``tier_curation`` block when re-writing
    the daily file.

    Added 2026-05-29 (Tier-V2 Ship 1) to close a race: the talker may
    pre-edit ``vault/daily/<date>.md`` with curation BEFORE the routine
    aggregator's 05:59 fire. The aggregator's pre-V2 write path would
    silently overwrite the curation. Now the aggregator does
    read-preserve-write — the curation survives.

    Read-side only: returns the parsed block as a dict or ``None`` when
    absent/malformed. The write path calls this once, merges into the
    new frontmatter dict, and only the routine aggregator's own keys
    (``type``, ``date``, ``routines_contributing``, ``critical_pending``)
    are owned by the aggregator. Tier curation is owned by Ship 2/4 +
    :mod:`alfred.tier.daily_curation` — this helper just preserves it.

    Race tolerance:
      * File doesn't exist → return None (first-run; no curation to
        preserve).
      * File exists but parse fails → return None (corrupt file; the
        aggregator's overwrite is the recovery path). Logged at warning.
      * File exists, parses, no ``tier_curation`` key → return None
        (clean aggregator-only state). NOT a defect.
      * File exists, parses, ``tier_curation`` is not a dict → return
        None (defensive against operator hand-edit corruption).
        Logged at warning so the operator sees the drop.
      * File exists, parses, ``tier_curation`` is a dict → return the
        dict verbatim. The aggregator caller merges into its
        frontmatter dict before writing.
    """
    if not file_path.exists():
        return None
    try:
        post = frontmatter.load(str(file_path))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "routine.aggregator.tier_curation_load_failed",
            path=str(file_path),
            error=str(exc),
        )
        return None
    raw = post.metadata.get("tier_curation") if post.metadata else None
    if raw is None:
        return None
    if not isinstance(raw, dict):
        log.warning(
            "routine.aggregator.tier_curation_wrong_type",
            path=str(file_path),
            actual_type=type(raw).__name__,
            detail=(
                "``tier_curation`` frontmatter key is not a dict — "
                "treating as absent. Operator hand-edit may have "
                "corrupted the block."
            ),
        )
        return None
    return raw


def run_aggregator_once(
    config: RoutineConfig,
    today: date,
    state_mgr: StateManager | None = None,
) -> str:
    """Scan active routines, write today's daily aggregator note, return
    the vault-relative path.

    ``state_mgr`` is optional — when provided, the run is recorded in
    state. Callers that just want to render (e.g. tests) may pass None.

    Read-preserve-write contract (added 2026-05-29 Tier-V2 Ship 1):
    if a pre-existing ``vault/daily/<date>.md`` carries a
    ``tier_curation`` frontmatter block (talker pre-edit before the
    aggregator's morning fire), the block is preserved verbatim in
    the new write. The aggregator's own keys (``type``, ``date``,
    ``routines_contributing``, ``critical_pending``) + the body
    content are recomputed from scratch each fire.
    """
    vault_path = Path(config.vault_path)
    iso = today.isoformat()
    records = _iter_routine_records(vault_path)

    if not records:
        # Per intentionally-left-blank: emit signal so a stable "no
        # routines configured" state is distinguishable from "broken."
        log.info(
            "routine.aggregator.no_active_routines",
            date=iso,
            scanned_dir=str(vault_path / "routine"),
        )

    items, contributing, critical_pending = _collect_items_for_today(
        records, today,
    )
    no_routines_overall = not items
    if no_routines_overall and records:
        # Records existed but none fired today — still useful signal.
        log.info(
            "routine.aggregator.no_routines_due_today",
            date=iso,
            scanned=len(records),
        )

    # Resolve the output path BEFORE rendering so the
    # read-preserve-write of any pre-existing tier_curation can pick
    # up the file (the same path the write step lands at).
    name = config.output.name_template.replace("{date}", iso)
    rel_path = f"{config.output.directory}/{name}.md"
    file_path = vault_path / rel_path

    # Preserve any pre-existing tier_curation block. Talker may have
    # pre-edited the daily file before the 05:59 aggregator fire; or
    # the operator may have run ``alfred routine`` manually mid-day
    # to refresh the aggregator side without touching the curation.
    preserved_curation = _load_existing_tier_curation(file_path)

    body = render_daily_body(items, no_routines_overall)
    fm: dict[str, Any] = {
        "type": "daily",
        "date": iso,
        "routines_contributing": contributing,
        "critical_pending": critical_pending,
    }
    if preserved_curation is not None:
        fm["tier_curation"] = preserved_curation
        log.info(
            "routine.aggregator.preserved_tier_curation",
            path=rel_path,
            date=iso,
            detail=(
                "pre-existing ``tier_curation`` block preserved in the "
                "aggregator's write. Talker pre-edit OR mid-day "
                "operator refresh likely cause; either way the curation "
                "stays intact."
            ),
        )
    content = serialize_record(fm, body)

    # Write the file (overwrite on stale-tolerated re-runs; the daemon
    # only fires once per day, but CLI re-runs may stomp).
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    log.info(
        "routine.aggregator.written",
        path=rel_path,
        item_count=len(items),
        critical_count=len(critical_pending),
        routines_contributing=contributing,
    )

    if state_mgr is not None:
        state_mgr.state.add_run(
            RoutineRun(
                date=iso,
                generated_at=datetime.now(timezone.utc).isoformat(),
                vault_path=rel_path,
                routines_contributing=contributing,
                item_count=len(items),
                critical_pending=len(critical_pending),
            ),
            max_history=config.state.max_history,
        )
        state_mgr.save()

    return rel_path


__all__ = [
    "DEFAULT_TRACKED_GAP_DAYS",
    "_decide_tier_handoff",
    "_format_cycle_aware_annotation",
    "_format_soft_cadence_annotation",
    "_load_existing_tier_curation",
    "render_daily_body",
    "run_aggregator_once",
]
