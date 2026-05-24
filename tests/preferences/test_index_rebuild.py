"""Tests for ``alfred.preferences.index`` — atomic rebuild + projection."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog

from alfred.preferences.index import load_index, rebuild_index

from ._fixtures import write_preference


def test_rebuild_projects_active_action_records(tmp_path: Path) -> None:
    """Active Shape A records land in the index; Shape B excluded."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "no-open-houses",
        name="No auto-track of open-house events",
        shape="action", scope="universal",
        matcher={"domain": "curator", "rule": "skip_event_if",
                 "args": {"title_regex": "(?i)open house"}},
    )
    write_preference(
        vault, "voice-only",
        name="Voice only",
        shape="voice", scope="universal",
    )
    write_preference(
        vault, "revoked-action",
        name="Revoked",
        shape="action", scope="universal",
        status="revoked",
        matcher={"domain": "curator", "rule": "skip_event_if",
                 "args": {"title_regex": "stale"}},
    )

    output = tmp_path / "data" / "operator_preferences.json"
    payload = rebuild_index(vault, output, instance="Salem")

    assert output.exists()
    on_disk = json.loads(output.read_text(encoding="utf-8"))
    assert on_disk == payload

    # Only one record projected (the active Shape A; Shape B + revoked excluded).
    assert len(payload["active_preferences"]) == 1
    pref = payload["active_preferences"][0]
    assert pref["slug"] == "no-open-houses"
    assert pref["shape"] == "action"
    assert pref["matcher"]["rule"] == "skip_event_if"
    assert pref["matcher"]["domain"] == "curator"


def test_rebuild_stamps_metadata(tmp_path: Path) -> None:
    """Index carries generated_at, instance, vault_path."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "x",
        name="X",
        shape="action", scope="universal",
        matcher={"domain": "brief", "rule": "skip_brief_event_if",
                 "args": {"title_regex": "x"}},
    )

    output = tmp_path / "data" / "ops.json"
    payload = rebuild_index(vault, output, instance="Hypatia")

    assert payload["instance"] == "Hypatia"
    assert payload["vault_path"] == str(vault)
    # ISO 8601 — round-trips through fromisoformat.
    from datetime import datetime
    parsed = datetime.fromisoformat(payload["generated_at"])
    assert parsed is not None


def test_rebuild_atomic_write_uses_tmp(tmp_path: Path) -> None:
    """The ``.tmp`` file is gone after rebuild (renamed over output)."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "x", name="X", shape="action", scope="universal",
        matcher={"domain": "curator", "rule": "skip_event_if",
                 "args": {"title_regex": "x"}},
    )

    output = tmp_path / "data" / "ops.json"
    rebuild_index(vault, output)

    # No leftover .tmp file.
    tmp_file = output.with_suffix(output.suffix + ".tmp")
    assert not tmp_file.exists()


def test_rebuild_zero_preferences_emits_log_and_empty_index(
    tmp_path: Path,
) -> None:
    """Zero active preferences → empty index, count=0 log fires.

    Per ``feedback_intentionally_left_blank.md`` — silent absence
    must be distinguishable from a working rebuild with zero results.
    """
    vault = tmp_path / "vault"
    (vault / "preference").mkdir(parents=True)
    # No records.

    output = tmp_path / "data" / "ops.json"

    with structlog.testing.capture_logs() as captured:
        payload = rebuild_index(vault, output)

    assert payload["active_preferences"] == []
    assert output.exists()

    # Pin the structlog event AND the count field.
    matches = [
        c for c in captured if c.get("event") == "preferences.index_rebuilt"
    ]
    assert len(matches) == 1
    assert matches[0]["count"] == 0


def test_rebuild_index_separation_per_instance(tmp_path: Path) -> None:
    """Per-instance separation: two different vaults → two different indexes.

    Per-instance index is the operator's source-of-truth for which gates
    apply on which instance. Cross-pollution between Salem and Hypatia
    indexes would defeat the routing contract.
    """
    salem_vault = tmp_path / "salem"
    hypatia_vault = tmp_path / "hypatia"

    write_preference(
        salem_vault, "salem-only",
        name="Salem rule",
        shape="action", scope="universal",
        matcher={"domain": "curator", "rule": "skip_event_if",
                 "args": {"title_regex": "salem"}},
    )
    write_preference(
        hypatia_vault, "hypatia-only",
        name="Hypatia rule",
        shape="action", scope="universal",
        matcher={"domain": "brief", "rule": "skip_brief_task_if",
                 "args": {"title_regex": "hypatia"}},
    )

    salem_out = tmp_path / "salem" / "data" / "ops.json"
    hypatia_out = tmp_path / "hypatia" / "data" / "ops.json"
    rebuild_index(salem_vault, salem_out, instance="Salem")
    rebuild_index(hypatia_vault, hypatia_out, instance="Hypatia")

    salem_payload = json.loads(salem_out.read_text(encoding="utf-8"))
    hypatia_payload = json.loads(hypatia_out.read_text(encoding="utf-8"))

    assert salem_payload["instance"] == "Salem"
    assert hypatia_payload["instance"] == "Hypatia"
    assert {p["slug"] for p in salem_payload["active_preferences"]} == {"salem-only"}
    assert {p["slug"] for p in hypatia_payload["active_preferences"]} == {"hypatia-only"}


def test_load_index_missing_returns_none(tmp_path: Path) -> None:
    """``load_index`` on a missing file returns None + logs."""
    missing = tmp_path / "never_written.json"
    result = load_index(missing)
    assert result is None


def test_load_index_roundtrip(tmp_path: Path) -> None:
    """Write via rebuild, read via load — same payload."""
    vault = tmp_path / "vault"
    write_preference(
        vault, "x", name="X", shape="action", scope="universal",
        matcher={"domain": "curator", "rule": "skip_event_if",
                 "args": {"title_regex": "x"}},
    )

    output = tmp_path / "data" / "ops.json"
    written = rebuild_index(vault, output, instance="Salem")
    loaded = load_index(output)
    assert loaded == written
