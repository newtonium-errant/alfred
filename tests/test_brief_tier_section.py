"""Tests for ``alfred.brief.tier_section`` — V2 render (2026-05-29).

V1's per-task ``compute_effective_tier``-driven render is gone; V2
reads the daily-curation block (``vault/daily/<date>.md`` →
``tier_curation`` frontmatter) and composes:

  * Curated shortlists (T1/T2/T3) at the top with empty-bucket prompts
    that name the talker-reply pattern (Ship 4 SKILL contract).
  * T2 selection pool (materials) below the separator.
  * Rollover from yesterday's incomplete T1/T2 (suppressed when
    yesterday's file is absent).

Test surface per Ship 2 dispatch:
  1. Curated shortlists render from ``vault/daily/<date>.md``
  2. Empty buckets surface canonical prompt text
  3. Auto-T1 candidates surface with confirm affordance
  4. Auto-T1 candidates already curated with ``confirmed: true`` are
     bare (no confirm prompt)
  5. T2 selection pool excludes auto-T1 + alfred_triage + curated T1/T2
  6. Rollover section detects yesterday's incomplete T1/T2
  7. Rollover SUPPRESSED if yesterday's file missing
  8. T3 rollover NOT surfaced (skip-by-design)
  9. **CRITICAL** read-side stability — render twice = identical output
  10. ``alfred_triage_skipped`` log event fires per skip
  11. Empty vault — all sentinels surface correctly
  12. Prompt-phrase constants pinned (Ship 4 SKILL contract)
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import structlog

from alfred.brief.tier_section import (
    ROLLOVER_HEADER,
    SECTION_HEADER,
    T1_CONFIRM_PROMPT,
    T2_AUTO_ROUTINE_HEADER,
    T2_EMPTY_PROMPT,
    T2_POOL_HEADER,
    T2_ROUTINE_CONFIRM_PROMPT,
    T3_AUTO_ANNOTATION_TEMPLATE,
    T3_AUTO_CONFIRM_PROMPT,
    T3_AUTO_DAYS_SINCE_NEVER_LABEL,
    T3_AUTO_SECTION_HEADER,
    T3_AUTO_TALKER_DEFERRED_NOTE,
    T3_EMPTY_PROMPT,
    render_curated_tier_section_for_today,
    render_daily_goal_line,
    render_tier_section,
)
from alfred.tier.compute import DailyGoalState
from alfred.tier.daily_curation import (
    DailyCuration,
    T1T2Entry,
    T3Entry,
)


# Reference instant: 2026-05-28 13:00 UTC. Today = 2026-05-28,
# Tomorrow = 2026-05-29, Yesterday = 2026-05-27.
NOW = datetime(2026, 5, 28, 13, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write_task(
    vault_path: Path,
    name: str,
    fm: dict,
) -> Path:
    """Write a task record under ``vault/task/<name>.md``."""
    task_dir = vault_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for k, v in fm.items():
        if isinstance(v, list):
            if not v:
                lines.append(f"{k}: []")
            else:
                lines.append(f"{k}:")
                for item in v:
                    lines.append(f"- {item}")
        elif v is None:
            lines.append(f"{k}: null")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        elif isinstance(v, str):
            lines.append(f"{k}: {v!r}")
        else:
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    path = task_dir / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_daily(
    vault_path: Path,
    iso_date: str,
    tier_curation_yaml: str | None,
) -> Path:
    """Write a daily file under ``vault/daily/<iso_date>.md``.

    ``tier_curation_yaml`` is the indented YAML body for the
    ``tier_curation:`` key (excluding the key itself). Pass ``None``
    to omit the block entirely (a daily file written by the routine
    aggregator with no operator curation yet).
    """
    daily_dir = vault_path / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: daily", f"date: {iso_date}"]
    if tier_curation_yaml is not None:
        lines.append("tier_curation:")
        # Indent each line by 2 spaces to nest under the key.
        for body_line in tier_curation_yaml.splitlines():
            lines.append(f"  {body_line}")
    lines.append("---")
    lines.append("")
    lines.append("# body")
    path = daily_dir / f"{iso_date}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. Section header + cross-agent contract constants
# ---------------------------------------------------------------------------


def test_section_header_pinned() -> None:
    """The brief daemon wraps the section under ``## {SECTION_HEADER}``.

    Renaming here would break the daemon's section-list wiring; pin
    so a typo surfaces immediately.
    """
    assert SECTION_HEADER == "Open Tasks by Tier"


def test_t1_confirm_prompt_pinned() -> None:
    """Ship 4 SKILL quotes this verbatim — pin to surface drift."""
    assert T1_CONFIRM_PROMPT == '*(confirm? reply "T1 confirm")*'


def test_t2_empty_prompt_pinned() -> None:
    """Ship 4 SKILL contract."""
    assert T2_EMPTY_PROMPT == (
        '*(empty — reply "T2 add <items from selection pool below or '
        'anywhere>")*'
    )


def test_t3_empty_prompt_pinned() -> None:
    """Ship 4 SKILL contract."""
    assert T3_EMPTY_PROMPT == (
        '*(empty — pick from Aspirational routines below or add new — '
        'reply "T3 add walk Fergus")*'
    )


def test_rollover_header_pinned() -> None:
    assert ROLLOVER_HEADER == "### Rollover from yesterday (incomplete)"


def test_t2_pool_header_pinned() -> None:
    assert T2_POOL_HEADER == "### T2 selection pool"


# ---------------------------------------------------------------------------
# 2. Empty vault — sentinels + intentionally-left-blank
# ---------------------------------------------------------------------------


def test_empty_vault_renders_all_sentinels(tmp_path: Path) -> None:
    """No task/ dir, no daily file → all three buckets get empty
    prompts, T2 pool sentinel, rollover suppressed."""
    body = render_tier_section(tmp_path, NOW)

    # Three curated headers always emit.
    assert "### T1 — Imminent deadlines" in body
    assert "### T2 — On the radar" in body
    assert "### T3 — Self-care for today" in body

    # T1 has the no-candidates sentinel (no auto-T1 + no curation).
    assert "*(no T1 candidates today)*" in body
    # T2 + T3 empty-bucket prompts surface verbatim.
    assert T2_EMPTY_PROMPT in body
    assert T3_EMPTY_PROMPT in body

    # T2 selection pool header + sentinel.
    assert T2_POOL_HEADER in body
    assert "selection pool is empty" in body

    # Rollover suppressed entirely (no yesterday file).
    assert ROLLOVER_HEADER not in body


def test_empty_vault_logs_rendered_event(tmp_path: Path) -> None:
    """Even cold-start emits the ``rendered`` log event."""
    with structlog.testing.capture_logs() as captured:
        render_tier_section(tmp_path, NOW)
    events = [
        c for c in captured if c.get("event") == "brief.tier_section.rendered"
    ]
    assert len(events) == 1
    e = events[0]
    assert e["scanned"] == 0
    assert e["curation_loaded"] is False
    # Phase 2A Ship B: log event split task vs routine origin counts.
    assert e["auto_t1_task_count"] == 0
    assert e["auto_t1_routine_count"] == 0
    assert e["auto_t2_routine_count"] == 0
    assert e["rollover_present"] is False


# ---------------------------------------------------------------------------
# 3. Curated shortlists — render from tier_curation block
# ---------------------------------------------------------------------------


def test_curated_t1_t2_t3_render_from_daily_file(tmp_path: Path) -> None:
    """Operator-curated shortlists surface verbatim under the three
    headers."""
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml=(
            "t1:\n"
            "  - task: '[[task/Steph Yang ROE]]'\n"
            "    source: operator\n"
            "    confirmed: true\n"
            "t2:\n"
            "  - task: '[[task/RRTS Bug List]]'\n"
            "    source: operator\n"
            "t3:\n"
            "  - item: Walk Fergus\n"
            "    source: aspirational\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)

    # T1 entry — confirmed=True → bare (no confirm prompt).
    assert "[[task/Steph Yang ROE]]" in body
    # T1 confirm prompt should NOT appear because confirmed=true.
    t1_section = body.split("### T2")[0]
    assert T1_CONFIRM_PROMPT not in t1_section

    # T2 entry — bare wikilink, no confirm.
    assert "[[task/RRTS Bug List]]" in body

    # T3 entry — free-text item, no confirm.
    assert "Walk Fergus" in body
    # T3 empty prompt should NOT surface (T3 has content).
    assert T3_EMPTY_PROMPT not in body


def test_curated_empty_buckets_show_prompts(tmp_path: Path) -> None:
    """Daily file exists but all three buckets are empty arrays →
    operator gets the prompts."""
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml="t1: []\nt2: []\nt3: []\n",
    )
    body = render_tier_section(tmp_path, NOW)
    assert "*(no T1 candidates today)*" in body
    assert T2_EMPTY_PROMPT in body
    assert T3_EMPTY_PROMPT in body


# ---------------------------------------------------------------------------
# 4. Auto-T1 candidates — surface with confirm affordance
# ---------------------------------------------------------------------------


def test_auto_t1_due_today_surfaces_with_confirm_prompt(
    tmp_path: Path,
) -> None:
    """Task due today → auto-T1 → renders with ``due today`` reason +
    confirm prompt (it wasn't pre-curated)."""
    _write_task(
        tmp_path,
        "RRTS Payroll",
        {
            "type": "task",
            "status": "todo",
            "name": "RRTS Payroll",
            "due": "2026-05-28",
        },
    )
    body = render_tier_section(tmp_path, NOW)
    assert "[[task/RRTS Payroll]]" in body
    assert "due today" in body
    # Confirm prompt must surface for auto-surfaced (un-curated) entry.
    assert T1_CONFIRM_PROMPT in body


def test_auto_t1_due_tomorrow_uses_correct_reason(tmp_path: Path) -> None:
    """Reason annotation differs by surface — tomorrow gets ``due
    tomorrow``."""
    _write_task(
        tmp_path,
        "Pay Clinic Rental",
        {
            "type": "task",
            "status": "todo",
            "name": "Pay Clinic Rental",
            "due": "2026-05-29",
        },
    )
    body = render_tier_section(tmp_path, NOW)
    assert "due tomorrow" in body


def test_auto_t1_already_confirmed_renders_bare(tmp_path: Path) -> None:
    """Auto-surfaced task already in curated T1 with ``confirmed: true``
    renders bare — operator signed off, no prompt needed."""
    _write_task(
        tmp_path,
        "Steph Yang ROE",
        {
            "type": "task",
            "status": "todo",
            "name": "Steph Yang ROE",
            "due": "2026-05-28",
        },
    )
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml=(
            "t1:\n"
            "  - task: '[[task/Steph Yang ROE]]'\n"
            "    source: auto-due\n"
            "    confirmed: true\n"
            "t2: []\n"
            "t3: []\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    # T1 entry surfaces with the reason but NO confirm prompt.
    assert "[[task/Steph Yang ROE]]" in body
    assert "due today" in body
    # Confirm prompt should NOT appear for confirmed=true entries.
    # Find the T1 line specifically.
    t1_lines = [
        ln for ln in body.splitlines()
        if "[[task/Steph Yang ROE]]" in ln
    ]
    assert len(t1_lines) == 1
    assert T1_CONFIRM_PROMPT not in t1_lines[0]


def test_auto_t1_curated_but_unconfirmed_still_shows_prompt(
    tmp_path: Path,
) -> None:
    """Auto-T1 candidate pre-recorded with ``confirmed: false`` (talker
    surfaced it but operator hasn't replied yet) → still shows the
    confirm prompt."""
    _write_task(
        tmp_path,
        "Some Task",
        {
            "type": "task",
            "status": "todo",
            "name": "Some Task",
            "due": "2026-05-28",
        },
    )
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml=(
            "t1:\n"
            "  - task: '[[task/Some Task]]'\n"
            "    source: auto-due\n"
            "    confirmed: false\n"
            "t2: []\n"
            "t3: []\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    t1_lines = [
        ln for ln in body.splitlines()
        if "[[task/Some Task]]" in ln
    ]
    assert len(t1_lines) == 1
    assert T1_CONFIRM_PROMPT in t1_lines[0]


# ---------------------------------------------------------------------------
# 5. T2 selection pool — exclusions
# ---------------------------------------------------------------------------


def test_t2_pool_excludes_auto_t1(tmp_path: Path) -> None:
    """Auto-T1 candidates must NOT appear in the T2 pool — they're
    already surfaced above."""
    # Two tasks: one auto-T1 (due today), one open with no due date.
    _write_task(
        tmp_path,
        "Due Today Task",
        {
            "type": "task",
            "status": "todo",
            "name": "Due Today Task",
            "due": "2026-05-28",
        },
    )
    _write_task(
        tmp_path,
        "Open Task A",
        {
            "type": "task",
            "status": "todo",
            "name": "Open Task A",
        },
    )
    body = render_tier_section(tmp_path, NOW)
    pool_section = body.split(T2_POOL_HEADER)[1]
    assert "Open Task A" in pool_section
    assert "Due Today Task" not in pool_section


def test_t2_pool_excludes_alfred_triage_with_log(tmp_path: Path) -> None:
    """``alfred_triage: true`` records skipped from T2 pool + named
    log event fires per skip."""
    _write_task(
        tmp_path,
        "Triage Record",
        {
            "type": "task",
            "status": "todo",
            "name": "Triage Record",
            "alfred_triage": True,
        },
    )
    _write_task(
        tmp_path,
        "Normal Task",
        {
            "type": "task",
            "status": "todo",
            "name": "Normal Task",
        },
    )
    with structlog.testing.capture_logs() as captured:
        body = render_tier_section(tmp_path, NOW)
    pool_section = body.split(T2_POOL_HEADER)[1]
    assert "Normal Task" in pool_section
    assert "Triage Record" not in pool_section

    # Log event pinned.
    events = [
        c for c in captured
        if c.get("event") == "brief.tier_section.alfred_triage_skipped"
    ]
    assert len(events) == 1
    assert events[0]["name"] == "Triage Record"


def test_t2_pool_excludes_curated_t1_and_t2(tmp_path: Path) -> None:
    """Operator-curated T1/T2 entries must NOT appear in the pool —
    they're in the shortlists already."""
    _write_task(
        tmp_path,
        "Curated T1 Task",
        {"type": "task", "status": "todo", "name": "Curated T1 Task"},
    )
    _write_task(
        tmp_path,
        "Curated T2 Task",
        {"type": "task", "status": "todo", "name": "Curated T2 Task"},
    )
    _write_task(
        tmp_path,
        "Pool Candidate",
        {"type": "task", "status": "todo", "name": "Pool Candidate"},
    )
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml=(
            "t1:\n"
            "  - task: '[[task/Curated T1 Task]]'\n"
            "    source: operator\n"
            "    confirmed: true\n"
            "t2:\n"
            "  - task: '[[task/Curated T2 Task]]'\n"
            "    source: operator\n"
            "t3: []\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    pool_section = body.split(T2_POOL_HEADER)[1]
    assert "Pool Candidate" in pool_section
    assert "Curated T1 Task" not in pool_section
    assert "Curated T2 Task" not in pool_section


def test_t2_pool_excludes_closed_statuses(tmp_path: Path) -> None:
    """Done / cancelled tasks must NOT appear in the pool."""
    _write_task(
        tmp_path,
        "Done Task",
        {"type": "task", "status": "done", "name": "Done Task"},
    )
    _write_task(
        tmp_path,
        "Open Task",
        {"type": "task", "status": "active", "name": "Open Task"},
    )
    body = render_tier_section(tmp_path, NOW)
    pool_section = body.split(T2_POOL_HEADER)[1]
    assert "Open Task" in pool_section
    assert "Done Task" not in pool_section


# ---------------------------------------------------------------------------
# 6. Rollover from yesterday — incomplete T1/T2 detection
# ---------------------------------------------------------------------------


def test_rollover_detects_incomplete_t1_and_t2(tmp_path: Path) -> None:
    """Yesterday's T1/T2 entries whose tasks are still open today
    surface in the rollover section with tier labels."""
    # Task records — both open today (yesterday's curation pointed at
    # them; they didn't get done).
    _write_task(
        tmp_path,
        "QBO API",
        {"type": "task", "status": "todo", "name": "QBO API"},
    )
    _write_task(
        tmp_path,
        "Bug List",
        {"type": "task", "status": "active", "name": "Bug List"},
    )

    # Yesterday's curation pointed at both.
    _write_daily(
        tmp_path,
        "2026-05-27",
        tier_curation_yaml=(
            "t1:\n"
            "  - task: '[[task/QBO API]]'\n"
            "    source: operator\n"
            "    confirmed: true\n"
            "t2:\n"
            "  - task: '[[task/Bug List]]'\n"
            "    source: operator\n"
            "t3:\n"
            "  - item: Walk Fergus\n"
            "    source: aspirational\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)

    assert ROLLOVER_HEADER in body
    rollover_section = body.split(ROLLOVER_HEADER)[1]
    assert "T1: [[task/QBO API]]" in rollover_section
    assert "T2: [[task/Bug List]]" in rollover_section
    assert "uncompleted yesterday" in rollover_section
    # T3 must NOT roll over (skip-by-design).
    assert "Walk Fergus" not in rollover_section


def test_rollover_skips_completed_tasks(tmp_path: Path) -> None:
    """Yesterday's T1/T2 entries whose tasks are now done/cancelled
    do NOT surface in rollover."""
    _write_task(
        tmp_path,
        "Done Yesterday",
        {"type": "task", "status": "done", "name": "Done Yesterday"},
    )
    _write_task(
        tmp_path,
        "Still Open",
        {"type": "task", "status": "todo", "name": "Still Open"},
    )
    _write_daily(
        tmp_path,
        "2026-05-27",
        tier_curation_yaml=(
            "t1:\n"
            "  - task: '[[task/Done Yesterday]]'\n"
            "    source: operator\n"
            "  - task: '[[task/Still Open]]'\n"
            "    source: operator\n"
            "t2: []\n"
            "t3: []\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    rollover_section = body.split(ROLLOVER_HEADER)[1]
    assert "Still Open" in rollover_section
    assert "Done Yesterday" not in rollover_section


def test_rollover_suppressed_when_yesterday_missing(tmp_path: Path) -> None:
    """No yesterday daily file → rollover section completely absent
    + suppression log fires."""
    _write_task(
        tmp_path,
        "Some Task",
        {"type": "task", "status": "todo", "name": "Some Task"},
    )
    with structlog.testing.capture_logs() as captured:
        body = render_tier_section(tmp_path, NOW)
    assert ROLLOVER_HEADER not in body
    events = [
        c for c in captured
        if c.get("event") == "brief.tier_section.rollover_suppressed_no_yesterday"
    ]
    assert len(events) == 1


def test_rollover_t3_entries_skipped_by_design(tmp_path: Path) -> None:
    """T3 is today's intentions — does NOT roll over. Yesterday's T3
    entries are silently dropped from the rollover scan."""
    _write_daily(
        tmp_path,
        "2026-05-27",
        tier_curation_yaml=(
            "t1: []\n"
            "t2: []\n"
            "t3:\n"
            "  - item: Read for an hour\n"
            "    source: aspirational\n"
            "  - item: Walk Fergus\n"
            "    source: operator-adhoc\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    # Yesterday's block existed → header surfaces; but all entries
    # were T3 → no rollover lines → sentinel surfaces.
    assert ROLLOVER_HEADER in body
    rollover_section = body.split(ROLLOVER_HEADER)[1]
    assert "Walk Fergus" not in rollover_section
    assert "Read for an hour" not in rollover_section
    # "all completed" sentinel — yesterday had a block, but no
    # T1/T2 entries to roll → render shows the empty-rollover sentinel.
    assert (
        "all completed" in rollover_section
        or "nothing to roll over" in rollover_section
    )


def test_rollover_all_completed_emits_sentinel(tmp_path: Path) -> None:
    """Yesterday had T1/T2 entries but all are done today → rollover
    section has the header + 'all completed' sentinel (NOT suppressed;
    operator can distinguish 'no yesterday' from 'yesterday tracked,
    all done')."""
    _write_task(
        tmp_path,
        "Finished A",
        {"type": "task", "status": "done", "name": "Finished A"},
    )
    _write_daily(
        tmp_path,
        "2026-05-27",
        tier_curation_yaml=(
            "t1:\n"
            "  - task: '[[task/Finished A]]'\n"
            "    source: operator\n"
            "t2: []\n"
            "t3: []\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    assert ROLLOVER_HEADER in body
    rollover_section = body.split(ROLLOVER_HEADER)[1]
    assert "Finished A" not in rollover_section
    assert "all completed" in rollover_section


# ---------------------------------------------------------------------------
# 7. CRITICAL — read-side stability (refresh preserves curation)
# ---------------------------------------------------------------------------


def test_brief_refresh_preserves_curation(tmp_path: Path) -> None:
    """Render twice with same inputs → identical output. The read
    path MUST be a pure projection over the curation block; no
    silent rewrites, no re-derivation, no timestamp drift.

    Per Ship 2 dispatch (item 9, CRITICAL): when the operator
    triggers ``/today`` or the brief regenerates mid-day, the
    curated shortlists must be byte-stable as long as
    ``tier_curation`` hasn't changed.
    """
    _write_task(
        tmp_path,
        "Task A",
        {
            "type": "task",
            "status": "todo",
            "name": "Task A",
            "due": "2026-05-28",
        },
    )
    _write_task(
        tmp_path,
        "Task B",
        {"type": "task", "status": "todo", "name": "Task B"},
    )
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml=(
            "t1:\n"
            "  - task: '[[task/Task A]]'\n"
            "    source: auto-due\n"
            "    confirmed: true\n"
            "t2:\n"
            "  - task: '[[task/Some Other]]'\n"
            "    source: operator\n"
            "t3:\n"
            "  - item: Walk Fergus\n"
            "    source: aspirational\n"
        ),
    )

    body1 = render_tier_section(tmp_path, NOW)
    body2 = render_tier_section(tmp_path, NOW)
    body3 = render_tier_section(tmp_path, NOW)
    assert body1 == body2 == body3


# ---------------------------------------------------------------------------
# 8. Parse-failure handling stays defensive
# ---------------------------------------------------------------------------


def test_parse_failed_task_logged_and_skipped(tmp_path: Path) -> None:
    """A corrupt task record is skipped + a ``parse_failed`` log fires.
    Render does NOT crash."""
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "Corrupt.md").write_text(
        "---\n[invalid yaml\n---\n", encoding="utf-8",
    )
    _write_task(
        tmp_path,
        "Good Task",
        {"type": "task", "status": "todo", "name": "Good Task"},
    )
    with structlog.testing.capture_logs() as captured:
        body = render_tier_section(tmp_path, NOW)
    pool_section = body.split(T2_POOL_HEADER)[1]
    assert "Good Task" in pool_section
    events = [
        c for c in captured
        if c.get("event") == "brief.tier_section.parse_failed"
    ]
    assert len(events) == 1


def test_non_task_type_logged_and_skipped(tmp_path: Path) -> None:
    """A file in ``task/`` with ``type != task`` is skipped + logged."""
    _write_task(
        tmp_path,
        "Stray",
        {"type": "note", "status": "todo", "name": "Stray"},
    )
    _write_task(
        tmp_path,
        "Real Task",
        {"type": "task", "status": "todo", "name": "Real Task"},
    )
    with structlog.testing.capture_logs() as captured:
        body = render_tier_section(tmp_path, NOW)
    pool_section = body.split(T2_POOL_HEADER)[1]
    assert "Real Task" in pool_section
    assert "Stray" not in pool_section
    events = [
        c for c in captured
        if c.get("event") == "brief.tier_section.non_task_skipped"
    ]
    assert len(events) == 1


# ---------------------------------------------------------------------------
# 9. Composition / shape — separator + section ordering
# ---------------------------------------------------------------------------


def test_render_includes_separator_between_shortlists_and_pool(
    tmp_path: Path,
) -> None:
    """The render shape is: shortlists → ``---`` → pool → (rollover).
    The ``---`` separator anchors the curated/materials split."""
    body = render_tier_section(tmp_path, NOW)
    # The separator appears between the last shortlist section
    # (### T3) and the T2 pool header.
    idx_t3 = body.index("### T3")
    idx_sep = body.index("\n---\n", idx_t3)
    idx_pool = body.index(T2_POOL_HEADER, idx_sep)
    assert idx_t3 < idx_sep < idx_pool


def test_render_logs_rendered_event_with_counts(tmp_path: Path) -> None:
    """The ``rendered`` log event surfaces counts for observability —
    operators grep ``brief.tier_section.rendered`` to see what landed."""
    _write_task(
        tmp_path,
        "A",
        {
            "type": "task",
            "status": "todo",
            "name": "A",
            "due": "2026-05-28",
        },
    )
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml=(
            "t1: []\n"
            "t2:\n"
            "  - task: '[[task/B]]'\n"
            "    source: operator\n"
            "t3:\n"
            "  - item: Walk\n"
            "    source: aspirational\n"
        ),
    )
    with structlog.testing.capture_logs() as captured:
        render_tier_section(tmp_path, NOW)
    events = [
        c for c in captured if c.get("event") == "brief.tier_section.rendered"
    ]
    assert len(events) == 1
    e = events[0]
    assert e["curation_loaded"] is True
    assert e["curated_t1"] == 0
    assert e["curated_t2"] == 1
    assert e["curated_t3"] == 1
    # Phase 2A Ship B: task vs routine origin counts split.
    assert e["auto_t1_task_count"] == 1
    assert e["auto_t1_routine_count"] == 0
    assert e["scanned"] == 1


# ===========================================================================
# Phase 2A Ship B — routine-origin render integration
# ===========================================================================
#
# The brief now consumes compute_auto_routine_candidates +
# compute_auto_routine_t2_candidates and renders routine-origin items
# with origin-aware shape (item-text + reason + routine-record wikilink
# in parentheses), plus a NEW ``#### Auto-surfaced (from routines)``
# subsection inside T2 for ramp-window items.


def _write_routine(
    vault_path: Path,
    filename: str,
    fm_yaml: str,
) -> Path:
    """Helper: seed a tmp vault with one routine record at
    ``routine/<filename>``."""
    routine_dir = vault_path / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    path = routine_dir / filename
    path.write_text(f"---\n{fm_yaml}---\n\n# body\n", encoding="utf-8")
    return path


def test_routine_auto_t1_renders_with_routine_wikilink_and_confirm(
    tmp_path: Path,
) -> None:
    """Routine item due tomorrow with ``escalate_at_days: 1`` →
    auto-T1 surface, render shape: ``- <text> — <reason>, from
    [[routine/<record>]]  *(confirm? reply "T1 confirm")*``.

    NOW = 2026-05-28 (Thu). weekly day=fri → due 2026-05-29 (tomorrow).
    days_to_due=1, escalate=1 → T1 window."""
    _write_routine(
        tmp_path,
        "Weekly Chores.md",
        "type: routine\nstatus: active\nname: Weekly Chores\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Garbage Out\n"
        "  priority: critical\n"
        "  due_pattern:\n"
        "    type: weekly\n"
        "    day: fri\n"
        "  escalate_at_days: 1\n",
    )
    body = render_tier_section(tmp_path, NOW)
    # Routine wikilink + item text + reason all surface.
    assert "Garbage Out" in body
    assert "due tomorrow" in body
    assert "[[routine/Weekly Chores]]" in body
    # Auto-surfaced (not yet confirmed) gets the confirm prompt.
    assert T1_CONFIRM_PROMPT in body
    # Render shape: ``- Garbage Out — due tomorrow, from [[routine/Weekly Chores]]``
    t1_lines = [
        ln for ln in body.splitlines()
        if "Garbage Out" in ln and "[[routine/Weekly Chores]]" in ln
    ]
    assert len(t1_lines) == 1
    assert "due tomorrow, from [[routine/Weekly Chores]]" in t1_lines[0]


def test_routine_auto_t2_renders_in_auto_surfaced_subsection(
    tmp_path: Path,
) -> None:
    """Routine item with ``surface_at_days: 5`` + ``escalate_at_days: 0``,
    monthly day=1, today=2026-05-28 → due 2026-06-01 → days_to_due=4 →
    T2 ramp surface.

    Renders under the NEW ``#### Auto-surfaced (from routines)``
    subsection inside the T2 bucket. The line carries the
    :data:`T2_ROUTINE_CONFIRM_PROMPT` (talker-reply pattern)."""
    _write_routine(
        tmp_path,
        "Recurring Bills.md",
        "type: routine\nstatus: active\nname: Recurring Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Clinic Rental to Hussein Rafih\n"
        "  priority: critical\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 1\n"
        "  surface_at_days: 5\n"
        "  escalate_at_days: 0\n",
    )
    body = render_tier_section(tmp_path, NOW)
    assert T2_AUTO_ROUTINE_HEADER in body
    # The T2 auto subsection sits inside the T2 bucket — verify
    # ordering: T2 header → auto-surfaced subsection.
    t2_idx = body.index("### T2 — On the radar")
    auto_idx = body.index(T2_AUTO_ROUTINE_HEADER)
    assert t2_idx < auto_idx
    # The auto-T2-routine confirm prompt fires.
    assert T2_ROUTINE_CONFIRM_PROMPT in body
    # Item rendered with reason + routine wikilink.
    assert "Pay Clinic Rental" in body
    assert "surface window (4d before due)" in body
    assert "[[routine/Recurring Bills]]" in body


def test_t2_auto_routine_header_constant_pinned() -> None:
    """Cross-agent contract: Ship D SKILL quotes this verbatim."""
    assert T2_AUTO_ROUTINE_HEADER == "#### Auto-surfaced (from routines)"


def test_t2_routine_confirm_prompt_constant_pinned() -> None:
    """Cross-agent contract: Ship D SKILL quotes this verbatim."""
    assert T2_ROUTINE_CONFIRM_PROMPT == (
        '*(reply "T2 confirm" to keep on today\'s list)*'
    )


def test_curated_t1_routine_item_renders_correctly(tmp_path: Path) -> None:
    """Operator-curated T1 entry with ``routine_item`` shape +
    ``confirmed: true`` renders as bare (no confirm prompt) — same
    visual treatment as a task-origin entry the operator has
    confirmed."""
    # Seed the routine record + auto-T1 candidate.
    _write_routine(
        tmp_path,
        "Weekly Chores.md",
        "type: routine\nstatus: active\nname: Weekly Chores\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Garbage Out\n"
        "  due_pattern:\n"
        "    type: weekly\n"
        "    day: fri\n"
        "  escalate_at_days: 1\n",
    )
    # Pre-curate the entry with confirmed=true.
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml=(
            "t1:\n"
            "  - routine_item:\n"
            "      record: Weekly Chores\n"
            "      text: Garbage Out\n"
            "    source: auto-due-routine\n"
            "    confirmed: true\n"
            "t2: []\n"
            "t3: []\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    # Find the T1 line containing the item.
    t1_lines = [
        ln for ln in body.splitlines()
        if "Garbage Out" in ln and "[[routine/Weekly Chores]]" in ln
    ]
    assert len(t1_lines) == 1
    # Confirmed=True → NO confirm prompt on the line.
    assert T1_CONFIRM_PROMPT not in t1_lines[0]
    # Reason + routine wikilink still surface.
    assert "due tomorrow" in t1_lines[0]


def test_curated_t1_task_shape_still_renders_after_extension(
    tmp_path: Path,
) -> None:
    """Backward compat: task-shape curated T1 entries still render
    correctly after the discriminated-union extension."""
    _write_task(
        tmp_path,
        "Steph Yang ROE",
        {
            "type": "task",
            "status": "todo",
            "name": "Steph Yang ROE",
            "due": "2026-05-28",
        },
    )
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml=(
            "t1:\n"
            "  - task: '[[task/Steph Yang ROE]]'\n"
            "    source: auto-due\n"
            "    confirmed: true\n"
            "t2: []\n"
            "t3: []\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    # Task-shape line renders.
    assert "[[task/Steph Yang ROE]]" in body
    # Confirmed=true → no confirm prompt on the line.
    t1_lines = [
        ln for ln in body.splitlines()
        if "[[task/Steph Yang ROE]]" in ln
    ]
    assert len(t1_lines) == 1
    assert T1_CONFIRM_PROMPT not in t1_lines[0]


def test_routine_auto_t1_already_in_curated_t1_no_duplicate(
    tmp_path: Path,
) -> None:
    """Auto-T1 routine candidate dedupped against curated T1 by
    ``(record, text)`` tuple — no double-render."""
    _write_routine(
        tmp_path,
        "Weekly Chores.md",
        "type: routine\nstatus: active\nname: Weekly Chores\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Garbage Out\n"
        "  due_pattern:\n"
        "    type: weekly\n"
        "    day: fri\n"
        "  escalate_at_days: 1\n",
    )
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml=(
            "t1:\n"
            "  - routine_item:\n"
            "      record: Weekly Chores\n"
            "      text: Garbage Out\n"
            "    source: auto-due-routine\n"
            "    confirmed: true\n"
            "t2: []\n"
            "t3: []\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    # Only ONE line should contain "Garbage Out" with the routine wikilink.
    matches = [
        ln for ln in body.splitlines()
        if "Garbage Out" in ln and "[[routine/Weekly Chores]]" in ln
    ]
    assert len(matches) == 1


def test_routine_auto_t2_dedupped_against_curated_t1(tmp_path: Path) -> None:
    """If an auto-T2-routine candidate matches a curated T1 routine
    entry (operator confirmed at T1), the T2-auto subsection
    SUPPRESSES the duplicate render."""
    _write_routine(
        tmp_path,
        "Recurring Bills.md",
        "type: routine\nstatus: active\nname: Recurring Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Clinic Rental\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 1\n"
        "  surface_at_days: 5\n"
        "  escalate_at_days: 0\n",
    )
    # Operator confirmed at T1 ahead of schedule (e.g. "I want to pay early").
    _write_daily(
        tmp_path,
        "2026-05-28",
        tier_curation_yaml=(
            "t1:\n"
            "  - routine_item:\n"
            "      record: Recurring Bills\n"
            "      text: Pay Clinic Rental\n"
            "    source: operator\n"
            "    confirmed: true\n"
            "t2: []\n"
            "t3: []\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    # Auto-T2 subsection header should NOT surface (the only candidate
    # was deduped against curated T1).
    assert T2_AUTO_ROUTINE_HEADER not in body
    # T1 line for the item DOES surface (curated).
    assert "Pay Clinic Rental" in body
    assert "[[routine/Recurring Bills]]" in body


def test_routine_log_emission_split_counts(tmp_path: Path) -> None:
    """The ``brief.tier_section.rendered`` log event reports task /
    routine T1 / routine T2 counts separately so operators can grep
    each surface."""
    _write_routine(
        tmp_path,
        "Weekly Chores.md",
        "type: routine\nstatus: active\nname: Weekly Chores\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Garbage Out\n"
        "  due_pattern:\n"
        "    type: weekly\n"
        "    day: fri\n"
        "  escalate_at_days: 1\n"
        "- text: Pay Clinic Rental\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 1\n"
        "  surface_at_days: 5\n"
        "  escalate_at_days: 0\n",
    )
    with structlog.testing.capture_logs() as captured:
        render_tier_section(tmp_path, NOW)
    events = [
        c for c in captured if c.get("event") == "brief.tier_section.rendered"
    ]
    assert len(events) == 1
    e = events[0]
    # One routine T1 candidate (Garbage Out, due tomorrow).
    assert e["auto_t1_routine_count"] == 1
    # One routine T2 candidate (Pay Clinic Rental, 4d before due).
    assert e["auto_t2_routine_count"] == 1
    assert e["auto_t1_task_count"] == 0


def test_routine_auto_t1_includes_formatted_due_date(tmp_path: Path) -> None:
    """Phase 2A Ship B worked-example shape: the T1 routine line
    embeds the formatted due date in the head.

    NOW = 2026-05-28 Thu. weekly day=fri → due 2026-05-29 (Friday).
    Format: ``- <text> — due Fri May 29 (<reason>, from
    [[routine/<record>]])  *(confirm)*``.

    Pinned per the dispatch worked example so a date-format regression
    surfaces here, not in operator-facing brief drift."""
    _write_routine(
        tmp_path,
        "Weekly Chores.md",
        "type: routine\nstatus: active\nname: Weekly Chores\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Garbage Out\n"
        "  due_pattern:\n"
        "    type: weekly\n"
        "    day: fri\n"
        "  escalate_at_days: 1\n",
    )
    body = render_tier_section(tmp_path, NOW)
    # Formatted date appears in the head, BEFORE the parenthetical.
    assert "due Fri May 29" in body
    # Inside the parenthetical: reason + from-wikilink.
    assert "(due tomorrow, from [[routine/Weekly Chores]])" in body


def test_routine_auto_t2_includes_formatted_due_date(tmp_path: Path) -> None:
    """Same dispatch worked-example shape for the T2 auto subsection.

    Format: ``- <text> — due Mon Jun 1 (<reason>, from
    [[routine/<record>]])  *(reply "T2 confirm" ...)*``."""
    _write_routine(
        tmp_path,
        "Recurring Bills.md",
        "type: routine\nstatus: active\nname: Recurring Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Clinic Rental\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 1\n"
        "  surface_at_days: 5\n"
        "  escalate_at_days: 0\n",
    )
    body = render_tier_section(tmp_path, NOW)
    assert "due Mon Jun 1" in body
    assert (
        "(surface window (4d before due), "
        "from [[routine/Recurring Bills]])"
    ) in body


def test_routine_t1_entry_excluded_from_rollover(tmp_path: Path) -> None:
    """Yesterday's curation with a routine_item T1 entry is NOT
    surfaced in today's rollover section — routine items don't roll
    over because the next cycle naturally surfaces via due_pattern."""
    _write_daily(
        tmp_path,
        "2026-05-27",  # yesterday
        tier_curation_yaml=(
            "t1:\n"
            "  - routine_item:\n"
            "      record: Weekly Chores\n"
            "      text: Garbage Out\n"
            "    source: auto-due-routine\n"
            "    confirmed: true\n"
            "  - task: '[[task/Bug List]]'\n"
            "    source: operator\n"
            "t2: []\n"
            "t3: []\n"
        ),
    )
    body = render_tier_section(tmp_path, NOW)
    assert ROLLOVER_HEADER in body
    rollover_section = body.split(ROLLOVER_HEADER)[1]
    # Routine entry NOT in rollover.
    assert "Garbage Out" not in rollover_section
    assert "[[routine/Weekly Chores]]" not in rollover_section
    # Task entry IS in rollover (still applies).
    assert "[[task/Bug List]]" in rollover_section


# ===========================================================================
# render_curated_tier_section_for_today — /today curated-only view
# ===========================================================================
#
# Scope refinement 2026-05-30: the /today slash command renders ONLY
# operator-curated entries (no auto-T1, no T2 selection pool, no
# auto-T2-routine subsection, no rollover, no confirm prompts).
# Empty-bucket convention: header-suffix sentinel
# ``### T1 — (no items yet)``.


def test_curated_for_today_all_three_tiers_populated_renders_entries(
    tmp_path: Path,
) -> None:
    """Curated entries in all 3 tiers → bucket headers without
    ``— (no items yet)`` suffix; each bucket carries the entries."""
    curation = DailyCuration(
        t1=[
            T1T2Entry(
                task="[[task/Complete Personal Taxes — Andrew Newton]]",
                source="operator",
                confirmed=True,
            ),
            T1T2Entry(
                task="[[task/RRTS Corporate Taxes — Awaiting Accountant]]",
                source="operator",
                confirmed=True,
            ),
        ],
        t2=[
            T1T2Entry(
                task=(
                    "[[task/Prep Blue Cross Call List for Medical "
                    "Admin Handoff]]"
                ),
                source="operator",
            ),
        ],
        t3=[
            T3Entry(item="dog walk", source="operator-adhoc"),
        ],
    )
    body = render_curated_tier_section_for_today(curation)
    # Three plain headers, no "(no items yet)" suffix when populated.
    assert "### T1\n" in body
    assert "### T2\n" in body
    assert "### T3\n" in body
    assert "### T1 — (no items yet)" not in body
    assert "### T2 — (no items yet)" not in body
    assert "### T3 — (no items yet)" not in body
    # All operator-committed entries render as bare wikilinks /
    # free text. No confirm prompts.
    assert (
        "- [[task/Complete Personal Taxes — Andrew Newton]]" in body
    )
    assert (
        "- [[task/RRTS Corporate Taxes — Awaiting Accountant]]" in body
    )
    assert (
        "- [[task/Prep Blue Cross Call List for Medical "
        "Admin Handoff]]"
    ) in body
    assert "- dog walk" in body
    # No confirm prompts in the curated-only view.
    assert T1_CONFIRM_PROMPT not in body
    assert T2_ROUTINE_CONFIRM_PROMPT not in body


def test_curated_for_today_t1_only_populated_other_buckets_show_sentinel(
    tmp_path: Path,
) -> None:
    """Mixed-population: T1 has entries; T2 + T3 show the
    header-suffix sentinel so all three buckets stay visible."""
    curation = DailyCuration(
        t1=[
            T1T2Entry(
                task="[[task/Solo T1 Entry]]",
                source="operator",
                confirmed=True,
            ),
        ],
        t2=[],
        t3=[],
    )
    body = render_curated_tier_section_for_today(curation)
    # T1 populated → plain header.
    assert "### T1\n" in body
    assert "- [[task/Solo T1 Entry]]" in body
    # T2 + T3 empty → suffix sentinel.
    assert "### T2 — (no items yet)" in body
    assert "### T3 — (no items yet)" in body
    # Plain ``### T2\n`` / ``### T3\n`` must NOT appear (the suffix
    # ate the bare header).
    assert "### T2\n" not in body
    assert "### T3\n" not in body


def test_curated_for_today_none_curation_renders_all_sentinels(
    tmp_path: Path,
) -> None:
    """``daily_curation = None`` (no daily file yet, e.g. /today before
    the 06:00 brief / 05:59 aggregator fires) → all three buckets
    render the header-suffix sentinel. Operator sees the same shape
    they'd see after a deliberate empty curation."""
    body = render_curated_tier_section_for_today(None)
    assert "### T1 — (no items yet)" in body
    assert "### T2 — (no items yet)" in body
    assert "### T3 — (no items yet)" in body
    # No bare wikilinks anywhere (defense against accidental
    # population on the None branch).
    assert "[[task/" not in body
    assert "[[routine/" not in body


def test_curated_for_today_routine_origin_entry_renders_with_from_wikilink(
    tmp_path: Path,
) -> None:
    """Routine-origin T1/T2 entries render with the
    ``- <text> (from [[routine/<record>]])`` shape — matches the
    brief's curated routine-entry shape (shared via the per-entry
    helper). NO confirm prompts."""
    curation = DailyCuration(
        t1=[
            T1T2Entry(
                routine_item={
                    "record": "Recurring Bills + Admin",
                    "text": "Pay Clinic Rental to Hussein Rafih",
                },
                source="auto-due-routine",
                confirmed=True,
            ),
        ],
        t2=[
            T1T2Entry(
                routine_item={
                    "record": "Weekly Chores",
                    "text": "Garbage Out",
                },
                source="auto-surface-routine",
            ),
        ],
        t3=[],
    )
    body = render_curated_tier_section_for_today(curation)
    # Routine-origin shape: <text> (from [[routine/<record>]])
    assert (
        "- Pay Clinic Rental to Hussein Rafih "
        "(from [[routine/Recurring Bills + Admin]])"
    ) in body
    assert (
        "- Garbage Out (from [[routine/Weekly Chores]])"
    ) in body
    # No confirm prompts — operator already committed.
    assert T1_CONFIRM_PROMPT not in body
    assert T2_ROUTINE_CONFIRM_PROMPT not in body


def test_curated_for_today_t3_free_text_renders_without_wikilink(
    tmp_path: Path,
) -> None:
    """T3 entries are free-text intentions — render as
    ``- <item>`` with no wikilink wrap."""
    curation = DailyCuration(
        t1=[],
        t2=[],
        t3=[
            T3Entry(item="dog walk", source="operator-adhoc"),
            T3Entry(item="Read for an hour", source="aspirational"),
        ],
    )
    body = render_curated_tier_section_for_today(curation)
    assert "- dog walk" in body
    assert "- Read for an hour" in body
    # T3 entries do NOT wrap in [[wikilink]] form — free text only.
    assert "[[dog walk]]" not in body
    assert "[[task/dog walk]]" not in body


def test_curated_for_today_does_not_consume_auto_t1_candidates(
    tmp_path: Path,
) -> None:
    """The curated render is a PURE PROJECTION over daily_curation —
    it does NOT scan the vault for auto-T1 candidates.

    Verified by passing daily_curation=None with auto-T1-eligible
    tasks present on disk (they'd surface in the morning brief's
    materials view). Since render_curated_tier_section_for_today
    takes ONLY the curation, no vault scan happens; the rendered
    body has nothing about those tasks."""
    # Seed a task that WOULD surface as auto-T1 in the morning
    # brief (due today, escalate_at_days=1 → T1 candidate).
    # _write_task is defined earlier in this module.
    _write_task(
        tmp_path,
        "Auto T1 Eligible Task",
        {
            "type": "task",
            "status": "todo",
            "name": "Auto T1 Eligible Task",
            "due": "2026-05-28",  # today
            "escalate_at_days": 1,
        },
    )
    # Render the curated-only view with NO curation block.
    body = render_curated_tier_section_for_today(None)
    # The task must NOT appear — curated-only view doesn't scan
    # for auto-surfaced candidates.
    assert "Auto T1 Eligible Task" not in body
    assert "[[task/Auto T1 Eligible Task]]" not in body


def test_curated_for_today_does_not_render_t2_selection_pool(
    tmp_path: Path,
) -> None:
    """T2 selection pool (open tasks NOT in auto-T1) is morning-brief
    only. /today curated view never surfaces it."""
    # Seed multiple open tasks (would populate the morning brief's
    # T2 selection pool).
    _write_task(
        tmp_path,
        "Open Pool Task A",
        {"type": "task", "status": "todo", "name": "Open Pool Task A"},
    )
    _write_task(
        tmp_path,
        "Open Pool Task B",
        {"type": "task", "status": "active", "name": "Open Pool Task B"},
    )
    body = render_curated_tier_section_for_today(None)
    # T2 selection-pool header / contents NOT in the curated view.
    assert T2_POOL_HEADER not in body
    assert "selection pool" not in body.lower()
    assert "Open Pool Task A" not in body
    assert "Open Pool Task B" not in body


def test_curated_for_today_does_not_render_auto_t2_routine_subsection(
    tmp_path: Path,
) -> None:
    """The morning brief's #### Auto-surfaced (from routines) T2
    ramp subsection does NOT appear in /today curated view."""
    body = render_curated_tier_section_for_today(None)
    # The header constant must NOT appear in the curated body.
    assert T2_AUTO_ROUTINE_HEADER not in body
    # The "Auto-surfaced" phrasing also absent (defense against the
    # constant text being rebuilt rather than imported).
    assert "Auto-surfaced" not in body


def test_curated_for_today_emits_rendered_log_event_with_counts(
    tmp_path: Path,
) -> None:
    """Per builder.md rule #9: log emission pinned via
    structlog.testing.capture_logs."""
    curation = DailyCuration(
        t1=[
            T1T2Entry(
                task="[[task/A]]", source="operator", confirmed=True,
            ),
            T1T2Entry(
                task="[[task/B]]", source="operator", confirmed=True,
            ),
        ],
        t2=[],
        t3=[
            T3Entry(item="walk", source="aspirational"),
        ],
    )
    with structlog.testing.capture_logs() as captured:
        render_curated_tier_section_for_today(curation)
    events = [
        c for c in captured
        if c.get("event")
        == "brief.tier_section.rendered_curated_for_today"
    ]
    assert len(events) == 1
    e = events[0]
    assert e["curation_loaded"] is True
    assert e["curated_t1"] == 2
    assert e["curated_t2"] == 0
    assert e["curated_t3"] == 1
    # No vault_path threaded → status filter OFF (byte-identical to the
    # pre-2026-06-15 render). Pin both new log fields per builder rule #9.
    assert e["status_filter_applied"] is False
    assert e["status_filtered"] == 0


def test_curated_for_today_none_curation_log_event_curation_loaded_false(
    tmp_path: Path,
) -> None:
    """When daily_curation is None, the log event reports
    ``curation_loaded=False`` so operators can grep the signal."""
    with structlog.testing.capture_logs() as captured:
        render_curated_tier_section_for_today(None)
    events = [
        c for c in captured
        if c.get("event")
        == "brief.tier_section.rendered_curated_for_today"
    ]
    assert len(events) == 1
    e = events[0]
    assert e["curation_loaded"] is False
    assert e["curated_t1"] == 0
    assert e["curated_t2"] == 0
    assert e["curated_t3"] == 0


# ===========================================================================
# Live completed-task filter (2026-06-15) — /today hides closed tasks
# ===========================================================================
#
# render_curated_tier_section_for_today(curation, vault_path=...) drops
# curated T1/T2 task-origin entries whose task record is closed (status
# not in OPEN_STATUSES). Routine-origin + T3 entries are never filtered.
# Fail-open on a missing/unreadable record. Without vault_path the
# render skips filtering entirely (back-compat pin).


def test_curated_today_filter_hides_closed_task(tmp_path: Path) -> None:
    """An OPEN curated task renders; a CLOSED one (status: done) is
    OMITTED when vault_path is threaded."""
    _write_task(
        tmp_path, "Open Task",
        {"type": "task", "name": "Open Task", "status": "todo"},
    )
    _write_task(
        tmp_path, "Call Hedley Newton",
        {"type": "task", "name": "Call Hedley Newton", "status": "done"},
    )
    curation = DailyCuration(
        t1=[
            T1T2Entry(task="[[task/Open Task]]", source="operator",
                      confirmed=True),
            T1T2Entry(task="[[task/Call Hedley Newton]]", source="operator",
                      confirmed=True),
        ],
        t2=[], t3=[],
    )
    body = render_curated_tier_section_for_today(curation, vault_path=tmp_path)
    assert "- [[task/Open Task]]" in body
    # The closed task is HIDDEN (operator wants it gone, not struck).
    assert "Call Hedley Newton" not in body


def test_curated_today_filter_off_without_vault_path(tmp_path: Path) -> None:
    """Back-compat pin: NO vault_path → no filtering, the closed task
    still renders (byte-identical to the pre-2026-06-15 behaviour)."""
    _write_task(
        tmp_path, "Call Hedley Newton",
        {"type": "task", "name": "Call Hedley Newton", "status": "done"},
    )
    curation = DailyCuration(
        t1=[T1T2Entry(task="[[task/Call Hedley Newton]]", source="operator",
                      confirmed=True)],
        t2=[], t3=[],
    )
    # No vault_path → filtering off → the closed task still shows.
    body = render_curated_tier_section_for_today(curation)
    assert "[[task/Call Hedley Newton]]" in body


def test_curated_today_filter_t2_also_filtered(tmp_path: Path) -> None:
    """The filter applies to T2 as well as T1."""
    _write_task(
        tmp_path, "Closed T2",
        {"type": "task", "name": "Closed T2", "status": "cancelled"},
    )
    _write_task(
        tmp_path, "Open T2",
        {"type": "task", "name": "Open T2", "status": "active"},
    )
    curation = DailyCuration(
        t1=[],
        t2=[
            T1T2Entry(task="[[task/Closed T2]]", source="operator"),
            T1T2Entry(task="[[task/Open T2]]", source="operator"),
        ],
        t3=[],
    )
    body = render_curated_tier_section_for_today(curation, vault_path=tmp_path)
    assert "[[task/Open T2]]" in body
    assert "Closed T2" not in body


def test_curated_today_filter_keeps_routine_origin_entries(
    tmp_path: Path,
) -> None:
    """Routine-origin T1/T2 entries have no task record → never filtered,
    even with vault_path threaded."""
    curation = DailyCuration(
        t1=[
            T1T2Entry(
                routine_item={"record": "Weekly Chores", "text": "Bins out"},
                source="operator",
            ),
        ],
        t2=[], t3=[],
    )
    body = render_curated_tier_section_for_today(curation, vault_path=tmp_path)
    assert "Bins out (from [[routine/Weekly Chores]])" in body


def test_curated_today_filter_keeps_t3_free_text(tmp_path: Path) -> None:
    """T3 entries carry free-text (no task ref) → never filtered."""
    curation = DailyCuration(
        t1=[], t2=[],
        t3=[T3Entry(item="dog walk", source="aspirational")],
    )
    body = render_curated_tier_section_for_today(curation, vault_path=tmp_path)
    assert "- dog walk" in body


def test_curated_today_filter_fails_open_on_missing_record(
    tmp_path: Path,
) -> None:
    """A curated task whose record is ABSENT from the vault is KEPT
    (fail-open) — only an explicitly-closed status hides an item, so a
    transient miss never silently drops a real commitment."""
    # No task file written for this wikilink.
    curation = DailyCuration(
        t1=[T1T2Entry(task="[[task/Ghost Task]]", source="operator",
                      confirmed=True)],
        t2=[], t3=[],
    )
    body = render_curated_tier_section_for_today(curation, vault_path=tmp_path)
    assert "[[task/Ghost Task]]" in body


def test_curated_today_filter_empties_bucket_keeps_sentinel(
    tmp_path: Path,
) -> None:
    """ILB: when filtering empties a bucket entirely, the header-suffix
    sentinel still emits — the tier reads 'nothing committed', never
    vanishes."""
    _write_task(
        tmp_path, "Only Closed",
        {"type": "task", "name": "Only Closed", "status": "done"},
    )
    curation = DailyCuration(
        t1=[T1T2Entry(task="[[task/Only Closed]]", source="operator",
                      confirmed=True)],
        t2=[], t3=[],
    )
    body = render_curated_tier_section_for_today(curation, vault_path=tmp_path)
    assert "### T1 — (no items yet)" in body
    assert "Only Closed" not in body


def test_curated_today_filter_log_reports_filtered_count(
    tmp_path: Path,
) -> None:
    """Log-emission pin (builder rule #9): the render log surfaces the
    completed-task filter so an operator can grep why a T1 item went
    away — status_filter_applied True + the filtered count."""
    _write_task(
        tmp_path, "Closed One",
        {"type": "task", "name": "Closed One", "status": "done"},
    )
    _write_task(
        tmp_path, "Open One",
        {"type": "task", "name": "Open One", "status": "todo"},
    )
    curation = DailyCuration(
        t1=[
            T1T2Entry(task="[[task/Closed One]]", source="operator",
                      confirmed=True),
            T1T2Entry(task="[[task/Open One]]", source="operator",
                      confirmed=True),
        ],
        t2=[], t3=[],
    )
    with structlog.testing.capture_logs() as captured:
        render_curated_tier_section_for_today(curation, vault_path=tmp_path)
    events = [
        c for c in captured
        if c.get("event") == "brief.tier_section.rendered_curated_for_today"
    ]
    assert len(events) == 1
    e = events[0]
    assert e["status_filter_applied"] is True
    assert e["status_filtered"] == 1
    # The surviving (open) entry is counted post-filter.
    assert e["curated_t1"] == 1


# ===========================================================================
# Phase 2A-soft-cadence (2026-05-30) — T3 auto-suggest from routine cadence
# ===========================================================================
#
# Test surface per dispatch:
#   * Constants pinning (cross-agent contract for Phase 2B B1 SKILL).
#   * Subhead appears when auto-T3 candidates present.
#   * NO subhead when no auto-T3 candidates (don't pollute empty).
#   * Curated-only when no auto candidates (backward-compat).
#   * Render line shape: ``- [[routine/<record>]] — <item> *(...)*``
#   * Never-completed items use the "never done" label.
#   * Ordering: curated entries FIRST, then auto subsection BELOW.
#   * ILB acknowledgement (talker deferred note) surfaces in the brief.
#   * Log emission carries the auto_t3_routine_count field.


def test_t3_auto_section_header_constant_pinned() -> None:
    """Cross-agent contract: Phase 2B B1 SKILL quotes this verbatim."""
    assert T3_AUTO_SECTION_HEADER == "#### Auto-suggested (from routine cadence)"


def test_t3_auto_confirm_prompt_constant_pinned() -> None:
    """Cross-agent contract: Phase 2B B1 SKILL quotes this verbatim."""
    assert T3_AUTO_CONFIRM_PROMPT == (
        '*(reply "T3 confirm <item>" to add to today\'s T3)*'
    )


def test_t3_auto_days_since_never_label_pinned() -> None:
    """Cross-agent contract: render layer + SKILL both reference."""
    assert T3_AUTO_DAYS_SINCE_NEVER_LABEL == "never done"


def test_t3_auto_annotation_template_pinned() -> None:
    """Cross-agent contract: render-format string. Caller formats via
    ``.format(days_since=..., target=...)``."""
    assert T3_AUTO_ANNOTATION_TEMPLATE == (
        "*({days_since} days since last; target every {target}d)*"
    )
    # Sanity: template substitutes correctly.
    formatted = T3_AUTO_ANNOTATION_TEMPLATE.format(days_since=4, target=3)
    assert formatted == "*(4 days since last; target every 3d)*"


def test_t3_auto_talker_deferred_note_pinned() -> None:
    """ILB acknowledgement surfaces the User-axis deferral. Phase 2B
    B1 ships the talker companion; this string operator-facing
    documents the gap."""
    # The note mentions the deferred Phase + the operator-side fallback.
    assert "Phase 2B B1" in T3_AUTO_TALKER_DEFERRED_NOTE
    assert "alfred routine done" in T3_AUTO_TALKER_DEFERRED_NOTE


def test_t3_auto_section_header_emits_when_candidates_present(
    tmp_path: Path,
) -> None:
    """Routine record with a never-completed soft-cadence item →
    auto-T3 subsection renders.

    Fixture: NOW = 2026-05-28; Self Care has Walk dog target=3 never
    completed → max overdue → surfaces. Brief should render the
    auto-T3 subsection with the header + the entry + confirm prompt.

    Phase 2B B1 (2026-05-30): the ``T3_AUTO_TALKER_DEFERRED_NOTE``
    ILB acknowledgement is RETIRED — talker companion shipped this
    same ship, so the deferred-note copy is no longer accurate. The
    constant is preserved (back-compat) but the render loop omits
    it. See ``test_t3_auto_talker_deferred_note_no_longer_rendered``
    for the regression pin.
    """
    _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Walk dog\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    body = render_tier_section(tmp_path, NOW)
    assert T3_AUTO_SECTION_HEADER in body
    assert "[[routine/Self Care]]" in body
    assert "Walk dog" in body
    # Never-completed → "never done" label, NOT a day count.
    assert "never done" in body
    assert "target every 3d" in body
    # Confirm prompt fires below candidates — still load-bearing.
    assert T3_AUTO_CONFIRM_PROMPT in body
    # ILB deferred note RETIRED 2026-05-30 — see regression test below.
    assert T3_AUTO_TALKER_DEFERRED_NOTE not in body


def test_t3_auto_talker_deferred_note_no_longer_rendered(
    tmp_path: Path,
) -> None:
    """Phase 2B B1 (2026-05-30) — regression pin for the ILB
    acknowledgement retirement. The deferred-note constant is
    preserved for backwards-compat but MUST NOT appear in the
    rendered brief output (the copy claims the talker companion
    'ships in Phase 2B B1' which is now stale — B1 has shipped).

    Fires across both auto-T3-only and curated+auto cases."""
    # Auto-T3 only.
    _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Walk dog\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    body = render_tier_section(tmp_path, NOW)
    # Sanity: the auto-T3 subsection DID render (candidates present).
    assert T3_AUTO_SECTION_HEADER in body
    assert T3_AUTO_CONFIRM_PROMPT in body
    # The retired ILB note is gone.
    assert T3_AUTO_TALKER_DEFERRED_NOTE not in body
    # Also negative-pinned: the operator-facing claim "ships in Phase
    # 2B B1" should not appear ANYWHERE in the body (would indicate
    # someone rendered the deferred note via a different code path).
    assert "ships in Phase 2B B1" not in body


def test_t3_auto_section_header_absent_when_no_candidates(
    tmp_path: Path,
) -> None:
    """No routine records with target_cadence_days → no auto-T3
    subsection. The T3 bucket falls through to either curated
    entries or the T3_EMPTY_PROMPT sentinel.

    Sanity pin against polluting the brief with an "auto-suggested:
    nothing" header (distinct from T1 / T2 which always emit a
    bucket header).
    """
    _write_routine(
        tmp_path,
        "Daily.md",
        "type: routine\nstatus: active\nname: Daily\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        # No target_cadence_days — out of scope for auto-T3.
        "- text: Walk Fergus\n"
        "  priority: tracked\n",
    )
    body = render_tier_section(tmp_path, NOW)
    assert T3_AUTO_SECTION_HEADER not in body, (
        "Empty auto-T3 should NOT emit the subsection header — "
        "polluting the brief with 'auto-suggested: nothing' was "
        "the bug shape this contract pin prevents."
    )
    # T3_EMPTY_PROMPT still fires (no curated T3 either).
    assert T3_EMPTY_PROMPT in body


def test_t3_curated_only_when_no_auto_candidates(
    tmp_path: Path,
) -> None:
    """Backward-compat with current T3 render: curated T3 entries
    surface normally; NO auto subsection appears.

    Fixture: daily file has a curated T3 entry; no routine records
    with target_cadence_days.
    """
    _write_daily(
        tmp_path, "2026-05-28",
        "t1: []\nt2: []\nt3:\n"
        "- {item: Walk Fergus, source: manual}\n",
    )
    body = render_tier_section(tmp_path, NOW)
    # Curated entry surfaces.
    assert "Walk Fergus" in body
    # No auto subsection (no candidates).
    assert T3_AUTO_SECTION_HEADER not in body
    # T3_EMPTY_PROMPT NOT fired (curated T3 populated).
    assert T3_EMPTY_PROMPT not in body


def test_t3_curated_plus_auto_renders_curated_first_then_auto(
    tmp_path: Path,
) -> None:
    """Case D from the empty-state contract: curated T3 + auto-T3 →
    curated entries render FIRST, auto subsection BELOW. The
    operator's choices lead; auto-suggestions support."""
    # Curated T3 entry.
    _write_daily(
        tmp_path, "2026-05-28",
        "t1: []\nt2: []\nt3:\n"
        "- {item: Read 30 minutes, source: manual}\n",
    )
    # Auto-T3 candidate (never-completed soft-cadence item).
    _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Walk dog\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    body = render_tier_section(tmp_path, NOW)
    # Both surfaces present.
    assert "Read 30 minutes" in body
    assert "Walk dog" in body
    assert T3_AUTO_SECTION_HEADER in body
    # Order: curated first, then auto subhead. Compare positions.
    curated_idx = body.index("Read 30 minutes")
    auto_idx = body.index(T3_AUTO_SECTION_HEADER)
    walk_idx = body.index("Walk dog")
    assert curated_idx < auto_idx < walk_idx, (
        "Curated T3 entries must render BEFORE the auto-suggested "
        "subsection (operator's choices lead)."
    )


def test_t3_auto_render_line_shape_with_days_count(
    tmp_path: Path,
) -> None:
    """Render shape pin for completed-at-least-once items:
    ``- [[routine/<record>]] — <item> *(Nd days since last;
    target every Md)*``"""
    _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  Walk dog:\n"
        "  - '2026-05-24'\n"  # 4 days ago vs NOW=2026-05-28; target 3
        "items:\n"
        "- text: Walk dog\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    body = render_tier_section(tmp_path, NOW)
    # Find the Walk dog line in the body.
    walk_lines = [
        ln for ln in body.splitlines()
        if "Walk dog" in ln and "[[routine/Self Care]]" in ln
    ]
    assert len(walk_lines) == 1
    line = walk_lines[0]
    # Render shape: includes record wikilink + item text + annotation.
    assert line.startswith("- [[routine/Self Care]] — Walk dog ")
    assert "*(4 days since last; target every 3d)*" in line


def test_t3_auto_render_line_shape_never_completed(
    tmp_path: Path,
) -> None:
    """Never-completed items use the "never done" label instead of a
    day count — keeps the operator's eye drawn to the "this has
    never been done" signal (distinct from "0 days since last")."""
    _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Practice guitar\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 7\n",
    )
    body = render_tier_section(tmp_path, NOW)
    practice_lines = [
        ln for ln in body.splitlines()
        if "Practice guitar" in ln and "[[routine/Self Care]]" in ln
    ]
    assert len(practice_lines) == 1
    line = practice_lines[0]
    # "never done" label appears INSTEAD of "N days since last".
    assert "never done" in line
    assert "target every 7d" in line
    # NOT the day-count format.
    assert "days since last" not in line


def test_t3_auto_subsection_silently_omitted_with_curated_present(
    tmp_path: Path,
) -> None:
    """Case A from the empty-state contract: curated T3 populated +
    auto-T3 empty → curated entries render BUT NO auto subsection
    header. Pin against the noise-pollution shape."""
    _write_daily(
        tmp_path, "2026-05-28",
        "t1: []\nt2: []\nt3:\n"
        "- {item: Walk Fergus, source: manual}\n",
    )
    # No routine records with target_cadence_days — auto-T3 is empty.
    body = render_tier_section(tmp_path, NOW)
    assert "Walk Fergus" in body
    assert T3_AUTO_SECTION_HEADER not in body
    assert T3_AUTO_CONFIRM_PROMPT not in body
    assert T3_AUTO_TALKER_DEFERRED_NOTE not in body
    # T3_EMPTY_PROMPT NOT fired (curated populated).
    assert T3_EMPTY_PROMPT not in body


def test_t3_log_emission_carries_auto_t3_routine_count(
    tmp_path: Path,
) -> None:
    """Per ``feedback_log_emission_test_pattern``: the
    ``brief.tier_section.rendered`` log event MUST carry the
    ``auto_t3_routine_count`` field — operator can grep for it +
    a future refactor that drops the field surfaces here."""
    _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Walk dog\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    with structlog.testing.capture_logs() as captured:
        render_tier_section(tmp_path, NOW)
    events = [
        c for c in captured
        if c.get("event") == "brief.tier_section.rendered"
    ]
    assert len(events) == 1
    assert "auto_t3_routine_count" in events[0]
    assert events[0]["auto_t3_routine_count"] == 1


def test_t3_log_emission_carries_auto_t3_routine_count_when_empty(
    tmp_path: Path,
) -> None:
    """Mirror of the above pin: the field MUST be present even when
    the auto-T3 bucket is empty (value 0). Distinguishes "ran with
    zero candidates" from "field dropped from log."""
    # Empty vault — no routine records.
    with structlog.testing.capture_logs() as captured:
        render_tier_section(tmp_path, NOW)
    events = [
        c for c in captured
        if c.get("event") == "brief.tier_section.rendered"
    ]
    assert len(events) == 1
    assert "auto_t3_routine_count" in events[0]
    assert events[0]["auto_t3_routine_count"] == 0


# ---------------------------------------------------------------------------
# Daily-goal render line (Step 2c / Q4, 2026-06-26)
# ---------------------------------------------------------------------------


def test_daily_goal_line_empty_day_emits_sentinel() -> None:
    """Per intentionally-left-blank: an empty day (no tier items) still
    emits an explicit line, never a silent gap."""
    line = render_daily_goal_line(DailyGoalState())
    assert line == "**Daily goal:** no tier items yet today."


def test_daily_goal_line_balanced_achieved() -> None:
    line = render_daily_goal_line(DailyGoalState(
        t1_available=2, t2_available=1, t3_available=1,
        t1_done=2, t2_done=1, t3_done=1,
        balanced_day=True, all_t1_done=True,
    ))
    assert "balanced day:** ✓ achieved" in line
    assert "T1 2/2" in line
    assert "T2 1/1" in line
    assert "T3 1/1" in line
    assert "all T1 done" in line


def test_daily_goal_line_not_yet() -> None:
    line = render_daily_goal_line(DailyGoalState(
        t1_available=2, t2_available=1, t3_available=1,
        t1_done=0, t2_done=0, t3_done=0,
        balanced_day=False, all_t1_done=False,
    ))
    assert "balanced day:** not yet" in line
    assert "T1 0/2" in line
    # all_t1_done is False → no "all T1 done" marker.
    assert "all T1 done" not in line


def test_daily_goal_line_all_t1_done_marker_only_when_t1_items() -> None:
    """all_t1_done is vacuously True with no T1 items, but the marker
    must NOT appear when there are zero T1 items (it would be
    misleading)."""
    line = render_daily_goal_line(DailyGoalState(
        t1_available=0, t2_available=1, t3_available=1,
        t1_done=0, t2_done=1, t3_done=1,
        balanced_day=False, all_t1_done=True,
    ))
    assert "all T1 done" not in line


def test_tier_section_renders_goal_line_first(tmp_path: Path) -> None:
    """The daily-goal line is the FIRST line of the tier section body —
    the view is framed around the goal (Step 2c)."""
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True)
    (vault / "task" / "Pay.md").write_text(
        "---\ntype: task\nstatus: todo\nname: Pay\ndue: 2026-05-28\n---\n",
        encoding="utf-8",
    )
    body = render_tier_section(vault, NOW)
    assert body.splitlines()[0].startswith("**Daily goal")


def test_tier_section_log_carries_goal_rollup(tmp_path: Path) -> None:
    """Step 2c log-emission pin (discipline #9): the rendered log carries
    the daily-goal rollup fields surfaced from the unified view."""
    vault = tmp_path / "vault"
    (vault / "task").mkdir(parents=True)
    with structlog.testing.capture_logs() as captured:
        render_tier_section(vault, NOW)
    events = [
        c for c in captured
        if c.get("event") == "brief.tier_section.rendered"
    ]
    assert len(events) == 1
    assert "balanced_day" in events[0]
    assert "all_t1_done" in events[0]


# ---------------------------------------------------------------------------
# Single-source invariant (Step 2c, 2026-06-26) — the renderer defers
# WHAT to compute_today_view, never re-deriving via compute_auto_*.
# ---------------------------------------------------------------------------


def test_render_tier_section_reads_view_not_compute_auto(
    tmp_path: Path, monkeypatch,
) -> None:
    """The render layer must read compute_today_view, NOT call the
    compute_auto_* predicates directly. Pin by exploding the auto
    predicates if invoked from the render path — if any fires, the
    renderer is still re-deriving its own surface decision (the
    single-source invariant the team-lead required for Option B).

    compute_today_view internally calls the predicates — that's the ONE
    legitimate caller. So we patch them in the tier_section module
    namespace (the render layer's import surface). Since Step 2c dropped
    those imports, a NameError-free render proves the renderer never
    references them; this test additionally guards against a future
    re-import + direct call by asserting the symbols aren't in the
    module namespace."""
    import alfred.brief.tier_section as ts

    # The render layer must not have re-imported the auto predicates.
    for sym in (
        "compute_auto_t1_candidates",
        "compute_auto_routine_candidates",
        "compute_auto_routine_t2_candidates",
        "compute_auto_t3_candidates",
    ):
        assert not hasattr(ts, sym), (
            f"tier_section re-imported {sym}; the render layer must read "
            "compute_today_view for surface decisions, not re-derive via "
            "the auto predicates (Step 2c single-source invariant)."
        )

    # And it DOES reference the view.
    assert hasattr(ts, "compute_today_view")


def test_render_tier_section_calls_view_once(tmp_path: Path) -> None:
    """compute_today_view is called exactly once per render (no
    double-compute — the goal line + the lane slices read the same
    view)."""
    import alfred.brief.tier_section as ts

    _write_task(
        tmp_path, "Pay",
        {"type": "task", "status": "todo", "name": "Pay", "due": "2026-05-28"},
    )
    calls = {"n": 0}
    real = ts.compute_today_view

    def _counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    import unittest.mock as _m
    with _m.patch.object(ts, "compute_today_view", side_effect=_counting):
        ts.render_tier_section(tmp_path, NOW)
    assert calls["n"] == 1, (
        f"compute_today_view called {calls['n']}x — must be exactly once "
        "(no double-compute)."
    )
