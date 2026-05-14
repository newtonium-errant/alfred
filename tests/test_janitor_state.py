"""Tests for ``alfred.janitor.state`` — specifically the ``last_error``
diagnostic field added 2026-05-14.

Background — mirror of the brief.state contract that landed today.
Janitor's daemon-loop has the same ``except Exception:`` swallow at
``daemon.py:639`` that swallowed the May 10 brief silent-failure
class; the cross-daemon audit memo
``project_cross_daemon_swallow_audit.md`` catalogued the pattern and
the dispatch picked janitor as the first follow-up because its
state-shape is a direct match for the brief template.

This module pins the ``last_error`` round-trip + clear-on-success
+ schema-tolerance contract. Sibling test surface for the probe-side
surfacing lives at ``tests/health/test_janitor_probes.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from alfred.janitor.state import JanitorState


# ---------------------------------------------------------------------------
# last_error round-trip — the new diagnostic field
# ---------------------------------------------------------------------------


class TestLastErrorRoundTrip:
    def test_default_is_none(self, tmp_path: Path) -> None:
        state = JanitorState(tmp_path / "s.json")
        assert state.last_error is None

    def test_save_includes_last_error(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state = JanitorState(state_path)
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
                "sweeps": {},
                "fix_log": [],
                "ignored": {},
                "pending_writes": {},
                "last_deep_sweep": None,
                "previous_sweep_issues": {},
                "triage_ids_seen": [],
                "last_error": {
                    "ts": "2026-05-14T06:00:00+00:00",
                    "message": "KeyError: 'foo'",
                },
            }),
            encoding="utf-8",
        )
        state = JanitorState(state_path)
        state.load()
        assert state.last_error == {
            "ts": "2026-05-14T06:00:00+00:00",
            "message": "KeyError: 'foo'",
        }

    def test_full_round_trip_with_last_error(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        s1 = JanitorState(state_path)
        s1.last_error = {"ts": "2026-05-14T06:00:00+00:00", "message": "boom"}
        s1.save()

        s2 = JanitorState(state_path)
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
        # Must load with default None.
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "files": {},
                "sweeps": {},
                "fix_log": [],
                "ignored": {},
                "pending_writes": {},
                "last_deep_sweep": None,
                "previous_sweep_issues": {},
                "triage_ids_seen": [],
            }),
            encoding="utf-8",
        )
        state = JanitorState(state_path)
        state.load()
        assert state.last_error is None

    def test_corrupt_last_error_value_degrades_to_none(
        self, tmp_path: Path,
    ) -> None:
        # If somehow last_error landed as a non-dict (operator hand-
        # edit, schema migration mishap), load() degrades to None so
        # downstream consumers don't crash on attribute access.
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "files": {},
                "sweeps": {},
                "fix_log": [],
                "ignored": {},
                "pending_writes": {},
                "last_deep_sweep": None,
                "previous_sweep_issues": {},
                "triage_ids_seen": [],
                "last_error": "not-a-dict",
            }),
            encoding="utf-8",
        )
        state = JanitorState(state_path)
        state.load()
        assert state.last_error is None


# ---------------------------------------------------------------------------
# record_error — captures + persists, defensive on save-failure
# ---------------------------------------------------------------------------


class TestRecordError:
    def test_record_error_sets_last_error(self, tmp_path: Path) -> None:
        state = JanitorState(tmp_path / "s.json")
        state.record_error("KeyError: 'foo'")
        assert state.last_error is not None
        assert state.last_error["message"] == "KeyError: 'foo'"
        assert "T" in state.last_error["ts"]

    def test_record_error_persists_to_disk(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state = JanitorState(state_path)
        state.record_error("KeyError: 'foo'")
        data = json.loads(state_path.read_text())
        assert data["last_error"]["message"] == "KeyError: 'foo'"

    def test_record_error_replaces_previous(self, tmp_path: Path) -> None:
        state = JanitorState(tmp_path / "s.json")
        state.record_error("first error")
        first_ts = state.last_error["ts"]
        state.record_error("second error")
        assert state.last_error["message"] == "second error"
        # Either equal (clock-resolution) or newer.
        assert state.last_error["ts"] >= first_ts

    def test_record_error_save_failure_does_not_crash(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Daemons must not crash on secondary save failures — that
        # would compound a sweep failure into a process exit.
        state = JanitorState(tmp_path / "s.json")

        def fail_save(self) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(JanitorState, "save", fail_save)
        # Must not raise; must emit the warning log so a future refactor
        # that drops the log line shows up in CI. Per
        # ``feedback_log_emission_test_pattern.md`` — log-emission tests
        # must drive the production code path.
        with structlog.testing.capture_logs() as captured:
            state.record_error("some error")
        # In-memory state still got updated; only persistence failed.
        assert state.last_error is not None
        matches = [
            c for c in captured
            if c.get("event") == "janitor.state.record_error_save_failed"
        ]
        assert len(matches) == 1
        assert "disk full" in matches[0]["error"]


# ---------------------------------------------------------------------------
# save_sweep_issues clear-on-success — the recovery semantics
# ---------------------------------------------------------------------------


class TestClearOnSuccess:
    def test_save_sweep_issues_clears_last_error(self, tmp_path: Path) -> None:
        # The recovery semantics: a successful sweep tick reaches
        # save_sweep_issues, which means the outer except DID NOT
        # fire — so wipe any stale error context.
        state = JanitorState(tmp_path / "s.json")
        state.last_error = {
            "ts": "2026-05-14T05:00:00+00:00",
            "message": "boom",
        }
        state.save_sweep_issues({"foo.md": ["LINK001"]})
        assert state.last_error is None

    def test_save_sweep_issues_with_no_prior_error_is_noop(
        self, tmp_path: Path,
    ) -> None:
        # No regression on the happy path: clear when nothing to clear.
        state = JanitorState(tmp_path / "s.json")
        state.save_sweep_issues({"foo.md": ["LINK001"]})
        assert state.last_error is None


# ---------------------------------------------------------------------------
# Integration — record_error → reload preserves
# ---------------------------------------------------------------------------


class TestRecordErrorThenReload:
    def test_record_error_then_reload_preserves(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        s1 = JanitorState(state_path)
        s1.record_error("KeyError: 'foo'")

        # Fresh state, same path — simulates daemon restart.
        s2 = JanitorState(state_path)
        s2.load()
        assert s2.last_error is not None
        assert s2.last_error["message"] == "KeyError: 'foo'"

    def test_successful_sweep_after_reload_clears_error(
        self, tmp_path: Path,
    ) -> None:
        state_path = tmp_path / "s.json"
        s1 = JanitorState(state_path)
        s1.record_error("KeyError: 'foo'")

        s2 = JanitorState(state_path)
        s2.load()
        s2.save_sweep_issues({"foo.md": ["LINK001"]})
        s2.save()

        s3 = JanitorState(state_path)
        s3.load()
        assert s3.last_error is None
