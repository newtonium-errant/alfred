"""Tests for ``alfred.instructor.state`` — specifically the
``last_error`` diagnostic field added 2026-05-14.

Background — mirror of the brief / janitor / distiller / daily_sync
last_error patterns shipped earlier today. Instructor's daemon-loop
has the same ``except Exception:`` swallow at ``daemon.py:331`` that
swallowed the May 10 brief silent-failure class; the cross-daemon
audit memo ``project_cross_daemon_swallow_audit.md`` catalogued the
pattern.

Per the dispatch, the clear-on-success boundary is :meth:`stamp_run`
(every successful poll-loop completion) rather than per-deep-run (as
distiller does) because instructor only has one tick cadence.

Sibling test surface for the probe-side rendering lives at
``tests/health/test_instructor_probes.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from alfred.instructor.state import InstructorState


# ---------------------------------------------------------------------------
# last_error round-trip — the new diagnostic field
# ---------------------------------------------------------------------------


class TestLastErrorRoundTrip:
    def test_default_is_none(self, tmp_path: Path) -> None:
        state = InstructorState(tmp_path / "s.json")
        assert state.last_error is None

    def test_save_includes_last_error(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state = InstructorState(state_path)
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
                "file_hashes": {},
                "retry_counts": {},
                "last_run_ts": None,
                "last_error": {
                    "ts": "2026-05-14T06:00:00+00:00",
                    "message": "KeyError: 'foo'",
                },
            }),
            encoding="utf-8",
        )
        state = InstructorState(state_path)
        state.load()
        assert state.last_error == {
            "ts": "2026-05-14T06:00:00+00:00",
            "message": "KeyError: 'foo'",
        }

    def test_full_round_trip_with_last_error(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        s1 = InstructorState(state_path)
        s1.last_error = {"ts": "2026-05-14T06:00:00+00:00", "message": "boom"}
        s1.save()

        s2 = InstructorState(state_path)
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
                "file_hashes": {"note/A.md": "hash-aaa"},
                "retry_counts": {},
                "last_run_ts": "2026-05-13T12:00:00+00:00",
            }),
            encoding="utf-8",
        )
        state = InstructorState(state_path)
        state.load()
        assert state.last_error is None
        # Other fields must still load — schema tolerance must not
        # collateral-damage the rest of the state.
        assert state.file_hashes == {"note/A.md": "hash-aaa"}
        assert state.last_run_ts == "2026-05-13T12:00:00+00:00"

    def test_corrupt_last_error_value_degrades_to_none(
        self, tmp_path: Path,
    ) -> None:
        # Non-dict last_error value (operator hand-edit, schema mishap)
        # degrades silently to None so consumers don't crash.
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "file_hashes": {},
                "retry_counts": {},
                "last_run_ts": None,
                "last_error": "not-a-dict",
            }),
            encoding="utf-8",
        )
        state = InstructorState(state_path)
        state.load()
        assert state.last_error is None

    def test_newer_state_with_unknown_top_level_field_loads(
        self, tmp_path: Path,
    ) -> None:
        # Forward-compat: a future state file with extra top-level keys
        # must not crash a current load(). The current InstructorState
        # ignores unknown keys by virtue of reading specific keys
        # rather than splatting kwargs — pin that contract.
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "file_hashes": {},
                "retry_counts": {},
                "last_run_ts": None,
                "last_error": None,
                "future_field_we_havent_invented_yet": {"any": "shape"},
            }),
            encoding="utf-8",
        )
        state = InstructorState(state_path)
        state.load()  # Must not raise.
        assert state.last_error is None


# ---------------------------------------------------------------------------
# record_error — captures + persists, defensive on save-failure
# ---------------------------------------------------------------------------


class TestRecordError:
    def test_record_error_sets_last_error(self, tmp_path: Path) -> None:
        state = InstructorState(tmp_path / "s.json")
        state.record_error("KeyError: 'foo'")
        assert state.last_error is not None
        assert state.last_error["message"] == "KeyError: 'foo'"
        assert "T" in state.last_error["ts"]

    def test_record_error_persists_to_disk(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state = InstructorState(state_path)
        state.record_error("KeyError: 'foo'")
        data = json.loads(state_path.read_text())
        assert data["last_error"]["message"] == "KeyError: 'foo'"

    def test_record_error_replaces_previous(self, tmp_path: Path) -> None:
        state = InstructorState(tmp_path / "s.json")
        state.record_error("first error")
        first_ts = state.last_error["ts"]
        state.record_error("second error")
        assert state.last_error["message"] == "second error"
        assert state.last_error["ts"] >= first_ts

    def test_record_error_preserves_other_state_fields(
        self, tmp_path: Path,
    ) -> None:
        # Existing file_hashes / retry_counts / last_run_ts must NOT be
        # clobbered by the error write — record_error persists the whole
        # state, not a partial fragment.
        state_path = tmp_path / "s.json"
        state = InstructorState(state_path)
        state.record_hash("note/A.md", "hash-aaa")
        state.bump_retry("task/B.md")
        state.stamp_run()
        first_run_ts = state.last_run_ts
        state.save()

        # Reload to simulate the daemon picking up after a tick.
        s2 = InstructorState(state_path)
        s2.load()
        s2.record_error("boom")

        # All prior fields survive.
        assert s2.file_hashes == {"note/A.md": "hash-aaa"}
        assert s2.get_retry_count("task/B.md") == 1
        assert s2.last_run_ts == first_run_ts
        # And last_error landed.
        assert s2.last_error["message"] == "boom"

    def test_record_error_save_failure_does_not_crash(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Daemons must not crash on secondary save failures.
        state = InstructorState(tmp_path / "s.json")

        def fail_save(self) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(InstructorState, "save", fail_save)
        # Must not raise; must emit warning log so a future refactor
        # that drops the log line shows up in CI. Per
        # ``feedback_log_emission_test_pattern.md``.
        with structlog.testing.capture_logs() as captured:
            state.record_error("some error")
        assert state.last_error is not None
        matches = [
            c for c in captured
            if c.get("event") == "instructor.state.record_error_save_failed"
        ]
        assert len(matches) == 1
        assert "disk full" in matches[0]["error"]


# ---------------------------------------------------------------------------
# stamp_run clear-on-success — the recovery semantics
# ---------------------------------------------------------------------------


class TestClearOnSuccess:
    def test_stamp_run_clears_last_error(self, tmp_path: Path) -> None:
        # Reaching stamp_run means the poll loop completed without
        # raising → wipe stale failure context.
        state = InstructorState(tmp_path / "s.json")
        state.last_error = {
            "ts": "2026-05-14T05:00:00+00:00",
            "message": "boom",
        }
        state.stamp_run()
        assert state.last_error is None
        # last_run_ts gets stamped — pin the existing contract too so a
        # future regression where stamp_run accidentally drops the
        # timestamp write shows up here, not three test files away.
        assert state.last_run_ts is not None
        assert "T" in state.last_run_ts

    def test_stamp_run_with_no_prior_error_is_noop(
        self, tmp_path: Path,
    ) -> None:
        # No regression on the happy path: stamping a tick when
        # last_error was already None doesn't break anything.
        state = InstructorState(tmp_path / "s.json")
        state.stamp_run()
        assert state.last_error is None
        assert state.last_run_ts is not None


# ---------------------------------------------------------------------------
# Integration — record_error → reload preserves; stamp_run → reload clears
# ---------------------------------------------------------------------------


class TestRecordErrorThenReload:
    def test_record_error_then_reload_preserves(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        s1 = InstructorState(state_path)
        s1.record_error("KeyError: 'foo'")

        s2 = InstructorState(state_path)
        s2.load()
        assert s2.last_error is not None
        assert s2.last_error["message"] == "KeyError: 'foo'"

    def test_stamp_run_after_reload_clears_error(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        s1 = InstructorState(state_path)
        s1.record_error("KeyError: 'foo'")

        s2 = InstructorState(state_path)
        s2.load()
        s2.stamp_run()
        s2.save()

        s3 = InstructorState(state_path)
        s3.load()
        assert s3.last_error is None
        # last_run_ts should now be set.
        assert s3.last_run_ts is not None
