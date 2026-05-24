"""Test for brief upcoming_events — preference action gate.

Per ``project_operator_preferences_v1.md`` Hard Contract — V1 wires
``skip_brief_event_if`` and ``skip_brief_task_if`` into the brief's
upcoming-events renderer. Filtered candidates are dropped from the
rendered output AND a footer line names the count so the operator
sees the gate fired without grepping logs.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path

import pytest
import structlog

from alfred.brief.config import UpcomingEventsConfig
from alfred.brief.upcoming_events import render_upcoming_events_section

from ._fixtures import write_preference


TODAY = date(2026, 5, 24)


def _write_event(
    vault: Path,
    name: str,
    event_date: str,
    *,
    status: str | None = None,
) -> None:
    """Drop a minimal event record into ``vault/event/<name>.md``."""
    lines = [
        "---",
        "type: event",
        f"name: {name}",
        f"date: {event_date}",
    ]
    if status is not None:
        lines.append(f"status: '{status}'")
    lines.extend([
        "created: 2026-05-01",
        "tags: []",
        "---",
        "",
        f"# {name}",
        "",
    ])
    (vault / "event").mkdir(parents=True, exist_ok=True)
    (vault / "event" / f"{name}.md").write_text("\n".join(lines), encoding="utf-8")


def test_open_house_event_filtered_from_brief(tmp_path: Path) -> None:
    """Open-house event in the upcoming window is omitted; footer fires."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "no-open-houses",
        name="No open houses",
        shape="action", scope="universal",
        matcher={"domain": "brief", "rule": "skip_brief_event_if",
                 "args": {"title_regex": "(?i)\\bopen house\\b"}},
    )

    # One filterable, one not.
    target = TODAY + timedelta(days=2)
    _write_event(vault, "Open House Tonight", target.isoformat())
    _write_event(vault, "Dentist", target.isoformat())

    out = render_upcoming_events_section(
        UpcomingEventsConfig(enabled=True, max_days_ahead=30),
        vault, TODAY,
    )
    assert "Open House Tonight" not in out
    assert "Dentist" in out
    # Footer line surfaces the gate effect to the operator.
    assert "filtered by operator preferences" in out
    assert "1 item filtered" in out


def test_multiple_filters_pluralised(tmp_path: Path) -> None:
    """N>1 filtered → footer says 'N items' (plural)."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "no-open-houses",
        name="No open houses",
        shape="action", scope="universal",
        matcher={"domain": "brief", "rule": "skip_brief_event_if",
                 "args": {"title_regex": "(?i)\\bopen house\\b"}},
    )

    target = TODAY + timedelta(days=3)
    _write_event(vault, "Open House at 99 Elm", target.isoformat())
    _write_event(vault, "Open House at 123 Main", target.isoformat())

    out = render_upcoming_events_section(
        UpcomingEventsConfig(enabled=True, max_days_ahead=30),
        vault, TODAY,
    )
    assert "2 items filtered" in out


def test_filter_log_fires_with_preference_slug(tmp_path: Path) -> None:
    """Per-drop log carries the preference slug + reason (operator-grep)."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "no-open-houses",
        name="No open houses",
        shape="action", scope="universal",
        matcher={"domain": "brief", "rule": "skip_brief_event_if",
                 "args": {"title_regex": "(?i)open house"}},
    )

    target = TODAY + timedelta(days=2)
    _write_event(vault, "Open House Tonight", target.isoformat())

    with structlog.testing.capture_logs() as captured:
        render_upcoming_events_section(
            UpcomingEventsConfig(enabled=True, max_days_ahead=30),
            vault, TODAY,
        )

    matches = [
        c for c in captured
        if c.get("event") == "upcoming_events.preference_filtered"
    ]
    assert len(matches) == 1
    assert matches[0]["preference_slug"] == "no-open-houses"
    assert matches[0]["rec_type"] == "event"


def test_no_preferences_no_filter_no_footer(tmp_path: Path) -> None:
    """Zero preferences → no filtering, no footer."""
    vault = tmp_path / "vault"
    (vault / "preference").mkdir(parents=True)
    # No preference records written.

    target = TODAY + timedelta(days=2)
    _write_event(vault, "Open House Tonight", target.isoformat())

    out = render_upcoming_events_section(
        UpcomingEventsConfig(enabled=True, max_days_ahead=30),
        vault, TODAY,
    )
    assert "Open House Tonight" in out
    assert "filtered by operator preferences" not in out


def test_filtered_to_empty_shows_sentinel_plus_footer(tmp_path: Path) -> None:
    """All events filtered → 'No upcoming events.' AND footer.

    The combined output explains itself: operator sees "nothing
    scheduled, AND N filtered" so they don't wonder whether the
    preference accidentally hid something they wanted.
    """
    vault = tmp_path / "vault"
    write_preference(
        vault, "no-open-houses",
        name="No open houses",
        shape="action", scope="universal",
        matcher={"domain": "brief", "rule": "skip_brief_event_if",
                 "args": {"title_regex": "(?i)open house"}},
    )

    target = TODAY + timedelta(days=2)
    _write_event(vault, "Open House Tonight", target.isoformat())

    out = render_upcoming_events_section(
        UpcomingEventsConfig(enabled=True, max_days_ahead=30),
        vault, TODAY,
    )
    assert "No upcoming events." in out
    assert "filtered by operator preferences" in out
