"""Curator state persistence for ``last_agent_failure`` (2026-07-29 incident).

Pins the load-time schema-tolerance contract for the new field: an old-schema
state file (no ``last_agent_failure`` key) loads to ``None`` rather than
crashing, a malformed value degrades to ``None``, and a round-trip through
``to_dict``/``from_dict`` (and ``StateManager`` save/load) preserves the
recorded failure.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import json
from pathlib import Path

from alfred.curator.state import State, StateManager


def test_record_agent_failure_shape() -> None:
    s = State()
    s.record_agent_failure(kind="quota_limited", summary="Exit code 1: stdout: hit your weekly limit")
    assert s.last_agent_failure is not None
    assert s.last_agent_failure["kind"] == "quota_limited"
    assert "weekly limit" in s.last_agent_failure["summary_tail"]
    assert isinstance(s.last_agent_failure["ts"], str) and s.last_agent_failure["ts"]


def test_record_agent_failure_empty_kind_defaults_to_other() -> None:
    s = State()
    s.record_agent_failure(kind="", summary="boom")
    assert s.last_agent_failure["kind"] == "other"


def test_record_does_not_touch_last_run() -> None:
    # last_run must stay the LAST SUCCESS so the BIT probe can compare the two.
    s = State()
    s.last_run = "2026-07-25T00:00:00+00:00"
    s.record_agent_failure(kind="quota_limited", summary="x")
    assert s.last_run == "2026-07-25T00:00:00+00:00"


def test_roundtrip_preserves_last_agent_failure() -> None:
    s = State()
    s.record_agent_failure(kind="auth", summary="Exit code 1: not logged in")
    restored = State.from_dict(s.to_dict())
    assert restored.last_agent_failure == s.last_agent_failure


def test_old_schema_file_loads_to_none() -> None:
    """A pre-2026-07-29 state file (no key) must load fine → None (tolerance)."""
    old = {"version": 2, "last_run": "2026-07-01T00:00:00+00:00", "processed": {}}
    restored = State.from_dict(old)
    assert restored.last_agent_failure is None
    assert restored.last_run == "2026-07-01T00:00:00+00:00"


def test_malformed_last_agent_failure_degrades_to_none() -> None:
    for bad in ("a string", 42, ["list"], True):
        restored = State.from_dict({"last_agent_failure": bad})
        assert restored.last_agent_failure is None


def test_statemanager_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "curator_state.json"
    mgr = StateManager(path)
    mgr.load()
    mgr.state.record_agent_failure(kind="quota_limited", summary="Exit code 1: hit your weekly limit")
    mgr.save()

    # Reload from disk via a fresh manager.
    mgr2 = StateManager(path)
    reloaded = mgr2.load()
    assert reloaded.last_agent_failure is not None
    assert reloaded.last_agent_failure["kind"] == "quota_limited"

    # And the on-disk JSON actually carries the key.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["last_agent_failure"]["kind"] == "quota_limited"
