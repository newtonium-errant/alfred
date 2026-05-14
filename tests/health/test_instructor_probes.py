"""Tests for the BIT instructor probes — specifically the
``last-successful-poll`` daemon-liveness probe added 2026-05-14 to
close the cross-daemon silent-failure observability sweep.

Brief / janitor / distiller / daily_sync all got their
last-successful-* probes earlier today; instructor mirrors the same
pattern. Per ``feedback_intentionally_left_blank.md``: silence
(instructor daemon polling but the poll loop quietly raising every
tick, pending directives never being processed, operator notices
stale ``alfred_instructions`` queues) is ambiguous between
healthy-quiet and broken; the probe disambiguates.

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alfred.health.types import Status
from alfred.instructor.health import (
    _POLL_FAIL_SECONDS,
    _POLL_WARN_SECONDS,
    _check_last_successful_poll,
    _read_last_error,
    _read_last_run_ts,
    _resolve_state_path,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_state(
    state_path: Path,
    last_run_ts: str | None = None,
    last_error: dict | None = None,
) -> None:
    """Write an instructor_state.json with the given fields.

    Mirrors the shape produced by :meth:`InstructorState.save` so the
    test fixtures match production. Sentinel ``None`` for
    ``last_error`` writes the field as ``null`` (post-recovery shape);
    omit the kwarg to leave the field absent (legacy-state shape).
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": 1,
        "file_hashes": {},
        "retry_counts": {},
        "last_run_ts": last_run_ts,
        "last_error": last_error,
    }
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_state_legacy(state_path: Path, last_run_ts: str | None) -> None:
    """Write a pre-2026-05-14 state file (no ``last_error`` key at all)."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": 1,
        "file_hashes": {},
        "retry_counts": {},
        "last_run_ts": last_run_ts,
    }
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seconds_ago_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _raw(state_path: Path | None = None) -> dict[str, Any]:
    """Build a minimal raw config with instructor section."""
    instructor: dict[str, Any] = {}
    if state_path is not None:
        instructor["state"] = {"path": str(state_path)}
    return {"instructor": instructor}


# ---------------------------------------------------------------------------
# _resolve_state_path — mirrors InstructorConfig's state-path resolution
# ---------------------------------------------------------------------------


class TestResolveStatePath:
    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom" / "instructor_state.json"
        raw = _raw(state_path=explicit)
        assert _resolve_state_path(raw) == explicit

    def test_fallback_uses_dataclass_default(self) -> None:
        # No state.path in config → falls back to dataclass default.
        # Don't actually walk ./data — just assert the path string
        # matches the loader's default.
        raw: dict[str, Any] = {"instructor": {}}
        assert str(_resolve_state_path(raw)) == "data/instructor_state.json"

    def test_missing_instructor_section_uses_default(self) -> None:
        # Defensive: even if the caller passes an empty raw dict,
        # state-path resolution returns the dataclass default rather
        # than crashing. The rollup gate handles SKIP earlier.
        raw: dict[str, Any] = {}
        assert str(_resolve_state_path(raw)) == "data/instructor_state.json"


# ---------------------------------------------------------------------------
# _read_last_run_ts — pure dict-walking, defensive against corruption
# ---------------------------------------------------------------------------


class TestReadLastRunTs:
    def test_missing_state_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_last_run_ts(tmp_path / "missing.json") is None

    def test_state_without_last_run_ts_returns_none(
        self, tmp_path: Path,
    ) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run_ts=None)
        assert _read_last_run_ts(state_path) is None

    def test_state_with_last_run_ts_returns_string(
        self, tmp_path: Path,
    ) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run_ts="2026-05-14T06:00:00+00:00")
        assert _read_last_run_ts(state_path) == "2026-05-14T06:00:00+00:00"

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        # JSONDecodeError must NOT crash the BIT run — degrade
        # gracefully to "we don't know" and let the probe SKIP.
        state_path = tmp_path / "s.json"
        state_path.write_text("not valid json {{{", encoding="utf-8")
        assert _read_last_run_ts(state_path) is None

    def test_state_not_a_dict_returns_none(self, tmp_path: Path) -> None:
        # Defensive against a future schema migration that breaks the
        # top-level shape.
        state_path = tmp_path / "s.json"
        state_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        assert _read_last_run_ts(state_path) is None

    def test_last_run_ts_non_string_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({"version": 1, "last_run_ts": 12345}),
            encoding="utf-8",
        )
        assert _read_last_run_ts(state_path) is None


# ---------------------------------------------------------------------------
# _read_last_error — defensive dict-walking for the diagnostic field
# ---------------------------------------------------------------------------


class TestReadLastError:
    def test_missing_state_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_last_error(tmp_path / "missing.json") is None

    def test_state_without_last_error_returns_none(
        self, tmp_path: Path,
    ) -> None:
        # Legacy-shape state file (pre-2026-05-14) — no last_error key.
        state_path = tmp_path / "s.json"
        _write_state_legacy(state_path, last_run_ts=_now_iso())
        assert _read_last_error(state_path) is None

    def test_last_error_null_returns_none(self, tmp_path: Path) -> None:
        # After a successful recovery the field is None, not absent.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run_ts=_now_iso(), last_error=None)
        assert _read_last_error(state_path) is None

    def test_last_error_populated_returns_dict(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        err = {"ts": "2026-05-14T06:00:00+00:00", "message": "KeyError: 'foo'"}
        _write_state(state_path, last_run_ts=_now_iso(), last_error=err)
        loaded = _read_last_error(state_path)
        assert loaded == err

    def test_last_error_not_a_dict_returns_none(self, tmp_path: Path) -> None:
        # Degrade gracefully on a corrupt shape.
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "last_run_ts": None,
                "last_error": "not-a-dict",
            }),
            encoding="utf-8",
        )
        assert _read_last_error(state_path) is None

    def test_last_error_without_message_returns_none(
        self, tmp_path: Path,
    ) -> None:
        # No actionable message → treat as absent. The probe-side then
        # omits the suffix rather than rendering "; last error: ".
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            last_run_ts=_now_iso(),
            last_error={"ts": "2026-05-14T06:00:00+00:00"},
        )
        assert _read_last_error(state_path) is None

    def test_last_error_message_not_string_returns_none(
        self, tmp_path: Path,
    ) -> None:
        # Defensive against a future schema migration that puts a dict
        # or list under message — the BIT detail expects a string.
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            last_run_ts=_now_iso(),
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": {"unexpected": "shape"},
            },
        )
        assert _read_last_error(state_path) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text("{ not json", encoding="utf-8")
        assert _read_last_error(state_path) is None


# ---------------------------------------------------------------------------
# _check_last_successful_poll — the four-state probe contract
# ---------------------------------------------------------------------------


class TestLastSuccessfulPollProbe:
    def test_recent_poll_is_ok(self, tmp_path: Path) -> None:
        # Steady-state: last_run_ts within the last few minutes.
        state_path = tmp_path / "instructor_state.json"
        _write_state(state_path, last_run_ts=_seconds_ago_iso(30))
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.OK
        assert result.name == "last-successful-poll"
        assert result.data["age_seconds"] < _POLL_WARN_SECONDS

    def test_poll_under_one_hour_is_ok(self, tmp_path: Path) -> None:
        # Just under the 1h WARN threshold — still OK.
        state_path = tmp_path / "instructor_state.json"
        _write_state(state_path, last_run_ts=_seconds_ago_iso(_POLL_WARN_SECONDS - 60))
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.OK

    def test_poll_just_over_one_hour_is_warn(self, tmp_path: Path) -> None:
        # Just over the 1h WARN threshold. Pin the exact boundary.
        state_path = tmp_path / "instructor_state.json"
        _write_state(state_path, last_run_ts=_seconds_ago_iso(_POLL_WARN_SECONDS + 60))
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.WARN
        assert "one missed cycle window" in result.detail

    def test_poll_two_hours_ago_is_warn(self, tmp_path: Path) -> None:
        # Mid-range WARN. 2h = abnormal but pre-FAIL.
        state_path = tmp_path / "instructor_state.json"
        _write_state(state_path, last_run_ts=_seconds_ago_iso(2 * 60 * 60))
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.WARN

    def test_poll_just_over_four_hours_is_fail(self, tmp_path: Path) -> None:
        # Just over the 4h FAIL threshold. The bug-of-record shape:
        # multi-hour silent failure.
        state_path = tmp_path / "instructor_state.json"
        _write_state(state_path, last_run_ts=_seconds_ago_iso(_POLL_FAIL_SECONDS + 60))
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.FAIL
        # The detail string must be operator-actionable.
        assert "silently failing" in result.detail

    def test_poll_one_day_ago_is_fail(self, tmp_path: Path) -> None:
        # Multi-hour silent failure — the bug class the cross-daemon
        # arc closed.
        state_path = tmp_path / "instructor_state.json"
        _write_state(state_path, last_run_ts=_seconds_ago_iso(24 * 60 * 60))
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.FAIL
        # Age-human renders as "1d" — pin so a future regression in
        # _humanise_age surfaces here too.
        assert "1d ago" in result.detail

    def test_no_state_file_is_skip_fresh_install(self, tmp_path: Path) -> None:
        # Fresh install — instructor hasn't polled yet. Not an error;
        # SKIP so the operator sees "we didn't check" rather than
        # spurious FAIL on a clean machine.
        state_path = tmp_path / "missing_state.json"
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.SKIP
        assert "fresh install" in result.detail

    def test_state_without_last_run_ts_is_skip(self, tmp_path: Path) -> None:
        # State file exists (perhaps the daemon wrote file_hashes but
        # never reached stamp_run, e.g. crashed mid-tick on first ever
        # poll). Treat as "we don't have a positive signal" → SKIP.
        state_path = tmp_path / "instructor_state.json"
        _write_state(state_path, last_run_ts=None)
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.SKIP
        assert "no last_run_ts" in result.detail

    def test_unparseable_last_run_ts_is_skip(self, tmp_path: Path) -> None:
        # Corrupt timestamp string in state → degrade gracefully to
        # SKIP, don't crash the BIT run.
        state_path = tmp_path / "instructor_state.json"
        _write_state(state_path, last_run_ts="not-a-timestamp")
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.SKIP
        assert "unparseable" in result.detail

    def test_naive_timestamp_treated_as_utc(self, tmp_path: Path) -> None:
        # Defensive against a state file written by an earlier
        # instructor version that used a naive datetime. The probe
        # should treat it as UTC and produce a sensible age — not
        # crash on tz-naive arithmetic.
        naive_now = datetime.now().isoformat()  # no tz suffix
        state_path = tmp_path / "instructor_state.json"
        _write_state(state_path, last_run_ts=naive_now)
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        # Status is OK because the naive timestamp is "now" — the key
        # property under test is "didn't raise on tz-naive datetime".
        assert result.status in (Status.OK, Status.WARN, Status.FAIL)

    def test_payload_carries_age_and_state_path(self, tmp_path: Path) -> None:
        # The structured payload is what JSON consumers (operator
        # dashboards) read; pin the contract.
        state_path = tmp_path / "instructor_state.json"
        _write_state(state_path, last_run_ts=_seconds_ago_iso(30))
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert "state_path" in result.data
        assert "age_seconds" in result.data
        assert "last_run_ts" in result.data


# ---------------------------------------------------------------------------
# _check_last_successful_poll — last_error surfacing in WARN/FAIL detail
# ---------------------------------------------------------------------------


class TestLastErrorSurfacing:
    """Pin that when ``last_error`` is populated, the probe's
    WARN/FAIL detail string includes the message so the BIT line
    carries the cause without requiring the operator to grep
    ``data/instructor.log``.

    Symmetric: when ``last_error`` is None or absent, the detail
    stays clean (no trailing "; last error: " sentinel).
    """

    def test_fail_includes_last_error_message(self, tmp_path: Path) -> None:
        state_path = tmp_path / "instructor_state.json"
        _write_state(
            state_path,
            last_run_ts=_seconds_ago_iso(_POLL_FAIL_SECONDS + 60),
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": "KeyError: 'foo'",
            },
        )
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.FAIL
        # Headline message stays present...
        assert "silently failing" in result.detail
        # ...AND the error cause is appended for diagnostic context.
        assert "last error: KeyError: 'foo'" in result.detail

    def test_warn_includes_last_error_message(self, tmp_path: Path) -> None:
        state_path = tmp_path / "instructor_state.json"
        _write_state(
            state_path,
            last_run_ts=_seconds_ago_iso(_POLL_WARN_SECONDS + 60),
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": "TimeoutError",
            },
        )
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.WARN
        assert "one missed cycle window" in result.detail
        assert "last error: TimeoutError" in result.detail

    def test_fail_without_last_error_omits_suffix(
        self, tmp_path: Path,
    ) -> None:
        # The intentionally-left-blank semantics: when there's no
        # error to surface, don't emit a bare "; last error: " — keep
        # the detail clean.
        state_path = tmp_path / "instructor_state.json"
        _write_state(
            state_path,
            last_run_ts=_seconds_ago_iso(_POLL_FAIL_SECONDS + 60),
            last_error=None,
        )
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.FAIL
        assert "last error:" not in result.detail

    def test_warn_without_last_error_omits_suffix(
        self, tmp_path: Path,
    ) -> None:
        state_path = tmp_path / "instructor_state.json"
        _write_state(
            state_path,
            last_run_ts=_seconds_ago_iso(_POLL_WARN_SECONDS + 60),
            last_error=None,
        )
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.WARN
        assert "last error:" not in result.detail

    def test_long_message_truncated_to_150_chars(
        self, tmp_path: Path,
    ) -> None:
        # The BIT line is a single-line operator surface — long
        # multi-line tracebacks would wreck readability. Cap at 150
        # chars with an ellipsis sentinel.
        state_path = tmp_path / "instructor_state.json"
        long_msg = "TypeError: " + ("x" * 500)
        _write_state(
            state_path,
            last_run_ts=_seconds_ago_iso(_POLL_FAIL_SECONDS + 60),
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": long_msg,
            },
        )
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.FAIL
        assert "..." in result.detail
        # Pull out the suffix and check the length ceiling.
        suffix = result.detail.split("last error: ", 1)[1]
        assert len(suffix) <= 150

    def test_short_message_not_truncated(self, tmp_path: Path) -> None:
        # Below the cap, message goes through verbatim.
        state_path = tmp_path / "instructor_state.json"
        _write_state(
            state_path,
            last_run_ts=_seconds_ago_iso(_POLL_FAIL_SECONDS + 60),
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": "short msg",
            },
        )
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert "last error: short msg" in result.detail
        assert "..." not in result.detail

    def test_ok_status_does_not_append_error_suffix(
        self, tmp_path: Path,
    ) -> None:
        # Defensive: if somehow last_error is set but last_run_ts is
        # fresh (shouldn't happen because stamp_run clears, but
        # don't let a state-file edit by an operator break the OK
        # path), the OK detail stays clean. Documents the intent
        # that error surfacing is a WARN/FAIL concern, not OK.
        state_path = tmp_path / "instructor_state.json"
        _write_state(
            state_path,
            last_run_ts=_seconds_ago_iso(30),
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": "stale error",
            },
        )
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.status == Status.OK
        assert "last error:" not in result.detail

    def test_payload_carries_last_error_for_json_consumers(
        self, tmp_path: Path,
    ) -> None:
        # JSON consumers of BIT output (operator dashboards, alert
        # routing) get the full structured error in ``result.data``,
        # not just the truncated detail-string suffix.
        state_path = tmp_path / "instructor_state.json"
        err = {"ts": "2026-05-14T06:00:00+00:00", "message": "KeyError: 'foo'"}
        _write_state(
            state_path,
            last_run_ts=_seconds_ago_iso(_POLL_FAIL_SECONDS + 60),
            last_error=err,
        )
        raw = _raw(state_path=state_path)
        result = _check_last_successful_poll(raw)
        assert result.data.get("last_error") == err


# ---------------------------------------------------------------------------
# health_check integration — the new probe is wired into the rollup
# ---------------------------------------------------------------------------


class TestHealthCheckIntegration:
    async def test_last_successful_poll_appears_in_results(
        self, tmp_path: Path,
    ) -> None:
        # End-to-end pin: invoking the rollup ``health_check`` produces
        # a CheckResult with ``name == "last-successful-poll"``.
        # Catches the regression class where a probe is implemented but
        # never wired into the results list.
        from alfred.instructor.health import health_check

        vault = tmp_path / "vault"
        vault.mkdir()
        state_path = tmp_path / "data" / "instructor_state.json"
        _write_state(state_path, last_run_ts=_seconds_ago_iso(30))

        raw: dict[str, Any] = {
            "vault": {"path": str(vault)},
            "logging": {"dir": str(tmp_path / "data")},
            "instructor": {
                "poll_interval_seconds": 60,
                "max_retries": 3,
                "state": {"path": str(state_path)},
            },
        }

        rollup = await health_check(raw, mode="quick")
        names = [r.name for r in rollup.results]
        assert "last-successful-poll" in names
        # Confirm it ran healthily on the recent timestamp.
        last = next(r for r in rollup.results if r.name == "last-successful-poll")
        assert last.status == Status.OK

    async def test_fail_propagates_to_rollup(self, tmp_path: Path) -> None:
        # End-to-end pin that a FAIL on last-successful-poll
        # propagates to the rollup status.
        from alfred.instructor.health import health_check

        vault = tmp_path / "vault"
        vault.mkdir()
        state_path = tmp_path / "data" / "instructor_state.json"
        _write_state(
            state_path,
            last_run_ts=_seconds_ago_iso(_POLL_FAIL_SECONDS + 60),
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": "KeyError: 'foo'",
            },
        )

        raw: dict[str, Any] = {
            "vault": {"path": str(vault)},
            "logging": {"dir": str(tmp_path / "data")},
            "instructor": {
                "poll_interval_seconds": 60,
                "max_retries": 3,
                "state": {"path": str(state_path)},
            },
        }

        rollup = await health_check(raw, mode="quick")
        assert rollup.status == Status.FAIL
        last = next(r for r in rollup.results if r.name == "last-successful-poll")
        assert last.status == Status.FAIL
        # The detail surfaces the cause to the rollup line.
        assert "KeyError: 'foo'" in last.detail
