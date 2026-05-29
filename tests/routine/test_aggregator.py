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


# ---------------------------------------------------------------------------
# 9. Tier-V2 Ship 1 — read-preserve-write of tier_curation
# ---------------------------------------------------------------------------
#
# The aggregator's pre-V2 write path silently overwrote any pre-existing
# daily file. Tier-V2 Ship 1 closes a race: the talker may pre-edit
# ``vault/daily/<date>.md`` with a ``tier_curation`` block BEFORE the
# routine aggregator's 05:59 fire, OR the operator may run ``alfred
# routine`` manually mid-day to refresh aggregator state without losing
# the morning's curation. Both must preserve the tier_curation block.
#
# Cross-layer key ownership contract:
#   - Aggregator owns: ``type``, ``date``, ``routines_contributing``,
#     ``critical_pending`` + the body content (Critical/Tracked/
#     Aspirational sections).
#   - DailyCuration owns: ``tier_curation`` frontmatter key.
# Both layers read-preserve-write the other's keys on their writes.


def test_aggregator_preserves_tier_curation(tmp_path: Path) -> None:
    """CRITICAL: pre-write a daily file with ``tier_curation`` block,
    fire aggregator, assert the block is preserved verbatim + the
    ``preserved_tier_curation`` log event fires.

    Per dispatch verbatim: "pre-write a daily file with tier_curation
    block, fire aggregator, assert block is preserved + log event fires."
    """
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    # Seed a routine so the aggregator has SOMETHING to write.
    _write_routine(vault, "Daily R", {
        "type": "routine",
        "status": "active",
        "name": "Daily R",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Brush AM", "priority": "tracked"},
        ],
    })

    # Pre-write the daily file with a tier_curation block (simulates
    # talker pre-edit before 05:59 aggregator fire).
    daily_dir = vault / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    pre_existing = (
        "---\n"
        "type: daily\n"
        "date: 2026-05-26\n"
        "tier_curation:\n"
        "  t1:\n"
        "    - task: '[[task/RRTS Payroll]]'\n"
        "      source: auto-due\n"
        "      confirmed: true\n"
        "  t2:\n"
        "    - task: '[[task/Bug List]]'\n"
        "      source: operator\n"
        "  t3:\n"
        "    - item: Walk Fergus\n"
        "      source: aspirational\n"
        "  curated_at: '2026-05-26T07:14:00-03:00'\n"
        "---\n\n"
        "# pre-existing body — should be overwritten by aggregator\n"
    )
    (daily_dir / "2026-05-26.md").write_text(pre_existing, encoding="utf-8")

    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as captured:
        rel = run_aggregator_once(config, today)

    # The aggregator wrote its own routines content but PRESERVED the
    # tier_curation block.
    file_path = vault / rel
    post = frontmatter.load(str(file_path))
    meta = post.metadata or {}

    # Aggregator-owned keys are present + recomputed.
    assert meta.get("type") == "daily"
    assert str(meta.get("date")) == "2026-05-26"
    assert meta.get("routines_contributing") == ["Daily R"]

    # Tier-curation block survived verbatim.
    assert "tier_curation" in meta
    tc = meta["tier_curation"]
    assert tc["t1"][0]["task"] == "[[task/RRTS Payroll]]"
    assert tc["t1"][0]["source"] == "auto-due"
    assert tc["t1"][0]["confirmed"] is True
    assert tc["t2"][0]["task"] == "[[task/Bug List]]"
    assert tc["t3"][0]["item"] == "Walk Fergus"
    assert tc["t3"][0]["source"] == "aspirational"
    assert tc["curated_at"] == "2026-05-26T07:14:00-03:00"

    # Body recomputed (pre-existing body line dropped, sections present).
    assert "## Critical" in post.content
    assert "## Tracked" in post.content
    assert "- [ ] Brush AM" in post.content
    assert "pre-existing body" not in post.content

    # The preserved_tier_curation log event fires with the canonical
    # field names per builder.md rule #9.
    events = [
        c for c in captured
        if c.get("event") == "routine.aggregator.preserved_tier_curation"
    ]
    assert len(events) == 1
    assert events[0]["path"] == rel
    assert events[0]["date"] == "2026-05-26"


def test_aggregator_writes_clean_file_without_existing_tier_curation(
    tmp_path: Path,
) -> None:
    """First-run path: no pre-existing daily file → aggregator writes
    a clean file with NO ``tier_curation`` key, and the
    ``preserved_tier_curation`` log event does NOT fire."""
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Daily R", {
        "type": "routine",
        "status": "active",
        "name": "Daily R",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Brush AM", "priority": "tracked"},
        ],
    })

    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as captured:
        rel = run_aggregator_once(config, today)

    file_path = vault / rel
    post = frontmatter.load(str(file_path))
    meta = post.metadata or {}

    # Aggregator-owned keys present.
    assert meta.get("type") == "daily"
    assert str(meta.get("date")) == "2026-05-26"
    # NO tier_curation key — clean first-run state.
    assert "tier_curation" not in meta

    # The preserve-log event does NOT fire on clean first-run.
    events = [
        c for c in captured
        if c.get("event") == "routine.aggregator.preserved_tier_curation"
    ]
    assert len(events) == 0


def test_aggregator_preserves_curation_across_multiple_runs(
    tmp_path: Path,
) -> None:
    """Idempotency: run aggregator → talker adds curation → run
    aggregator AGAIN → curation still preserved on the second fire.

    Simulates the morning ritual: 05:59 aggregator fires (clean
    write), talker pre-edits T1/T2/T3, operator manually re-runs
    aggregator mid-morning, curation must NOT be lost."""
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Daily R", {
        "type": "routine",
        "status": "active",
        "name": "Daily R",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Brush AM", "priority": "tracked"},
        ],
    })

    config = _config(vault, tmp_path)

    # First run — clean write.
    rel = run_aggregator_once(config, today)
    file_path = vault / rel

    # Talker pre-edits the file to add tier_curation.
    post = frontmatter.load(str(file_path))
    meta = dict(post.metadata or {})
    meta["tier_curation"] = {
        "t1": [{"task": "[[task/X]]", "source": "auto-due"}],
        "t2": [],
        "t3": [],
    }
    post.metadata = meta
    file_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    # Second run — must preserve curation.
    run_aggregator_once(config, today)

    post2 = frontmatter.load(str(file_path))
    meta2 = post2.metadata or {}
    assert "tier_curation" in meta2
    assert meta2["tier_curation"]["t1"][0]["task"] == "[[task/X]]"


def test_aggregator_drops_malformed_tier_curation_and_logs(
    tmp_path: Path,
) -> None:
    """Defensive: if the operator hand-edits ``tier_curation`` to a
    non-dict value (e.g. a string), aggregator drops it cleanly +
    logs ``tier_curation_wrong_type`` so the operator sees the drop."""
    vault = tmp_path / "vault"
    today = date(2026, 5, 26)

    _write_routine(vault, "Daily R", {
        "type": "routine",
        "status": "active",
        "name": "Daily R",
        "cadence": {"type": "daily"},
        "items": [{"text": "Brush AM", "priority": "tracked"}],
    })

    # Pre-write a daily file with tier_curation as a string (malformed).
    daily_dir = vault / "daily"
    daily_dir.mkdir(parents=True, exist_ok=True)
    (daily_dir / "2026-05-26.md").write_text(
        "---\n"
        "type: daily\n"
        "date: 2026-05-26\n"
        "tier_curation: 'oops — this should be a dict'\n"
        "---\n\n# body\n",
        encoding="utf-8",
    )

    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as captured:
        rel = run_aggregator_once(config, today)

    file_path = vault / rel
    post = frontmatter.load(str(file_path))
    meta = post.metadata or {}
    # Malformed curation dropped — clean state.
    assert "tier_curation" not in meta

    # The wrong-type log event fires with actual_type pinned.
    events = [
        c for c in captured
        if c.get("event") == "routine.aggregator.tier_curation_wrong_type"
    ]
    assert len(events) == 1
    assert events[0]["actual_type"] == "str"


# ===========================================================================
# Phase 2A Ship B — routine-section dedup + cycle-aware annotation
# ===========================================================================
#
# Items with ``due_pattern`` whose ``today`` falls in their T1/T2 window
# are HANDED OFF to the tier section (skipped from Critical/Tracked
# render). Items with ``due_pattern`` but outside the windows render
# with cycle-aware annotation (replaces gap-based annotation).
#
# NOW = 2026-05-28 (Thursday). Tests use this as the "today" reference.


_NOW_THU_2026_05_28 = date(2026, 5, 28)


def _read_body(vault: Path, today_iso: str) -> str:
    """Read the daily file body after run_aggregator_once."""
    daily_file = vault / "daily" / f"{today_iso}.md"
    return daily_file.read_text(encoding="utf-8")


def test_item_in_t1_window_skipped_from_render(tmp_path: Path) -> None:
    """Item with due_pattern + escalate_at_days=1, due tomorrow (Fri) →
    T1 window → SKIPPED from Critical render (tier section will surface)."""
    vault = tmp_path / "vault"
    today = _NOW_THU_2026_05_28
    _write_routine(vault, "Weekly Chores", {
        "type": "routine",
        "status": "active",
        "name": "Weekly Chores",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Garbage Out",
                "priority": "critical",
                "due_pattern": {"type": "weekly", "day": "fri"},
                "escalate_at_days": 1,
            },
            # Sibling item without due_pattern — should still render.
            {"text": "Sibling Item", "priority": "critical"},
        ],
    })
    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)
    body = _read_body(vault, today.isoformat())
    # Item handed off — NOT in body.
    assert "Garbage Out" not in body
    # Sibling renders normally.
    assert "Sibling Item" in body


def test_item_in_t2_window_skipped_from_render(tmp_path: Path) -> None:
    """Item with surface_at_days=5, escalate_at_days=0, monthly day=1 →
    today 2026-05-28 → due 2026-06-01 → days_to_due=4 → T2 window →
    SKIPPED."""
    vault = tmp_path / "vault"
    today = _NOW_THU_2026_05_28
    _write_routine(vault, "Recurring Bills", {
        "type": "routine",
        "status": "active",
        "name": "Recurring Bills",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Pay Clinic Rental",
                "priority": "critical",
                "due_pattern": {"type": "monthly", "day": 1},
                "surface_at_days": 5,
                "escalate_at_days": 0,
            },
        ],
    })
    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)
    body = _read_body(vault, today.isoformat())
    assert "Pay Clinic Rental" not in body


def test_item_outside_windows_renders_normally(tmp_path: Path) -> None:
    """Item with surface_at_days=5, escalate_at_days=0, monthly day=1 →
    today 2026-05-23 (8 days before 1st) → OUTSIDE both windows →
    renders in routine section."""
    vault = tmp_path / "vault"
    # 2026-05-23: days_to_due = 9 (since due = 2026-06-01); outside
    # surface_at_days=5 window.
    today = date(2026, 5, 23)
    _write_routine(vault, "Recurring Bills", {
        "type": "routine",
        "status": "active",
        "name": "Recurring Bills",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Pay Clinic Rental",
                "priority": "tracked",
                "due_pattern": {"type": "monthly", "day": 1},
                "surface_at_days": 5,
                "escalate_at_days": 0,
            },
        ],
    })
    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)
    body = _read_body(vault, today.isoformat())
    # Item RENDERS (outside windows).
    assert "Pay Clinic Rental" in body


def test_item_without_due_pattern_renders_unchanged(tmp_path: Path) -> None:
    """Pre-Phase-2A items (no due_pattern) render unchanged — Walk Fergus
    shape stays in routine section with gap-based annotation."""
    vault = tmp_path / "vault"
    today = _NOW_THU_2026_05_28
    _write_routine(vault, "Daily Self-Care", {
        "type": "routine",
        "status": "active",
        "name": "Daily Self-Care",
        "cadence": {"type": "daily"},
        "items": [
            {"text": "Walk Fergus", "priority": "tracked"},
        ],
    })
    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)
    body = _read_body(vault, today.isoformat())
    assert "Walk Fergus" in body
    # Gap-based annotation (no completions → sentinel).
    assert "no completions yet" in body


def test_aspirational_items_never_handed_off(tmp_path: Path) -> None:
    """Aspirational items NEVER hand off to tier section even if they
    accidentally have due_pattern — defensive carve-out (operator-stated:
    T3 is self-care intentions, not deadline work)."""
    vault = tmp_path / "vault"
    today = _NOW_THU_2026_05_28
    _write_routine(vault, "Aspirational Items", {
        "type": "routine",
        "status": "active",
        "name": "Aspirational Items",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Read for an hour",
                "priority": "aspirational",
                # Defensive: even if operator adds due_pattern, T3 doesn't
                # hand off. (Realistically aspirational items shouldn't
                # carry due_pattern; this is the carve-out test.)
                "due_pattern": {"type": "weekly", "day": "fri"},
                "escalate_at_days": 1,
            },
        ],
    })
    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)
    body = _read_body(vault, today.isoformat())
    assert "Read for an hour" in body


def test_cycle_aware_annotation_done_this_cycle(tmp_path: Path) -> None:
    """Tracked item with due_pattern, completion log shows completion
    in current cycle → annotation: ``*(done this cycle)*``."""
    vault = tmp_path / "vault"
    today = _NOW_THU_2026_05_28
    _write_routine(vault, "Weekly Chores", {
        "type": "routine",
        "status": "active",
        "name": "Weekly Chores",
        "cadence": {"type": "daily"},
        "completion_log": {
            "Vacuum": ["2026-05-26"],  # Mon this week
        },
        "items": [
            # NO escalate_at_days → never tier-surfaces → routine section
            # with cycle-aware annotation.
            {
                "text": "Vacuum",
                "priority": "tracked",
                "due_pattern": {"type": "weekly", "day": "fri"},
            },
        ],
    })
    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)
    body = _read_body(vault, today.isoformat())
    assert "Vacuum" in body
    assert "*(done this cycle)*" in body


def test_cycle_aware_annotation_due_in_n_days(tmp_path: Path) -> None:
    """Tracked item with due_pattern, no completions, due in 5+ days →
    annotation: ``*(due in Nd)*``."""
    vault = tmp_path / "vault"
    # NOW = 2026-05-28 Thu; monthly day=10 → due 2026-06-10 → 13 days.
    today = _NOW_THU_2026_05_28
    _write_routine(vault, "Monthly", {
        "type": "routine",
        "status": "active",
        "name": "Monthly",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Quarterly Review",
                "priority": "tracked",
                "due_pattern": {"type": "monthly", "day": 10},
                # NB: no escalate_at_days → no tier handoff. Stays in
                # routine section with cycle-aware annotation.
            },
        ],
    })
    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)
    body = _read_body(vault, today.isoformat())
    assert "Quarterly Review" in body
    assert "due in 13d" in body


def test_cycle_aware_annotation_due_today(tmp_path: Path) -> None:
    """Tracked item with due_pattern, no completions, due today →
    annotation: ``*(due today)*`` (consistent with tier phrasing)."""
    vault = tmp_path / "vault"
    today = _NOW_THU_2026_05_28
    # monthly day=28 → due 2026-05-28 = today.
    _write_routine(vault, "Monthly", {
        "type": "routine",
        "status": "active",
        "name": "Monthly",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Some Habit",
                "priority": "tracked",
                "due_pattern": {"type": "monthly", "day": 28},
                # No escalate_at_days → routine section + cycle annotation.
            },
        ],
    })
    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)
    body = _read_body(vault, today.isoformat())
    assert "Some Habit" in body
    assert "*(due today)*" in body


def test_cycle_aware_annotation_due_tomorrow(tmp_path: Path) -> None:
    """Tracked item, due_pattern weekly day=fri (tomorrow), no
    completions → annotation: ``*(due tomorrow)*``."""
    vault = tmp_path / "vault"
    today = _NOW_THU_2026_05_28
    _write_routine(vault, "Weekly", {
        "type": "routine",
        "status": "active",
        "name": "Weekly",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Friday Habit",
                "priority": "tracked",
                "due_pattern": {"type": "weekly", "day": "fri"},
                # No escalate_at_days → routine section + cycle annotation.
            },
        ],
    })
    config = _config(vault, tmp_path)
    run_aggregator_once(config, today)
    body = _read_body(vault, today.isoformat())
    assert "Friday Habit" in body
    assert "*(due tomorrow)*" in body


def test_handed_off_to_tier_log_event_fires(tmp_path: Path) -> None:
    """The ``routine.aggregator.handed_off_to_tier`` log event fires
    per skip with item_text + tier + days_to_due + routine_record."""
    vault = tmp_path / "vault"
    today = _NOW_THU_2026_05_28
    _write_routine(vault, "Weekly Chores", {
        "type": "routine",
        "status": "active",
        "name": "Weekly Chores",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Garbage Out",
                "priority": "critical",
                "due_pattern": {"type": "weekly", "day": "fri"},
                "escalate_at_days": 1,
            },
        ],
    })
    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as captured:
        run_aggregator_once(config, today)
    events = [
        c for c in captured
        if c.get("event") == "routine.aggregator.handed_off_to_tier"
    ]
    assert len(events) == 1
    e = events[0]
    assert e["item_text"] == "Garbage Out"
    assert e["tier"] == 1
    assert e["days_to_due"] == 1
    assert e["routine_record"] == "Weekly Chores"


def test_handed_off_log_fires_for_t2_handoff(tmp_path: Path) -> None:
    """Same log event fires for T2 handoffs — ``tier=2``."""
    vault = tmp_path / "vault"
    today = _NOW_THU_2026_05_28
    _write_routine(vault, "Bills", {
        "type": "routine",
        "status": "active",
        "name": "Bills",
        "cadence": {"type": "daily"},
        "items": [
            {
                "text": "Pay Clinic Rental",
                "priority": "critical",
                "due_pattern": {"type": "monthly", "day": 1},
                "surface_at_days": 5,
                "escalate_at_days": 0,
            },
        ],
    })
    config = _config(vault, tmp_path)
    with structlog.testing.capture_logs() as captured:
        run_aggregator_once(config, today)
    events = [
        c for c in captured
        if c.get("event") == "routine.aggregator.handed_off_to_tier"
    ]
    assert len(events) == 1
    assert events[0]["tier"] == 2
    assert events[0]["days_to_due"] == 4
