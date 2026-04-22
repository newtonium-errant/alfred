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
) -> None:
    """Drop a task record into ``vault/task/{name}.md``. ``due`` may be
    omitted to test the "task without due is excluded" path."""
    lines = ["---", "type: task", "status: todo", f"name: {name}"]
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
