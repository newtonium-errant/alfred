"""Brief integration — render the "Open Tasks by Tier" section (V2).

Tier-V2 reframes tier as a **daily curation ritual** stored in
``vault/daily/<date>.md`` (the ``tier_curation`` frontmatter block —
see :mod:`alfred.tier.daily_curation`). This module reads that block
plus the open-task pool and composes a two-section render: **curated
shortlists** at the top + **materials** (T2 selection pool + rollover
from yesterday's incomplete) below.

The V1 surface (per-task ``base_tier``/``escalate_to`` projection
through ``compute_effective_tier``) is gone from this module and from
:mod:`alfred.tier.compute` itself (Ship 3 atomic drop, 2026-05-29 —
last-consumer-rewrite ratified pattern #22). The ``base_tier`` /
``escalate_to`` fields were removed from the schema surface 2026-06-25
(routine-systems consolidation Step 1); the ~24 stale records are
being stripped, not backfilled, so the once-deferred "Ship 5 backfill"
is moot. The migration script ``scripts/migrate_tier_phase1.py`` (which
populated those fields) is ARCHIVED as a completed one-time migration.

Render shape (the section body — the brief renderer wraps it under
``## Open Tasks by Tier``):

    ### T1 — Imminent deadlines (auto-surfaced — confirm or drop)
    - [[task/Steph Yang ROE]] — due today  *(confirm? reply "T1 confirm")*
    - [[task/Pay Clinic Rental]] — due tomorrow

    ### T2 — On the radar
    *(empty — reply "T2 add <items from selection pool below or anywhere>")*

    ### T3 — Self-care for today
    *(empty — pick from Aspirational routines below or add new — reply "T3 add walk Fergus")*

    ---

    ### T2 selection pool
    (open ``todo``/``active`` tasks, NOT auto-T1, NOT alfred_triage)
    - [[task/RRTS Bug List — Burn Through]]
    - [[task/Set Up QuickBooks Online Developer Access for RRTS Website]]

    ### Rollover from yesterday (incomplete)
    - T2: [[task/Connect QBO API — RRTS]] *(uncompleted yesterday)*

Read path (Step 2c, 2026-06-26 — the SINGLE computed view + materials):

  1. ``load_daily_curation(vault_path, today)`` — today's
     ``tier_curation`` block. ``None`` when un-curated yet
     (operator's "selection pool" mode); populated when talker has
     already curated.
  2. ``compute_today_view(vault_path, now)`` — THE single source of what
     surfaces / which lane (T1/T2/T3 lanes + the daily goal). This
     render layer no longer calls the ``compute_auto_*`` predicates
     directly; it slices the view's lanes (by origin + auto-source) into
     the candidate shapes the formatters consume, so no surface decision
     is re-derived here. The view merged curated + auto per lane via the
     single ``classify_routine_item`` predicate.
  3. ``load_daily_curation(vault_path, today - 1 day)`` — yesterday's
     curation, for rollover detection. Each yesterday-T1/T2 entry is
     checked against the current task record's status; incomplete
     entries surface in the Rollover section. (Render-only material —
     not a substrate lane assignment.)
  4. Open-task pool scan over ``vault/task/*.md`` for the T2 selection
     pool (status in OPEN_STATUSES, NOT ``alfred_triage``, NOT in
     today's auto-T1 set, NOT already-curated T1/T2). (Render-only
     material — not a substrate lane assignment.)

Cross-agent contract — operator-facing prompt phrases:

The :data:`T1_CONFIRM_PROMPT` / :data:`T2_EMPTY_PROMPT` /
:data:`T3_EMPTY_PROMPT` / :data:`ROLLOVER_HEADER` / :data:`T2_POOL_HEADER`
module-level constants are quoted verbatim by Ship 4's SKILL so the
talker recognises the operator-reply pattern. Renaming these here =
update SKILL in lockstep. Pinned via tests.

Read-side stability (CRITICAL for refresh): when the operator triggers
``/today`` or the brief regenerates mid-day, the curated shortlists
must be byte-stable as long as ``tier_curation`` hasn't changed. The
render is a pure projection over the block — no re-derivation, no
silent rewrites. The :func:`render_tier_section` signature stays the
same as V1 (``vault_path, now``) so the daemon + ``/today`` wiring
doesn't need to change.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]
import structlog
import yaml

from alfred.tier.compute import (
    OPEN_STATUSES,
    AutoT1Candidate,
    AutoT3Candidate,
    DailyGoalState,
    TodayView,
    compute_today_view,
)
from alfred.tier.daily_curation import (
    DailyCuration,
    T1T2Entry,
    T3Entry,
    load_daily_curation,
)

from .utils import SectionReadStatus, safe_read_section_file

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Section header — referenced by ``brief/daemon.py`` + ``today_command.py``.
# Single source of truth so a rename here propagates without grep-replace.
# ---------------------------------------------------------------------------

SECTION_HEADER = "Open Tasks by Tier"


# ---------------------------------------------------------------------------
# Operator-facing prompt phrases — CROSS-AGENT CONTRACT
#
# Ship 4 SKILL imports + quotes these verbatim so the talker recognises
# the canonical reply patterns ("T1 confirm", "T2 add ...", "T3 add ...").
# A rename here MUST be matched by a SKILL update in the same arc — code
# + prompt are two sides of the same contract.
# ---------------------------------------------------------------------------

T1_CONFIRM_PROMPT = '*(confirm? reply "T1 confirm")*'
T2_EMPTY_PROMPT = (
    '*(empty — reply "T2 add <items from selection pool below or anywhere>")*'
)
T3_EMPTY_PROMPT = (
    '*(empty — pick from Aspirational routines below or add new — '
    'reply "T3 add walk Fergus")*'
)
ROLLOVER_HEADER = "### Rollover from yesterday (incomplete)"
T2_POOL_HEADER = "### T2 selection pool"

# Phase 2A Ship B (2026-05-29): routine-origin tier surfaces.
#
# T2 ramp items from routine due_patterns render in a subsection between
# the curated T2 bucket and the T2 selection pool. The auto-routine T2
# items aren't curated yet (operator hasn't confirmed) — the prompt
# names the canonical talker reply for confirmation.
T2_AUTO_ROUTINE_HEADER = "#### Auto-surfaced (from routines)"
T2_ROUTINE_CONFIRM_PROMPT = (
    '*(reply "T2 confirm" to keep on today\'s list)*'
)

# Phase 2A-soft-cadence (2026-05-30): T3 auto-suggest subsection
# constants — CROSS-AGENT CONTRACT (Phase 2B B1 SKILL quotes verbatim).
#
# T3 auto-suggestions surface routine items overdue against their soft
# cadence target (``target_cadence_days``). Distinct subsection inside
# the T3 bucket (after any curated T3 entries). Rendered ONLY when
# auto-T3 candidates exist — empty auto-T3 with populated curated T3
# is silently omitted (no spurious "auto-suggested: nothing" header).
# Empty curated T3 + empty auto-T3 falls through to the existing
# ``T3_EMPTY_PROMPT`` sentinel.
#
# ``T3_AUTO_CONFIRM_PROMPT`` is the talker reply pattern Phase 2B B1
# recognises (``T3 confirm <item text>``). The talker SKILL +
# ``routine_done`` tool path shipped 2026-05-30; the prompt is now
# actionable (pre-B1 the prompt was operator-axis only).
#
# ``T3_AUTO_TALKER_DEFERRED_NOTE`` is RETIRED as of Phase 2B B1.
# The constant is preserved for backwards-compat (downstream
# consumers may have grepped for it), but the brief render loop
# deliberately omits it from the output — the deferred-note copy
# is no longer accurate now that the talker companion has shipped.
# Per the ILB-acknowledgement-retirement pattern: when the deferred
# capability lands, retire the acknowledgement in the same ship.
#
# ``T3_AUTO_DAYS_SINCE_NEVER_LABEL`` is the per-item display string
# for items with empty completion_log (never completed). Keeps the
# render layer free of magic strings.
#
# ``T3_AUTO_ANNOTATION_TEMPLATE`` is the per-item annotation format
# string. ``{days_since}`` and ``{target}`` are the only fields;
# call site formats it via ``.format(...)``.
T3_AUTO_SECTION_HEADER = "#### Auto-suggested (from routine cadence)"
T3_AUTO_CONFIRM_PROMPT = (
    '*(reply "T3 confirm <item>" to add to today\'s T3)*'
)
T3_AUTO_DAYS_SINCE_NEVER_LABEL = "never done"
T3_AUTO_ANNOTATION_TEMPLATE = (
    "*({days_since} days since last; target every {target}d)*"
)
T3_AUTO_TALKER_DEFERRED_NOTE = (
    "*(talker T3 confirm grammar ships in Phase 2B B1; meanwhile use "
    '`alfred routine done "<item text>"` to mark complete, or '
    "edit the daily file directly.)*"
)


# ---------------------------------------------------------------------------
# YAML pre-validation — reused from V1 (python-frontmatter is lenient on
# bad YAML and silently returns empty metadata; we want the explicit raise
# so the parse-failed log line stays reachable).
# ---------------------------------------------------------------------------


def _validate_frontmatter_yaml(path: Path) -> str | None:
    """Pre-validate a record's YAML frontmatter block.

    Returns ``None`` when well-formed (or no frontmatter at all);
    returns a short error string on failure. ``python-frontmatter`` is
    lenient on invalid YAML — without this pre-pass, broken records
    would silently render as zero-fielded entries instead of triggering
    the parse_failed log line operators rely on. See V1's history at
    commit ``91504ea`` for the underlying gotcha.
    """
    # Defensive read via the shared helper — catches FileNotFoundError,
    # other OSError, AND UnicodeDecodeError uniformly (the last subclasses
    # ValueError, not OSError, so a bare ``except OSError`` misses it).
    read = safe_read_section_file(path)
    if read.status is SectionReadStatus.DECODE_ERROR:
        return f"not utf-8: {read.detail}"
    if read.status is not SectionReadStatus.OK:
        # NOT_FOUND + other OSError — same "read failed" message the prior
        # ``except OSError`` (which included FileNotFoundError) produced.
        return f"read failed: {read.detail}"
    text = read.text

    if not text.startswith("---"):
        return None

    lines = text.splitlines()
    if len(lines) < 2 or lines[0].strip() != "---":
        return "frontmatter opener malformed (no newline after leading ---)"

    close_idx: int | None = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            close_idx = idx
            break

    if close_idx is None:
        return "frontmatter block not closed (no trailing --- found)"

    block = "\n".join(lines[1:close_idx])
    try:
        yaml.safe_load(block)
    except yaml.YAMLError as exc:
        first_line = (
            str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        )
        return f"yaml: {first_line}"

    return None


# ---------------------------------------------------------------------------
# Task-record iteration — yields (path, fm, name) tuples
# ---------------------------------------------------------------------------


def _iter_task_records(vault_path: Path) -> list[tuple[Path, dict, str]]:
    """Walk ``vault/task/*.md`` and yield non-broken task records.

    Filters at this layer:
      * Skip parse-failed records (logged at warning).
      * Skip non-task ``type:`` (logged at info — defensive against
        stray templates / janitor stubs).

    Does NOT filter by ``status`` here; callers filter at the
    bucket-population step so a future surface (e.g. ``alfred tier
    list``) could scan ALL tasks without re-walking.
    """
    task_dir = vault_path / "task"
    if not task_dir.is_dir():
        log.info(
            "brief.tier_section.no_task_dir",
            path=str(task_dir),
            detail=(
                "vault/task/ does not exist — selection pool will be empty."
            ),
        )
        return []

    out: list[tuple[Path, dict, str]] = []
    for path in sorted(task_dir.glob("*.md")):
        validation_error = _validate_frontmatter_yaml(path)
        if validation_error is not None:
            log.warning(
                "brief.tier_section.parse_failed",
                path=str(path),
                error=validation_error,
            )
            continue
        try:
            post = frontmatter.load(str(path))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "brief.tier_section.parse_failed",
                path=str(path),
                error=f"frontmatter.load: {exc}",
            )
            continue
        fm = dict(post.metadata or {})
        record_type = fm.get("type")
        if record_type != "task":
            log.info(
                "brief.tier_section.non_task_skipped",
                path=str(path),
                type=record_type,
            )
            continue
        name = str(fm.get("name") or path.stem)
        out.append((path, fm, name))
    return out


def _is_open(fm: dict[str, Any]) -> bool:
    """Return True if the task's status is in ``OPEN_STATUSES``.

    Missing ``status`` is treated as ``"todo"`` (forward-compat).
    """
    status = fm.get("status") or "todo"
    if not isinstance(status, str):
        return False
    return status.lower() in OPEN_STATUSES


def _format_due_date(due_iso: str) -> str:
    """Format an ISO date as ``"Mon Jun 1"``-style display string.

    Phase 2A Ship B (2026-05-29): routine-origin tier renders include
    the actual due date in the line head per the dispatch's worked
    example. Returns the raw ISO string on parse failure so the brief
    never silently swallows a date display.

    Example:
      ``_format_due_date("2026-06-01")`` → ``"Mon Jun 1"``
      ``_format_due_date("not-a-date")`` → ``"not-a-date"`` (fallback)
    """
    try:
        d = date.fromisoformat(due_iso)
    except (ValueError, TypeError):
        return due_iso
    # ``%a`` = abbreviated weekday (Mon), ``%b`` = abbreviated month
    # (Jun), ``%-d`` = day without leading zero. Use Python's
    # platform-neutral form (``%d`` then strip leading zero) since
    # ``%-d`` is Linux-only.
    weekday = d.strftime("%a")
    month = d.strftime("%b")
    day = str(d.day)  # no leading zero
    return f"{weekday} {month} {day}"


def _wikilink_to_record_name(wikilink: str) -> str | None:
    """Extract the record name from a ``[[task/Name]]`` wikilink.

    Returns ``None`` on malformed input (no ``[[…]]`` or no ``task/``
    prefix). Used to map curated T1/T2 ``task:`` strings back to the
    task pool for rollover-status checking + auto-T1 dedup.
    """
    if not isinstance(wikilink, str):
        return None
    s = wikilink.strip()
    if not (s.startswith("[[") and s.endswith("]]")):
        return None
    inner = s[2:-2].strip()
    if "/" not in inner:
        return None
    type_part, _, name_part = inner.partition("/")
    if type_part.strip() != "task":
        return None
    return name_part.strip()


# ---------------------------------------------------------------------------
# Curated-shortlist render
# ---------------------------------------------------------------------------


def _render_t1_entry(
    entry: T1T2Entry,
    auto_t1_reason_by_name: dict[str, str],
    auto_t1_reason_by_routine_key: dict[tuple[str, str], str],
    auto_t1_due_iso_by_routine_key: dict[tuple[str, str], str],
) -> str:
    """Render one T1 line.

    Origin discrimination:
      * Task-origin (``entry.task`` populated) — renders as
        ``- [[task/Name]] — due today  *(confirm)*``. Reason
        lookup keyed on record name.
      * Routine-origin (``entry.routine_item`` populated) — renders as
        ``- <text> — due <date> (<reason>, from
        [[routine/<record>]])  *(confirm)*`` when an auto-T1 candidate
        matches (date + reason inline); otherwise
        ``- <text> (from [[routine/<record>]])`` for operator-
        added entries the auto layer doesn't know about.

    Confirm-affordance logic (same for both origins):
      * If ``confirmed is True`` → render bare (operator signed off).
      * Else → append :data:`T1_CONFIRM_PROMPT` so the talker reply
        pattern is visible.

    Surface reason (``due today`` / ``due tomorrow`` / ``escalate
    window ...``) comes from the auto-T1 candidate map when the entry
    matches one; otherwise no reason annotation (operator manually
    added a T1 entry that wasn't auto-surfaced).

    Worked example (the canonical dispatch shape):
      ``- Garbage Out — due Fri May 29 (escalate window (1d before
      due), from [[routine/Weekly Chores]])  *(confirm? reply "T1 confirm")*``
    """
    # Routine-origin discrimination — exactly one of task / routine_item
    # is populated per the T1T2Entry invariant.
    if entry.routine_item is not None:
        record = str(entry.routine_item.get("record", ""))
        text = str(entry.routine_item.get("text", ""))
        reason = auto_t1_reason_by_routine_key.get((record, text), "")
        due_iso = auto_t1_due_iso_by_routine_key.get((record, text), "")
        if reason and due_iso:
            head = (
                f"- {text} — due {_format_due_date(due_iso)} "
                f"({reason}, from [[routine/{record}]])"
            )
        elif reason:
            head = (
                f"- {text} — {reason}, from [[routine/{record}]]"
            )
        else:
            head = f"- {text} (from [[routine/{record}]])"
    else:
        # Task-origin path (the original Tier-V2 Ship 1 shape).
        task_str = entry.task or ""
        record_name = _wikilink_to_record_name(task_str) or ""
        reason = auto_t1_reason_by_name.get(record_name, "")
        if reason:
            head = f"- {task_str} — {reason}"
        else:
            head = f"- {task_str}"

    if entry.confirmed is True:
        return head
    # Auto-surfaced (confirmed=False) OR operator-added (confirmed=None)
    # both get the confirm affordance — the prompt names the canonical
    # talker-reply pattern.
    return f"{head}  {T1_CONFIRM_PROMPT}"


def _render_t2_entry(entry: T1T2Entry) -> str:
    """Render one curated T2 line — bare wikilink (or routine reference)
    with no confirm affordance.

    Origin discrimination matches :func:`_render_t1_entry` but without
    the confirm prompt (T2 entries are operator-curated; the add itself
    is the confirmation).
    """
    if entry.routine_item is not None:
        record = str(entry.routine_item.get("record", ""))
        text = str(entry.routine_item.get("text", ""))
        return f"- {text} (from [[routine/{record}]])"
    return f"- {entry.task or ''}"


def _render_auto_t2_routine_entry(candidate: Any) -> str:
    """Render one auto-surfaced T2 routine candidate line.

    ``candidate`` is an :class:`alfred.tier.compute.AutoT1Candidate`
    with ``origin == "routine"``. Renders as:

      ``- <text> — due <date> (<reason>, from
      [[routine/<record>]])  *(reply "T2 confirm" to keep on today's
      list)*``

    matching the dispatch's worked example for the
    :data:`T2_AUTO_ROUTINE_HEADER` subsection.

    Phase 2A Ship B contract: this is the ONLY auto-surface in the
    tier section that renders WITHOUT being merged into curated state.
    The brief shows it; the operator confirms via talker; Ship D writes
    the curation back via ``save_tier_curation``. Curation read-side
    stability stays intact (this render is a pure projection of
    compute-layer output).
    """
    record = candidate.routine_record or ""
    text = candidate.item_text or candidate.name
    reason = candidate.surface_reason
    due_display = _format_due_date(candidate.due_iso)
    return (
        f"- {text} — due {due_display} "
        f"({reason}, from [[routine/{record}]])"
        f"  {T2_ROUTINE_CONFIRM_PROMPT}"
    )


def _render_t3_entry(entry: T3Entry) -> str:
    """Render one T3 line — bare free-text item (no confirm affordance).

    Note T3 entries carry ``item:`` (free-text) not ``task:`` (wikilink).
    """
    return f"- {entry.item}"


def _render_auto_t3_routine_entry(candidate: Any) -> str:
    """Render one auto-suggested T3 routine candidate line.

    ``candidate`` is an :class:`alfred.tier.compute.AutoT3Candidate`.
    Renders as:

      ``- [[routine/<record>]] — <item> *(Nd days since last;
      target every Md)*``

    For never-completed items (``days_since_last_completed is None``)
    the annotation uses :data:`T3_AUTO_DAYS_SINCE_NEVER_LABEL` instead
    of an integer day count. Keeps the operator's eye drawn to the
    "this has never been done" signal — distinct from "0 days since
    last" which would imply "done today."

    Phase 2A-soft-cadence contract: this is the auto-T3 sibling to
    :func:`_render_auto_t2_routine_entry` (T2 ramp surface). The two
    render paths share the wikilink shape but the annotation format
    differs because the semantics differ (deadline-driven vs
    cadence-driven). Per-candidate confirm prompt is NOT inlined per
    item — instead, a single :data:`T3_AUTO_CONFIRM_PROMPT` line
    fires once below the candidate list. Mirrors the T2-auto
    subsection's prompt placement.
    """
    record = candidate.routine_record or ""
    text = candidate.item_text or ""
    target = candidate.target_cadence_days
    days_since = candidate.days_since_last_completed
    if days_since is None:
        annotation = (
            f"*({T3_AUTO_DAYS_SINCE_NEVER_LABEL}; "
            f"target every {target}d)*"
        )
    else:
        annotation = T3_AUTO_ANNOTATION_TEMPLATE.format(
            days_since=days_since, target=target,
        )
    return (
        f"- [[routine/{record}]] — {text} {annotation}"
    )


def _merge_auto_t1_into_curated(
    curated_t1: list[T1T2Entry],
    auto_t1_task_candidates: list[Any],     # list[AutoT1Candidate] origin=task
    auto_t1_routine_candidates: list[Any],  # list[AutoT1Candidate] origin=routine
) -> tuple[
    list[T1T2Entry],
    dict[str, str],
    dict[tuple[str, str], str],
    dict[tuple[str, str], str],
]:
    """Merge auto-T1 candidates (both task + routine origin) with
    operator-curated T1 entries.

    Returns ``(merged_t1, reason_by_name, reason_by_routine_key,
    due_iso_by_routine_key)``:
      * ``merged_t1`` — curated_t1 entries kept verbatim (operator
        wins on the per-entry confirmed state). Auto-T1 task candidates
        NOT already in curated_t1 are appended as ``confirmed=False``
        entries with ``source="auto-due"``. Auto-T1 routine candidates
        NOT already in curated_t1 are appended with
        ``source="auto-due-routine"``.
      * ``reason_by_name`` — map of task-record-name → canonical
        surface reason string. Used by :func:`_render_t1_entry`'s
        task-origin branch to inline the reason text.
      * ``reason_by_routine_key`` — map of ``(record, text)`` tuple →
        canonical surface reason string. Used by the routine-origin
        branch.
      * ``due_iso_by_routine_key`` — map of ``(record, text)`` tuple →
        ISO due-date string. Used by the routine-origin branch to
        inline the formatted due date per the dispatch worked example.

    Dedup keys:
      * Task-origin: record name (via wikilink parse).
      * Routine-origin: ``(routine_record, item_text)`` tuple.

    Cross-Ship contract: this merge is read-side only — the resulting
    list reflects what the brief SHOULD show, not what the operator's
    curation block contains. The persisted curation is left
    untouched (Ship 4's talker writes confirmations back via
    :func:`save_tier_curation`).
    """
    reason_by_name: dict[str, str] = {}
    for cand in auto_t1_task_candidates:
        reason_by_name[cand.name] = cand.surface_reason

    reason_by_routine_key: dict[tuple[str, str], str] = {}
    due_iso_by_routine_key: dict[tuple[str, str], str] = {}
    for cand in auto_t1_routine_candidates:
        # ``cand.routine_record`` + ``cand.item_text`` populated for
        # routine-origin (Ship A contract). Defensive fallback to name
        # for the item_text key in case a future variant omits it.
        record = cand.routine_record or ""
        text = cand.item_text or cand.name
        key = (record, text)
        reason_by_routine_key[key] = cand.surface_reason
        due_iso_by_routine_key[key] = cand.due_iso

    # Build dedup sets from curated entries — separate sets for task
    # vs routine origin keep the discriminated-union clean.
    curated_task_names: set[str] = set()
    curated_routine_keys: set[tuple[str, str]] = set()
    for entry in curated_t1:
        if entry.routine_item is not None:
            record = str(entry.routine_item.get("record", ""))
            text = str(entry.routine_item.get("text", ""))
            curated_routine_keys.add((record, text))
        elif entry.task is not None:
            rec_name = _wikilink_to_record_name(entry.task)
            if rec_name:
                curated_task_names.add(rec_name)

    merged: list[T1T2Entry] = list(curated_t1)

    # Task-origin auto-T1 candidates not yet curated.
    for cand in auto_t1_task_candidates:
        if cand.name in curated_task_names:
            continue
        wikilink = f"[[task/{cand.name}]]"
        merged.append(T1T2Entry(
            task=wikilink,
            source="auto-due",
            confirmed=False,
        ))

    # Routine-origin auto-T1 candidates not yet curated.
    for cand in auto_t1_routine_candidates:
        record = cand.routine_record or ""
        text = cand.item_text or cand.name
        if (record, text) in curated_routine_keys:
            continue
        merged.append(T1T2Entry(
            routine_item={"record": record, "text": text},
            source="auto-due-routine",
            confirmed=False,
        ))

    return (
        merged,
        reason_by_name,
        reason_by_routine_key,
        due_iso_by_routine_key,
    )


def _render_curated_shortlists(
    curation: DailyCuration | None,
    auto_t1_task_candidates: list[Any],
    auto_t1_routine_candidates: list[Any],
    auto_t2_routine_candidates: list[Any],
    auto_t3_routine_candidates: list[Any] | None = None,
) -> str:
    """Compose the three ``### T1 / T2 / T3`` subsections.

    When curation is ``None`` (un-curated state — file missing or no
    ``tier_curation`` block yet), we still surface auto-T1 candidates
    + empty-bucket prompts so the operator's first brief of the day
    is actionable.

    Phase 2A Ship B (2026-05-29): T1 merges both task-origin AND
    routine-origin auto candidates. T2 grows an
    :data:`T2_AUTO_ROUTINE_HEADER` subsection BELOW the curated T2
    bucket — auto-surfaced routine items that are inside their T2 ramp
    window but the operator hasn't yet confirmed via talker.

    Auto-T2-routine items dedup against curated T1 + T2: if the
    operator has already curated the (record, text) into either tier,
    suppress the auto-T2-routine render line (the curated entry
    already covers it).

    Phase 2A-soft-cadence (2026-05-30): T3 grows an
    :data:`T3_AUTO_SECTION_HEADER` subsection BELOW the curated T3
    entries — auto-suggested routine items overdue against their
    soft cadence target. Empty auto-T3 with populated curated T3 →
    auto subsection silently omitted (NOT polluted with "auto-
    suggested: nothing" header). Empty curated T3 + empty auto-T3 →
    existing :data:`T3_EMPTY_PROMPT` sentinel fires. Empty curated T3
    + populated auto-T3 → subsection renders normally with NO
    ``T3_EMPTY_PROMPT`` (the auto candidates fill the bucket).

    Operator-axis ILB acknowledgement
    (:data:`T3_AUTO_TALKER_DEFERRED_NOTE`) RETIRED 2026-05-30 when
    Phase 2B B1 shipped the talker T3 confirm grammar + the
    ``routine_done`` conversational completion tool. The deferred
    note is no longer rendered in the auto-T3 subsection; the
    constant is preserved for backwards-compat but the render loop
    deliberately omits it. The auto-T3 subsection now emits only
    the confirm prompt after the candidate list.
    """
    auto_t3_routine_candidates = auto_t3_routine_candidates or []
    curated_t1: list[T1T2Entry] = curation.t1 if curation else []
    curated_t2: list[T1T2Entry] = curation.t2 if curation else []
    curated_t3: list[T3Entry] = curation.t3 if curation else []

    (
        merged_t1,
        reason_by_name,
        reason_by_routine_key,
        due_iso_by_routine_key,
    ) = _merge_auto_t1_into_curated(
        curated_t1,
        auto_t1_task_candidates,
        auto_t1_routine_candidates,
    )

    # --- T1 -----------------------------------------------------------
    t1_lines = [
        "### T1 — Imminent deadlines (auto-surfaced — confirm or drop)",
        "",
    ]
    if not merged_t1:
        t1_lines.append("*(no T1 candidates today)*")
        t1_lines.append("")
    else:
        for entry in merged_t1:
            t1_lines.append(_render_t1_entry(
                entry,
                reason_by_name,
                reason_by_routine_key,
                due_iso_by_routine_key,
            ))
        t1_lines.append("")

    # --- T2 -----------------------------------------------------------
    t2_lines = ["### T2 — On the radar", ""]
    if not curated_t2:
        t2_lines.append(T2_EMPTY_PROMPT)
        t2_lines.append("")
    else:
        for entry in curated_t2:
            t2_lines.append(_render_t2_entry(entry))
        t2_lines.append("")

    # Auto-surfaced T2-routine subsection — dedup against curated T1
    # (operator may have confirmed already at T1) + curated T2.
    curated_routine_keys: set[tuple[str, str]] = set()
    for entry in curated_t1 + curated_t2:
        if entry.routine_item is not None:
            curated_routine_keys.add((
                str(entry.routine_item.get("record", "")),
                str(entry.routine_item.get("text", "")),
            ))
    visible_auto_t2: list[Any] = []
    for cand in auto_t2_routine_candidates:
        record = cand.routine_record or ""
        text = cand.item_text or cand.name
        if (record, text) in curated_routine_keys:
            continue
        visible_auto_t2.append(cand)

    if visible_auto_t2:
        t2_lines.append(T2_AUTO_ROUTINE_HEADER)
        t2_lines.append("")
        for cand in visible_auto_t2:
            t2_lines.append(_render_auto_t2_routine_entry(cand))
        t2_lines.append("")

    # --- T3 -----------------------------------------------------------
    # Phase 2A-soft-cadence (2026-05-30): T3 bucket now has two
    # populating sources — curated entries (operator-added) and
    # auto-T3 candidates (overdue against soft cadence). The empty-
    # state contract distinguishes three cases:
    #
    #   Case A: curated T3 populated + auto-T3 empty
    #     → curated entries + NO auto subsection (silent omission;
    #     a "auto-suggested: nothing" header would be noise).
    #
    #   Case B: curated T3 empty + auto-T3 populated
    #     → auto subsection ONLY (no T3_EMPTY_PROMPT — the auto
    #     candidates fill the bucket).
    #
    #   Case C: curated T3 empty + auto-T3 empty
    #     → T3_EMPTY_PROMPT sentinel (existing behavior preserved).
    #
    #   Case D: curated T3 populated + auto-T3 populated
    #     → curated entries FIRST, then auto subsection BELOW.
    t3_lines = ["### T3 — Self-care for today", ""]
    has_curated_t3 = bool(curated_t3)
    has_auto_t3 = bool(auto_t3_routine_candidates)
    if has_curated_t3:
        for entry in curated_t3:
            t3_lines.append(_render_t3_entry(entry))
        t3_lines.append("")
    if has_auto_t3:
        t3_lines.append(T3_AUTO_SECTION_HEADER)
        t3_lines.append("")
        for cand in auto_t3_routine_candidates:
            t3_lines.append(_render_auto_t3_routine_entry(cand))
        t3_lines.append("")
        t3_lines.append(T3_AUTO_CONFIRM_PROMPT)
        t3_lines.append("")
        # ILB acknowledgement RETIRED 2026-05-30 — Phase 2B B1 ships
        # the talker T3 confirm grammar + the conversational
        # completion path (``routine_done`` tool). The deferred-note
        # line is no longer accurate; rendering it would be stale
        # operator-facing copy. The constant
        # ``T3_AUTO_TALKER_DEFERRED_NOTE`` is preserved for backwards-
        # compat with any downstream consumer that may have grepped
        # for it, but the render loop deliberately doesn't emit it.
        # See ``project_routine_followups.md`` Phase 2B B1 handoff
        # note for the contract retirement.
    if not has_curated_t3 and not has_auto_t3:
        t3_lines.append(T3_EMPTY_PROMPT)
        t3_lines.append("")

    return "\n".join(t1_lines + t2_lines + t3_lines)


# ---------------------------------------------------------------------------
# T2 selection pool — open tasks NOT auto-T1, NOT alfred_triage, NOT curated
# ---------------------------------------------------------------------------


def _render_t2_selection_pool(
    records: list[tuple[Path, dict, str]],
    auto_t1_record_names: set[str],
    curated_t1_record_names: set[str],
    curated_t2_record_names: set[str],
) -> str:
    """Compose the ``### T2 selection pool`` subsection (materials).

    The pool surfaces tasks the operator might want to add to T2.
    Filters (in order):
      1. ``status`` in :data:`OPEN_STATUSES`
      2. NOT ``alfred_triage: True`` (logged per skip)
      3. NOT in today's auto-T1 set (already in T1 shortlist)
      4. NOT already in curated T1 (operator confirmed) or T2 (operator
         picked)

    Empty-pool path emits a sentinel line per intentionally-left-blank.
    """
    pool: list[tuple[str, Path]] = []  # (display_name, path) for sort
    alfred_triage_skipped = 0
    for path, fm, name in records:
        if not _is_open(fm):
            continue
        if fm.get("alfred_triage") is True:
            log.info(
                "brief.tier_section.alfred_triage_skipped",
                path=str(path),
                name=name,
                detail=(
                    "janitor-generated triage record is not "
                    "tier-rankable work; surfaces in Daily Sync "
                    "instead."
                ),
            )
            alfred_triage_skipped += 1
            continue
        if name in auto_t1_record_names:
            continue
        if name in curated_t1_record_names:
            continue
        if name in curated_t2_record_names:
            continue
        pool.append((name, path))

    pool.sort(key=lambda np: np[0].lower())

    out = [
        T2_POOL_HEADER,
        (
            "(open `todo`/`active` tasks, NOT auto-T1, NOT "
            "alfred_triage)"
        ),
        "",
    ]
    if not pool:
        out.append("*(selection pool is empty — no other open tasks)*")
        out.append("")
        return "\n".join(out)
    for name, _path in pool:
        out.append(f"- [[task/{name}]]")
    out.append("")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Rollover from yesterday — incomplete T1 / T2 entries
# ---------------------------------------------------------------------------


def _build_status_lookup(
    records: list[tuple[Path, dict, str]],
) -> dict[str, str]:
    """Build a ``{record_name: status_lower}`` map from the task pool.

    Used by the rollover scan to check whether yesterday's T1/T2
    entries are still open today. Missing entries are NOT in the
    lookup (the task may have been deleted/moved; rollover treats
    those as "incomplete-and-missing" — surfaced with a note).
    """
    lookup: dict[str, str] = {}
    for _path, fm, name in records:
        status = fm.get("status") or "todo"
        if isinstance(status, str):
            lookup[name] = status.lower()
    return lookup


def _render_rollover_section(
    yesterday_curation: DailyCuration | None,
    status_by_name: dict[str, str],
) -> str:
    """Compose the ``### Rollover from yesterday (incomplete)`` subsection.

    Logic:
      * If ``yesterday_curation`` is ``None`` (no yesterday daily file
        OR no ``tier_curation`` block) → return empty string (the
        section is suppressed entirely — rollover is opt-in by data
        existence, not unconditional like the curated shortlists).
      * Walk yesterday's T1 + T2 entries. For each:
          - Parse the wikilink to a record name.
          - Look up the current status.
          - If status is missing OR in OPEN_STATUSES → incomplete,
            surface in rollover.
          - Otherwise (done/cancelled today) → completed, skip.

    T3 is NOT included in rollover per dispatch — T3 is today's
    intentions, picked fresh each day.

    Empty-rollover path (yesterday had a block, but everything was
    completed) → surface the header + sentinel rather than suppress,
    so the operator can distinguish "no yesterday file" (suppressed)
    from "yesterday tracked, all done" (header + sentinel).
    """
    if yesterday_curation is None:
        # Section suppressed entirely. Per intentionally-left-blank,
        # we DO emit a log signal so the operator can grep the brief
        # log for "did rollover run?" — the suppression here is
        # render-level only.
        log.info(
            "brief.tier_section.rollover_suppressed_no_yesterday",
            detail=(
                "yesterday's daily file is absent or has no "
                "tier_curation block; rollover section omitted."
            ),
        )
        return ""

    incomplete: list[tuple[str, str]] = []  # (tier_label, wikilink)
    for entry in yesterday_curation.t1:
        # Phase 2A Ship B: routine-origin entries don't roll over —
        # the next cycle resolves naturally via the routine's
        # due_pattern. Skip them silently (the routine's compute
        # surface will re-fire next morning if still due).
        if entry.routine_item is not None:
            continue
        if entry.task is None:
            continue
        rec_name = _wikilink_to_record_name(entry.task)
        if rec_name is None:
            continue
        status = status_by_name.get(rec_name)
        # Missing OR open → incomplete (treat missing as "task may have
        # been moved/deleted; flag to operator").
        if status is None or status in OPEN_STATUSES:
            incomplete.append(("T1", entry.task))
    for entry in yesterday_curation.t2:
        if entry.routine_item is not None:
            continue
        if entry.task is None:
            continue
        rec_name = _wikilink_to_record_name(entry.task)
        if rec_name is None:
            continue
        status = status_by_name.get(rec_name)
        if status is None or status in OPEN_STATUSES:
            incomplete.append(("T2", entry.task))

    out = [ROLLOVER_HEADER, ""]
    if not incomplete:
        out.append(
            "*(yesterday's tracked items all completed — nothing to "
            "roll over)*"
        )
        out.append("")
        return "\n".join(out)
    for tier_label, wikilink in incomplete:
        out.append(
            f"- {tier_label}: {wikilink} *(uncompleted yesterday)*"
        )
    out.append("")
    return "\n".join(out)


def render_daily_goal_line(goal: DailyGoalState) -> str:
    """Render the one-of-each-tier daily-goal status line (Q4, 2026-06-26).

    The PURPOSE of tiering per the spec: finish at least one item from
    each of T1/T2/T3 each day (a balanced day — urgent + medium +
    self-care), ideally all T1 done. This line surfaces that goal's
    progress at the top of the tier section so the view is rendered
    AROUND the goal, not just as three buckets.

    Minimal register per Q4 (voice polish — gentle/plain phrasing — is
    deferred to prompt-tuner). Per ``feedback_intentionally_left_blank``:
    ALWAYS emits a line, even on an empty day ("no tier items yet
    today"), so the goal signal is never a silent absence.

    Shape (per-lane ``done/available`` ticks + a balanced-day marker):

        **Daily goal — balanced day:** ✓ achieved · T1 1/2 · T2 1/1 · T3 0/1
        **Daily goal — balanced day:** not yet · T1 0/2 · T2 0/1 · T3 0/1
        **Daily goal:** no tier items yet today.
    """
    total = (
        goal.t1_available + goal.t2_available + goal.t3_available
    )
    if total == 0:
        return "**Daily goal:** no tier items yet today."

    def _lane(label: str, done: int, avail: int) -> str:
        return f"{label} {done}/{avail}"

    status = "✓ achieved" if goal.balanced_day else "not yet"
    # Note the ideal (all T1 done) when it holds AND there are T1 items.
    ideal = ""
    if goal.t1_available > 0 and goal.all_t1_done:
        ideal = " · all T1 done"
    return (
        f"**Daily goal — balanced day:** {status}"
        f" · {_lane('T1', goal.t1_done, goal.t1_available)}"
        f" · {_lane('T2', goal.t2_done, goal.t2_available)}"
        f" · {_lane('T3', goal.t3_done, goal.t3_available)}"
        f"{ideal}"
    )


# ---------------------------------------------------------------------------
# View → formatter-input adapters (Step 2c, 2026-06-26)
# ---------------------------------------------------------------------------
#
# The render layer no longer calls the ``compute_auto_*`` predicates. It
# reads ``compute_today_view``'s lane assignments — the SINGLE source of
# what surfaces / which lane — and these adapters slice the view's lanes
# back into the candidate shapes the existing formatters consume. The
# "auto" subset of each lane is selected by ``source`` (the view marks
# auto candidates with ``auto-*`` sources; curated entries the formatters
# read separately from the curation block). This keeps the markdown
# byte-identical (the view's membership is equivalent to the prior direct
# compute — proven by the unchanged output pins) while making the view
# the only place a surface decision is made.

def _auto_t1_task_from_view(view: TodayView) -> list[AutoT1Candidate]:
    """Task-origin T1 entries carrying an auto reason, sliced from the
    view's T1 lane.

    Returns EVERY task-origin T1 entry that has a ``surface_reason`` —
    including a CURATED entry the operator confirmed that also
    auto-surfaces (the view annotates such entries with the auto
    reason/due). The downstream merge keys reason lookups off this list
    AND dedups appends against the curation block, so returning the
    curated-coinciding entry populates the reason map without
    double-rendering. Curated entries with NO auto reason (operator
    added a task that isn't deadline-near) carry no reason and are
    skipped — they render bare from the curation block."""
    out: list[AutoT1Candidate] = []
    for e in view.t1:
        if e.origin != "task" or not e.surface_reason:
            continue
        out.append(AutoT1Candidate(
            path=e.path,
            name=e.name,
            due_iso=e.due_iso or "",
            surface_reason=e.surface_reason,
            origin="task",
        ))
    return out


def _auto_t1_routine_from_view(view: TodayView) -> list[AutoT1Candidate]:
    """Routine-origin T1 entries carrying an auto reason, sliced from the
    view's T1 lane. Same curated-coinciding inclusion as the task
    variant (the view annotates curated routine_item entries that also
    auto-surface)."""
    out: list[AutoT1Candidate] = []
    for e in view.t1:
        if e.origin != "routine_item" or not e.surface_reason:
            continue
        out.append(AutoT1Candidate(
            path=e.path,
            name=e.name,
            due_iso=e.due_iso or "",
            surface_reason=e.surface_reason,
            origin="routine",
            routine_record=e.routine_record,
            item_text=e.item_text,
        ))
    return out


def _auto_t2_routine_from_view(view: TodayView) -> list[AutoT1Candidate]:
    """Routine-origin auto-T2 ramp candidates, sliced from the T2 lane."""
    out: list[AutoT1Candidate] = []
    for e in view.t2:
        if e.origin != "routine_item" or e.source != "auto-surface-routine":
            continue
        out.append(AutoT1Candidate(
            path=e.path,
            name=e.name,
            due_iso=e.due_iso or "",
            surface_reason=e.surface_reason or "",
            origin="routine",
            routine_record=e.routine_record,
            item_text=e.item_text,
        ))
    return out


def _auto_t3_routine_from_view(view: TodayView) -> list[AutoT3Candidate]:
    """Cadence-driven auto-T3 candidates, sliced from the T3 lane. The
    cadence metadata (target / days-since / ratio) is carried on the
    view's TierEntry so the annotation render is a pure read."""
    out: list[AutoT3Candidate] = []
    for e in view.t3:
        if e.origin != "routine_item" or e.source != "auto-cadence-routine":
            continue
        out.append(AutoT3Candidate(
            path=e.path,
            routine_record=e.routine_record or "",
            item_text=e.item_text or e.name,
            target_cadence_days=e.target_cadence_days or 0,
            days_since_last_completed=e.days_since_last_completed,
            overdue_ratio=(
                e.overdue_ratio if e.overdue_ratio is not None
                else float("inf")
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def render_tier_section(
    vault_path: Path,
    now: datetime,
    tier_defaults: Any = None,
) -> str:
    """Render the brief's ``Open Tasks by Tier`` section body (V2).

    ``now`` is the reference instant — passed by the brief daemon at
    fire time + by ``/today`` at request time. ``now.date()`` is "today"
    for the curation lookup.

    ``tier_defaults`` (Q3 Option A, 2026-06-26): optional global tier-
    window defaults, passed straight through to ``compute_today_view`` so
    the 06:00 brief applies the SAME defaults the aggregator's 05:59 pass
    does. ``None`` → no defaults (opt-out semantics unchanged).

    Always returns a non-empty string per
    ``feedback_intentionally_left_blank``: even the cold-start case
    (no vault, no curation, no records) emits an explicit "ran,
    nothing to do" composition.

    Read-side stability: this function is a pure projection over the
    inputs (today's curation + auto-T1 candidates + yesterday's
    curation + task pool snapshot). Called twice with identical inputs
    it returns identical output — Ship 4 talker reads + writes
    curation separately; this render never mutates the block.
    """
    today = now.date()

    # --- 1. Read today's curation ---------------------------------
    curation = load_daily_curation(vault_path, today)

    # --- 2. Compute the unified today view ------------------------
    # Step 2c (Option B, 2026-06-26): the SINGLE source of what
    # surfaces / which lane. This render layer no longer calls the
    # ``compute_auto_*`` predicates directly — it reads the view's lane
    # assignments and re-presents them. The view already merged curated +
    # auto candidates per lane (via ``classify_routine_item``, the single
    # predicate); the auto-candidate lists below are SLICED from the
    # view's lanes (by origin + auto-source), not independently computed.
    # So the renderer makes NO surface decision of its own — it's a pure
    # formatter of the view's WHAT. (Membership is byte-equivalent to the
    # prior direct-compute path — proven by the unchanged
    # ``test_brief_tier_section`` output pins.) The selection pool +
    # rollover are render-only MATERIALS (not substrate lane assignment)
    # and stay computed here.
    today_view = compute_today_view(vault_path, now, tier_defaults)

    auto_t1_task_candidates = _auto_t1_task_from_view(today_view)
    auto_t1_routine_candidates = _auto_t1_routine_from_view(today_view)
    auto_t2_routine_candidates = _auto_t2_routine_from_view(today_view)
    auto_t3_routine_candidates = _auto_t3_routine_from_view(today_view)
    auto_t1_record_names = {c.name for c in auto_t1_task_candidates}

    # --- 3. Read yesterday's curation for rollover ----------------
    yesterday = today - timedelta(days=1)
    yesterday_curation = load_daily_curation(vault_path, yesterday)

    # --- 4. Scan task pool ----------------------------------------
    records = _iter_task_records(vault_path)
    status_by_name = _build_status_lookup(records)

    # Build the curated-name sets for the selection-pool exclusion.
    # Only task-origin curated entries pollute these sets — routine-
    # origin entries don't shadow task-pool entries.
    curated_t1_names: set[str] = set()
    curated_t2_names: set[str] = set()
    if curation is not None:
        for e in curation.t1:
            if e.task is not None:
                n = _wikilink_to_record_name(e.task)
                if n:
                    curated_t1_names.add(n)
        for e in curation.t2:
            if e.task is not None:
                n = _wikilink_to_record_name(e.task)
                if n:
                    curated_t2_names.add(n)

    # --- 5. Compose render --------------------------------------------
    shortlists = _render_curated_shortlists(
        curation,
        auto_t1_task_candidates,
        auto_t1_routine_candidates,
        auto_t2_routine_candidates,
        auto_t3_routine_candidates,
    )
    pool = _render_t2_selection_pool(
        records,
        auto_t1_record_names,
        curated_t1_names,
        curated_t2_names,
    )
    rollover = _render_rollover_section(yesterday_curation, status_by_name)

    # Daily-goal status line (Q4, 2026-06-26). Read from the SAME
    # ``today_view`` computed once at the top — no second compute. The
    # line renders first so the tier view is framed around the
    # balanced-day goal, not just three buckets.
    goal_line = render_daily_goal_line(today_view.daily_goal)

    # Compose: goal line first, then shortlists, separator, pool, and
    # (optional) rollover. Rollover is appended only when non-empty
    # (suppressed when yesterday's file is absent).
    parts = [goal_line, "", shortlists, "---", "", pool]
    if rollover:
        parts.append(rollover)

    body = "\n".join(parts)

    log.info(
        "brief.tier_section.rendered",
        scanned=len(records),
        curation_loaded=curation is not None,
        curated_t1=len(curation.t1) if curation else 0,
        curated_t2=len(curation.t2) if curation else 0,
        curated_t3=len(curation.t3) if curation else 0,
        auto_t1_task_count=len(auto_t1_task_candidates),
        auto_t1_routine_count=len(auto_t1_routine_candidates),
        auto_t2_routine_count=len(auto_t2_routine_candidates),
        # Phase 2A-soft-cadence (2026-05-30): T3 soft-cadence auto-
        # suggest count. ``feedback_log_emission_test_pattern`` pin:
        # test asserts this field is present in the log when
        # candidates exist, AND when bucket is empty.
        auto_t3_routine_count=len(auto_t3_routine_candidates),
        rollover_present=bool(rollover),
        yesterday_curation_loaded=yesterday_curation is not None,
        # Step 2c (2026-06-26): the daily-goal rollup, surfaced from the
        # unified compute_today_view, pinned per
        # ``feedback_log_emission_test_pattern``.
        balanced_day=today_view.daily_goal.balanced_day,
        all_t1_done=today_view.daily_goal.all_t1_done,
    )
    return body


def _build_task_status_map(vault_path: Path) -> dict[str, dict[str, Any]]:
    """Map each task record's NAME → its frontmatter, for status lookup.

    Built once per ``/today`` render from the existing
    :func:`_iter_task_records` walk (which already skips broken /
    non-task files). Keyed on the record name (matching what
    :func:`_wikilink_to_record_name` extracts from a ``[[task/Name]]``
    curated entry). On a duplicate name the first walked record wins
    (sorted glob order); name collisions in ``task/`` are a separate
    janitor concern.
    """
    status_map: dict[str, dict[str, Any]] = {}
    for _path, fm, name in _iter_task_records(vault_path):
        status_map.setdefault(name, fm)
    return status_map


def _curated_entry_is_closed(
    entry: T1T2Entry,
    status_map: dict[str, dict[str, Any]],
) -> bool:
    """Return True iff a curated T1/T2 entry references a CLOSED task.

    Only task-origin entries (``entry.task`` is a ``[[task/Name]]``
    wikilink) are status-checked. Routine-origin entries
    (``entry.routine_item``) have no task record and are NEVER closed
    by this gate (return False → kept).

    Fail-OPEN on a lookup miss: an entry whose task record is missing,
    unreadable, or absent from ``status_map`` returns False (kept), so a
    transient parse failure never silently hides a real commitment —
    only an EXPLICITLY closed status (``not _is_open``) hides the entry.
    Reuses :func:`_is_open` / ``OPEN_STATUSES`` — no new status set.
    """
    if entry.routine_item is not None:
        return False  # routine-origin — no task record to check
    record_name = _wikilink_to_record_name(entry.task or "")
    if record_name is None:
        return False  # malformed/absent task ref → fail-open (keep)
    fm = status_map.get(record_name)
    if fm is None:
        return False  # task record not found → fail-open (keep)
    return not _is_open(fm)


def render_curated_tier_section_for_today(
    daily_curation: DailyCuration | None,
    vault_path: Path | None = None,
) -> str:
    """Render the ``/today`` curated-only tier section body.

    Operator-committed view for the ``/today`` slash command (2026-05-30
    scope refinement). Renders ONLY the operator-curated T1/T2/T3
    shortlists from the daily_curation block — no auto-T1 candidates,
    no T2 selection pool, no auto-T2-routine subsection, no rollover,
    no confirm prompts. Operator already committed; the view's purpose
    is "what's on my plate right now" not "what should I commit to."

    **Live completed-task filter** (2026-06-15). When ``vault_path`` is
    provided, each curated T1/T2 task-origin entry is checked against
    its task record's CURRENT status and OMITTED when closed (status not
    in ``OPEN_STATUSES``) — operator closes ``task/Foo`` via the talker
    and ``/today`` stops showing it. Routine-origin T1/T2 entries and
    all T3 free-text entries have no task record and always pass. The
    filter fails OPEN on a missing/unreadable record (only an
    explicitly-closed status hides an item). When ``vault_path`` is
    ``None`` (the default — e.g. the morning brief's full-materials
    render, or any caller that doesn't thread the path) NO filtering
    happens and the render is byte-identical to the pre-2026-06-15
    behaviour, so existing callers are unaffected.

    Contrast with :func:`render_tier_section` (the full materials view
    the morning brief uses): that function consumes the same
    daily_curation PLUS auto-T1 candidates + selection pool + rollover
    + confirm affordances. The two surfaces share the per-entry
    rendering primitives (:func:`_render_t2_entry`,
    :func:`_render_t3_entry`) so the shape of a single curated entry
    stays consistent across both views.

    Empty-bucket convention (per ``feedback_intentionally_left_blank``,
    Andrew-ratified 2026-05-30): header-suffix sentinel
    ``### T1 — (no items yet)`` keeps all three headers visible while
    distinguishing "operator hasn't curated yet" from "broken render."
    The morning brief uses a separate per-bucket sentinel line — the
    ``/today`` view prefers the suffix because the operator-committed
    framing means an empty bucket reads as "nothing committed to T2
    yet" rather than "what's available for T2."

    When ``daily_curation`` is ``None`` (no daily file exists yet for
    today, e.g. running ``/today`` before the 06:00 brief / 05:59
    aggregator has fired), all three buckets render with the empty-
    suffix sentinel so the operator sees the same shape they'd see
    after a deliberate empty curation.

    Cross-Ship contract: T1/T2/T3 entries render identically to the
    morning brief's curated section (same per-entry helpers), minus
    the confirm prompts that fire on auto-surfaced candidates the
    operator hasn't yet committed to. A render-shape change on the
    morning brief side propagates here through the shared helpers.
    """
    curated_t1 = daily_curation.t1 if daily_curation else []
    curated_t2 = daily_curation.t2 if daily_curation else []
    curated_t3 = daily_curation.t3 if daily_curation else []

    # Live completed-task filter (2026-06-15). Only when a vault_path is
    # threaded (the /today composer). T1/T2 task-origin entries whose
    # referenced task record is closed are dropped; routine-origin + T3
    # entries are untouched (no task record). An emptied bucket still
    # hits the header-suffix sentinel below (ILB preserved).
    filtered_closed = 0
    if vault_path is not None:
        status_map = _build_task_status_map(vault_path)
        before = len(curated_t1) + len(curated_t2)
        curated_t1 = [
            e for e in curated_t1
            if not _curated_entry_is_closed(e, status_map)
        ]
        curated_t2 = [
            e for e in curated_t2
            if not _curated_entry_is_closed(e, status_map)
        ]
        filtered_closed = before - (len(curated_t1) + len(curated_t2))

    def _bucket(header_label: str, entries: list, render_entry) -> list[str]:
        """Compose one bucket's lines.

        Empty bucket → header-suffix sentinel only (single line +
        trailing blank). Populated → header + entries + trailing
        blank. The shared shape keeps the three-bucket render
        rhythm uniform.
        """
        if not entries:
            return [f"### {header_label} — (no items yet)", ""]
        out = [f"### {header_label}", ""]
        for entry in entries:
            out.append(render_entry(entry))
        out.append("")
        return out

    # Reuse the existing per-entry helpers — they already discriminate
    # task vs routine_item shape WITHOUT confirm prompts, which is
    # exactly what ``/today`` wants. T1 uses :func:`_render_t2_entry`
    # (NOT :func:`_render_t1_entry`) because the T1 render path with
    # confirm/reason annotations is for the auto-surfaced morning-brief
    # view, not the operator-committed ``/today`` view.
    lines: list[str] = []
    lines.extend(_bucket("T1", curated_t1, _render_t2_entry))
    lines.extend(_bucket("T2", curated_t2, _render_t2_entry))
    lines.extend(_bucket("T3", curated_t3, _render_t3_entry))

    body = "\n".join(lines).rstrip() + "\n"

    log.info(
        "brief.tier_section.rendered_curated_for_today",
        curation_loaded=daily_curation is not None,
        curated_t1=len(curated_t1),
        curated_t2=len(curated_t2),
        curated_t3=len(curated_t3),
        # ILB: surface the completed-task filter so an operator can grep
        # "why did my T1 item disappear" — distinguishes "I closed it"
        # (status_filtered>0) from "render dropped it" (a bug).
        # status_filter_applied is False when no vault_path was threaded
        # (filtering off — e.g. the brief's full-materials render).
        status_filter_applied=vault_path is not None,
        status_filtered=filtered_closed,
    )
    return body


__all__ = [
    "ROLLOVER_HEADER",
    "SECTION_HEADER",
    "T1_CONFIRM_PROMPT",
    "T2_AUTO_ROUTINE_HEADER",
    "T2_EMPTY_PROMPT",
    "T2_POOL_HEADER",
    "T2_ROUTINE_CONFIRM_PROMPT",
    "T3_AUTO_ANNOTATION_TEMPLATE",
    "T3_AUTO_CONFIRM_PROMPT",
    "T3_AUTO_DAYS_SINCE_NEVER_LABEL",
    "T3_AUTO_SECTION_HEADER",
    "T3_AUTO_TALKER_DEFERRED_NOTE",
    "T3_EMPTY_PROMPT",
    "render_curated_tier_section_for_today",
    "render_tier_section",
]
