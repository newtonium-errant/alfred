"""Clinic-capture Piece 3 — the local Captured Tasks view.

Reads ``task/*.md`` where ``created_by_capture: true`` and writes
``process/Captured Tasks.md`` — LOCAL only, no push. Pins the filter (only
capture tasks), the ILB empty-state, and the atomic write.
"""

from __future__ import annotations

from pathlib import Path

from alfred.telegram.captured_tasks_view import (
    CAPTURED_TASKS_VIEW_REL,
    regenerate_captured_tasks_view,
    render_captured_tasks_view,
)


def _write_task(vault: Path, stem: str, *, capture: bool, **fm) -> None:
    (vault / "task").mkdir(parents=True, exist_ok=True)
    lines = ["---", "type: task"]
    if capture:
        lines.append("created_by_capture: true")
    for k, v in fm.items():
        lines.append(f"{k}: {v}")
    lines += ["---", "", "# body", ""]
    (vault / "task" / f"{stem}.md").write_text("\n".join(lines), encoding="utf-8")


def test_view_lists_only_capture_tasks(tmp_path: Path) -> None:
    """Only ``created_by_capture: true`` tasks appear; a hand-written task is
    excluded. Mutation: drop the created_by_capture filter → the manual task
    leaks in → fails."""
    _write_task(tmp_path, "captured-one", capture=True, status="todo",
                name="Write the note", capture_confidence="unverified")
    _write_task(tmp_path, "manual-task", capture=False, status="todo",
                name="Hand written")

    md = render_captured_tasks_view(tmp_path)
    assert "[[task/captured-one]]" in md
    assert "manual-task" not in md
    assert "item_count: 1" in md


def test_view_empty_state_is_explicit(tmp_path: Path) -> None:
    """No captured tasks → an explicit ``(none)`` (ILB), not a blank/absent
    section."""
    (tmp_path / "task").mkdir()
    md = render_captured_tasks_view(tmp_path)
    assert "item_count: 0" in md
    assert "## Open\n(none)" in md


def test_view_no_task_dir_is_empty_not_crash(tmp_path: Path) -> None:
    md = render_captured_tasks_view(tmp_path)          # no task/ dir at all
    assert "item_count: 0" in md


def test_regenerate_writes_process_view(tmp_path: Path) -> None:
    """regenerate writes ``process/Captured Tasks.md`` atomically."""
    _write_task(tmp_path, "cap", capture=True, status="todo", name="Do X")
    assert regenerate_captured_tasks_view(tmp_path) is True
    out = tmp_path / CAPTURED_TASKS_VIEW_REL
    assert out.exists()
    assert "[[task/cap]]" in out.read_text()
    # No stray .tmp left behind (atomic rename completed).
    assert not (out.parent / (out.name + ".tmp")).exists()
