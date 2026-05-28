"""Tests for ``alfred.brief.tier_section`` — vault scan + tier render.

Covers:
- Empty task/ directory → three buckets + sentinels (intentionally-left-blank).
- Open vs closed status filtering — done/cancelled excluded; blocked included.
- Per-task render shape: bare / priority-derived / escalated / overdue.
- Per-bucket sorting by due date.
- Log emissions per builder.md rule #9.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from textwrap import dedent

import pytest
import structlog

from alfred.brief.tier_section import SECTION_HEADER, render_tier_section


# Reference instant for deterministic tests.
NOW = datetime(2026, 5, 28, 13, 0, 0, tzinfo=timezone.utc)


def _write_task(
    vault_path: Path,
    name: str,
    frontmatter: dict,
    body: str = "",
) -> Path:
    """Write a task record under ``<vault>/task/<name>.md``."""
    task_dir = vault_path / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    fm_lines = ["---"]
    for k, v in frontmatter.items():
        if isinstance(v, list):
            if not v:
                fm_lines.append(f"{k}: []")
            else:
                fm_lines.append(f"{k}:")
                for item in v:
                    fm_lines.append(f"- {item}")
        elif v is None:
            fm_lines.append(f"{k}: null")
        else:
            fm_lines.append(f"{k}: {v!r}" if isinstance(v, str) else f"{k}: {v}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(body)
    path = task_dir / f"{name}.md"
    path.write_text("\n".join(fm_lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Section header constant
# ---------------------------------------------------------------------------


def test_section_header_pinned() -> None:
    """The section name is operator-facing; pin it so a rename surfaces."""
    assert SECTION_HEADER == "Open Tasks by Tier"


# ---------------------------------------------------------------------------
# Empty / no tasks
# ---------------------------------------------------------------------------


def test_no_task_dir_renders_buckets_with_sentinels(tmp_path: Path) -> None:
    """No ``vault/task/`` directory → render full three-bucket sentinel."""
    with structlog.testing.capture_logs() as captured:
        body = render_tier_section(tmp_path, NOW)

    assert "### Tier 1" in body
    assert "### Tier 2" in body
    assert "### Tier 3" in body
    assert "no open tasks" in body.lower()
    # Log pin: no_task_dir event fires.
    matches = [c for c in captured if c.get("event") == "brief.tier_section.no_task_dir"]
    assert len(matches) == 1


def test_empty_task_dir_renders_buckets_with_sentinels(tmp_path: Path) -> None:
    (tmp_path / "task").mkdir()
    with structlog.testing.capture_logs() as captured:
        body = render_tier_section(tmp_path, NOW)
    assert "### Tier 1" in body
    assert "### Tier 2" in body
    assert "### Tier 3" in body
    assert "no open tasks at any tier" in body
    # Log pin: no_open_tasks event fires (different signal from no_task_dir —
    # task dir exists but no records, so we scan 0 records).
    matches = [c for c in captured if c.get("event") == "brief.tier_section.no_open_tasks"]
    assert len(matches) == 1
    assert matches[0]["scanned"] == 0


def test_all_done_tasks_renders_no_open_sentinel(tmp_path: Path) -> None:
    """Records exist but all are done/cancelled → no_open_tasks sentinel."""
    _write_task(
        tmp_path, "Done Task",
        {"type": "task", "status": "done", "base_tier": 1, "created": "2026-05-01"},
    )
    _write_task(
        tmp_path, "Cancelled Task",
        {"type": "task", "status": "cancelled", "base_tier": 2, "created": "2026-05-01"},
    )
    with structlog.testing.capture_logs() as captured:
        body = render_tier_section(tmp_path, NOW)
    assert "no open tasks at any tier" in body
    matches = [c for c in captured if c.get("event") == "brief.tier_section.no_open_tasks"]
    assert len(matches) == 1
    assert matches[0]["scanned"] == 2  # both records scanned but filtered out


# ---------------------------------------------------------------------------
# Status filtering — open statuses
# ---------------------------------------------------------------------------


def test_blocked_status_surfaces_in_queue(tmp_path: Path) -> None:
    """Per dispatch ratification: blocked tasks must surface."""
    _write_task(
        tmp_path, "Blocked Task",
        {"type": "task", "status": "blocked", "base_tier": 2, "created": "2026-05-01"},
    )
    body = render_tier_section(tmp_path, NOW)
    assert "Blocked Task" in body


def test_todo_status_surfaces(tmp_path: Path) -> None:
    _write_task(
        tmp_path, "Todo Task",
        {"type": "task", "status": "todo", "base_tier": 3, "created": "2026-05-01"},
    )
    body = render_tier_section(tmp_path, NOW)
    assert "Todo Task" in body


def test_active_status_surfaces(tmp_path: Path) -> None:
    _write_task(
        tmp_path, "Active Task",
        {"type": "task", "status": "active", "base_tier": 1, "created": "2026-05-01"},
    )
    body = render_tier_section(tmp_path, NOW)
    assert "Active Task" in body


def test_missing_status_treated_as_todo(tmp_path: Path) -> None:
    """Forward-compat: operator-authored records without status surface."""
    _write_task(
        tmp_path, "No Status Task",
        {"type": "task", "base_tier": 2, "created": "2026-05-01"},
    )
    body = render_tier_section(tmp_path, NOW)
    assert "No Status Task" in body


def test_done_status_excluded(tmp_path: Path) -> None:
    _write_task(
        tmp_path, "Open Task",
        {"type": "task", "status": "todo", "base_tier": 1, "created": "2026-05-01"},
    )
    _write_task(
        tmp_path, "Closed Task",
        {"type": "task", "status": "done", "base_tier": 1, "created": "2026-05-01"},
    )
    body = render_tier_section(tmp_path, NOW)
    assert "Open Task" in body
    assert "Closed Task" not in body


# ---------------------------------------------------------------------------
# Render shape — annotations
# ---------------------------------------------------------------------------


def test_bare_render_for_base_tier_no_due(tmp_path: Path) -> None:
    """Base-tier-set, no due → bare ``T<n>`` annotation."""
    _write_task(
        tmp_path, "Standing Task",
        {"type": "task", "status": "todo", "base_tier": 3, "created": "2026-05-01"},
    )
    body = render_tier_section(tmp_path, NOW)
    assert "- [ ] [[task/Standing Task]] — T3" in body
    # Should NOT have a parenthetical (bare).
    line = next(L for L in body.splitlines() if "Standing Task" in L)
    assert "(" not in line


def test_priority_derived_annotation(tmp_path: Path) -> None:
    """Pre-migration task with priority but no base_tier → '(from priority)'."""
    _write_task(
        tmp_path, "Legacy Task",
        {"type": "task", "status": "todo", "priority": "high", "created": "2026-05-01"},
    )
    body = render_tier_section(tmp_path, NOW)
    assert "[[task/Legacy Task]] — T2 (from priority)" in body


def test_escalated_render_shape(tmp_path: Path) -> None:
    """Escalated → ``T<base>→T<eff> (due <date>, <distance>)``."""
    _write_task(
        tmp_path, "Payroll",
        {
            "type": "task",
            "status": "todo",
            "base_tier": 2,
            "due": "2026-05-30",
            "escalate_at_days": 3,
            "created": "2026-05-01",
        },
    )
    body = render_tier_section(tmp_path, NOW)
    # Find the line.
    line = next(L for L in body.splitlines() if "Payroll" in L)
    assert "T2→T1" in line
    assert "due 2026-05-30" in line
    assert "2d" in line


def test_overdue_render_shape(tmp_path: Path) -> None:
    """Overdue → ``T<base>→T<eff> (overdue Nd)``, no 'due <date>' since
    the date is in the past and 'overdue' is more useful framing."""
    _write_task(
        tmp_path, "Late Task",
        {
            "type": "task",
            "status": "todo",
            "base_tier": 3,
            "due": "2026-05-25",
            "created": "2026-05-01",
        },
    )
    body = render_tier_section(tmp_path, NOW)
    line = next(L for L in body.splitlines() if "Late Task" in L)
    assert "T3→T2" in line
    assert "overdue 3d" in line


def test_same_day_due_renders_hours(tmp_path: Path) -> None:
    """Same-day due renders hours-to-end-of-day instead of '0d'."""
    _write_task(
        tmp_path, "Today Task",
        {
            "type": "task",
            "status": "todo",
            "base_tier": 2,
            "due": "2026-05-28",
            "escalate_at_days": 1,
            "created": "2026-05-01",
        },
    )
    body = render_tier_section(tmp_path, NOW)
    line = next(L for L in body.splitlines() if "Today Task" in L)
    assert "T2→T1" in line
    # Hours format — should have an "Nh" token.
    assert "h)" in line


# ---------------------------------------------------------------------------
# Bucketing — tasks land in the right T1/T2/T3 sections
# ---------------------------------------------------------------------------


def test_tasks_bucket_by_effective_tier(tmp_path: Path) -> None:
    """Escalation should place a task in its EFFECTIVE bucket, not base."""
    _write_task(
        tmp_path, "Aspirational",
        {"type": "task", "status": "todo", "base_tier": 3, "created": "2026-05-01"},
    )
    _write_task(
        tmp_path, "Escalated",
        {
            "type": "task",
            "status": "todo",
            "base_tier": 3,
            "due": "2026-05-29",
            "escalate_at_days": 2,
            "escalate_to": 1,
            "created": "2026-05-01",
        },
    )
    body = render_tier_section(tmp_path, NOW)

    # Find each task's bucket by line position.
    lines = body.splitlines()
    t1_idx = lines.index("### Tier 1")
    t2_idx = lines.index("### Tier 2")
    t3_idx = lines.index("### Tier 3")

    escalated_idx = next(i for i, L in enumerate(lines) if "Escalated" in L)
    aspirational_idx = next(i for i, L in enumerate(lines) if "Aspirational" in L)

    assert t1_idx < escalated_idx < t2_idx
    assert t3_idx < aspirational_idx


def test_empty_bucket_renders_sentinel(tmp_path: Path) -> None:
    """A bucket with no tasks emits a sentinel — three-bucket render is
    unconditional per intentionally-left-blank."""
    _write_task(
        tmp_path, "Only T1",
        {"type": "task", "status": "todo", "base_tier": 1, "created": "2026-05-01"},
    )
    body = render_tier_section(tmp_path, NOW)
    assert "### Tier 1" in body
    assert "Only T1" in body
    assert "### Tier 2" in body
    assert "no open tasks at Tier 2" in body
    assert "### Tier 3" in body
    assert "no open tasks at Tier 3" in body


# ---------------------------------------------------------------------------
# Sorting — within bucket, by due date ascending
# ---------------------------------------------------------------------------


def test_within_bucket_sort_by_due_ascending(tmp_path: Path) -> None:
    """Earliest due first; no-due last; tiebreak by name."""
    _write_task(
        tmp_path, "Far Due",
        {
            "type": "task", "status": "todo", "base_tier": 2,
            "due": "2026-06-15", "created": "2026-05-01",
        },
    )
    _write_task(
        tmp_path, "Soon Due",
        {
            "type": "task", "status": "todo", "base_tier": 2,
            "due": "2026-06-01", "created": "2026-05-01",
        },
    )
    _write_task(
        tmp_path, "No Due",
        {
            "type": "task", "status": "todo", "base_tier": 2,
            "created": "2026-05-01",
        },
    )
    body = render_tier_section(tmp_path, NOW)
    lines = body.splitlines()
    soon_idx = next(i for i, L in enumerate(lines) if "Soon Due" in L)
    far_idx = next(i for i, L in enumerate(lines) if "Far Due" in L)
    no_idx = next(i for i, L in enumerate(lines) if "No Due" in L)
    assert soon_idx < far_idx < no_idx


# ---------------------------------------------------------------------------
# Robustness — bad records don't crash the render
# ---------------------------------------------------------------------------


def test_unparseable_record_logs_and_continues(tmp_path: Path) -> None:
    """A record that fails to parse should be skipped with a log line."""
    task_dir = tmp_path / "task"
    task_dir.mkdir(parents=True)
    # Write a deliberately broken frontmatter file.
    (task_dir / "Broken.md").write_text(
        "---\nstatus: todo\nbase_tier: [unclosed\n",
        encoding="utf-8",
    )
    # And a valid one to confirm the render continues.
    _write_task(
        tmp_path, "Valid Task",
        {"type": "task", "status": "todo", "base_tier": 1, "created": "2026-05-01"},
    )
    with structlog.testing.capture_logs() as captured:
        body = render_tier_section(tmp_path, NOW)
    assert "Valid Task" in body
    parse_fails = [c for c in captured if c.get("event") == "brief.tier_section.parse_failed"]
    assert len(parse_fails) == 1


# ---------------------------------------------------------------------------
# Log emission pin — successful render
# ---------------------------------------------------------------------------


def test_rendered_event_emits_bucket_counts(tmp_path: Path) -> None:
    """Per builder.md rule #9: pin the rendered log line and its fields."""
    _write_task(
        tmp_path, "T1 Task",
        {"type": "task", "status": "todo", "base_tier": 1, "created": "2026-05-01"},
    )
    _write_task(
        tmp_path, "T2 Task",
        {"type": "task", "status": "todo", "base_tier": 2, "created": "2026-05-01"},
    )
    _write_task(
        tmp_path, "T3 Task A",
        {"type": "task", "status": "todo", "base_tier": 3, "created": "2026-05-01"},
    )
    _write_task(
        tmp_path, "T3 Task B",
        {"type": "task", "status": "todo", "base_tier": 3, "created": "2026-05-01"},
    )
    with structlog.testing.capture_logs() as captured:
        render_tier_section(tmp_path, NOW)
    matches = [c for c in captured if c.get("event") == "brief.tier_section.rendered"]
    assert len(matches) == 1
    rec = matches[0]
    assert rec["scanned"] == 4
    assert rec["open_count"] == 4
    assert rec["t1"] == 1
    assert rec["t2"] == 1
    assert rec["t3"] == 2
