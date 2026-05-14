"""Tests for ``alfred.distiller.state`` — specifically the
``last_error`` diagnostic field added 2026-05-14.

Background — mirror of the brief.state contract that landed today.
Distiller's daemon-loop has the same ``except Exception:`` swallow
at ``daemon.py:749`` that swallowed the May 10 brief silent-failure
class; the cross-daemon audit memo
``project_cross_daemon_swallow_audit.md`` catalogued the pattern.
This module pins the ``last_error`` round-trip + clear-on-success
+ schema-tolerance contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from alfred.distiller.state import DistillerState, RunResult


# ---------------------------------------------------------------------------
# last_error round-trip — the new diagnostic field
# ---------------------------------------------------------------------------


class TestLastErrorRoundTrip:
    def test_default_is_none(self, tmp_path: Path) -> None:
        state = DistillerState(tmp_path / "s.json")
        assert state.last_error is None

    def test_save_includes_last_error(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state = DistillerState(state_path)
        state.last_error = {
            "ts": "2026-05-14T06:00:00+00:00",
            "message": "KeyError: 'foo'",
        }
        state.save()
        data = json.loads(state_path.read_text())
        assert data["last_error"] == state.last_error

    def test_load_restores_last_error(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "files": {},
                "runs": {},
                "extraction_log": [],
                "pending_writes": {},
                "last_deep_extraction": None,
                "last_error": {
                    "ts": "2026-05-14T06:00:00+00:00",
                    "message": "KeyError: 'foo'",
                },
            }),
            encoding="utf-8",
        )
        state = DistillerState(state_path)
        state.load()
        assert state.last_error == {
            "ts": "2026-05-14T06:00:00+00:00",
            "message": "KeyError: 'foo'",
        }

    def test_full_round_trip_with_last_error(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        s1 = DistillerState(state_path)
        s1.last_error = {"ts": "2026-05-14T06:00:00+00:00", "message": "boom"}
        s1.save()

        s2 = DistillerState(state_path)
        s2.load()
        assert s2.last_error == s1.last_error


# ---------------------------------------------------------------------------
# Schema tolerance — older state files without the field, corrupt shape
# ---------------------------------------------------------------------------


class TestSchemaTolerance:
    def test_loads_older_state_without_last_error_field(
        self, tmp_path: Path,
    ) -> None:
        # Pre-2026-05-14 state files won't have last_error at all.
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "files": {},
                "runs": {},
                "extraction_log": [],
                "pending_writes": {},
                "last_deep_extraction": None,
            }),
            encoding="utf-8",
        )
        state = DistillerState(state_path)
        state.load()
        assert state.last_error is None

    def test_corrupt_last_error_value_degrades_to_none(
        self, tmp_path: Path,
    ) -> None:
        # Non-dict last_error value (operator hand-edit, schema mishap)
        # degrades silently to None so consumers don't crash.
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "files": {},
                "runs": {},
                "extraction_log": [],
                "pending_writes": {},
                "last_deep_extraction": None,
                "last_error": "not-a-dict",
            }),
            encoding="utf-8",
        )
        state = DistillerState(state_path)
        state.load()
        assert state.last_error is None


# ---------------------------------------------------------------------------
# record_error — captures + persists, defensive on save-failure
# ---------------------------------------------------------------------------


class TestRecordError:
    def test_record_error_sets_last_error(self, tmp_path: Path) -> None:
        state = DistillerState(tmp_path / "s.json")
        state.record_error("KeyError: 'foo'")
        assert state.last_error is not None
        assert state.last_error["message"] == "KeyError: 'foo'"
        assert "T" in state.last_error["ts"]

    def test_record_error_persists_to_disk(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state = DistillerState(state_path)
        state.record_error("KeyError: 'foo'")
        data = json.loads(state_path.read_text())
        assert data["last_error"]["message"] == "KeyError: 'foo'"

    def test_record_error_replaces_previous(self, tmp_path: Path) -> None:
        state = DistillerState(tmp_path / "s.json")
        state.record_error("first error")
        first_ts = state.last_error["ts"]
        state.record_error("second error")
        assert state.last_error["message"] == "second error"
        assert state.last_error["ts"] >= first_ts

    def test_record_error_save_failure_does_not_crash(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Daemons must not crash on secondary save failures.
        state = DistillerState(tmp_path / "s.json")

        def fail_save(self) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(DistillerState, "save", fail_save)
        # Must not raise; must emit warning log so a future refactor
        # that drops the log line shows up in CI. Per
        # ``feedback_log_emission_test_pattern.md``.
        with structlog.testing.capture_logs() as captured:
            state.record_error("some error")
        assert state.last_error is not None
        matches = [
            c for c in captured
            if c.get("event") == "distiller.state.record_error_save_failed"
        ]
        assert len(matches) == 1
        assert "disk full" in matches[0]["error"]


# ---------------------------------------------------------------------------
# add_run clear-on-success — the recovery semantics
# ---------------------------------------------------------------------------


class TestClearOnSuccess:
    def test_add_run_clears_last_error(self, tmp_path: Path) -> None:
        # Reaching add_run means a deep extraction completed without
        # raising → wipe stale failure context.
        state = DistillerState(tmp_path / "s.json")
        state.last_error = {
            "ts": "2026-05-14T05:00:00+00:00",
            "message": "boom",
        }
        result = RunResult(
            run_id="r1",
            timestamp="2026-05-14T06:00:00+00:00",
        )
        state.add_run(result)
        assert state.last_error is None
        # Run was actually recorded.
        assert "r1" in state.runs

    def test_add_run_with_no_prior_error_is_noop(self, tmp_path: Path) -> None:
        # No regression on happy path.
        state = DistillerState(tmp_path / "s.json")
        result = RunResult(
            run_id="r1",
            timestamp="2026-05-14T06:00:00+00:00",
        )
        state.add_run(result)
        assert state.last_error is None
        assert "r1" in state.runs


# ---------------------------------------------------------------------------
# Integration — record_error → reload preserves
# ---------------------------------------------------------------------------


class TestRecordErrorThenReload:
    def test_record_error_then_reload_preserves(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        s1 = DistillerState(state_path)
        s1.record_error("KeyError: 'foo'")

        s2 = DistillerState(state_path)
        s2.load()
        assert s2.last_error is not None
        assert s2.last_error["message"] == "KeyError: 'foo'"

    def test_successful_run_after_reload_clears_error(
        self, tmp_path: Path,
    ) -> None:
        state_path = tmp_path / "s.json"
        s1 = DistillerState(state_path)
        s1.record_error("KeyError: 'foo'")

        s2 = DistillerState(state_path)
        s2.load()
        result = RunResult(
            run_id="r1",
            timestamp="2026-05-14T06:00:00+00:00",
        )
        s2.add_run(result)
        s2.save()

        s3 = DistillerState(state_path)
        s3.load()
        assert s3.last_error is None
