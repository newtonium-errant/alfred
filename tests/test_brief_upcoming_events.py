"""Tests for the Brief's Upcoming Events section (Phase 1).

Covers the rule-free Phase 1 contract:
- Today / This Week / Later bucketing relative to a passed-in ``today``.
- 30-day window cap (input-side filter).
- Past records excluded.
- Tasks without ``due`` excluded.
- Empty state renders the literal "No upcoming events." sentinel.
- ``enabled: false`` makes the daemon omit the section entirely.

The renderer takes ``today`` as a parameter so tests don't have to
freeze the system clock — the daemon is the only caller that resolves
``today`` from the wall clock.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from alfred.brief.config import UpcomingEventsConfig, load_from_unified
from alfred.brief.upcoming_events import (
    _bucket,
    _UpcomingItem,
    render_upcoming_events_section,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


TODAY = date(2026, 5, 1)


def _write_event(
    vault: Path,
    name: str,
    event_date: str,
    *,
    location: str | None = None,
    description: str | None = None,
    status: str | None = None,
) -> None:
    """Drop an event record into ``vault/event/{name}.md``.

    ``status`` is optional — when None, the field is omitted entirely
    (pre-Phase-A+ shape). Pass a string (``"cancelled"``, ``""``, etc.)
    to exercise the closed-state filter on event records.
    """
    lines = [
        "---",
        "type: event",
        f"name: {name}",
        f"date: {event_date}",
    ]
    if status is not None:
        # Quote to allow empty string and avoid YAML interpreting unquoted
        # values. Single-quote is safe for the values exercised here.
        lines.append(f"status: '{status}'")
    if location is not None:
        lines.append(f"location: {location}")
    if description is not None:
        lines.append(f"description: {description}")
    lines.extend(
        [
            "created: 2026-04-01",
            "tags: []",
            "---",
            "",
            f"# {name}",
            "",
        ]
    )
    (vault / "event").mkdir(exist_ok=True)
    (vault / "event" / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")


def _write_task(
    vault: Path,
    name: str,
    *,
    due: str | None = None,
    description: str | None = None,
    status: str | None = "todo",
) -> None:
    """Drop a task record into ``vault/task/{name}.md``. ``due`` may be
    omitted to test the "task without due is excluded" path. ``status``
    defaults to ``"todo"``; pass ``None`` to omit the field entirely
    (so the Phase 2 filter sees ``fm.get("status") is None`` and
    treats it as "open" — i.e., included)."""
    lines = ["---", "type: task", f"name: {name}"]
    if status is not None:
        lines.append(f"status: {status}")
    if due is not None:
        lines.append(f"due: {due}")
    if description is not None:
        lines.append(f"description: {description}")
    lines.extend(["created: 2026-04-01", "tags: []", "---", "", f"# {name}", ""])
    (vault / "task").mkdir(exist_ok=True)
    (vault / "task" / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    (v / "event").mkdir()
    (v / "task").mkdir()
    return v


def _default_config() -> UpcomingEventsConfig:
    return UpcomingEventsConfig(enabled=True, max_days_ahead=30)


def _write_event_with_start(
    vault: Path,
    name: str,
    *,
    start: str,
    end: str | None = None,
    date_field: str | None = None,
    location: str | None = None,
    description: str | None = None,
) -> None:
    """Drop a Phase-A+-style event record (with ISO ``start`` field).

    Mirrors what Salem writes via the cross-instance event-propose
    handler + the GCal sync writeback paths since SKILL update
    ``a923c1b``: ``start`` is full ISO datetime with tz offset; ``date``
    is optional (handler writes both for redundancy / brief
    compatibility, but a backfilled record may have only ``start``).
    """
    lines = [
        "---",
        "type: event",
        f"name: {name}",
        f"start: '{start}'",
    ]
    if end is not None:
        lines.append(f"end: '{end}'")
    if date_field is not None:
        lines.append(f"date: {date_field}")
    if location is not None:
        lines.append(f"location: {location}")
    if description is not None:
        lines.append(f"description: {description}")
    lines.extend(
        [
            "created: 2026-04-01",
            "tags: []",
            "---",
            "",
            f"# {name}",
            "",
        ]
    )
    (vault / "event").mkdir(exist_ok=True)
    (vault / "event" / f"{name}.md").write_text(
        "\n".join(lines), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Bucketing
# ---------------------------------------------------------------------------


def test_today_bucket_populated(vault: Path) -> None:
    _write_event(vault, "Lunch with Jane", TODAY.isoformat())
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "### Today" in out
    assert "Lunch with Jane" in out
    assert TODAY.isoformat() in out


def test_this_week_bucket_three_days_out(vault: Path) -> None:
    target = TODAY + timedelta(days=3)
    _write_event(vault, "Dentist", target.isoformat())
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "### This Week" in out
    assert "Dentist" in out
    assert target.isoformat() in out
    # Should NOT show Today header for an empty Today bucket.
    assert "### Today" not in out


def test_later_bucket_twenty_days_out(vault: Path) -> None:
    target = TODAY + timedelta(days=20)
    _write_event(vault, "Conference", target.isoformat())
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "### Later" in out
    assert "Conference" in out
    assert "### This Week" not in out


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def test_event_past_cutoff_excluded(vault: Path) -> None:
    """An event 31 days out is past the 30-day window and should drop."""
    target = TODAY + timedelta(days=31)
    _write_event(vault, "Way Out", target.isoformat())
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Way Out" not in out
    assert out == "No upcoming events."


def test_past_events_excluded(vault: Path) -> None:
    target = TODAY - timedelta(days=1)
    _write_event(vault, "Yesterday", target.isoformat())
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Yesterday" not in out


def test_task_with_due_included(vault: Path) -> None:
    target = TODAY + timedelta(days=2)
    _write_task(vault, "Pay invoice", due=target.isoformat())
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Pay invoice" in out
    assert "### This Week" in out


def test_task_without_due_excluded(vault: Path) -> None:
    _write_task(vault, "Some open todo", due=None)
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Some open todo" not in out
    assert out == "No upcoming events."


# ---------------------------------------------------------------------------
# Phase 2 candidate #1: status filter on tasks
#
# Per project_brief_upcoming_events_phase2.md and the spec memo: tasks
# with ``status`` in {cancelled, done, superseded} should NOT appear in
# the brief, even if their ``due`` is in the future. Pre-fix a task
# cancelled or completed weeks ago but with ``due`` set to a future
# date kept showing up in the brief — visual noise the operator had
# already triaged.
#
# The filter applies to TASKS ONLY. Events go through the unchanged
# ``_event_date`` path; the closed-state check is in the ``elif
# rec_type == "task"`` branch of ``_collect_items``.
# ---------------------------------------------------------------------------


def test_task_with_cancelled_status_excluded(vault: Path) -> None:
    """A task with ``status: cancelled`` and a future ``due`` must NOT
    appear in the brief."""
    target = TODAY + timedelta(days=3)
    _write_task(
        vault, "Cancelled future work",
        due=target.isoformat(), status="cancelled",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Cancelled future work" not in out
    # Brief should be empty (only this one record was written).
    assert out == "No upcoming events."


def test_task_with_done_status_excluded(vault: Path) -> None:
    """A task with ``status: done`` and a future ``due`` must NOT
    appear in the brief. Future-due-with-done is a real shape: the
    operator marked the task done early but didn't clear the due
    field — no need to surface it again."""
    target = TODAY + timedelta(days=5)
    _write_task(
        vault, "Already done early",
        due=target.isoformat(), status="done",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Already done early" not in out
    assert out == "No upcoming events."


def test_task_with_superseded_status_excluded(vault: Path) -> None:
    """A task with ``status: superseded`` and a future ``due`` must NOT
    appear in the brief. ``superseded`` is the standard schema status
    for replaced records (the work moved to a newer task) — including
    the original would double-surface the same work item."""
    target = TODAY + timedelta(days=10)
    _write_task(
        vault, "Old superseded task",
        due=target.isoformat(), status="superseded",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Old superseded task" not in out
    assert out == "No upcoming events."


def test_task_with_open_status_still_included(vault: Path) -> None:
    """Positive regression pin: open-state tasks (status in {todo,
    doing, blocked, ...}) MUST still appear. The filter is an
    explicit denylist, not a generic predicate — a future schema
    addition that introduces a new active-state status name must
    not get caught up in the filter.

    Pre-fix this would have passed too (no filter); this test
    locks in the post-fix contract that the filter is restricted to
    the closed-state denyset."""
    target = TODAY + timedelta(days=2)
    _write_task(
        vault, "Pay invoice",
        due=target.isoformat(), status="todo",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Pay invoice" in out
    assert "### This Week" in out


def test_task_without_status_field_still_included(vault: Path) -> None:
    """Defensive coverage: a task missing the ``status`` field
    altogether must still appear. ``fm.get("status")`` returns None;
    None is not in the denyset, so the record passes through.

    This guards against a future refactor that tightens the filter to
    ``status not in {<active-state allowlist>}`` — under that shape, a
    task without status would be DROPPED, which is the wrong default
    (treats absence as "closed"). Locking the inclusive default here
    forces the conversation if anyone ever proposes flipping it."""
    target = TODAY + timedelta(days=4)
    _write_task(
        vault, "Status-less task",
        due=target.isoformat(), status=None,
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Status-less task" in out


def test_event_with_cancelled_status_excluded(vault: Path) -> None:
    """An event with ``status: cancelled`` and a future date must NOT
    appear in the brief.

    Regression pin for 2026-05-22 incident: operator (Andrew) marked
    an open-house event ``status: cancelled`` on 2026-05-21 via
    talker → vault_edit; 2026-05-22's morning brief still surfaced
    it under "### This Week" because the pre-fix closed-state gate
    only applied inside the ``task`` branch of ``_collect_items``,
    not the ``event`` branch.

    Post-fix the gate applies to BOTH branches via the shared
    ``_is_closed_status`` helper. The schema reality justifying the
    change: events grew a meaningful ``status`` field once GCal sync
    went live — the cancel hook PATCHes the GCal event when the vault
    record flips to ``status: cancelled``.
    """
    target = TODAY + timedelta(days=3)
    _write_event(
        vault, "Open House — 12636 Hwy 1",
        target.isoformat(), status="cancelled",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Open House" not in out
    # Brief should be empty (only this one record was written).
    assert out == "No upcoming events."


def test_event_with_done_status_excluded(vault: Path) -> None:
    """An event with ``status: done`` is excluded. ``done`` isn't a
    typical event status (events usually flip to ``cancelled`` rather
    than ``done``), but the denyset is shared across record types and
    coverage parity matters for refactor safety."""
    target = TODAY + timedelta(days=5)
    _write_event(
        vault, "Workshop already done", target.isoformat(), status="done",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Workshop already done" not in out
    assert out == "No upcoming events."


def test_event_with_superseded_status_excluded(vault: Path) -> None:
    """An event with ``status: superseded`` is excluded. Mirror of
    the task-side test."""
    target = TODAY + timedelta(days=10)
    _write_event(
        vault, "Replaced event", target.isoformat(), status="superseded",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Replaced event" not in out
    assert out == "No upcoming events."


def test_event_without_status_field_surfaces_normally(vault: Path) -> None:
    """A pre-Phase-A+ event with no ``status`` field at all must
    surface normally. ``fm.get("status")`` returns None; the shared
    gate ``_is_closed_status`` treats None as open.

    Locks in the inclusive default — absence is treated as "open",
    not "closed". A future refactor that tightens the gate to a
    status-allowlist would DROP these records, which is the wrong
    default for the brief surface."""
    target = TODAY + timedelta(days=4)
    # status kwarg omitted entirely => no field in frontmatter
    _write_event(vault, "Status-less event", target.isoformat())
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Status-less event" in out
    assert target.isoformat() in out


def test_event_with_empty_status_string_surfaces_normally(
    vault: Path,
) -> None:
    """An event with ``status: ""`` (explicit empty string in YAML)
    must surface normally. Empty string is treated as open — same as
    a missing field.

    Real-world shape: a janitor edit that clears the status field
    might leave it as an empty string rather than removing the line.
    The brief shouldn't filter such a record."""
    target = TODAY + timedelta(days=2)
    _write_event(vault, "Empty status event", target.isoformat(), status="")
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Empty status event" in out
    assert target.isoformat() in out


def test_event_with_capitalised_cancelled_status_excluded(
    vault: Path,
) -> None:
    """Defensive coverage: ``status: Cancelled`` (or any case
    variant) MUST still filter. The shared gate normalises via
    ``.casefold()`` so capitalised, uppercase, and mixed-case values
    all match the denyset.

    STATUS_BY_TYPE schema canonicalises to lowercase, but operator
    edits via talker/vault_edit don't enforce case — a manual
    ``status: Cancelled`` shouldn't escape the filter just because of
    the typo class."""
    target = TODAY + timedelta(days=3)
    _write_event(
        vault, "Capitalised cancelled", target.isoformat(),
        status="Cancelled",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Capitalised cancelled" not in out
    assert out == "No upcoming events."


def test_future_dated_cancelled_event_excluded(vault: Path) -> None:
    """A cancelled event whose date is still in the window must be
    excluded — the cancellation overrides the upcoming-date qualifier.

    Direct reproduction of the operator-flagged shape: a future
    event marked cancelled but still within the 30-day window. This
    test mirrors the exact failure mode rather than the unit-level
    gate behavior."""
    target = TODAY + timedelta(days=2)  # well within 30-day window
    _write_event(
        vault, "Future cancelled event", target.isoformat(),
        status="cancelled",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Future cancelled event" not in out
    assert "### This Week" not in out
    assert out == "No upcoming events."


def test_closed_status_emits_log_line(vault: Path) -> None:
    """Per ``feedback_intentionally_left_blank.md`` + the
    log-emission-test-pattern discipline: the closed-state filter
    must emit a log line that operators can grep
    (``upcoming_events.closed_status_excluded``).

    Uses ``structlog.testing.capture_logs`` rather than caplog —
    same pattern as ``test_event_with_neither_start_nor_date_skipped_with_log``
    below; structlog's ConsoleRenderer and pytest's caplog don't
    reliably interoperate through the LoggerFactory boundary.
    """
    from structlog.testing import capture_logs

    target = TODAY + timedelta(days=3)
    _write_event(
        vault, "Cancelled with log", target.isoformat(),
        status="cancelled",
    )
    with capture_logs() as captured:
        out = render_upcoming_events_section(_default_config(), vault, TODAY)

    assert "Cancelled with log" not in out
    matches = [
        c for c in captured
        if c.get("event") == "upcoming_events.closed_status_excluded"
    ]
    assert len(matches) == 1, (
        f"expected 1 closed_status_excluded log, got {len(matches)}: {captured}"
    )
    assert matches[0].get("rec_type") == "event"
    assert matches[0].get("status") == "cancelled"
    assert "Cancelled with log" in matches[0].get("path", "")


# ---------------------------------------------------------------------------
# Empty state + disabled
# ---------------------------------------------------------------------------


def test_empty_state_renders_sentinel(vault: Path) -> None:
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert out == "No upcoming events."


def test_disabled_returns_empty_string(vault: Path) -> None:
    """Daemon uses empty string as the 'omit the section entirely' signal."""
    _write_event(vault, "Lunch", TODAY.isoformat())
    cfg = UpcomingEventsConfig(enabled=False, max_days_ahead=30)
    out = render_upcoming_events_section(cfg, vault, TODAY)
    assert out == ""


# ---------------------------------------------------------------------------
# Display formatting
# ---------------------------------------------------------------------------


def test_location_appended_when_present(vault: Path) -> None:
    _write_event(
        vault,
        "Workshop",
        TODAY.isoformat(),
        location="Halifax HQ",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Workshop (Halifax HQ)" in out


def test_description_indented_when_present(vault: Path) -> None:
    _write_event(
        vault,
        "Quarterly review",
        TODAY.isoformat(),
        description="Walk through Q2 numbers",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "*Walk through Q2 numbers*" in out


def test_items_sorted_by_date_then_name(vault: Path) -> None:
    """Within a bucket, items sort by date asc then name asc."""
    later = TODAY + timedelta(days=2)
    _write_event(vault, "Zebra meeting", later.isoformat())
    _write_event(vault, "Alpha meeting", later.isoformat())
    earlier = TODAY + timedelta(days=1)
    _write_event(vault, "Mid event", earlier.isoformat())
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    # The earlier date should come first; within the later date,
    # Alpha precedes Zebra.
    pos_mid = out.index("Mid event")
    pos_alpha = out.index("Alpha meeting")
    pos_zebra = out.index("Zebra meeting")
    assert pos_mid < pos_alpha < pos_zebra


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


def test_load_from_unified_defaults(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "brief": {},
    }
    cfg = load_from_unified(raw)
    assert cfg.upcoming_events.enabled is True
    assert cfg.upcoming_events.max_days_ahead == 30


def test_load_from_unified_overrides(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "brief": {
            "upcoming_events": {"enabled": False, "max_days_ahead": 14},
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.upcoming_events.enabled is False
    assert cfg.upcoming_events.max_days_ahead == 14


# ---------------------------------------------------------------------------
# `start` field — Phase-A+ graceful upgrade
# ---------------------------------------------------------------------------
#
# Per Salem SKILL update ``a923c1b``, every new event ships with both
# ``start`` (full ISO datetime, tz offset) and ``date`` (the
# Halifax-local date derived from ``start.astimezone().date()``). The
# renderer prefers ``start`` so backfilled records that have ONLY
# ``start`` (no redundant ``date``) still surface in the brief.


def test_event_with_start_only_renders_correctly(vault: Path) -> None:
    """Backfilled / GCal-synced records may have ``start`` but no ``date``.
    Brief must still find them via the new lookup path.
    """
    target = TODAY + timedelta(days=4)
    _write_event_with_start(
        vault,
        "VAC marketing call",
        start=f"{target.isoformat()}T14:15:00-03:00",
        end=f"{target.isoformat()}T15:00:00-03:00",
        # date_field deliberately omitted — the gap pre-fix
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "VAC marketing call" in out
    assert target.isoformat() in out
    assert "### This Week" in out


def test_event_with_both_start_and_date_uses_start(vault: Path) -> None:
    """When both fields are present (the common case post-2026-05-02),
    ``start`` wins. Output is identical to the ``date`` path because the
    cross-instance propose handler derives ``date`` from ``start.astimezone()``
    — so both paths produce the same Halifax-local date string.

    Test guards against future divergence: if ``date`` ever drifts away
    from ``start`` (e.g., a janitor edit normalizes one but not the
    other), the brief's source of truth stays ``start`` and this test
    pins which one wins.
    """
    target = TODAY + timedelta(days=2)
    # Both fields encode the same date.
    _write_event_with_start(
        vault,
        "Coaching session",
        start=f"{target.isoformat()}T14:00:00-03:00",
        end=f"{target.isoformat()}T15:00:00-03:00",
        date_field=target.isoformat(),
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Coaching session" in out
    assert target.isoformat() in out


def test_event_with_only_date_legacy_still_works(vault: Path) -> None:
    """Pre-Phase-A+ records have only ``date``. Fallback path keeps
    them rendering."""
    target = TODAY + timedelta(days=5)
    _write_event(vault, "Legacy event", target.isoformat())
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Legacy event" in out
    assert target.isoformat() in out


def test_event_with_neither_start_nor_date_skipped_with_log(
    vault: Path,
) -> None:
    """Per ``feedback_intentionally_left_blank.md``: a malformed event
    (no ``start``, no ``date``) is a real signal — not noise. Skip the
    record but emit a log line so an operator can grep
    ``upcoming_events.event_missing_date`` to spot the gap.

    Uses ``structlog.testing.capture_logs`` rather than pytest's
    ``caplog`` — same pattern documented in
    ``test_integrations_gcal_p2.py``: pytest caplog and structlog's
    ConsoleRenderer don't reliably interoperate when the emit happens
    via the LoggerFactory boundary, so capture at the structlog
    processor layer instead.
    """
    from structlog.testing import capture_logs

    # Manually drop a malformed event (neither helper supports this
    # state because production paths never produce it).
    (vault / "event").mkdir(exist_ok=True)
    (vault / "event" / "Malformed.md").write_text(
        "---\ntype: event\nname: Malformed\ncreated: 2026-04-01\n"
        "tags: []\n---\n\n# Malformed\n",
        encoding="utf-8",
    )
    with capture_logs() as captured:
        out = render_upcoming_events_section(_default_config(), vault, TODAY)

    # The record is excluded from the rendered brief.
    assert "Malformed" not in out
    assert out == "No upcoming events."
    # ...but the log line is present, distinguishable from idle.
    missing_logs = [
        c for c in captured
        if c.get("event") == "upcoming_events.event_missing_date"
    ]
    assert len(missing_logs) == 1
    assert "Malformed.md" in missing_logs[0].get("path", "")


def test_event_with_start_as_yaml_datetime_value(vault: Path) -> None:
    """YAML can parse ``start`` as a datetime value (when the value is
    NOT quoted in the source). Brief must handle both string + datetime
    shapes — the existing ``_coerce_date`` helper covers both branches.
    """
    target = TODAY + timedelta(days=3)
    # Unquoted ISO datetime in YAML — python-frontmatter will parse
    # this into a Python ``datetime`` object, not a string.
    (vault / "event").mkdir(exist_ok=True)
    (vault / "event" / "YAMLDateTime.md").write_text(
        f"---\n"
        f"type: event\n"
        f"name: YAMLDateTime\n"
        f"start: {target.isoformat()}T14:00:00-03:00\n"
        f"created: 2026-04-01\n"
        f"tags: []\n"
        f"---\n\n# YAMLDateTime\n",
        encoding="utf-8",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "YAMLDateTime" in out
    assert target.isoformat() in out


# ---------------------------------------------------------------------------
# scope="today_tomorrow" — /today Telegram slash-command surface
#
# Per the 2026-05-30 scope refinement (mirrors the same-day curated-only
# tier surface narrowing): /today renders Upcoming Events with
# ``scope="today_tomorrow"`` so the glance-view shows only today +
# tomorrow. The morning brief retains the full 7-day window — only
# /today is narrowed. Operator framing: "what's on my plate immediately"
# vs "what's on the schedule for the next week".
#
# Tests cover:
#   1. Today + tomorrow buckets render; +3d / +7d events absent.
#   2. ``### This Week`` / ``### Later`` headers NEVER appear in
#      today_tomorrow scope output.
#   3. Default scope (no kwarg) preserves existing three-bucket
#      behavior — regression pin for the morning brief.
#   4. ``_bucket`` direct call with scope="today_tomorrow" returns
#      exactly two buckets.
#   5. Window clamp is renderer-enforced: even if config carries
#      max_days_ahead=30, today_tomorrow scope drops +2d events.
#   6. Empty buckets in today_tomorrow scope fall through to the
#      "No upcoming events." sentinel (intentionally-left-blank pin
#      mirroring the brief-scope behavior).
# ---------------------------------------------------------------------------


def test_today_tomorrow_scope_renders_today_and_tomorrow_only(
    vault: Path,
) -> None:
    """Fixture vault has events on today, tomorrow, +3d, +7d. Only
    today + tomorrow appear in today_tomorrow scope output."""
    today_event = "Today Lunch"
    tomorrow_event = "Tomorrow Standup"
    three_days_event = "Three Days Out"
    week_event = "Week Out"
    _write_event(vault, today_event, TODAY.isoformat())
    _write_event(vault, tomorrow_event, (TODAY + timedelta(days=1)).isoformat())
    _write_event(vault, three_days_event, (TODAY + timedelta(days=3)).isoformat())
    _write_event(vault, week_event, (TODAY + timedelta(days=7)).isoformat())

    out = render_upcoming_events_section(
        _default_config(), vault, TODAY, scope="today_tomorrow",
    )
    # Today + Tomorrow records present.
    assert today_event in out
    assert tomorrow_event in out
    # +3d and +7d records ABSENT — clamped out by the 1-day window.
    assert three_days_event not in out
    assert week_event not in out


def test_today_tomorrow_scope_never_emits_this_week_or_later_headers(
    vault: Path,
) -> None:
    """Pin the visible-header contract: the two non-applicable headers
    NEVER appear in today_tomorrow scope output, even if events exist
    that would have populated them under brief scope."""
    _write_event(vault, "Today Item", TODAY.isoformat())
    _write_event(vault, "Tomorrow Item", (TODAY + timedelta(days=1)).isoformat())
    # +3d event — would land in "This Week" under brief scope.
    _write_event(vault, "Three Days", (TODAY + timedelta(days=3)).isoformat())
    # +10d event — would land in "Later" under brief scope.
    _write_event(vault, "Ten Days", (TODAY + timedelta(days=10)).isoformat())

    out = render_upcoming_events_section(
        _default_config(), vault, TODAY, scope="today_tomorrow",
    )
    # Today + Tomorrow headers present.
    assert "### Today" in out
    assert "### Tomorrow" in out
    # NEVER the brief-scope headers — these are the regression pin.
    assert "### This Week" not in out
    assert "### Later" not in out


def test_today_tomorrow_scope_window_clamp_overrides_config(
    vault: Path,
) -> None:
    """Pin the clamp contract: even when config.max_days_ahead=30, the
    today_tomorrow scope hard-clamps the window to 1 day. Operator's
    narrow-view choice wins over per-instance config widening."""
    # Config says 30 days; scope says 1.
    config = UpcomingEventsConfig(enabled=True, max_days_ahead=30)
    # +2d event — would be in the 30-day window but NOT in the 1-day
    # clamp.
    _write_event(vault, "Two Days Out", (TODAY + timedelta(days=2)).isoformat())
    _write_event(vault, "Today Item", TODAY.isoformat())

    out = render_upcoming_events_section(
        config, vault, TODAY, scope="today_tomorrow",
    )
    assert "Today Item" in out
    # +2d event clamped out despite 30-day config.
    assert "Two Days Out" not in out


def test_default_scope_preserves_three_bucket_behavior(vault: Path) -> None:
    """Regression pin: the default scope (no kwarg) — used by the
    morning brief daemon — preserves the existing Today / This Week /
    Later three-bucket behavior. Mirrors
    ``test_today_bucket_populated`` + ``test_this_week_bucket_three_
    days_out`` + ``test_later_bucket_twenty_days_out`` in a single
    fixture so the default-scope contract is pinned end-to-end."""
    _write_event(vault, "Today Event", TODAY.isoformat())
    _write_event(vault, "Midweek Event", (TODAY + timedelta(days=3)).isoformat())
    _write_event(vault, "Far Event", (TODAY + timedelta(days=20)).isoformat())

    # No scope kwarg — defaults to "brief".
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    # All three brief-scope headers present.
    assert "### Today" in out
    assert "### This Week" in out
    assert "### Later" in out
    # The Tomorrow header NEVER appears in brief scope (regression
    # pin against an accidental scope-leak in either direction).
    assert "### Tomorrow" not in out
    # All three events visible.
    assert "Today Event" in out
    assert "Midweek Event" in out
    assert "Far Event" in out


def test_brief_scope_explicit_kwarg_matches_default(vault: Path) -> None:
    """Pin that explicitly passing ``scope="brief"`` produces identical
    output to omitting the kwarg. Catches any regression where the
    default-binding diverges from the named-binding (e.g. a refactor
    that introduces a separate default path)."""
    _write_event(vault, "Today", TODAY.isoformat())
    _write_event(vault, "Week", (TODAY + timedelta(days=3)).isoformat())

    out_default = render_upcoming_events_section(
        _default_config(), vault, TODAY,
    )
    out_explicit = render_upcoming_events_section(
        _default_config(), vault, TODAY, scope="brief",
    )
    assert out_default == out_explicit


def test_bucket_direct_today_tomorrow_returns_two_buckets() -> None:
    """Direct ``_bucket`` call with scope="today_tomorrow" — assert
    two-bucket shape (Today + Tomorrow keys; no This Week / Later).
    Pins the internal contract so a refactor that adds a third bucket
    in today_tomorrow scope surfaces here rather than at render time."""
    items = [
        _UpcomingItem(
            date_iso=TODAY.isoformat(),
            name="Today A",
            location=None,
            description=None,
        ),
        _UpcomingItem(
            date_iso=(TODAY + timedelta(days=1)).isoformat(),
            name="Tomorrow B",
            location=None,
            description=None,
        ),
        # +5d — should be defensively dropped (not in any bucket).
        _UpcomingItem(
            date_iso=(TODAY + timedelta(days=5)).isoformat(),
            name="Far C",
            location=None,
            description=None,
        ),
    ]
    buckets = _bucket(items, TODAY, scope="today_tomorrow")
    assert set(buckets.keys()) == {"Today", "Tomorrow"}
    assert len(buckets["Today"]) == 1
    assert buckets["Today"][0].name == "Today A"
    assert len(buckets["Tomorrow"]) == 1
    assert buckets["Tomorrow"][0].name == "Tomorrow B"


def test_bucket_direct_default_scope_returns_three_buckets() -> None:
    """Regression pin for the default ``_bucket`` shape — same three
    keys (Today / This Week / Later) the daemon has consumed since
    Phase 1."""
    items = [
        _UpcomingItem(
            date_iso=TODAY.isoformat(),
            name="Today A",
            location=None,
            description=None,
        ),
    ]
    buckets = _bucket(items, TODAY)
    assert set(buckets.keys()) == {"Today", "This Week", "Later"}


def test_today_tomorrow_scope_empty_fixture_emits_sentinel(
    vault: Path,
) -> None:
    """No events at all → today_tomorrow scope falls through to the
    "No upcoming events." sentinel — same intentionally-left-blank
    behavior as brief scope. Pin so a refactor doesn't silently drop
    the sentinel on the narrow-scope path."""
    out = render_upcoming_events_section(
        _default_config(), vault, TODAY, scope="today_tomorrow",
    )
    assert out == "No upcoming events."


def test_today_tomorrow_scope_only_far_events_emits_sentinel(
    vault: Path,
) -> None:
    """Events exist but all are beyond the 1-day clamp → sentinel
    fires (operator sees "section ran, nothing in scope" rather than
    a silent omission)."""
    _write_event(vault, "Three Days", (TODAY + timedelta(days=3)).isoformat())
    _write_event(vault, "Ten Days", (TODAY + timedelta(days=10)).isoformat())
    out = render_upcoming_events_section(
        _default_config(), vault, TODAY, scope="today_tomorrow",
    )
    assert out == "No upcoming events."
    # Far events NOT mentioned in the body.
    assert "Three Days" not in out
    assert "Ten Days" not in out


def test_today_tomorrow_scope_only_today_renders_today_header_only(
    vault: Path,
) -> None:
    """Empty Tomorrow bucket → Tomorrow header omitted (existing empty-
    bucket-omit behavior preserved across scopes)."""
    _write_event(vault, "Just Today", TODAY.isoformat())
    out = render_upcoming_events_section(
        _default_config(), vault, TODAY, scope="today_tomorrow",
    )
    assert "### Today" in out
    assert "Just Today" in out
    # Tomorrow bucket empty → its header omitted.
    assert "### Tomorrow" not in out


def test_today_tomorrow_scope_only_tomorrow_renders_tomorrow_header_only(
    vault: Path,
) -> None:
    """Mirror of the previous test — empty Today bucket, populated
    Tomorrow bucket. Pin both empty-bucket edges."""
    _write_event(
        vault, "Just Tomorrow", (TODAY + timedelta(days=1)).isoformat(),
    )
    out = render_upcoming_events_section(
        _default_config(), vault, TODAY, scope="today_tomorrow",
    )
    assert "### Tomorrow" in out
    assert "Just Tomorrow" in out
    assert "### Today" not in out


# ---------------------------------------------------------------------------
# compose_today_reply smoke — pin the call-site wiring
#
# The today_command.compose_today_reply composer is the only production
# call site that uses scope="today_tomorrow". Smoke test asserts the
# render output's events section contains only the narrow-scope
# headers + never the brief-scope headers. Keeps the wiring honest
# against a future refactor that drops the kwarg.
# ---------------------------------------------------------------------------


def test_compose_today_reply_uses_today_tomorrow_scope(
    vault: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: ``compose_today_reply`` renders the Upcoming Events
    section with today_tomorrow scope — output contains ``### Today``
    and/or ``### Tomorrow`` but NEVER ``### This Week`` or ``### Later``.

    Fixture loads events on today, tomorrow, +3d, +7d. Without the
    scope wiring, the brief-scope render would surface "This Week"
    header (the +3d event lands there).
    """
    from datetime import datetime

    from alfred.telegram.today_command import compose_today_reply

    # Set up the directory shape compose_today_reply expects.
    (vault / "daily").mkdir(exist_ok=True)
    # No daily curation file → tier section renders the empty-curation
    # sentinel. That's fine; we're testing the upcoming-events section
    # wiring, not the tier section.

    # Events across the brief-scope buckets.
    _write_event(vault, "Today Lunch", TODAY.isoformat())
    _write_event(vault, "Tomorrow Standup", (TODAY + timedelta(days=1)).isoformat())
    _write_event(vault, "Three Days Out", (TODAY + timedelta(days=3)).isoformat())
    _write_event(vault, "Seven Days Out", (TODAY + timedelta(days=7)).isoformat())

    # Compose at TODAY's instant — pass a `now` datetime matching TODAY.
    now = datetime(TODAY.year, TODAY.month, TODAY.day, 9, 0, 0)
    out = compose_today_reply(vault, now)

    # Today-tomorrow scope headers present.
    assert "### Today" in out
    assert "### Tomorrow" in out
    # Brief-scope headers NEVER appear — the regression pin.
    assert "### This Week" not in out
    assert "### Later" not in out
    # +3d and +7d events absent from output.
    assert "Three Days Out" not in out
    assert "Seven Days Out" not in out
    # Today + Tomorrow events present.
    assert "Today Lunch" in out
    assert "Tomorrow Standup" in out
