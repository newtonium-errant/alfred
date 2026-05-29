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

from datetime import datetime, timezone
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
