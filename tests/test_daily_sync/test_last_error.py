"""Tests for the daily_sync ``last_error`` diagnostic helpers added
2026-05-14.

Background — mirror of the brief.state contract (commit 279c0c0) and
its janitor/distiller siblings (66a6344 + 13529c5). daily_sync's
daemon-loop has the same ``except Exception:`` swallow at
``daemon.py:378`` that swallowed the May 10 brief silent-failure
class; the cross-daemon audit memo
``project_cross_daemon_swallow_audit.md`` catalogued the pattern.

daily_sync state is dict-shaped (not a dataclass StateManager like
brief / janitor / distiller); the helpers
:func:`record_error_on_state` and :func:`clear_last_error_on_state`
provide the equivalent semantics on the inline-dict shape.

Sibling test surface for the probe-side rendering lives at
``tests/health/test_daily_sync_probes.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from alfred.daily_sync.confidence import (
    clear_last_error_on_state,
    load_state,
    record_error_on_state,
    save_state,
)


# ---------------------------------------------------------------------------
# record_error_on_state — captures + persists
# ---------------------------------------------------------------------------


class TestRecordErrorOnState:
    def test_sets_last_error_on_disk(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        record_error_on_state(state_path, "KeyError: 'foo'")
        data = json.loads(state_path.read_text())
        assert data["last_error"]["message"] == "KeyError: 'foo'"
        assert "T" in data["last_error"]["ts"]

    def test_preserves_existing_state_fields(self, tmp_path: Path) -> None:
        # An existing state dict (last_fired_date, confidence, etc.)
        # must NOT be clobbered by the error write.
        state_path = tmp_path / "s.json"
        save_state(state_path, {
            "last_fired_date": "2026-05-13",
            "confidence": {"high": True, "medium": False, "low": False, "spam": False},
        })
        record_error_on_state(state_path, "boom")
        data = json.loads(state_path.read_text())
        assert data["last_fired_date"] == "2026-05-13"
        assert data["confidence"] == {
            "high": True, "medium": False, "low": False, "spam": False,
        }
        assert data["last_error"]["message"] == "boom"

    def test_replaces_previous_error(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        record_error_on_state(state_path, "first")
        first = json.loads(state_path.read_text())["last_error"]
        record_error_on_state(state_path, "second")
        second = json.loads(state_path.read_text())["last_error"]
        assert second["message"] == "second"
        assert second["ts"] >= first["ts"]

    def test_save_failure_does_not_crash(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Daemons must not crash on secondary save failures.
        state_path = tmp_path / "s.json"

        def fail_save(*args, **kwargs) -> None:
            raise OSError("disk full")

        # Patch the save_state used inside confidence.py.
        monkeypatch.setattr(
            "alfred.daily_sync.confidence.save_state", fail_save,
        )
        # Must not raise; must emit warning log so a future refactor
        # that drops the log line shows up in CI. Per
        # ``feedback_log_emission_test_pattern.md``.
        with structlog.testing.capture_logs() as captured:
            record_error_on_state(state_path, "some error")
        matches = [
            c for c in captured
            if c.get("event") == "daily_sync.state.record_error_save_failed"
        ]
        assert len(matches) == 1
        assert "disk full" in matches[0]["error"]

    def test_works_on_empty_state_file(self, tmp_path: Path) -> None:
        # Fresh-install path: state file doesn't exist yet. The
        # load_state returns {} and the write creates the file.
        state_path = tmp_path / "s.json"
        assert not state_path.exists()
        record_error_on_state(state_path, "early failure")
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        assert data["last_error"]["message"] == "early failure"


# ---------------------------------------------------------------------------
# clear_last_error_on_state — wipes the field, defensive on absence
# ---------------------------------------------------------------------------


class TestClearLastErrorOnState:
    def test_clears_populated_last_error(self) -> None:
        state = {
            "last_error": {
                "ts": "2026-05-14T05:00:00+00:00",
                "message": "boom",
            },
            "last_fired_date": "2026-05-13",
        }
        clear_last_error_on_state(state)
        assert state["last_error"] is None
        # Other fields untouched.
        assert state["last_fired_date"] == "2026-05-13"

    def test_noop_when_last_error_absent(self) -> None:
        # The happy path on a fresh state dict — clearing a missing
        # field must not add an empty entry that confuses downstream
        # consumers.
        state = {"last_fired_date": "2026-05-13"}
        clear_last_error_on_state(state)
        # Key was absent — stays absent. The probe-side _read_last_error
        # treats both absent-key and None-value identically, but we
        # don't bloat the state file with explicit nulls.
        assert "last_error" not in state

    def test_clears_null_last_error_to_null(self) -> None:
        # If last_error was already None (e.g. a previous successful
        # tick wrote it), clearing keeps it None. No-op behaviour.
        state = {"last_error": None}
        clear_last_error_on_state(state)
        assert state["last_error"] is None


# ---------------------------------------------------------------------------
# Integration — record then clear round-trip via load/save
# ---------------------------------------------------------------------------


class TestRecordThenClearRoundTrip:
    def test_record_then_clear_then_reload(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        # Record a failure.
        record_error_on_state(state_path, "boom")
        loaded = load_state(state_path)
        assert loaded["last_error"]["message"] == "boom"

        # Clear-on-success path: load, clear, save.
        loaded["last_fired_date"] = "2026-05-14"
        clear_last_error_on_state(loaded)
        save_state(state_path, loaded)

        # Reload and verify cleared.
        reloaded = load_state(state_path)
        assert reloaded["last_error"] is None
        assert reloaded["last_fired_date"] == "2026-05-14"

    def test_corrupt_state_file_recovers_via_record_error(
        self, tmp_path: Path,
    ) -> None:
        # load_state tolerates corrupt JSON by returning {}.
        # record_error_on_state must still work — it starts from a
        # clean dict and writes a valid state file.
        state_path = tmp_path / "s.json"
        state_path.write_text("not json {{{", encoding="utf-8")
        record_error_on_state(state_path, "post-corrupt boom")
        # File is now valid JSON containing the error.
        data = json.loads(state_path.read_text())
        assert data["last_error"]["message"] == "post-corrupt boom"
