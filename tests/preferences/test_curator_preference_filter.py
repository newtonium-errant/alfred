"""Test for curator stage 1.5 — preference action gate.

Per ``project_operator_preferences_v1.md`` Hard Contract — V1 wires
``skip_event_if`` into the curator pipeline BEFORE Stage 2 (entity
creation) so a filtered event never lands on disk. The unit-level
test exercises ``_apply_preference_filter`` directly with a synthetic
manifest; integration via the live OpenClaw pipeline is out of scope
for the unit suite.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import structlog

from alfred.curator.pipeline import _apply_preference_filter
from alfred.preferences.loader import Preference, load_active_preferences

from ._fixtures import write_preference


def _build_pref(
    slug: str,
    rule: str,
    title_regex: str,
    *,
    domain: str = "curator",
) -> Preference:
    """Build a Preference dataclass directly (no on-disk write).

    Mirrors what the loader would return for a Shape A action record
    with the given matcher dispatch.
    """
    return Preference(
        slug=slug,
        name=slug,
        shape="action",
        scope="universal",
        applies_to_instance=None,
        applies_to_user=None,
        cites_canonical=None,
        source_quote="",
        source_session="",
        matcher={
            "domain": domain,
            "rule": rule,
            "args": {"title_regex": title_regex},
        },
        body="",
        path=Path("/tmp/test"),
        raw={},
    )


def test_open_house_event_dropped_from_manifest() -> None:
    """Stage 1 manifest with an open-house event → dropped by the gate."""
    manifest = [
        {"type": "person", "name": "Sarah Smith"},
        {"type": "event", "name": "Open House at 123 Main"},
        {"type": "event", "name": "Lunch with Sarah"},
    ]
    prefs = [_build_pref(
        "no-open-houses", "skip_event_if", r"(?i)\bopen house\b",
    )]

    result = _apply_preference_filter(manifest, prefs)
    names = [e["name"] for e in result]
    assert "Open House at 123 Main" not in names
    assert "Lunch with Sarah" in names
    assert "Sarah Smith" in names
    assert len(result) == 2


def test_dropped_log_carries_preference_slug_and_reason() -> None:
    """Per ``feedback_log_emission_test_pattern.md`` — pin the drop log."""
    manifest = [
        {"type": "event", "name": "Open House Tonight"},
    ]
    prefs = [_build_pref(
        "no-open-houses", "skip_event_if", r"(?i)\bopen house\b",
    )]

    with structlog.testing.capture_logs() as captured:
        _apply_preference_filter(manifest, prefs)

    drops = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_dropped"
    ]
    assert len(drops) == 1
    assert drops[0]["preference_slug"] == "no-open-houses"
    assert drops[0]["entity_type"] == "event"
    assert drops[0]["entity_name"] == "Open House Tonight"
    assert drops[0]["rule"] == "skip_event_if"
    # Reason is the matcher's grep-able string.
    assert "Open House Tonight" in drops[0]["reason"]


def test_no_prefs_short_circuits_with_run_log() -> None:
    """Zero preferences → manifest passes through, run log fires with drops=0.

    Per ``feedback_intentionally_left_blank.md`` — the no-op case
    must still emit a run signal so operator-grep can distinguish
    "filter ran, nothing matched" from "filter never ran."
    """
    manifest = [{"type": "event", "name": "Anything"}]

    with structlog.testing.capture_logs() as captured:
        result = _apply_preference_filter(manifest, [])

    assert result == manifest
    runs = [
        c for c in captured
        if c.get("event") == "curator.preference_filter_run"
    ]
    assert len(runs) == 1
    assert runs[0]["drops"] == 0
    assert runs[0]["preferences_loaded"] == 0


def test_non_event_types_pass_through() -> None:
    """Person / org / project candidates are unaffected by V1 (event-only)."""
    manifest = [
        {"type": "person", "name": "Open House Promoter"},
        {"type": "org", "name": "Open House Realty"},
    ]
    prefs = [_build_pref(
        "no-open-houses", "skip_event_if", r"(?i)\bopen house\b",
    )]

    result = _apply_preference_filter(manifest, prefs)
    # Both pass — V1 only gates event records.
    assert len(result) == 2


def test_domain_filter_curator_only() -> None:
    """A preference whose matcher.domain is ``brief`` doesn't fire for curator."""
    manifest = [{"type": "event", "name": "Open House"}]
    pref_brief = _build_pref(
        "brief-only", "skip_event_if", r"(?i)open house", domain="brief",
    )
    pref_curator = _build_pref(
        "curator-pref", "skip_event_if", r"(?i)open house", domain="curator",
    )

    # Brief-domain preference doesn't fire in curator pipeline.
    result = _apply_preference_filter(manifest, [pref_brief])
    assert len(result) == 1  # kept

    # Curator-domain preference does fire.
    result = _apply_preference_filter(manifest, [pref_curator])
    assert len(result) == 0  # dropped


def test_loader_integration_writes_then_filters(tmp_path: Path) -> None:
    """End-to-end: write a pref via the fixture, load via loader, filter."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "no-open-houses",
        name="No open houses",
        shape="action", scope="universal",
        matcher={"domain": "curator", "rule": "skip_event_if",
                 "args": {"title_regex": "(?i)\\bopen house\\b"}},
    )

    prefs = load_active_preferences(vault, shape="action")
    manifest = [
        {"type": "event", "name": "Open House at 99 Elm"},
        {"type": "event", "name": "Birthday party"},
    ]
    result = _apply_preference_filter(manifest, prefs)
    assert len(result) == 1
    assert result[0]["name"] == "Birthday party"
