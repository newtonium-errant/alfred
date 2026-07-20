"""Tests for ``alfred.brief.state`` — round-trip + the ``last_error``
diagnostic field added 2026-05-14.

Background — the brief daemon silently failed for 10 days
(2026-04-30 → 2026-05-10) due to a TypeError swallowed by
``except Exception:`` at ``daemon.py:289``. The May 10 P0 fix coerced
the API boundary and added the ``last-successful-brief`` BIT probe
(``5e28885``) which caught the silent-fail pattern via
``brief_state.json`` consultation. **Still missing** at that point:
the probe could say "last brief was Nd ago" but not WHY.

This module pins the ``last_error`` round-trip + clear-on-success +
schema-tolerance contract that closes the diagnostic gap. Sibling
test surface lives at ``tests/health/test_brief_probes.py`` for the
probe-side surfacing.
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

from alfred.brief.state import BriefRun, State, StateManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run(date_str: str = "2026-05-10", success: bool = True) -> BriefRun:
    return BriefRun(
        date=date_str,
        generated_at=f"{date_str}T06:00:00+00:00",
        vault_path=f"run/Morning Brief {date_str}.md",
        sections=["weather"],
        success=success,
    )


# ---------------------------------------------------------------------------
# BriefRun + State round-trip (pre-existing contract — schema tolerance
# tightened 2026-05-14 to match the canonical pattern)
# ---------------------------------------------------------------------------


class TestBriefRunRoundTrip:
    def test_to_dict_from_dict_round_trip(self) -> None:
        run = _make_run("2026-05-10")
        recovered = BriefRun.from_dict(run.to_dict())
        assert recovered == run

    def test_from_dict_tolerates_unknown_keys(self) -> None:
        # Schema-tolerance contract per CLAUDE.md: a newer-version state
        # file with extra fields must NOT crash an older loader.
        data = {
            "date": "2026-05-10",
            "generated_at": "x",
            "vault_path": "y",
            "sections": ["weather"],
            "success": True,
            "future_field_we_havent_invented_yet": "boom",
        }
        # Must not raise.
        run = BriefRun.from_dict(data)
        assert run.date == "2026-05-10"

    def test_from_dict_missing_keys_defaults(self) -> None:
        # Symmetric tolerance — older-version state file missing fields
        # the current loader expects must still load (with defaults).
        run = BriefRun.from_dict({"date": "2026-05-10"})
        assert run.date == "2026-05-10"
        assert run.success is False  # default


class TestStateRoundTrip:
    def test_empty_state_round_trip(self) -> None:
        s = State()
        assert State.from_dict(s.to_dict()) == s

    def test_state_with_runs_round_trip(self) -> None:
        s = State()
        s.add_run(_make_run("2026-05-09"))
        s.add_run(_make_run("2026-05-10"))
        recovered = State.from_dict(s.to_dict())
        assert recovered.last_run == s.last_run
        assert len(recovered.runs) == 2
        assert recovered.runs[0].date == "2026-05-09"
        assert recovered.runs[1].date == "2026-05-10"

    def test_state_from_dict_tolerates_unknown_keys(self) -> None:
        # Schema-tolerance contract per CLAUDE.md.
        data = {
            "version": 1,
            "last_run": "x",
            "runs": [],
            "future_top_level_field": {"anything": "boom"},
        }
        s = State.from_dict(data)
        assert s.version == 1

    def test_state_loads_older_file_without_last_error_field(self) -> None:
        # An older brief_state.json written before 2026-05-14 won't
        # have ``last_error`` at all. Must load with default None.
        data = {"version": 1, "last_run": "x", "runs": []}
        s = State.from_dict(data)
        assert s.last_error is None


# ---------------------------------------------------------------------------
# last_error round-trip — the new diagnostic field
# ---------------------------------------------------------------------------


class TestLastErrorRoundTrip:
    def test_default_is_none(self) -> None:
        s = State()
        assert s.last_error is None

    def test_to_dict_includes_last_error(self) -> None:
        s = State()
        s.last_error = {"ts": "2026-05-14T06:00:00+00:00", "message": "KeyError: 'visib'"}
        assert s.to_dict()["last_error"] == s.last_error

    def test_from_dict_loads_last_error(self) -> None:
        data = {
            "version": 1,
            "last_run": "",
            "runs": [],
            "last_error": {
                "ts": "2026-05-14T06:00:00+00:00",
                "message": "KeyError: 'visib'",
            },
        }
        s = State.from_dict(data)
        assert s.last_error == {
            "ts": "2026-05-14T06:00:00+00:00",
            "message": "KeyError: 'visib'",
        }

    def test_full_round_trip_with_last_error(self) -> None:
        s = State()
        s.last_error = {"ts": "2026-05-14T06:00:00+00:00", "message": "boom"}
        recovered = State.from_dict(s.to_dict())
        assert recovered.last_error == s.last_error


# ---------------------------------------------------------------------------
# StateManager.record_error — captures + persists
# ---------------------------------------------------------------------------


class TestRecordError:
    def test_record_error_sets_last_error_on_state(self, tmp_path: Path) -> None:
        mgr = StateManager(tmp_path / "brief_state.json")
        mgr.record_error("KeyError: 'visib'")
        assert mgr.state.last_error is not None
        assert mgr.state.last_error["message"] == "KeyError: 'visib'"
        # The timestamp must be present and ISO-shaped.
        assert "T" in mgr.state.last_error["ts"]

    def test_record_error_persists_to_disk(self, tmp_path: Path) -> None:
        state_path = tmp_path / "brief_state.json"
        mgr = StateManager(state_path)
        mgr.record_error("KeyError: 'visib'")
        # File on disk must reflect the call — this is what the BIT
        # probe consults.
        data = json.loads(state_path.read_text())
        assert data["last_error"]["message"] == "KeyError: 'visib'"

    def test_record_error_replaces_previous(self, tmp_path: Path) -> None:
        # Successive failures overwrite — only the most recent matters
        # for diagnostics.
        mgr = StateManager(tmp_path / "brief_state.json")
        mgr.record_error("first error")
        first_ts = mgr.state.last_error["ts"]
        mgr.record_error("second error")
        assert mgr.state.last_error["message"] == "second error"
        # The ts may equal or exceed the first (clock-resolution); just
        # confirm we overwrote, not appended.
        assert mgr.state.last_error["ts"] >= first_ts

    def test_record_error_save_failure_does_not_crash(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Daemons must not crash on secondary save failures — that
        # would compound a brief failure into a process exit.
        mgr = StateManager(tmp_path / "brief_state.json")

        def fail_save(self) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(StateManager, "save", fail_save)
        # Must not raise, and must emit the warning log so a future
        # refactor that drops the log line shows up in CI. Per
        # ``feedback_log_emission_test_pattern.md`` — log-emission
        # tests must drive the production code path.
        with structlog.testing.capture_logs() as captured:
            mgr.record_error("some error")
        # In-memory state still got updated; only persistence failed.
        assert mgr.state.last_error is not None
        matches = [
            c for c in captured
            if c.get("event") == "brief.state.record_error_save_failed"
        ]
        assert len(matches) == 1
        assert "disk full" in matches[0]["error"]


# ---------------------------------------------------------------------------
# add_run clear-on-success — the recovery semantics
# ---------------------------------------------------------------------------


class TestClearOnSuccess:
    def test_successful_run_clears_last_error(self) -> None:
        # The recovery semantics: a successful brief means we're back
        # to healthy; the probe should report OK with no stale error
        # trailing.
        s = State()
        s.last_error = {"ts": "2026-05-14T05:00:00+00:00", "message": "boom"}
        s.add_run(_make_run("2026-05-14", success=True))
        assert s.last_error is None

    def test_failed_run_does_not_clear_last_error(self) -> None:
        # Failed runs leave last_error untouched — the daemon's
        # ``record_error`` is what owns the failure-side write.
        # Mixing the two would race / overwrite.
        s = State()
        s.last_error = {"ts": "2026-05-14T05:00:00+00:00", "message": "boom"}
        s.add_run(_make_run("2026-05-14", success=False))
        assert s.last_error == {
            "ts": "2026-05-14T05:00:00+00:00",
            "message": "boom",
        }

    def test_successful_run_with_no_prior_error_is_noop(self) -> None:
        # No regression on the happy path: a successful run when
        # last_error was already None doesn't break anything.
        s = State()
        s.add_run(_make_run("2026-05-14", success=True))
        assert s.last_error is None


# ---------------------------------------------------------------------------
# Integration — StateManager.load → record_error → reload preserves
# ---------------------------------------------------------------------------


class TestStateManagerIntegration:
    def test_record_error_then_reload(self, tmp_path: Path) -> None:
        state_path = tmp_path / "brief_state.json"
        mgr1 = StateManager(state_path)
        mgr1.record_error("KeyError: 'visib'")

        # Fresh manager, same path — simulates daemon restart.
        mgr2 = StateManager(state_path)
        loaded = mgr2.load()
        assert loaded.last_error is not None
        assert loaded.last_error["message"] == "KeyError: 'visib'"

    def test_successful_run_after_reload_clears_error(
        self, tmp_path: Path,
    ) -> None:
        state_path = tmp_path / "brief_state.json"
        mgr1 = StateManager(state_path)
        mgr1.record_error("KeyError: 'visib'")

        mgr2 = StateManager(state_path)
        mgr2.load()
        mgr2.state.add_run(_make_run("2026-05-14", success=True))
        mgr2.save()

        mgr3 = StateManager(state_path)
        loaded = mgr3.load()
        assert loaded.last_error is None
        assert len(loaded.runs) == 1


class TestStateManagerLoadDegrade:
    """load() must degrade a corrupt/unreadable state file to a fresh
    State() + warning rather than raising — it's called UNGUARDED at
    daemon.py inside run_daemon, so an escaping read exception crash-loops
    the brief daemon at startup (orchestrator retries 5× then gives up).
    The old ``(json.JSONDecodeError, KeyError)`` catch missed BOTH
    UnicodeDecodeError and OSError. (#25 4th leg, N-A.)
    """

    def test_non_utf8_state_degrades_to_fresh(self, tmp_path: Path) -> None:
        """A non-UTF-8 brief_state.json → fresh State() + warning, not an
        escaping UnicodeDecodeError (subclasses ValueError, not OSError, so
        the old catch missed it). Mutation: revert load() to the
        ``(json.JSONDecodeError, KeyError)`` catch → this raises."""
        state_path = tmp_path / "brief_state.json"
        state_path.write_bytes(b"\xff\xfe not utf-8 at all")
        mgr = StateManager(state_path)
        with structlog.testing.capture_logs() as cap:
            loaded = mgr.load()
        assert loaded.runs == []
        assert loaded.last_error is None
        warns = [c for c in cap if c.get("event") == "brief.state.load_failed"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "UnicodeDecodeError"

    def test_oserror_state_degrades_to_fresh(self, tmp_path: Path) -> None:
        """An OSError on read (here: the path is a DIRECTORY →
        IsADirectoryError, an OSError NOT caught by the old
        ``(json.JSONDecodeError, KeyError)``) → fresh State() + warning.
        Mutation: revert load() to that catch → this raises."""
        state_path = tmp_path / "brief_state.json"
        state_path.mkdir()  # exists() is True, but read_text raises IsADirectoryError
        mgr = StateManager(state_path)
        with structlog.testing.capture_logs() as cap:
            loaded = mgr.load()
        assert loaded.runs == []
        warns = [c for c in cap if c.get("event") == "brief.state.load_failed"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "IsADirectoryError"

    def test_bad_json_state_degrades_to_fresh(self, tmp_path: Path) -> None:
        """A clean-read but non-JSON state file → fresh State() + warning
        (the JSONDecodeError branch, preserved from pre-migration behavior;
        now also surfaces error_type)."""
        state_path = tmp_path / "brief_state.json"
        state_path.write_text("{ not json", encoding="utf-8")
        mgr = StateManager(state_path)
        with structlog.testing.capture_logs() as cap:
            loaded = mgr.load()
        assert loaded.runs == []
        warns = [c for c in cap if c.get("event") == "brief.state.load_failed"]
        assert len(warns) == 1
        assert warns[0]["error_type"] == "JSONDecodeError"
