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
last-consumer-rewrite ratified pattern #22). The migration script
``scripts/migrate_tier_phase1.py`` is preserved for the deferred
backfill of the 24 existing ``base_tier`` records (Ship 5).

Render shape (the section body — the brief renderer wraps it under
``## Open Tasks by Tier``):

    ### T1 — Imminent deadlines (auto-surfaced — confirm or drop)
    - [ ] [[task/Steph Yang ROE]] — due today  *(confirm? reply "T1 confirm")*
    - [ ] [[task/Pay Clinic Rental]] — due tomorrow

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

Read path (per dispatch — three vault reads + one auto-T1 compute):

  1. ``load_daily_curation(vault_path, today)`` — today's
     ``tier_curation`` block. ``None`` when un-curated yet
     (operator's "selection pool" mode); populated when talker has
     already curated.
  2. ``compute_auto_t1_candidates(vault_path, now)`` — the auto-T1
     surface (due today / due tomorrow / inside ``escalate_at_days``
     window). Used to merge auto-candidates with operator-curated T1
     entries + surface the confirm affordance.
  3. ``load_daily_curation(vault_path, today - 1 day)`` — yesterday's
     curation, for rollover detection. Each yesterday-T1/T2 entry is
     checked against the current task record's status; incomplete
     entries surface in the Rollover section.
  4. Open-task pool scan over ``vault/task/*.md`` for the T2 selection
     pool (status in OPEN_STATUSES, NOT ``alfred_triage``, NOT in
     today's auto-T1 set, NOT already-curated T1/T2).

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
    compute_auto_routine_candidates,
    compute_auto_routine_t2_candidates,
    compute_auto_t1_candidates,
)
from alfred.tier.daily_curation import (
    DailyCuration,
    T1T2Entry,
    T3Entry,
    load_daily_curation,
)

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
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"read failed: {exc}"
    except UnicodeDecodeError as exc:
        return f"not utf-8: {exc}"

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
        ``- [ ] [[task/Name]] — due today  *(confirm)*``. Reason
        lookup keyed on record name.
      * Routine-origin (``entry.routine_item`` populated) — renders as
        ``- [ ] <text> — due <date> (<reason>, from
        [[routine/<record>]])  *(confirm)*`` when an auto-T1 candidate
        matches (date + reason inline); otherwise
        ``- [ ] <text> (from [[routine/<record>]])`` for operator-
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
      ``- [ ] Garbage Out — due Fri May 29 (escalate window (1d before
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
                f"- [ ] {text} — due {_format_due_date(due_iso)} "
                f"({reason}, from [[routine/{record}]])"
            )
        elif reason:
            head = (
                f"- [ ] {text} — {reason}, from [[routine/{record}]]"
            )
        else:
            head = f"- [ ] {text} (from [[routine/{record}]])"
    else:
        # Task-origin path (the original Tier-V2 Ship 1 shape).
        task_str = entry.task or ""
        record_name = _wikilink_to_record_name(task_str) or ""
        reason = auto_t1_reason_by_name.get(record_name, "")
        if reason:
            head = f"- [ ] {task_str} — {reason}"
        else:
            head = f"- [ ] {task_str}"

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
        return f"- [ ] {text} (from [[routine/{record}]])"
    return f"- [ ] {entry.task or ''}"


def _render_auto_t2_routine_entry(candidate: Any) -> str:
    """Render one auto-surfaced T2 routine candidate line.

    ``candidate`` is an :class:`alfred.tier.compute.AutoT1Candidate`
    with ``origin == "routine"``. Renders as:

      ``- [ ] <text> — due <date> (<reason>, from
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
        f"- [ ] {text} — due {due_display} "
        f"({reason}, from [[routine/{record}]])"
        f"  {T2_ROUTINE_CONFIRM_PROMPT}"
    )


def _render_t3_entry(entry: T3Entry) -> str:
    """Render one T3 line — bare free-text item (no confirm affordance).

    Note T3 entries carry ``item:`` (free-text) not ``task:`` (wikilink).
    """
    return f"- [ ] {entry.item}"


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
    """
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
    t3_lines = ["### T3 — Self-care for today", ""]
    if not curated_t3:
        t3_lines.append(T3_EMPTY_PROMPT)
        t3_lines.append("")
    else:
        for entry in curated_t3:
            t3_lines.append(_render_t3_entry(entry))
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


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def render_tier_section(
    vault_path: Path,
    now: datetime,
) -> str:
    """Render the brief's ``Open Tasks by Tier`` section body (V2).

    ``now`` is the reference instant — passed by the brief daemon at
    fire time + by ``/today`` at request time. ``now.date()`` is "today"
    for the curation lookup.

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

    # --- 2. Compute auto-T1 / auto-T2 candidates ------------------
    # Task-origin (the original Ship 2 surface).
    auto_t1_task_candidates = compute_auto_t1_candidates(vault_path, now)
    # Routine-origin T1 + T2 ramp (Phase 2A Ship B, 2026-05-29).
    auto_t1_routine_candidates = compute_auto_routine_candidates(
        vault_path, now,
    )
    auto_t2_routine_candidates = compute_auto_routine_t2_candidates(
        vault_path, now,
    )
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
    )
    pool = _render_t2_selection_pool(
        records,
        auto_t1_record_names,
        curated_t1_names,
        curated_t2_names,
    )
    rollover = _render_rollover_section(yesterday_curation, status_by_name)

    # Compose with separator between shortlists and materials. Rollover
    # is appended only when non-empty (suppressed when yesterday's
    # file is absent).
    parts = [shortlists, "---", "", pool]
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
        rollover_present=bool(rollover),
        yesterday_curation_loaded=yesterday_curation is not None,
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
    "T3_EMPTY_PROMPT",
    "render_tier_section",
]
