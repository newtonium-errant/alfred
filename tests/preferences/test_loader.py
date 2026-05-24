"""Tests for ``alfred.preferences.loader`` — round-trip + filter behaviour.

V1 contract:
- ``load_active_preferences`` returns only ``status: active`` records.
- ``shape`` filter restricts to one shape.
- Missing preference/ directory returns [] without raising + emits a log.
- Per-record fields are coerced via the dataclass constructor.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
import structlog

from alfred.preferences.loader import Preference, load_active_preferences

from ._fixtures import write_preference


def test_round_trip_loads_active_action_and_voice(tmp_path: Path) -> None:
    """Two active preferences (one Shape A, one Shape B) both load."""
    vault = tmp_path / "vault"
    write_preference(
        vault,
        "no-auto-open-houses",
        name="No auto-track of open-house events",
        shape="action",
        scope="universal",
        matcher={
            "domain": "curator",
            "rule": "skip_event_if",
            "args": {"title_regex": "(?i)\\bopen house\\b"},
        },
    )
    write_preference(
        vault,
        "hypatia-no-stop-opener",
        name="Hypatia avoid stop-prefix replies",
        shape="voice",
        scope="instance",
        applies_to_instance="Hypatia",
        policy_body="Don't open replies with the word 'stop'.",
    )

    prefs = load_active_preferences(vault)
    assert len(prefs) == 2
    slugs = {p.slug for p in prefs}
    assert slugs == {"no-auto-open-houses", "hypatia-no-stop-opener"}

    action = next(p for p in prefs if p.shape == "action")
    assert action.matcher is not None
    assert action.matcher["rule"] == "skip_event_if"
    assert action.matcher["args"]["title_regex"] == r"(?i)\bopen house\b"

    voice = next(p for p in prefs if p.shape == "voice")
    assert voice.applies_to_instance == "Hypatia"
    assert "stop" in voice.body.lower()


def test_revoked_records_excluded(tmp_path: Path) -> None:
    """``status: revoked`` records do not appear in the active list."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "active-one",
        name="Active",
        shape="voice", scope="universal",
        status="active",
    )
    write_preference(
        vault, "revoked-one",
        name="Revoked",
        shape="voice", scope="universal",
        status="revoked",
    )

    prefs = load_active_preferences(vault)
    assert len(prefs) == 1
    assert prefs[0].slug == "active-one"


def test_shape_filter_action(tmp_path: Path) -> None:
    """``shape='action'`` returns only action records."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "action-pref",
        name="A",
        shape="action", scope="universal",
        matcher={"domain": "brief", "rule": "skip_brief_event_if",
                 "args": {"title_regex": "test"}},
    )
    write_preference(
        vault, "voice-pref",
        name="V",
        shape="voice", scope="universal",
    )

    action_only = load_active_preferences(vault, shape="action")
    assert len(action_only) == 1
    assert action_only[0].shape == "action"

    voice_only = load_active_preferences(vault, shape="voice")
    assert len(voice_only) == 1
    assert voice_only[0].shape == "voice"


def test_missing_directory_returns_empty_and_logs(
    tmp_path: Path,
) -> None:
    """Missing preference/ directory returns [] AND emits no_directory log.

    Per ``feedback_intentionally_left_blank.md`` — silent absence
    must be distinguishable from a working loader with zero records.

    Uses ``structlog.testing.capture_logs`` (not caplog) per
    ``feedback_structlog_assertion_patterns.md`` — caplog has a
    cross-test cache-bust dependency that causes ordering-dependent
    failures in the full suite. capture_logs is the cache-safe path
    even though the loader is sync; it pins the structlog event
    name + key fields rather than relying on stdlib log shadowing.
    """
    vault = tmp_path / "no-such-vault"
    vault.mkdir()
    # NO preference/ subdirectory created.

    with structlog.testing.capture_logs() as captured:
        prefs = load_active_preferences(vault)

    assert prefs == []
    matches = [c for c in captured if c.get("event") == "preferences.no_directory"]
    assert len(matches) == 1, (
        f"expected exactly one no_directory event; got {len(matches)} — "
        f"events: {[c.get('event') for c in captured]}"
    )
    assert "shape_filter" in matches[0]
    assert matches[0]["shape_filter"] is None


def test_structlog_loaded_event_fires_with_count(
    tmp_path: Path,
) -> None:
    """``preferences.loaded`` structlog event fires with count field.

    Per ``feedback_log_emission_test_pattern.md`` — pin observability
    log emissions, not just behaviour, so refactors that drop the
    log fail tests rather than silently degrading the operator-grep
    workflow.
    """
    vault = tmp_path / "vault"
    write_preference(
        vault, "one-pref",
        name="One",
        shape="voice", scope="universal",
    )

    with structlog.testing.capture_logs() as captured:
        prefs = load_active_preferences(vault)

    assert len(prefs) == 1
    matches = [c for c in captured if c.get("event") == "preferences.loaded"]
    assert len(matches) == 1
    assert matches[0]["count"] == 1
    assert matches[0]["shape_filter"] is None


def test_preference_dataclass_preserves_raw(tmp_path: Path) -> None:
    """Unknown fields land in ``raw`` for forward-compat consumers."""
    vault = tmp_path / "vault"
    pref_dir = vault / "preference"
    pref_dir.mkdir(parents=True)
    # Write a record with an extra ``v2_future_field`` that the
    # dataclass doesn't know about — should still load + preserve in raw.
    (pref_dir / "future.md").write_text(
        "---\n"
        "type: preference\n"
        "status: active\n"
        "name: Future\n"
        "shape: voice\n"
        "scope: universal\n"
        "v2_future_field: experimental_value\n"
        "created: '2026-05-24'\n"
        "---\n"
        "\n"
        "# Future\n",
        encoding="utf-8",
    )

    prefs = load_active_preferences(vault)
    assert len(prefs) == 1
    assert prefs[0].raw.get("v2_future_field") == "experimental_value"


def test_non_preference_files_ignored(tmp_path: Path) -> None:
    """Files in preference/ that aren't ``type: preference`` are skipped."""
    vault = tmp_path / "vault"
    pref_dir = vault / "preference"
    pref_dir.mkdir(parents=True)
    # A README-style file in the directory.
    (pref_dir / "README.md").write_text(
        "---\ntype: note\nname: README\ncreated: '2026-05-24'\n---\nReadme.\n",
        encoding="utf-8",
    )
    write_preference(
        vault, "real-pref",
        name="Real",
        shape="voice", scope="universal",
    )

    prefs = load_active_preferences(vault)
    assert len(prefs) == 1
    assert prefs[0].slug == "real-pref"
