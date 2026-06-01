"""Tests for ``alfred.tier.compute`` — V2 auto-T1 candidate discovery.

V1 retired in Ship 3 (2026-05-29). The per-task ``base_tier`` /
``escalate_to`` / priority-fallback projection through
``compute_effective_tier`` is gone, along with ``PRIORITY_TO_BASE_TIER``,
``derive_base_tier_from_priority``, ``TierResult``, and
``DEFAULT_ESCALATION_GAP``. V2's only compute primitive is
:func:`compute_auto_t1_candidates`.

Test surface:
  * Boundary cases for the auto-T1 discovery filter (due today /
    tomorrow / escalate window / status filter / alfred_triage /
    wrong type / no due / unparseable due / overdue separate surface)
  * Sort order pin (due_iso ascending then name)
  * Parse-failure defensive skip
  * Pure-function invariant (no log emissions)
  * Dataclass field-name contract pin (Ship 2 + Ship 4 reference)
  * V1 symbol absence pin — importing V1 symbols MUST raise
    ImportError. Pins the atomic-drop contract so a re-introduction
    of V1 symbols surfaces immediately.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import structlog

from alfred.tier.compute import (
    OPEN_STATUSES,
    AutoT1Candidate,
    coerce_due_date,
    compute_auto_t1_candidates,
)


# Reference instant — 2026-05-28 13:00 UTC. Tests pass deterministic
# ``now`` to keep day-boundary math reproducible. "Today" = 2026-05-28,
# "Tomorrow" = 2026-05-29.
NOW = datetime(2026, 5, 28, 13, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Module-level constant pins
# ---------------------------------------------------------------------------


def test_open_statuses_includes_blocked() -> None:
    """Ratified 2026-05-28: blocked tasks surface in the queue."""
    assert "blocked" in OPEN_STATUSES
    assert "todo" in OPEN_STATUSES
    assert "active" in OPEN_STATUSES
    assert "done" not in OPEN_STATUSES
    assert "cancelled" not in OPEN_STATUSES


# ---------------------------------------------------------------------------
# coerce_due_date — public helper used by render layer
# ---------------------------------------------------------------------------


def test_coerce_due_date_from_iso_string() -> None:
    """Operator-edited records carry ``due: '2026-05-28'`` (quoted)."""
    assert coerce_due_date("2026-05-28") == date_2026_05_28()


def test_coerce_due_date_from_date_object() -> None:
    """PyYAML parses bare ``due: 2026-05-28`` as a date object."""
    from datetime import date
    assert coerce_due_date(date(2026, 5, 28)) == date(2026, 5, 28)


def test_coerce_due_date_from_datetime_object() -> None:
    """A stray datetime normalises to its date component."""
    from datetime import date
    assert coerce_due_date(datetime(2026, 5, 28, 12, 0)) == date(2026, 5, 28)


def test_coerce_due_date_unparseable_returns_none() -> None:
    """Bad strings → None (caller treats as no-due)."""
    assert coerce_due_date("soonish") is None
    assert coerce_due_date("") is None
    assert coerce_due_date(None) is None
    assert coerce_due_date(123) is None


def date_2026_05_28():
    """Tiny helper to keep the import-at-test-time pattern clean."""
    from datetime import date
    return date(2026, 5, 28)


# ---------------------------------------------------------------------------
# V1 symbol absence — pin the atomic-drop contract
# ---------------------------------------------------------------------------


def test_v1_symbols_no_longer_importable_from_compute() -> None:
    """V1 retired in Ship 3 (2026-05-29). Reintroducing any of these
    would silently break the atomic-drop contract — pin so the
    surface stays clean.

    Per the ratified pattern #22: drop V1 symbols in the SAME commit
    as the LAST consumer rewrite. Ship 3 is that commit; the symbols
    below MUST NOT be importable post-drop.
    """
    with pytest.raises(ImportError):
        from alfred.tier.compute import PRIORITY_TO_BASE_TIER  # noqa: F401
    with pytest.raises(ImportError):
        from alfred.tier.compute import derive_base_tier_from_priority  # noqa: F401
    with pytest.raises(ImportError):
        from alfred.tier.compute import compute_effective_tier  # noqa: F401
    with pytest.raises(ImportError):
        from alfred.tier.compute import TierResult  # noqa: F401
    with pytest.raises(ImportError):
        from alfred.tier.compute import DEFAULT_ESCALATION_GAP  # noqa: F401


def test_v1_symbols_no_longer_in_tier_package_namespace() -> None:
    """Mirror of the import test against the package surface — Ship
    3's __init__.py rewrite drops the re-exports."""
    import alfred.tier as tier_pkg
    assert not hasattr(tier_pkg, "PRIORITY_TO_BASE_TIER")
    assert not hasattr(tier_pkg, "derive_base_tier_from_priority")
    assert not hasattr(tier_pkg, "compute_effective_tier")
    assert not hasattr(tier_pkg, "TierResult")
    assert not hasattr(tier_pkg, "DEFAULT_ESCALATION_GAP")


# ===========================================================================
# compute_auto_t1_candidates — boundary coverage
# ===========================================================================
#
# The function walks ``vault_path/task/*.md`` and decides which tasks
# auto-surface as T1 candidates this morning. Tests use a tmp vault dir;
# NOW = 2026-05-28 13:00 UTC so "today" is 2026-05-28 and "tomorrow"
# is 2026-05-29.


def _make_vault_with_task(
    tmp_path: Path, filename: str, fm_yaml: str, body: str = "# body\n",
) -> Path:
    """Helper: seed a tmp vault with one task record at task/<filename>."""
    vault = tmp_path / "vault"
    task_dir = vault / "task"
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / filename).write_text(
        f"---\n{fm_yaml}---\n\n{body}",
        encoding="utf-8",
    )
    return vault


def test_auto_t1_empty_vault_returns_empty_list(tmp_path: Path) -> None:
    """No ``task/`` dir → empty list (idle-not-broken signal)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = compute_auto_t1_candidates(vault, NOW)
    assert result == []


def test_auto_t1_due_today_surfaces(tmp_path: Path) -> None:
    """Task due today (NOW.date() == 2026-05-28) → surface with
    reason ``due today``."""
    vault = _make_vault_with_task(
        tmp_path,
        "RRTS Payroll.md",
        "type: task\nstatus: todo\nname: RRTS Payroll\ndue: 2026-05-28\n",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert len(result) == 1
    assert result[0].name == "RRTS Payroll"
    assert result[0].surface_reason == "due today"
    assert result[0].due_iso == "2026-05-28"
    assert result[0].path == "task/RRTS Payroll.md"


def test_auto_t1_due_tomorrow_surfaces(tmp_path: Path) -> None:
    """Task due tomorrow → surface with reason ``due tomorrow``."""
    vault = _make_vault_with_task(
        tmp_path,
        "Bug List.md",
        "type: task\nstatus: todo\nname: Bug List\ndue: 2026-05-29\n",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert len(result) == 1
    assert result[0].surface_reason == "due tomorrow"
    assert result[0].due_iso == "2026-05-29"


def test_auto_t1_due_in_2d_no_escalate_window_does_not_surface(
    tmp_path: Path,
) -> None:
    """Due 2 days out + no ``escalate_at_days`` → NOT surfaced.

    Bare 2-day deadlines stay in T2 territory until the operator
    decides to escalate (via ``escalate_at_days``)."""
    vault = _make_vault_with_task(
        tmp_path,
        "Task A.md",
        "type: task\nstatus: todo\nname: Task A\ndue: 2026-05-30\n",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert result == []


def test_auto_t1_due_in_3d_with_escalate_at_3_surfaces(
    tmp_path: Path,
) -> None:
    """Boundary: due in 3d + ``escalate_at_days: 3`` → surface with
    canonical reason ``escalate window (3d before due)``."""
    vault = _make_vault_with_task(
        tmp_path,
        "Task B.md",
        "type: task\nstatus: todo\nname: Task B\n"
        "due: 2026-05-31\nescalate_at_days: 3\n",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert len(result) == 1
    assert result[0].surface_reason == "escalate window (3d before due)"


def test_auto_t1_due_in_4d_with_escalate_at_3_does_not_surface(
    tmp_path: Path,
) -> None:
    """Outside escalate window: 4d to due + ``escalate_at_days: 3`` →
    NOT surfaced (escalation hasn't fired yet)."""
    vault = _make_vault_with_task(
        tmp_path,
        "Task C.md",
        "type: task\nstatus: todo\nname: Task C\n"
        "due: 2026-06-01\nescalate_at_days: 3\n",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert result == []


def test_auto_t1_alfred_triage_excluded(tmp_path: Path) -> None:
    """``alfred_triage: True`` records (janitor-generated triage) MUST
    never surface as tier candidates — they go to the Daily Sync
    Triage Queue (Ship 3 section). Defensive carve-out per the
    operator-stated tier model."""
    vault = _make_vault_with_task(
        tmp_path,
        "Triage Item.md",
        "type: task\nstatus: todo\nname: Triage Item\n"
        "due: 2026-05-28\nalfred_triage: true\n",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert result == []


def test_auto_t1_closed_status_excluded(tmp_path: Path) -> None:
    """``status: done`` / ``status: cancelled`` excluded — only open
    statuses surface."""
    vault = tmp_path / "vault"
    task_dir = vault / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "Done Task.md").write_text(
        "---\ntype: task\nstatus: done\nname: Done\ndue: 2026-05-28\n---\n",
        encoding="utf-8",
    )
    (task_dir / "Cancelled Task.md").write_text(
        "---\ntype: task\nstatus: cancelled\nname: X\ndue: 2026-05-28\n---\n",
        encoding="utf-8",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert result == []


def test_auto_t1_blocked_status_surfaces(tmp_path: Path) -> None:
    """``blocked`` is in OPEN_STATUSES — blocked tasks still surface
    (operator needs to see them in the daily queue)."""
    vault = _make_vault_with_task(
        tmp_path,
        "Blocked Task.md",
        "type: task\nstatus: blocked\nname: Blocked Task\n"
        "due: 2026-05-28\n",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert len(result) == 1
    assert result[0].name == "Blocked Task"


def test_auto_t1_wrong_type_excluded(tmp_path: Path) -> None:
    """Defensive: a non-task file under ``task/`` (shouldn't happen
    but operators paste stuff) is skipped."""
    vault = tmp_path / "vault"
    task_dir = vault / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "Stray.md").write_text(
        "---\ntype: note\nstatus: todo\ndue: 2026-05-28\n---\n",
        encoding="utf-8",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert result == []


def test_auto_t1_no_due_excluded(tmp_path: Path) -> None:
    """No ``due`` field → can't auto-surface (no deadline = no T1
    signal). The task may still be operator-picked into T2 manually
    in Ship 2's brief."""
    vault = _make_vault_with_task(
        tmp_path,
        "No Due.md",
        "type: task\nstatus: todo\nname: No Due\n",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert result == []


def test_auto_t1_unparseable_due_excluded(tmp_path: Path) -> None:
    """Bad ``due`` value → coerce_due_date returns None → not surfaced."""
    vault = _make_vault_with_task(
        tmp_path,
        "Bad Due.md",
        "type: task\nstatus: todo\nname: Bad Due\ndue: 'soonish'\n",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert result == []


def test_auto_t1_overdue_does_not_surface_via_this_path(
    tmp_path: Path,
) -> None:
    """Past-due tasks are handled by a separate "overdue" surface in
    Ship 2's brief, not by the auto-T1 candidate path. This function
    surfaces ONLY today/tomorrow/window — overdue is its own layer."""
    vault = _make_vault_with_task(
        tmp_path,
        "Overdue.md",
        "type: task\nstatus: todo\nname: Overdue\ndue: 2026-05-20\n",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert result == []


def test_auto_t1_results_sorted_by_due_then_name(tmp_path: Path) -> None:
    """Deterministic order: by ``due_iso`` ascending, then by
    case-insensitive ``name``."""
    vault = tmp_path / "vault"
    task_dir = vault / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "Z.md").write_text(
        "---\ntype: task\nstatus: todo\nname: Zeta\ndue: 2026-05-29\n---\n",
        encoding="utf-8",
    )
    (task_dir / "A.md").write_text(
        "---\ntype: task\nstatus: todo\nname: Alpha\ndue: 2026-05-28\n---\n",
        encoding="utf-8",
    )
    (task_dir / "M.md").write_text(
        "---\ntype: task\nstatus: todo\nname: Mu\ndue: 2026-05-29\n---\n",
        encoding="utf-8",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    assert [c.name for c in result] == ["Alpha", "Mu", "Zeta"]


def test_auto_t1_parse_failure_skipped(tmp_path: Path) -> None:
    """A corrupt task file → skipped silently (parse-fail records are
    handled by janitor; not the tier path's job to surface)."""
    vault = tmp_path / "vault"
    task_dir = vault / "task"
    task_dir.mkdir(parents=True)
    (task_dir / "Corrupt.md").write_text(
        "---\n[invalid yaml\n---\n",
        encoding="utf-8",
    )
    (task_dir / "Good.md").write_text(
        "---\ntype: task\nstatus: todo\nname: Good\ndue: 2026-05-28\n---\n",
        encoding="utf-8",
    )
    result = compute_auto_t1_candidates(vault, NOW)
    # Good still surfaces; corrupt silently dropped.
    assert len(result) == 1
    assert result[0].name == "Good"


def test_auto_t1_no_log_emissions(tmp_path: Path) -> None:
    """Per dispatch contract: ``compute_auto_t1_candidates`` is a
    pure projection — no log lines. Per-sweep observability lives at
    the caller (Ship 2's brief, the routine aggregator)."""
    vault = _make_vault_with_task(
        tmp_path,
        "Task.md",
        "type: task\nstatus: todo\nname: Task\ndue: 2026-05-28\n",
    )
    with structlog.testing.capture_logs() as captured:
        compute_auto_t1_candidates(vault, NOW)
    assert captured == []


def test_auto_t1_candidate_is_dataclass() -> None:
    """Pin the contract surface — Ship 2 + Ship 4 reference these
    field names verbatim. A rename here = lockstep update there."""
    c = AutoT1Candidate(
        path="task/X.md",
        name="X",
        due_iso="2026-05-28",
        surface_reason="due today",
    )
    assert c.path == "task/X.md"
    assert c.name == "X"
    assert c.due_iso == "2026-05-28"
    assert c.surface_reason == "due today"
    # Phase 2A Ship A: origin defaults to "task" for backward-compat.
    assert c.origin == "task"
    assert c.routine_record is None
    assert c.item_text is None


def test_auto_t1_candidate_routine_origin_fields() -> None:
    """Phase 2A Ship A: routine-origin candidates carry
    routine_record + item_text. Pin the discriminated-union shape so
    Ship B + Ship D drift surfaces here."""
    c = AutoT1Candidate(
        path="routine/Recurring Bills + Admin.md",
        name="Pay Clinic Rental to Hussein Rafih",
        due_iso="2026-06-01",
        surface_reason="due today",
        origin="routine",
        routine_record="Recurring Bills + Admin",
        item_text="Pay Clinic Rental to Hussein Rafih",
    )
    assert c.origin == "routine"
    assert c.routine_record == "Recurring Bills + Admin"
    assert c.item_text == "Pay Clinic Rental to Hussein Rafih"


# ===========================================================================
# Phase 2A Ship A — compute_auto_routine_candidates (T1 window)
# ===========================================================================
#
# Test surface per dispatch:
#   11. routine item due tomorrow, escalate_at_days=1 → T1 (1 in [0,1])
#   12. routine item due today, escalate_at_days=0 → T1 (0 in [0,0])
#   13. routine item done in current cycle → NOT surfaced
#   14. escalate_at_days absent → NOT surfaced
# Plus boundary pins (no due_pattern, archived routine, alfred_triage).


from alfred.tier.compute import (  # noqa: E402
    compute_auto_routine_candidates,
    compute_auto_routine_t2_candidates,
)


def _write_routine(
    tmp_path: Path, filename: str, fm_yaml: str, body: str = "# body\n",
) -> Path:
    """Helper: seed a tmp vault with one routine record at routine/<filename>."""
    vault = tmp_path / "vault"
    routine_dir = vault / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    (routine_dir / filename).write_text(
        f"---\n{fm_yaml}---\n\n{body}",
        encoding="utf-8",
    )
    return vault


def test_auto_routine_due_tomorrow_with_escalate_at_1_surfaces_t1(
    tmp_path: Path,
) -> None:
    """Garbage Day shape: weekly day=fri, escalate_at_days=1, today
    Thursday → due tomorrow → T1 'due tomorrow'."""
    # NOW = 2026-05-28 (Thursday). weekly day=fri → due 2026-05-29.
    vault = _write_routine(
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
    result = compute_auto_routine_candidates(vault, NOW)
    assert len(result) == 1
    c = result[0]
    assert c.origin == "routine"
    assert c.routine_record == "Weekly Chores"
    assert c.item_text == "Garbage Out"
    assert c.name == "Garbage Out"
    assert c.due_iso == "2026-05-29"
    assert c.surface_reason == "due tomorrow"
    assert c.path == "routine/Weekly Chores.md"


def test_auto_routine_due_today_with_escalate_at_0_surfaces_t1(
    tmp_path: Path,
) -> None:
    """Clinic Rental shape: monthly day=28 (= NOW's date 2026-05-28),
    escalate_at_days=0 → due today → T1 'due today'."""
    vault = _write_routine(
        tmp_path,
        "Recurring Bills.md",
        "type: routine\nstatus: active\nname: Recurring Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Clinic Rental\n"
        "  priority: critical\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 28\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_candidates(vault, NOW)
    assert len(result) == 1
    assert result[0].surface_reason == "due today"
    assert result[0].due_iso == "2026-05-28"


def test_auto_routine_done_in_current_cycle_not_surfaced(
    tmp_path: Path,
) -> None:
    """Item done this cycle → NOT surfaced (operator already
    completed; should disappear until next cycle's window opens).
    Today 2026-05-28, monthly day=28 due today, completion log shows
    completion today → cycle = May → done → skip."""
    vault = _write_routine(
        tmp_path,
        "Recurring Bills.md",
        "type: routine\nstatus: active\nname: Recurring Bills\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  Pay Clinic Rental:\n"
        "  - '2026-05-28'\n"
        "items:\n"
        "- text: Pay Clinic Rental\n"
        "  priority: critical\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 28\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_candidates(vault, NOW)
    assert result == []


def test_auto_routine_escalate_at_days_absent_not_surfaced(
    tmp_path: Path,
) -> None:
    """Walk Fergus shape: no escalate_at_days → never tier-surfaces.
    The item with due_pattern but no escalate_at_days lives in the
    routines section only, not the tier section."""
    vault = _write_routine(
        tmp_path,
        "Daily Self-Care.md",
        "type: routine\nstatus: active\nname: Daily Self-Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Walk Fergus\n"
        "  priority: tracked\n"
        "  due_pattern:\n"
        "    type: weekly\n"
        "    day: thu\n",
        # NB: no escalate_at_days field.
    )
    result = compute_auto_routine_candidates(vault, NOW)
    assert result == []


def test_auto_routine_no_due_pattern_not_surfaced(tmp_path: Path) -> None:
    """Item without ``due_pattern`` → never surfaces (no deadline)."""
    vault = _write_routine(
        tmp_path,
        "Daily Self-Care.md",
        "type: routine\nstatus: active\nname: Daily Self-Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Brush Teeth\n"
        "  priority: tracked\n",
    )
    result = compute_auto_routine_candidates(vault, NOW)
    assert result == []


def test_auto_routine_archived_status_excluded(tmp_path: Path) -> None:
    """Archived routine → all items excluded regardless of windows."""
    vault = _write_routine(
        tmp_path,
        "Old Routine.md",
        "type: routine\nstatus: archived\nname: Old Routine\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Should Not Surface\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 28\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_candidates(vault, NOW)
    assert result == []


def test_auto_routine_alfred_triage_excluded(tmp_path: Path) -> None:
    """alfred_triage: True on the routine record → defensive skip
    (mirrors the task-path filter; routines shouldn't be triage-
    flagged but defense-in-depth)."""
    vault = _write_routine(
        tmp_path,
        "Triaged Routine.md",
        "type: routine\nstatus: active\nname: Triaged Routine\n"
        "alfred_triage: true\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Clinic Rental\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 28\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_candidates(vault, NOW)
    assert result == []


def test_auto_routine_wrong_type_excluded(tmp_path: Path) -> None:
    """Defensive: non-routine type under ``routine/`` is skipped."""
    vault = _write_routine(
        tmp_path,
        "Stray.md",
        "type: note\nname: Stray\n"
        "items:\n"
        "- text: Should Not Surface\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 28\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_candidates(vault, NOW)
    assert result == []


def test_auto_routine_no_routine_dir_returns_empty(tmp_path: Path) -> None:
    """No vault/routine/ directory → empty list."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = compute_auto_routine_candidates(vault, NOW)
    assert result == []


def test_auto_routine_no_log_emissions(tmp_path: Path) -> None:
    """compute_auto_routine_candidates is a pure projection — no
    log emissions. Per-sweep observability lives at the caller."""
    vault = _write_routine(
        tmp_path,
        "R.md",
        "type: routine\nstatus: active\nname: R\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: X\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 28\n"
        "  escalate_at_days: 0\n",
    )
    with structlog.testing.capture_logs() as captured:
        compute_auto_routine_candidates(vault, NOW)
    # Pure compute → no logs.
    assert captured == []


# ===========================================================================
# Phase 2A Ship A — compute_auto_routine_t2_candidates (T2 ramp window)
# ===========================================================================
#
# Test surface per dispatch:
#   15. surface_at_days=5, due in 4 days, escalate_at_days=0 → T2
#       (4 in (0, 5])
#   16. (Plan-ratified per dispatch's closing line): T2 window =
#       (escalate_at_days, surface_at_days]. With escalate=0,
#       surface=5, days_to_due=1 → in (0, 5] → SURFACED as T2.
#       The dispatch's bullet text contradicts the formula; trust
#       the formula since "re-verify during build" was explicit.
#
# Plus boundary pins (surface_at_days <= escalate_at_days → no T2,
# done-in-cycle skip, surface_at_days absent → no T2).


def test_auto_routine_t2_surface_4d_with_surface_5_escalate_0(
    tmp_path: Path,
) -> None:
    """Pay Clinic Rental shape: monthly day=1, surface_at_days=5,
    escalate_at_days=0. Today = 2026-05-28 → due 2026-06-01 →
    days_to_due=4 → T2 window (0, 5] contains 4 → SURFACE.

    Reason format: 'surface window (4d before due)' — the LIVE
    days_to_due value, not the surface_at_days outer bound."""
    vault = _write_routine(
        tmp_path,
        "Recurring Bills.md",
        "type: routine\nstatus: active\nname: Recurring Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Clinic Rental\n"
        "  priority: critical\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 1\n"
        "  surface_at_days: 5\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_t2_candidates(vault, NOW)
    assert len(result) == 1
    c = result[0]
    assert c.surface_reason == "surface window (4d before due)"
    assert c.due_iso == "2026-06-01"
    assert c.origin == "routine"
    assert c.routine_record == "Recurring Bills"
    assert c.item_text == "Pay Clinic Rental"


def test_auto_routine_t2_at_day_1_surface_5_escalate_0_does_surface(
    tmp_path: Path,
) -> None:
    """Plan-ratified boundary: surface_at_days=5, escalate_at_days=0,
    days_to_due=1 → (0, 5] contains 1 → SURFACES as T2.

    The dispatch's bullet text said 'NOT surfaced as T2' but the
    closing-line Plan formula 'T2 window = (escalate_at_days,
    surface_at_days]' contradicts. Per the dispatch's explicit
    'Re-verify the boundary semantics during build' directive,
    trust the formula: days_to_due=1, escalate=0, surface=5 →
    0 < 1 <= 5 → T2."""
    # NOW = 2026-05-28. monthly day=29 → due 2026-05-29 → days_to_due=1.
    vault = _write_routine(
        tmp_path,
        "Recurring Bills.md",
        "type: routine\nstatus: active\nname: Recurring Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Item Due Tomorrow\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 29\n"
        "  surface_at_days: 5\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_t2_candidates(vault, NOW)
    assert len(result) == 1
    assert result[0].surface_reason == "surface window (1d before due)"


def test_auto_routine_t2_at_day_5_boundary_inclusive(
    tmp_path: Path,
) -> None:
    """Boundary inclusive: days_to_due=5, surface_at_days=5 → SURFACE
    (matches the operator's worked example: 27th appears for 1st-due
    when surface_at_days=5)."""
    # NOW = 2026-05-28. monthly day=2 → due 2026-06-02 → days_to_due=5.
    vault = _write_routine(
        tmp_path,
        "Bills.md",
        "type: routine\nstatus: active\nname: Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Due In 5d\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 2\n"
        "  surface_at_days: 5\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_t2_candidates(vault, NOW)
    assert len(result) == 1
    assert result[0].surface_reason == "surface window (5d before due)"


def test_auto_routine_t2_at_day_6_outside_window_no_surface(
    tmp_path: Path,
) -> None:
    """Outside window: days_to_due=6, surface_at_days=5 → NO T2."""
    # NOW = 2026-05-28. monthly day=3 → due 2026-06-03 → days_to_due=6.
    vault = _write_routine(
        tmp_path,
        "Bills.md",
        "type: routine\nstatus: active\nname: Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Due In 6d\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 3\n"
        "  surface_at_days: 5\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_t2_candidates(vault, NOW)
    assert result == []


def test_auto_routine_t2_at_day_0_t1_window_not_t2(tmp_path: Path) -> None:
    """days_to_due=0 → T1 window, NOT T2 (the strict-above-escalate
    gate excludes day 0 from T2)."""
    # NOW = 2026-05-28. monthly day=28 → due 2026-05-28 → days_to_due=0.
    vault = _write_routine(
        tmp_path,
        "Bills.md",
        "type: routine\nstatus: active\nname: Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Due Today\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 28\n"
        "  surface_at_days: 5\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_t2_candidates(vault, NOW)
    assert result == []
    # The same item DOES surface as T1.
    t1_result = compute_auto_routine_candidates(vault, NOW)
    assert len(t1_result) == 1
    assert t1_result[0].surface_reason == "due today"


def test_auto_routine_t2_surface_le_escalate_no_t2_window(
    tmp_path: Path,
) -> None:
    """Garbage Day shape: surface_at_days <= escalate_at_days → T1-only
    item (no T2 ramp); T2 surface returns empty."""
    vault = _write_routine(
        tmp_path,
        "Weekly Chores.md",
        "type: routine\nstatus: active\nname: Weekly Chores\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Garbage Out\n"
        "  due_pattern:\n"
        "    type: weekly\n"
        "    day: fri\n"
        "  surface_at_days: 1\n"  # equal to escalate → no T2
        "  escalate_at_days: 1\n",
    )
    result = compute_auto_routine_t2_candidates(vault, NOW)
    assert result == []


def test_auto_routine_t2_surface_at_days_absent_no_t2(
    tmp_path: Path,
) -> None:
    """surface_at_days absent → no T2 ramp (item is T1-only)."""
    vault = _write_routine(
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
    result = compute_auto_routine_t2_candidates(vault, NOW)
    assert result == []


def test_auto_routine_t2_done_in_cycle_not_surfaced(tmp_path: Path) -> None:
    """Item already completed this cycle → T2 also skips (operator
    has resolved; should not nag)."""
    # monthly day=1 due 2026-06-01; days_to_due=4 → would surface T2.
    # Completion log shows completion 2026-06-01 → cycle = June → done.
    vault = _write_routine(
        tmp_path,
        "Bills.md",
        "type: routine\nstatus: active\nname: Bills\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  Pay Clinic Rental:\n"
        "  - '2026-06-01'\n"
        "items:\n"
        "- text: Pay Clinic Rental\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 1\n"
        "  surface_at_days: 5\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_t2_candidates(vault, NOW)
    assert result == []


# ===========================================================================
# Phase 2A-soft-cadence (2026-05-30) — compute_auto_t3_candidates
# ===========================================================================
#
# Test surface per dispatch:
#   * never-completed soft-cadence item → overdue_ratio = inf → ranks first
#   * sorted by overdue_ratio descending
#   * boundary at days_since == target_cadence_days → SURFACE (inclusive)
#   * days_since == target - 1 → NOT surfaced
#   * dataclass shape: AutoT3Candidate fields present + sane
#   * pure-compute: no log emissions
#   * mirror with aggregator._decide_tier_handoff T3 branch — identical
#     inputs → identical decisions (regression pin for
#     feedback_two_layer_window_math_mirror)


from alfred.tier.compute import (  # noqa: E402
    AutoT3Candidate,
    compute_auto_t3_candidates,
)


def test_auto_t3_candidate_is_dataclass() -> None:
    """Pin dataclass shape — fields present, types as documented."""
    c = AutoT3Candidate(
        path="routine/Self Care.md",
        routine_record="Self Care",
        item_text="Walk dog",
        target_cadence_days=3,
        days_since_last_completed=4,
        overdue_ratio=4 / 3,
    )
    assert c.path == "routine/Self Care.md"
    assert c.routine_record == "Self Care"
    assert c.item_text == "Walk dog"
    assert c.target_cadence_days == 3
    assert c.days_since_last_completed == 4
    assert c.overdue_ratio == pytest.approx(4 / 3)


def test_compute_auto_t3_empty_vault_returns_empty_list(
    tmp_path: Path,
) -> None:
    """No routine dir → empty list (no crash)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    result = compute_auto_t3_candidates(vault, NOW)
    assert result == []


def test_compute_auto_t3_never_completed_treated_as_max_overdue(
    tmp_path: Path,
) -> None:
    """Item with target_cadence_days but no completion_log →
    overdue_ratio = ``inf`` → ranks first. Never-completed contract."""
    vault = _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Practice guitar\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 7\n",
    )
    result = compute_auto_t3_candidates(vault, NOW)
    assert len(result) == 1
    cand = result[0]
    assert cand.item_text == "Practice guitar"
    assert cand.routine_record == "Self Care"
    assert cand.target_cadence_days == 7
    assert cand.days_since_last_completed is None
    assert cand.overdue_ratio == float("inf")


def test_compute_auto_t3_sorted_by_overdue_ratio_descending(
    tmp_path: Path,
) -> None:
    """Three items with different overdue ratios → sort descending.

    Fixture (NOW = 2026-05-28):
      * Item A: target=3, last 8 days ago → ratio 8/3 ≈ 2.67
      * Item B: target=7, last 8 days ago → ratio 8/7 ≈ 1.14
      * Item C: target=10, never completed → ratio inf

    Expected sort order: C (inf), A (2.67), B (1.14).
    """
    vault = _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  Item A:\n"
        "  - '2026-05-20'\n"  # 8 days ago vs NOW=2026-05-28
        "  Item B:\n"
        "  - '2026-05-20'\n"  # 8 days ago
        "items:\n"
        "- text: Item A\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n"
        "- text: Item B\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 7\n"
        "- text: Item C\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 10\n",
    )
    result = compute_auto_t3_candidates(vault, NOW)
    assert len(result) == 3
    # Order: C (inf), A (~2.67), B (~1.14).
    assert result[0].item_text == "Item C"
    assert result[0].overdue_ratio == float("inf")
    assert result[1].item_text == "Item A"
    assert result[1].overdue_ratio == pytest.approx(8 / 3)
    assert result[2].item_text == "Item B"
    assert result[2].overdue_ratio == pytest.approx(8 / 7)


def test_compute_auto_t3_threshold_at_one_inclusive(
    tmp_path: Path,
) -> None:
    """Boundary pin: ratio exactly 1.0 surfaces (days_since == target,
    inclusive); ratio 0.99 (days_since == target - 1) does NOT.

    Fixture:
      * Item Exactly: target=4, last 4 days ago → ratio 1.0 → SURFACE.
      * Item Below: target=5, last 4 days ago → ratio 0.8 → SKIP.
    """
    vault = _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  Item Exactly:\n"
        "  - '2026-05-24'\n"  # 4 days ago vs NOW=2026-05-28
        "  Item Below:\n"
        "  - '2026-05-24'\n"  # 4 days ago, target 5 → below
        "items:\n"
        "- text: Item Exactly\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 4\n"
        "- text: Item Below\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 5\n",
    )
    result = compute_auto_t3_candidates(vault, NOW)
    # Only Item Exactly surfaces.
    names = {c.item_text for c in result}
    assert names == {"Item Exactly"}
    assert result[0].overdue_ratio == pytest.approx(1.0)


def test_compute_auto_t3_due_pattern_wins_when_both_set(
    tmp_path: Path,
) -> None:
    """Item with BOTH due_pattern AND target_cadence_days → due_pattern
    wins → NOT surfaced in T3 compute. Mutually-exclusive precedence
    enforced at the compute layer (the aggregator emits the operator-
    facing warn log; this compute path defensively matches the same
    outcome silently)."""
    vault = _write_routine(
        tmp_path,
        "Mixed Modes.md",
        "type: routine\nstatus: active\nname: Mixed Modes\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Ambiguous Item\n"
        "  priority: tracked\n"
        "  due_pattern:\n"
        "    type: weekly\n"
        "    day: thu\n"
        "  escalate_at_days: 0\n"
        "  target_cadence_days: 7\n",
    )
    result = compute_auto_t3_candidates(vault, NOW)
    assert result == [], (
        "Items with both due_pattern AND target_cadence_days should "
        "NOT surface in T3 compute — due_pattern wins per the "
        "mutually-exclusive precedence rule."
    )


def test_compute_auto_t3_archived_routine_excluded(tmp_path: Path) -> None:
    """Archived routines: items NOT surfaced (mirror of T1/T2 scan)."""
    vault = _write_routine(
        tmp_path,
        "Old.md",
        "type: routine\nstatus: archived\nname: Old\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Walk dog\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    assert compute_auto_t3_candidates(vault, NOW) == []


def test_compute_auto_t3_alfred_triage_excluded(tmp_path: Path) -> None:
    """Defense-in-depth: alfred_triage on routine record → skip."""
    vault = _write_routine(
        tmp_path,
        "Triaged.md",
        "type: routine\nstatus: active\nname: Triaged\n"
        "alfred_triage: true\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Walk dog\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    assert compute_auto_t3_candidates(vault, NOW) == []


def test_compute_auto_t3_negative_target_excluded(tmp_path: Path) -> None:
    """Defensive: target_cadence_days <= 0 → skip (undefined semantics
    for the overdue ratio)."""
    vault = _write_routine(
        tmp_path,
        "Bad.md",
        "type: routine\nstatus: active\nname: Bad\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Walk dog\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 0\n"
        "- text: Walk Fergus\n"
        "  priority: aspirational\n"
        "  target_cadence_days: -3\n",
    )
    assert compute_auto_t3_candidates(vault, NOW) == []


def test_compute_auto_t3_item_without_target_cadence_excluded(
    tmp_path: Path,
) -> None:
    """Item with no target_cadence_days → not in scope for T3 compute
    (it's a regular routine item)."""
    vault = _write_routine(
        tmp_path,
        "Daily.md",
        "type: routine\nstatus: active\nname: Daily\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Walk Fergus\n"
        "  priority: tracked\n",
    )
    assert compute_auto_t3_candidates(vault, NOW) == []


def test_compute_auto_t3_no_log_emissions(tmp_path: Path) -> None:
    """Pure-compute path → no log emissions. Mirror of the T1/T2
    no-log invariants."""
    vault = _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Practice guitar\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 7\n",
    )
    with structlog.testing.capture_logs() as captured:
        compute_auto_t3_candidates(vault, NOW)
    # No log lines from compute.
    compute_emissions = [
        c for c in captured
        if c.get("event", "").startswith("tier.compute")
        or c.get("event", "").startswith("compute_auto_t3")
    ]
    assert compute_emissions == []


def test_compute_auto_t3_ties_broken_by_item_text_alpha(
    tmp_path: Path,
) -> None:
    """Two items with identical overdue_ratio → tie broken by
    item_text case-insensitive ascending."""
    vault = _write_routine(
        tmp_path,
        "Self Care.md",
        "type: routine\nstatus: active\nname: Self Care\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        # Both never completed → ratio inf → tied → alpha sort.
        "- text: Z Last\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 7\n"
        "- text: A First\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 7\n"
        "- text: M Middle\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 7\n",
    )
    result = compute_auto_t3_candidates(vault, NOW)
    names = [c.item_text for c in result]
    assert names == ["A First", "M Middle", "Z Last"]


# ---------------------------------------------------------------------------
# Mirror predicate pin (regression for
# feedback_two_layer_window_math_mirror)
#
# Both layers must agree on the T3 handoff predicate
# (``days_since >= target_cadence_days``). If the compute path
# surfaces an item, the aggregator must suppress it; if the
# aggregator suppresses, the compute path must surface.
# ---------------------------------------------------------------------------


def test_mirror_decide_tier_handoff_t3_matches_compute_auto_t3(
    tmp_path: Path,
) -> None:
    """Side-by-side: identical inputs → identical decisions.

    For each of three fixture cases (overdue, exactly-at-boundary,
    within-window), assert that:
      * The aggregator's ``_decide_tier_handoff`` returns ``3`` iff
        the compute path produces a candidate.
      * The aggregator returns ``None`` iff the compute path skips.

    This is the canonical mirror pin per
    ``feedback_two_layer_window_math_mirror``. A future refactor
    that drifts one layer's predicate fires this test."""
    from datetime import date as _date

    from alfred.routine.aggregator import _decide_tier_handoff

    today = _date(2026, 5, 28)

    # --- Case 1: overdue (4 days since, target 3) → handoff + surface
    completion_log = {"X": ["2026-05-24"]}  # 4 days ago
    handoff = _decide_tier_handoff(
        due_pattern=None,
        surface_at_days=None,
        escalate_at_days=None,
        today=today,
        target_cadence_days=3,
        completion_log=completion_log,
        item_text="X",
        routine_record="Mirror Pin",
    )
    assert handoff == 3

    vault1 = _write_routine(
        tmp_path / "case1",
        "Mirror Pin.md",
        "type: routine\nstatus: active\nname: Mirror Pin\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  X:\n"
        "  - '2026-05-24'\n"
        "items:\n"
        "- text: X\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    candidates1 = compute_auto_t3_candidates(vault1, NOW)
    assert len(candidates1) == 1
    assert candidates1[0].item_text == "X"

    # --- Case 2: exactly at boundary (3 days since, target 3)
    completion_log_2 = {"X": ["2026-05-25"]}  # 3 days ago
    handoff_2 = _decide_tier_handoff(
        due_pattern=None,
        surface_at_days=None,
        escalate_at_days=None,
        today=today,
        target_cadence_days=3,
        completion_log=completion_log_2,
        item_text="X",
        routine_record="Mirror Pin",
    )
    assert handoff_2 == 3

    vault2 = _write_routine(
        tmp_path / "case2",
        "Mirror Pin.md",
        "type: routine\nstatus: active\nname: Mirror Pin\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  X:\n"
        "  - '2026-05-25'\n"
        "items:\n"
        "- text: X\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    candidates2 = compute_auto_t3_candidates(vault2, NOW)
    assert len(candidates2) == 1

    # --- Case 3: within window (2 days since, target 3) → both skip
    completion_log_3 = {"X": ["2026-05-26"]}  # 2 days ago
    handoff_3 = _decide_tier_handoff(
        due_pattern=None,
        surface_at_days=None,
        escalate_at_days=None,
        today=today,
        target_cadence_days=3,
        completion_log=completion_log_3,
        item_text="X",
        routine_record="Mirror Pin",
    )
    assert handoff_3 is None

    vault3 = _write_routine(
        tmp_path / "case3",
        "Mirror Pin.md",
        "type: routine\nstatus: active\nname: Mirror Pin\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  X:\n"
        "  - '2026-05-26'\n"
        "items:\n"
        "- text: X\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    candidates3 = compute_auto_t3_candidates(vault3, NOW)
    assert candidates3 == []

    # --- Case 4: never completed → handoff (treat as max overdue) +
    # surface (treat as max overdue).
    handoff_4 = _decide_tier_handoff(
        due_pattern=None,
        surface_at_days=None,
        escalate_at_days=None,
        today=today,
        target_cadence_days=3,
        completion_log={},
        item_text="X",
        routine_record="Mirror Pin",
    )
    assert handoff_4 == 3

    vault4 = _write_routine(
        tmp_path / "case4",
        "Mirror Pin.md",
        "type: routine\nstatus: active\nname: Mirror Pin\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: X\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    candidates4 = compute_auto_t3_candidates(vault4, NOW)
    assert len(candidates4) == 1
    assert candidates4[0].days_since_last_completed is None


# ===========================================================================
# Compute-side aspirational gate (2026-05-31) — mirror-discipline pin
# ===========================================================================
#
# Phase 2A-soft-cadence reviewer NOTE-1 (2026-05-30 review) flagged a
# latent asymmetry: routine/aggregator.py's _collect_items_for_today
# had an aspirational-priority gate on the hard-deadline T1/T2 handoff,
# but tier/compute.py's _compute_auto_routine did NOT. An aspirational
# item carrying due_pattern + escalate_at_days would:
#   - aggregator: skip handoff, item renders in routine section
#   - compute: STILL surface as AutoT1Candidate → brief renders it
#     in T1 candidates
# Result: double-render. Latent because no real records combine
# aspirational + hard cadence; fix shipped before a future record
# shape exercises the bug.
#
# Tests below pin the compute-side gate + the side-by-side mirror
# contract per feedback_two_layer_window_math_mirror.


def test_aspirational_routine_item_with_due_pattern_does_not_surface_in_t1(
    tmp_path: Path,
) -> None:
    """Compute-side regression pin: aspirational item with full
    hard-cadence fields (due_pattern + escalate_at_days) MUST NOT
    surface as AutoT1Candidate. Mirrors the aggregator's existing
    gate at _collect_items_for_today's should_check_handoff
    precondition.

    Pre-fix this test would have FAILED — the compute path lacked
    the aspirational gate and would emit the candidate despite the
    aggregator suppressing the routine-section render.

    Fixture: item with priority=aspirational + due_pattern that
    resolves into the T1 window (every_n_days n=1 anchor=today
    → due today → escalate_at_days=0 → in T1 window). Compute
    asserts zero candidates emitted from this item.
    """
    # NOW = 2026-05-28. anchor=today means due=today, which would
    # put days_to_due=0 — in the T1 window for escalate_at_days=0.
    # A non-aspirational item with this shape would surface; the
    # aspirational gate skips it.
    vault = _write_routine(
        tmp_path,
        "Aspirational With Deadline.md",
        "type: routine\nstatus: active\nname: Aspirational With Deadline\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Read for an hour\n"
        "  priority: aspirational\n"
        "  due_pattern:\n"
        "    type: every_n_days\n"
        "    n: 1\n"
        "    anchor: '2026-05-28'\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_candidates(vault, NOW)
    assert result == [], (
        "Aspirational item with hard-cadence fields MUST NOT surface "
        "as AutoT1Candidate — operator-stated semantic (T3 is for "
        "self-care, not deadline work). Mirror gate to "
        "_collect_items_for_today in routine/aggregator.py."
    )
    # Also verify the T2 ramp scan applies the same gate. Same
    # fixture, different window — `surface_at_days` would be
    # needed for a real T2 surface, but the gate fires BEFORE the
    # window check so this still pins the no-surface invariant.
    result_t2 = compute_auto_routine_t2_candidates(vault, NOW)
    assert result_t2 == [], (
        "Aspirational gate must apply to BOTH T1 and T2 routine "
        "scans — they share the _compute_auto_routine helper."
    )


def test_aspirational_routine_item_still_surfaces_in_t3_when_target_cadence_overdue(
    tmp_path: Path,
) -> None:
    """Counterpart to the above: the aspirational gate ONLY affects
    the hard-cadence T1/T2 surface. The soft-cadence T3 path
    (target_cadence_days → compute_auto_t3_candidates) IS the
    legitimate aspirational surface — the dog-walk / read-for-an-hour
    use case from Phase 2A-soft-cadence.

    Pin so a future regression that extends the aspirational gate to
    T3 too would silently break the operator-facing T3 auto-suggest
    contract. NOW = 2026-05-28; last completion 5 days ago; target
    3 days → overdue → must surface.
    """
    vault = _write_routine(
        tmp_path,
        "Aspirational Soft Cadence.md",
        "type: routine\nstatus: active\nname: Aspirational Soft Cadence\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  Walk dog:\n"
        "  - '2026-05-23'\n"
        "items:\n"
        "- text: Walk dog\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    result = compute_auto_t3_candidates(vault, NOW)
    assert len(result) == 1, (
        "Aspirational gate must NOT apply to the T3 soft-cadence "
        "surface — T3 is the legitimate aspirational surface (per "
        "Phase 2A-soft-cadence operator-stated semantic)."
    )
    assert result[0].item_text == "Walk dog"


def test_mirror_aspirational_t1_predicate_matches_aggregator(
    tmp_path: Path,
) -> None:
    """Side-by-side trace pin per feedback_two_layer_window_math_mirror:
    identical inputs → identical decisions across the two layers.

    Three cases exercise the mirror contract:
      Case A: aspirational + hard cadence → BOTH skip handoff (gate
              fires).
      Case B: tracked + hard cadence → BOTH proceed to handoff (gate
              doesn't fire; tracked is in-scope for T1/T2).
      Case C: aspirational + soft cadence → BOTH proceed via T3 path
              (gate is T1/T2-only; soft-cadence is the legitimate
              aspirational surface).

    For each case, the test asserts BOTH layers agree:
      * aggregator: _collect_items_for_today's should_check_handoff
        precondition. We exercise via the run_aggregator_once path
        (writes a daily file; we read it back to verify the item
        rendered/suppressed).
      * compute: _compute_auto_routine (T1) emits / skips the
        candidate.

    Drift between the layers would fire one half of the assertion
    while the other passes — the failure message names the gap.
    """
    from datetime import date as _date

    from alfred.routine.aggregator import run_aggregator_once
    from alfred.routine.config import RoutineConfig

    today = _date(2026, 5, 28)

    def _aggregator_renders_item_in_routine_section(
        case_dir: Path, item_text: str,
    ) -> bool:
        """Helper: run aggregator on case_dir's vault, return whether
        item_text appears in the body (rendered in routine section)."""
        vault_path = case_dir / "vault"
        config = RoutineConfig(
            vault_path=str(vault_path),
            instance_name="salem",
        )
        config.state.path = str(case_dir / "routine_state.json")
        run_aggregator_once(config, today)
        body = (vault_path / "daily" / "2026-05-28.md").read_text(
            encoding="utf-8",
        )
        return item_text in body

    # --- Case A: aspirational + hard cadence -------------------------
    # Both layers must SKIP the T1/T2 handoff. Aggregator: item
    # renders in routine section (Aspirational bucket). Compute:
    # zero AutoT1Candidates.
    case_a_dir = tmp_path / "case_a"
    case_a_dir.mkdir()
    vault_a = _write_routine(
        case_a_dir,
        "Mirror Pin.md",
        "type: routine\nstatus: active\nname: Mirror Pin\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: A_item\n"
        "  priority: aspirational\n"
        "  due_pattern:\n"
        "    type: every_n_days\n"
        "    n: 1\n"
        "    anchor: '2026-05-28'\n"
        "  escalate_at_days: 0\n",
    )
    compute_a = compute_auto_routine_candidates(vault_a, NOW)
    aggregator_a_rendered = (
        _aggregator_renders_item_in_routine_section(case_a_dir, "A_item")
    )
    assert compute_a == [], (
        "Case A (aspirational + hard cadence): compute MUST skip T1 "
        "candidate emission. Mirror gap with aggregator."
    )
    assert aggregator_a_rendered, (
        "Case A (aspirational + hard cadence): aggregator MUST render "
        "the item in routine section (no handoff). Mirror gap with "
        "compute."
    )

    # --- Case B: tracked + hard cadence ------------------------------
    # Both layers must PROCEED to handoff. Aggregator: item is
    # handed off (suppressed from routine section). Compute: emits
    # one AutoT1Candidate.
    case_b_dir = tmp_path / "case_b"
    case_b_dir.mkdir()
    vault_b = _write_routine(
        case_b_dir,
        "Mirror Pin.md",
        "type: routine\nstatus: active\nname: Mirror Pin\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: B_item\n"
        "  priority: tracked\n"
        "  due_pattern:\n"
        "    type: every_n_days\n"
        "    n: 1\n"
        "    anchor: '2026-05-28'\n"
        "  escalate_at_days: 0\n",
    )
    compute_b = compute_auto_routine_candidates(vault_b, NOW)
    aggregator_b_rendered = (
        _aggregator_renders_item_in_routine_section(case_b_dir, "B_item")
    )
    assert len(compute_b) == 1, (
        "Case B (tracked + hard cadence): compute MUST emit exactly "
        "one AutoT1Candidate. Mirror gap with aggregator."
    )
    assert not aggregator_b_rendered, (
        "Case B (tracked + hard cadence): aggregator MUST suppress "
        "the item from routine section (handoff to tier). Mirror gap "
        "with compute."
    )

    # --- Case C: aspirational + soft cadence -------------------------
    # T1/T2 gate is the FIRST check in compute (lives at the top of
    # the per-item loop, before the due_pattern check); aspirational
    # items WITHOUT due_pattern would never reach that gate, but
    # WITH due_pattern they're filtered. For the T3 path
    # (compute_auto_t3_candidates), aspirational items ARE allowed
    # — the soft-cadence surface is the legitimate aspirational
    # path. Aggregator: hands off to T3 (suppresses from routine
    # section). Compute: emits AutoT3Candidate.
    case_c_dir = tmp_path / "case_c"
    case_c_dir.mkdir()
    vault_c = _write_routine(
        case_c_dir,
        "Mirror Pin.md",
        "type: routine\nstatus: active\nname: Mirror Pin\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  C_item:\n"
        "  - '2026-05-23'\n"  # 5 days ago → overdue vs target 3
        "items:\n"
        "- text: C_item\n"
        "  priority: aspirational\n"
        "  target_cadence_days: 3\n",
    )
    compute_c_t1 = compute_auto_routine_candidates(vault_c, NOW)
    compute_c_t3 = compute_auto_t3_candidates(vault_c, NOW)
    aggregator_c_rendered = (
        _aggregator_renders_item_in_routine_section(case_c_dir, "C_item")
    )
    assert compute_c_t1 == [], (
        "Case C (aspirational + soft cadence): T1 scan MUST skip "
        "(no due_pattern at all → no hard-cadence handoff)."
    )
    assert len(compute_c_t3) == 1, (
        "Case C (aspirational + soft cadence): T3 compute MUST emit "
        "AutoT3Candidate. The soft-cadence path IS the legitimate "
        "aspirational surface."
    )
    assert not aggregator_c_rendered, (
        "Case C (aspirational + soft cadence): aggregator MUST "
        "suppress from routine section (handed off to T3 per the "
        "Phase 2A-soft-cadence contract)."
    )


# ===========================================================================
# Phase 2C C1 (2026-06-01) — completion-aware predicate mirror
# ===========================================================================
#
# Operator bug 2026-06-01: Pay Clinic Rental (monthly day=1) marked
# complete May 29, still auto-surfaced T1 on June 1's brief.
# ``_compute_auto_routine`` historically used ``is_done_in_current_
# cycle`` (calendar-month window), which misses cross-month-boundary
# completions like May 29 → June 1. Replaced with the new
# ``completion_satisfies_current_cycle`` nearest-cycle ±half-cycle
# helper which catches the bug case.
#
# Symmetric concern: missed-deadline retention via
# ``overdue_effective_due`` — same shape, same gate-order as the
# aggregator's ``_decide_tier_handoff``.
#
# The mirror test below pins side-by-side behavior: both layers
# return the same decision (handoff vs skip) on identical fixtures.


def test_auto_routine_suppressed_when_completion_within_half_cycle_before_due(
    tmp_path: Path,
) -> None:
    """Operator bug repro at the compute layer: monthly day=1,
    completion May 29, today June 1 → suppress."""
    # NOW = 2026-05-28; for this test we need today=2026-06-01.
    later_now = datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
    vault = _write_routine(
        tmp_path,
        "Recurring Bills.md",
        "type: routine\nstatus: active\nname: Recurring Bills\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  Pay Clinic Rental:\n"
        "  - '2026-05-29'\n"
        "items:\n"
        "- text: Pay Clinic Rental\n"
        "  priority: critical\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 1\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_candidates(vault, later_now)
    assert result == [], (
        f"Pay Clinic Rental auto-surfaced despite May 29 completion. "
        f"Got: {[c.name for c in result]}. Pre-2C-C1 bug regressing?"
    )


def test_auto_routine_surfaces_without_completion(tmp_path: Path) -> None:
    """No completion log → existing window math applies → surfaces.
    Regression pin: the new helper must not over-suppress when no
    completion exists."""
    later_now = datetime(2026, 6, 1, 13, 0, 0, tzinfo=timezone.utc)
    vault = _write_routine(
        tmp_path,
        "Recurring Bills.md",
        "type: routine\nstatus: active\nname: Recurring Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay Clinic Rental\n"
        "  priority: critical\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 1\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_candidates(vault, later_now)
    assert len(result) == 1
    assert result[0].name == "Pay Clinic Rental"
    assert result[0].surface_reason == "due today"


def test_auto_routine_overdue_retention(tmp_path: Path) -> None:
    """Missed-deadline retention: monthly day=15, today June 17, no
    completion → effective_due=June 15, days_to_due=-2 → T1 with
    overdue reason. Pre-2C-C1: silently dropped (resolver rolled to
    July 15, days_to_due=28, out of window)."""
    overdue_now = datetime(2026, 6, 17, 13, 0, 0, tzinfo=timezone.utc)
    vault = _write_routine(
        tmp_path,
        "Mid Month Bills.md",
        "type: routine\nstatus: active\nname: Mid Month Bills\n"
        "cadence:\n  type: daily\n"
        "items:\n"
        "- text: Pay credit card\n"
        "  priority: critical\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 15\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_candidates(vault, overdue_now)
    assert len(result) == 1
    c = result[0]
    assert c.name == "Pay credit card"
    assert c.due_iso == "2026-06-15", (
        f"effective_due should be prev_due=June 15, got {c.due_iso}"
    )
    # surface_reason names the overdue case for operator log review.
    assert "overdue by 2d" in c.surface_reason


def test_auto_routine_overdue_retention_suppressed_when_completed(
    tmp_path: Path,
) -> None:
    """When operator HAS completed within ±half_cycle of prev_due,
    overdue retention does NOT fire — returns current_due (next
    cycle's date) which is far out → not in T1 window."""
    overdue_now = datetime(2026, 6, 17, 13, 0, 0, tzinfo=timezone.utc)
    vault = _write_routine(
        tmp_path,
        "Mid Month Bills.md",
        "type: routine\nstatus: active\nname: Mid Month Bills\n"
        "cadence:\n  type: daily\n"
        "completion_log:\n"
        "  Pay credit card:\n"
        "  - '2026-06-12'\n"
        "items:\n"
        "- text: Pay credit card\n"
        "  priority: critical\n"
        "  due_pattern:\n"
        "    type: monthly\n"
        "    day: 15\n"
        "  escalate_at_days: 0\n",
    )
    result = compute_auto_routine_candidates(vault, overdue_now)
    assert result == [], (
        f"Pay credit card should NOT surface — June 12 completion "
        f"covers prev cycle (June 15). Got: {[c.name for c in result]}"
    )


_MIRROR_CASES = [
    # (case_id, item_text, due_pattern_kwargs, completion_log,
    #  today, expected_handoff_tier, expected_compute_count)
    # Case A — operator bug repro: Pay Clinic monthly day=1,
    # completion May 29, today June 1 → SUPPRESS at both layers.
    (
        "A_suppress_via_recent_completion",
        "Pay Clinic Rental",
        {"type": "monthly", "day": 1},
        {"Pay Clinic Rental": [date(2026, 5, 29)]},
        date(2026, 6, 1),
        None,   # aggregator returns None (no handoff)
        0,      # compute returns empty list (no candidate)
    ),
    # Case B — no completion: monthly day=1, today June 1, empty log
    # → SURFACE T1 at both layers ('due today').
    (
        "B_no_completion_surfaces_t1",
        "Pay Clinic Rental",
        {"type": "monthly", "day": 1},
        {},
        date(2026, 6, 1),
        1,      # T1
        1,      # one candidate
    ),
    # Case C — overdue retention: monthly day=15, today June 17, no
    # completion → effective_due=June 15, days_to_due=-2 → T1 at
    # both layers.
    (
        "C_overdue_retention",
        "Pay credit card",
        {"type": "monthly", "day": 15},
        {},
        date(2026, 6, 17),
        1,      # T1 (overdue retention)
        1,      # one candidate
    ),
    # Case D — completion too old: monthly day=1, completion Apr 17
    # (covers prev cycle May 1, NOT current June 1), today June 1 →
    # SURFACE T1 at both layers (completion did NOT cover current
    # cycle; overdue retention skipped because prev cycle WAS
    # covered).
    (
        "D_completion_too_old_for_current_cycle",
        "Pay Clinic Rental",
        {"type": "monthly", "day": 1},
        {"Pay Clinic Rental": [date(2026, 4, 17)]},
        date(2026, 6, 1),
        1,      # T1 'due today'
        1,      # one candidate
    ),
]


@pytest.mark.parametrize(
    "case_id,item_text,due_pattern_kwargs,completion_log,today,"
    "expected_handoff_tier,expected_compute_count",
    _MIRROR_CASES,
    ids=[c[0] for c in _MIRROR_CASES],
)
def test_mirror_completion_predicate_aggregator_matches_compute(
    tmp_path: Path,
    case_id: str,
    item_text: str,
    due_pattern_kwargs: dict,
    completion_log: dict,
    today: date,
    expected_handoff_tier: int | None,
    expected_compute_count: int,
) -> None:
    """Side-by-side mirror: identical fixture → both layers return
    the same decision. Pins the
    ``feedback_two_layer_window_math_mirror`` contract for the new
    completion-aware gates.

    Four cases (matches the dispatch's trace table):

      * A — suppress via recent completion (operator bug repro)
      * B — no completion, surfaces T1
      * C — overdue retention (missed deadline → prev_due as
        effective_due → T1 with negative days_to_due)
      * D — completion too old to cover current cycle → surfaces T1

    Each case asserts BOTH layers reach the same decision. Drift
    here would silently break the mirror contract (one layer
    suppresses, the other surfaces).
    """
    from alfred.routine.aggregator import _decide_tier_handoff
    from alfred.routine.config import DuePattern

    # Build the routine record YAML for the compute side.
    if completion_log:
        completion_log_yaml = (
            "completion_log:\n"
            + "\n".join(
                f"  {k}:\n"
                + "\n".join(f"  - '{d.isoformat()}'" for d in v)
                for k, v in completion_log.items()
            )
            + "\n"
        )
    else:
        completion_log_yaml = ""

    # Render due_pattern as YAML.
    dp_yaml = "  due_pattern:\n"
    for k, v in due_pattern_kwargs.items():
        dp_yaml += f"    {k}: {v}\n"

    fm_yaml = (
        "type: routine\nstatus: active\nname: Recurring Bills\n"
        "cadence:\n  type: daily\n"
        + completion_log_yaml
        + "items:\n"
        + f"- text: {item_text}\n"
        + "  priority: critical\n"
        + dp_yaml
        + "  escalate_at_days: 0\n"
    )

    # Compute side — use a per-case tmp subdirectory so parametrized
    # cases don't collide on the shared tmp_path.
    case_root = tmp_path / case_id
    case_root.mkdir()
    later_now = datetime(
        today.year, today.month, today.day, 13, 0, 0,
        tzinfo=timezone.utc,
    )
    vault_compute = _write_routine(
        case_root, "Recurring Bills.md", fm_yaml,
    )
    compute_result = compute_auto_routine_candidates(vault_compute, later_now)

    # Aggregator side — direct predicate invocation with the same args.
    agg_result = _decide_tier_handoff(
        due_pattern=DuePattern(**due_pattern_kwargs),
        surface_at_days=None,
        escalate_at_days=0,
        today=today,
        completion_log=completion_log,
        item_text=item_text,
        routine_record="Recurring Bills",
    )

    # Mirror assertion: both layers' decisions must agree.
    assert agg_result == expected_handoff_tier, (
        f"[{case_id}] aggregator returned tier {agg_result}, "
        f"expected {expected_handoff_tier}. Mirror broken on "
        f"aggregator side."
    )
    assert len(compute_result) == expected_compute_count, (
        f"[{case_id}] compute returned {len(compute_result)} "
        f"candidates, expected {expected_compute_count}. Mirror "
        f"broken on compute side. Got: "
        f"{[c.name for c in compute_result]}"
    )
