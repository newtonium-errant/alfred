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
    T2_EMPTY_PROMPT,
    T2_POOL_HEADER,
    T3_EMPTY_PROMPT,
    render_tier_section,
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
    assert e["auto_t1_count"] == 0
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
    assert e["auto_t1_count"] == 1
    assert e["scanned"] == 1
