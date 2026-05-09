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
from alfred.brief.upcoming_events import render_upcoming_events_section


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
) -> None:
    """Drop an event record into ``vault/event/{name}.md``."""
    lines = [
        "---",
        "type: event",
        f"name: {name}",
        f"date: {event_date}",
    ]
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


def test_event_status_does_not_trigger_task_filter(vault: Path) -> None:
    """The Phase 2 #1 filter applies to TASKS ONLY. An event record
    with future date is unchanged by the new gate.

    Today (Phase 1) events don't carry a ``status`` field at all, but
    a future schema extension might. Per the spec: events go through
    ``_event_date``, NOT the task branch where the closed-state check
    lives. So even if an event eventually grows a ``status`` field
    that happens to match the denyset, the brief renderer doesn't
    apply the task-only gate to it. This test pins that scope."""
    target = TODAY + timedelta(days=3)
    # Drop an event with a status that would match the task-side
    # denyset, just to prove the task gate doesn't apply.
    (vault / "event").mkdir(exist_ok=True)
    (vault / "event" / "Workshop with status.md").write_text(
        "---\n"
        "type: event\n"
        "name: Workshop with status\n"
        f"date: {target.isoformat()}\n"
        # Even if the operator someday added status to events, the
        # task-side gate ignores it. Today this field is just inert.
        "status: cancelled\n"
        "created: 2026-04-01\n"
        "tags: []\n"
        "---\n\n"
        "# Workshop with status\n",
        encoding="utf-8",
    )
    out = render_upcoming_events_section(_default_config(), vault, TODAY)
    assert "Workshop with status" in out
    assert target.isoformat() in out


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
