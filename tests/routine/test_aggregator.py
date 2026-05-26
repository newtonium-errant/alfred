"""Aggregator tests — routine record scanning + daily note rendering.

The aggregator is the second load-bearing primitive (after cadence).
Walks ``<vault>/routine/`` for active records, groups items by priority,
de-duplicates by text, annotates tracked items with gap-since-last-
completion, and writes ``<vault>/daily/<date>.md``.

Test surface (per dispatch):
  - 3 routine records (daily, weekly+today, weekly+not-today): the
    not-today record contributes nothing.
  - Dedup-by-text: two routines listing the same item text — first
    occurrence wins.
  - Tracked annotation: gap > warn_after_gap_days emits the threshold
    callout; within threshold emits plain N-days; aspirational gets
    nothing; no log entries → "no completions yet".
  - Intentionally-left-blank: empty routine/ dir, no records due today,
    no records at all — all three paths emit a structured log signal.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from textwrap import dedent

import frontmatter  # type: ignore[import-untyped]
import pytest
import structlog
import yaml

from alfred.routine.aggregator import (
    DEFAULT_TRACKED_GAP_DAYS,
    render_daily_body,
    run_aggregator_once,
)
from alfred.routine.config import RoutineConfig
from alfred.routine.state import StateManager


# ---------------------------------------------------------------------------
# Helpers — fixture builders
# ---------------------------------------------------------------------------


def _write_routine(vault_path: Path, name: str, payload: dict) -> Path:
    """Write a ``routine/<name>.md`` record with the given frontmatter."""
    routine_dir = vault_path / "routine"
    routine_dir.mkdir(parents=True, exist_ok=True)
    fm_str = yaml.dump(payload, default_flow_style=False, sort_keys=False)
    path = routine_dir / f"{name}.md"
    path.write_text(f"---\n{fm_str}---\n\n# {name}\n", encoding="utf-8")
    return path


def _config(vault_path: Path, tmp_path: Path) -> RoutineConfig:
    """Build a RoutineConfig pointed at the test vault."""
    config = RoutineConfig(
        vault_path=str(vault_path),
        instance_name="salem",
    )
    config.state.path = str(tmp_path / "routine_state.json")
    return config


# ---------------------------------------------------------------------------
# 1. Three records: one daily, one weekly+today, one weekly+not-today
# ---------------------------------------------------------------------------


def test_three_routines_only_matching_records_contribute(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    # 2026-05-26 is a Tuesday.
    today = date(2026, 5, 26)

    _write_routine(vault, "Core Daily", {
        "type": "routine",
        "status": "active",
        "name": "Core Daily",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Brush Teeth AM", "priority": "tracked"},
            {"text": "Reading for pleasure", "priority": "aspirational"},
        ],
    })

    _write_routine(vault, "Tuesday Things", {
        "type": "routine",
        "status": "active",
        "name": "Tuesday Things",
        "cadence": {"type": "weekly", "days": ["Tue"]},
        "items": [
            {"text": "Garbage Out", "priority": "tracked"},
        ],
    })

    _write_routine(vault, "Friday Only", {
        "type": "routine",
        "status": "active",
        "name": "Friday Only",
        "cadence": {"type": "weekly", "days": ["Fri"]},
        "items": [
            {"text": "Should not appear", "priority": "tracked"},
        ],
    })

    config = _config(vault, tmp_path)
    path = run_aggregator_once(config, today)

    daily_file = vault / path
    assert daily_file.exists()

    post = frontmatter.load(str(daily_file))
    fm = dict(post.metadata)
    assert fm["type"] == "daily"
    assert fm["date"] == "2026-05-26"
    assert fm["routines_contributing"] == ["Core Daily", "Tuesday Things"]
    body = post.content
    assert "Brush Teeth AM" in body
    assert "Reading for pleasure" in body
    assert "Garbage Out" in body
    assert "Should not appear" not in body


def test_archived_routines_skipped(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Active One", {
        "type": "routine",
        "status": "active",
        "name": "Active One",
        "cadence": {"type": "daily"},
        "items": [{"text": "Visible Item", "priority": "tracked"}],
    })

    _write_routine(vault, "Archived One", {
        "type": "routine",
        "status": "archived",
        "name": "Archived One",
        "cadence": {"type": "daily"},
        "items": [{"text": "Hidden Item", "priority": "tracked"}],
    })

    config = _config(vault, tmp_path)
    path = run_aggregator_once(config, today)
    body = (vault / path).read_text(encoding="utf-8")
    assert "Visible Item" in body
    assert "Hidden Item" not in body


# ---------------------------------------------------------------------------
# 2. Dedup by text
# ---------------------------------------------------------------------------


def test_duplicate_text_first_occurrence_wins(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    # ``Aaa First`` is alphabetically before ``Bbb Second`` — sorted iter
    # walks ``Aaa First.md`` first, so its priority sticks.
    _write_routine(vault, "Aaa First", {
        "type": "routine",
        "name": "Aaa First",
        "cadence": {"type": "daily"},
        "items": [{"text": "Shared Habit", "priority": "critical"}],
    })

    _write_routine(vault, "Bbb Second", {
        "type": "routine",
        "name": "Bbb Second",
        "cadence": {"type": "daily"},
        "items": [{"text": "Shared Habit", "priority": "aspirational"}],
    })

    config = _config(vault, tmp_path)
    path = run_aggregator_once(config, today)
    text = (vault / path).read_text(encoding="utf-8")
    # Split off the YAML frontmatter — "Shared Habit" legitimately
    # appears in frontmatter ``critical_pending`` AND in the body
    # checklist. We're testing body-side dedup.
    _, _, body = text.partition("---\n\n")

    # Shared Habit should appear under Critical (first-occurrence wins),
    # NOT under Aspirational.
    critical_section, _, rest = body.partition("## Tracked")
    assert "Shared Habit" in critical_section
    aspirational_section = rest.split("## Aspirational")[-1] if "## Aspirational" in rest else ""
    assert "Shared Habit" not in aspirational_section
    # Should appear exactly once in the body (the dedupped checklist line).
    assert body.count("Shared Habit") == 1


# ---------------------------------------------------------------------------
# 3. Tracked annotation rules
# ---------------------------------------------------------------------------


def test_tracked_gap_exceeds_threshold_emits_callout(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Gap Test", {
        "type": "routine",
        "name": "Gap Test",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Dog Walk",
                "priority": "tracked",
                "warn_after_gap_days": 3,
            },
        ],
        # Last walked 4 days ago — past the 3-day threshold.
        "completion_log": {"Dog Walk": ["2026-05-22"]},
    })

    config = _config(vault, tmp_path)
    path = run_aggregator_once(config, today)
    body = (vault / path).read_text(encoding="utf-8")
    assert "Dog Walk" in body
    assert "4 days ago" in body
    assert "past 3-day threshold" in body


def test_tracked_gap_within_threshold_no_callout(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Recent", {
        "type": "routine",
        "name": "Recent",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Brush Teeth AM",
                "priority": "tracked",
                "warn_after_gap_days": 3,
            },
        ],
        # Yesterday.
        "completion_log": {"Brush Teeth AM": ["2026-05-25"]},
    })

    config = _config(vault, tmp_path)
    path = run_aggregator_once(config, today)
    body = (vault / path).read_text(encoding="utf-8")
    assert "1 days ago" in body
    assert "threshold" not in body


def test_tracked_no_completions_yet(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Fresh", {
        "type": "routine",
        "name": "Fresh",
        "cadence": {"type": "daily"},
        "items": [{"text": "New Habit", "priority": "tracked"}],
    })

    config = _config(vault, tmp_path)
    path = run_aggregator_once(config, today)
    body = (vault / path).read_text(encoding="utf-8")
    assert "no completions yet" in body


def test_aspirational_gets_no_annotation(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Aspire", {
        "type": "routine",
        "name": "Aspire",
        "cadence": {"type": "daily"},
        "items": [{"text": "Long Writing", "priority": "aspirational"}],
        "completion_log": {"Long Writing": ["2026-01-01"]},
    })

    config = _config(vault, tmp_path)
    path = run_aggregator_once(config, today)
    body = (vault / path).read_text(encoding="utf-8")
    # No "ago" annotation for aspirational items.
    assert "Long Writing" in body
    assert "days ago" not in body


def test_default_warn_gap_days_is_5(tmp_path: Path) -> None:
    """When ``warn_after_gap_days`` is omitted, default is 5."""
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Default", {
        "type": "routine",
        "name": "Default",
        "cadence": {"type": "daily"},
        "items": [{"text": "Item", "priority": "tracked"}],
        "completion_log": {"Item": ["2026-05-20"]},   # 6 days ago
    })

    config = _config(vault, tmp_path)
    path = run_aggregator_once(config, today)
    body = (vault / path).read_text(encoding="utf-8")
    assert "6 days ago" in body
    assert f"past {DEFAULT_TRACKED_GAP_DAYS}-day threshold" in body


# ---------------------------------------------------------------------------
# 4. Critical pending in frontmatter + time annotation
# ---------------------------------------------------------------------------


def test_critical_pending_in_frontmatter(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Critical", {
        "type": "routine",
        "name": "Critical",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Kiki Insulin", "priority": "critical", "time": "12:00"},
            {"text": "Red Pill Pippin", "priority": "critical", "time": "16:00"},
        ],
    })

    config = _config(vault, tmp_path)
    path = run_aggregator_once(config, today)
    post = frontmatter.load(str(vault / path))
    critical_pending = list(post.metadata["critical_pending"])
    assert critical_pending == [
        "Kiki Insulin @ 12:00",
        "Red Pill Pippin @ 16:00",
    ]
    body = post.content
    assert "- [ ] Kiki Insulin @ 12:00" in body
    assert "- [ ] Red Pill Pippin @ 16:00" in body


# ---------------------------------------------------------------------------
# 5. Intentionally-left-blank paths
# ---------------------------------------------------------------------------


def test_no_routine_dir_emits_signal(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    today = date(2026, 5, 26)
    config = _config(vault, tmp_path)

    with structlog.testing.capture_logs() as captured:
        path = run_aggregator_once(config, today)

    events = [c.get("event") for c in captured]
    assert "routine.aggregator.no_routine_dir" in events

    # File should STILL be written with the empty-state body.
    daily = vault / path
    assert daily.exists()
    body = daily.read_text(encoding="utf-8")
    assert "no routines due today" in body
    assert "## Critical" in body
    assert "## Tracked" in body
    assert "## Aspirational" in body


def test_no_routines_due_today_emits_signal(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)   # Tuesday

    # Routine fires only on Sundays.
    _write_routine(vault, "Sunday Only", {
        "type": "routine",
        "name": "Sunday Only",
        "cadence": {"type": "weekly", "days": ["Sun"]},
        "items": [{"text": "Sabbath Read", "priority": "aspirational"}],
    })

    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as captured:
        path = run_aggregator_once(config, today)

    events = [c.get("event") for c in captured]
    assert "routine.aggregator.no_routines_due_today" in events
    matches = [c for c in captured
               if c.get("event") == "routine.aggregator.no_routines_due_today"]
    assert matches[0].get("scanned") == 1

    body = (vault / path).read_text(encoding="utf-8")
    assert "no routines due today" in body


def test_aggregator_emits_written_log_event(tmp_path: Path) -> None:
    """Pin the canonical written event — emission is operator-grep-able."""
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Single", {
        "type": "routine",
        "name": "Single",
        "cadence": {"type": "daily"},
        "items": [{"text": "An Item", "priority": "tracked"}],
    })

    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as captured:
        run_aggregator_once(config, today)

    matches = [c for c in captured
               if c.get("event") == "routine.aggregator.written"]
    assert len(matches) == 1
    assert matches[0].get("item_count") == 1
    assert matches[0].get("routines_contributing") == ["Single"]


# ---------------------------------------------------------------------------
# 6. Malformed input survives — error logged, sweep continues
# ---------------------------------------------------------------------------


def test_malformed_cadence_logged_and_skipped(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Good", {
        "type": "routine",
        "name": "Good",
        "cadence": {"type": "daily"},
        "items": [{"text": "Good Item", "priority": "tracked"}],
    })

    _write_routine(vault, "Bad", {
        "type": "routine",
        "name": "Bad",
        "cadence": {"type": "fortnightly"},   # not a valid type
        "items": [{"text": "Bad Item", "priority": "tracked"}],
    })

    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as captured:
        path = run_aggregator_once(config, today)

    matches = [c for c in captured
               if c.get("event") == "routine.aggregator.malformed_cadence"]
    assert len(matches) == 1
    assert matches[0].get("name") == "Bad"

    body = (vault / path).read_text(encoding="utf-8")
    assert "Good Item" in body
    assert "Bad Item" not in body


def test_items_not_list_logged_and_skipped(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Malformed", {
        "type": "routine",
        "name": "Malformed",
        "cadence": {"type": "daily"},
        "items": "this should be a list",
    })

    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as captured:
        run_aggregator_once(config, today)

    matches = [c for c in captured
               if c.get("event") == "routine.aggregator.items_not_list"]
    assert len(matches) == 1


# ---------------------------------------------------------------------------
# 7. State recording
# ---------------------------------------------------------------------------


def test_state_records_run(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "R1", {
        "type": "routine",
        "name": "R1",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "i1", "priority": "tracked"},
            {"text": "i2", "priority": "critical", "time": "08:00"},
        ],
    })

    config = _config(vault, tmp_path)
    state_mgr = StateManager(config.state.path)
    state_mgr.load()
    run_aggregator_once(config, today, state_mgr)

    state_mgr2 = StateManager(config.state.path)
    state_mgr2.load()
    latest = state_mgr2.state.latest()
    assert latest is not None
    assert latest.date == "2026-05-26"
    assert latest.routines_contributing == ["R1"]
    assert latest.item_count == 2
    assert latest.critical_pending == 1


# ---------------------------------------------------------------------------
# 8. render_daily_body — direct unit test
# ---------------------------------------------------------------------------


def test_render_daily_body_empty_state_has_three_headers() -> None:
    body = render_daily_body([], no_routines_overall=True)
    assert "## Critical" in body
    assert "## Tracked" in body
    assert "## Aspirational" in body
    assert "no routines due today" in body


def test_render_daily_body_section_headers_emitted_even_when_section_empty() -> None:
    items = [{"text": "X", "priority": "tracked", "annotation": None, "time": ""}]
    body = render_daily_body(items, no_routines_overall=False)
    # All three headers present even though only Tracked has items.
    assert "## Critical" in body
    assert "## Tracked" in body
    assert "## Aspirational" in body
    assert "- [ ] X" in body
    assert "no critical routines today" in body
    assert "no aspirational routines today" in body
