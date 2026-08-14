"""Returned snoozes and waiting-chases — the READ half of Phase 2c+h.

The scheduler fires a reminder and writes the ruled ``slot:`` onto the
record. Before this lane nothing ever read it back: no module outside
``transport/`` touched ``remind_at``/``reminded_at``, and an undated task
could not become a deck candidate by any path. Four of the six live
carriers have no due date, so their returns had nowhere to land.

These tests cover the predicate, the read-time escalation, and — the one
that matters most — that the reader is actually THREADED into
``compute_today_view`` rather than merely existing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from alfred.tier.compute import (
    compute_returned_task_candidates,
    compute_today_view,
)

NOW = datetime(2026, 8, 21, 13, 0, 0, tzinfo=timezone.utc)
FIRED = "2026-08-21T09:00:00+00:00"


def _task(
    task_dir: Path,
    name: str,
    *,
    status: str = "todo",
    reminded_at: str | None = FIRED,
    remind_at: str | None = None,
    extra: str = "",
) -> Path:
    lines = [
        "type: task",
        f"name: {name}",
        f"status: {status}",
        "created: 2026-08-01",
    ]
    if reminded_at is not None:
        lines.append(f'reminded_at: "{reminded_at}"')
    if remind_at is not None:
        lines.append(f'remind_at: "{remind_at}"')
    if extra:
        lines.append(extra)
    path = task_dir / f"{name}.md"
    path.write_text(
        "---\n" + "\n".join(lines) + f"\n---\n\n# {name}\n\nBody.\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    (tmp_path / "task").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


def test_returned_task_surfaces(vault: Path) -> None:
    _task(vault / "task", "Pay MBF", extra='return_slot: "duty"')
    got = compute_returned_task_candidates(vault, NOW)
    assert [c.name for c in got] == ["Pay MBF"]
    assert got[0].explicit_slot == "duty"
    assert got[0].surface_reason == "returned"


def test_completed_task_does_not_surface(vault: Path) -> None:
    """Completing it is one of the three existing exits from returned-state."""
    _task(vault / "task", "Pay MBF", status="done", extra='return_slot: "duty"')
    assert compute_returned_task_candidates(vault, NOW) == []


def test_re_armed_task_does_not_surface(vault: Path) -> None:
    """Re-snoozing sets remind_at — it is a pending reminder again, not a
    return. The scheduler's re-snooze signal covers the correction side."""
    _task(
        vault / "task", "Pay MBF",
        remind_at="2026-09-01T09:00:00+00:00",
        extra='return_slot: "duty"',
    )
    assert compute_returned_task_candidates(vault, NOW) == []


def test_never_reminded_task_does_not_surface(vault: Path) -> None:
    """Positive control lives in the same vault: an ordinary open task with
    no reminder history must not be swept up, while a genuine return in the
    same scan still is. Without the second record this would pass against a
    reader that returns nothing at all."""
    _task(vault / "task", "Ordinary task", reminded_at=None)
    _task(vault / "task", "Genuine return", extra='return_slot: "duty"')
    assert [c.name for c in compute_returned_task_candidates(vault, NOW)] == [
        "Genuine return"
    ]


def test_non_task_records_ignored(vault: Path) -> None:
    (vault / "task" / "not-a-task.md").write_text(
        '---\ntype: note\nname: Note\nstatus: todo\n'
        f'reminded_at: "{FIRED}"\n---\n\nBody.\n',
        encoding="utf-8",
    )
    assert compute_returned_task_candidates(vault, NOW) == []


# ---------------------------------------------------------------------------
# Slot + escalation at READ time
# ---------------------------------------------------------------------------


def test_operator_routine_wording_resolves_to_rhythm(vault: Path) -> None:
    _task(vault / "task", "TheJamieClinic", extra='return_slot: "routine"')
    assert compute_returned_task_candidates(vault, NOW)[0].explicit_slot == (
        "rhythm"
    )


def test_escalation_applies_when_boundary_passes_after_the_fire(
    vault: Path,
) -> None:
    """The septic case, which fire-time escalation cannot reach.

    Septic fires 2029-06-01 as rhythm; its escalation boundary is
    2029-09-01, three months later, while the task sits in returned-state.
    Only a read-time computation can notice that.
    """
    _task(
        vault / "task", "Pump Septic Tank",
        extra=(
            'return_slot: "routine"\n'
            'escalate_on: "2029-09-01"\n'
            'escalate_to: "duty"'
        ),
    )
    after = datetime(2029, 9, 2, 12, 0, tzinfo=timezone.utc)
    assert compute_returned_task_candidates(vault, after)[0].explicit_slot == (
        "duty"
    )


def test_no_escalation_before_the_boundary(vault: Path) -> None:
    """Positive control for the escalation pin — the SAME record before its
    date keeps its ordinary slot, proving the date is what fires rather
    than the mere presence of the fields."""
    _task(
        vault / "task", "Pump Septic Tank",
        extra=(
            'return_slot: "routine"\n'
            'escalate_on: "2029-09-01"\n'
            'escalate_to: "duty"'
        ),
    )
    before = datetime(2029, 6, 2, 12, 0, tzinfo=timezone.utc)
    assert compute_returned_task_candidates(vault, before)[0].explicit_slot == (
        "rhythm"
    )


def test_waiting_item_frames_as_chase(vault: Path) -> None:
    """Record-side chase framing: the surface reason says what the next
    physical action is, and carries no invented slot."""
    _task(vault / "task", "Fix Carfax mileage", extra='waiting_on: "Carfax"')
    got = compute_returned_task_candidates(vault, NOW)[0]
    assert got.surface_reason == "chase Carfax"
    assert got.explicit_slot is None


# ---------------------------------------------------------------------------
# THREADING — the reader must be wired into the production entry point
# ---------------------------------------------------------------------------


def test_returned_task_reaches_today_view_t1(vault: Path) -> None:
    """The pin that matters. A reader nobody calls is the same
    write-live/read-dead failure it was built to fix, one layer up — and
    every unit test above would still be green.
    """
    _task(vault / "task", "Pay MBF", extra='return_slot: "duty"')

    view = compute_today_view(vault, NOW)

    names = [e.name for e in view.t1]
    assert "Pay MBF" in names
    entry = next(e for e in view.t1 if e.name == "Pay MBF")
    assert entry.source == "auto-returned"
    assert entry.surface_reason == "returned"
    assert entry.origin == "task"


def test_returned_task_carries_its_ruled_slot_through_today_view(
    vault: Path,
) -> None:
    """End to end: the operator's ruling reaches the day plan's slot axis
    via rule 1 of the classifier."""
    _task(vault / "task", "Pay MBF", extra='return_slot: "duty"')
    _task(vault / "task", "TheJamieClinic", extra='return_slot: "routine"')

    view = compute_today_view(vault, NOW)
    by_name = {e.name: e for e in view.t1}
    assert by_name["Pay MBF"].slot == "duty"
    assert by_name["TheJamieClinic"].slot == "rhythm"


def test_waiting_chase_reaches_today_view(vault: Path) -> None:
    _task(vault / "task", "Fix Carfax mileage", extra='waiting_on: "Carfax"')
    view = compute_today_view(vault, NOW)
    entry = next(e for e in view.t1 if e.name == "Fix Carfax mileage")
    assert entry.surface_reason == "chase Carfax"
    assert entry.source == "auto-returned"


def test_returned_task_that_is_also_due_today_surfaces_once(
    vault: Path,
) -> None:
    """Dedup against the auto-due block: the deadline framing wins and the
    task appears exactly once rather than twice."""
    today_iso = NOW.date().isoformat()
    _task(
        vault / "task", "Pay MBF",
        extra=f'return_slot: "duty"\ndue: "{today_iso}"',
    )

    view = compute_today_view(vault, NOW)
    matches = [e for e in view.t1 if e.name == "Pay MBF"]
    assert len(matches) == 1
    assert matches[0].source == "auto-due"
    assert matches[0].surface_reason == "due today"


def test_today_view_without_returns_is_unaffected(vault: Path) -> None:
    """Negative control on the threading: adding the reader must not
    invent T1 entries on a vault that has no returns."""
    _task(vault / "task", "Ordinary task", reminded_at=None)
    view = compute_today_view(vault, NOW)
    assert [e for e in view.t1 if e.source == "auto-returned"] == []
