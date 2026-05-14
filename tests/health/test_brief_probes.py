"""Tests for the BIT brief probes — specifically the
``last-successful-brief`` daemon-liveness probe added 2026-05-10
after the 10-day silent-failure incident.

The probe consults the brief daemon's existing ``brief_state.json``
and FAILs if the most recent successful brief is older than 2 days
(in the brief's configured timezone). This is the operator-visible
signal the 2026-04-30 → 2026-05-10 incident was missing — the brief
daemon's ``except Exception:`` swallowed a TypeError on every clear
day, no error surfaced to BIT, and ``vault/run/`` going empty was
the only diagnostic.

Per ``feedback_intentionally_left_blank.md``: silence is ambiguous
between idle-healthy and broken; an explicit liveness probe
disambiguates.

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alfred.brief.health import (
    _check_last_successful_brief,
    _most_recent_successful_brief_date,
    _read_last_error,
    _resolve_state_path,
)
from alfred.health.types import Status


# ---------------------------------------------------------------------------
# Helpers — write a brief_state.json with arbitrary date/success shape
# ---------------------------------------------------------------------------


def _write_state(state_path: Path, runs: list[dict]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps({"version": 1, "last_run": "", "runs": runs}, indent=2),
        encoding="utf-8",
    )


def _write_state_with_error(
    state_path: Path, runs: list[dict], last_error: dict | None,
) -> None:
    """Variant of ``_write_state`` that also writes the ``last_error``
    field. Added 2026-05-14 for the diagnostic-surfacing pins.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "last_run": "",
                "runs": runs,
                "last_error": last_error,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _run_record(date_str: str, success: bool = True) -> dict:
    """Mirror the ``BriefRun`` shape from alfred.brief.state."""
    return {
        "date": date_str,
        "generated_at": f"{date_str}T06:00:00+00:00",
        "vault_path": f"run/Morning Brief {date_str}.md",
        "sections": ["weather", "ops"],
        "success": success,
    }


def _today_iso(tz_name: str = "America/Halifax") -> str:
    return datetime.now(ZoneInfo(tz_name)).date().isoformat()


def _yesterday_iso(tz_name: str = "America/Halifax") -> str:
    return (datetime.now(ZoneInfo(tz_name)).date() - timedelta(days=1)).isoformat()


def _days_ago_iso(n: int, tz_name: str = "America/Halifax") -> str:
    return (datetime.now(ZoneInfo(tz_name)).date() - timedelta(days=n)).isoformat()


def _raw(tmp_path: Path, *, state_path: Path | None = None, tz: str = "America/Halifax") -> dict[str, Any]:
    """Build a minimal raw config that exercises the probe's path
    resolution + timezone consumption.

    When ``state_path`` is None, the probe falls back through
    ``logging.dir`` → ``./data/brief_state.json`` resolution.
    """
    raw: dict[str, Any] = {
        "logging": {"dir": str(tmp_path)},
        "brief": {
            "schedule": {"time": "06:00", "timezone": tz},
        },
    }
    if state_path is not None:
        raw["brief"]["state"] = {"path": str(state_path)}
    return raw


# ---------------------------------------------------------------------------
# _resolve_state_path — mirrors brief.config.load_from_unified
# ---------------------------------------------------------------------------


class TestResolveStatePath:
    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom" / "brief_state.json"
        raw = _raw(tmp_path, state_path=explicit)
        assert _resolve_state_path(raw, raw["brief"]) == explicit

    def test_fallback_uses_logging_dir(self, tmp_path: Path) -> None:
        raw = _raw(tmp_path)
        assert _resolve_state_path(raw, raw["brief"]) == tmp_path / "brief_state.json"

    def test_fallback_uses_data_when_logging_dir_absent(
        self, tmp_path: Path,
    ) -> None:
        # Mirror the loader's default: logging.dir absent → ``./data``.
        raw: dict[str, Any] = {"brief": {}}
        # Don't actually walk ./data — just assert the path string.
        assert str(_resolve_state_path(raw, raw["brief"])) == "data/brief_state.json"


# ---------------------------------------------------------------------------
# _most_recent_successful_brief_date — pure dict-walking
# ---------------------------------------------------------------------------


class TestMostRecentSuccessfulDate:
    def test_missing_state_file_returns_none(self, tmp_path: Path) -> None:
        assert _most_recent_successful_brief_date(tmp_path / "missing.json") is None

    def test_empty_runs_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, [])
        assert _most_recent_successful_brief_date(state_path) is None

    def test_only_unsuccessful_runs_returns_none(self, tmp_path: Path) -> None:
        # Daemon recorded attempts but all failed — the probe MUST
        # treat this as "no successful runs", not "the most recent
        # date is X" (which would let a long-failing daemon pass).
        state_path = tmp_path / "s.json"
        _write_state(state_path, [
            _run_record("2026-05-09", success=False),
            _run_record("2026-05-10", success=False),
        ])
        assert _most_recent_successful_brief_date(state_path) is None

    def test_returns_max_of_successful_dates(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, [
            _run_record("2026-05-08", success=True),
            _run_record("2026-05-10", success=True),
            _run_record("2026-05-09", success=True),
        ])
        # max() over ISO strings == max() over real dates as long as
        # zero-padded YYYY-MM-DD; pin the contract.
        assert _most_recent_successful_brief_date(state_path) == "2026-05-10"

    def test_skips_unsuccessful_when_mixed(self, tmp_path: Path) -> None:
        # The most-recent ATTEMPT (success=False) must NOT be returned;
        # only the most-recent SUCCESS counts. This is the bug-of-record
        # discrimination — a daemon failing every day for 10 days
        # would have ``last_run`` updated but no successful date.
        state_path = tmp_path / "s.json"
        _write_state(state_path, [
            _run_record("2026-04-25", success=True),
            _run_record("2026-05-09", success=False),
            _run_record("2026-05-10", success=False),
        ])
        assert _most_recent_successful_brief_date(state_path) == "2026-04-25"

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        # JSONDecodeError must NOT crash the BIT run — degrade
        # gracefully to "we don't know" and let the probe SKIP.
        state_path = tmp_path / "s.json"
        state_path.write_text("not valid json {{{", encoding="utf-8")
        assert _most_recent_successful_brief_date(state_path) is None

    def test_runs_not_a_list_returns_none(self, tmp_path: Path) -> None:
        # Defensive against a future schema migration that breaks
        # the ``runs`` key shape.
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({"version": 1, "runs": {"oops": "dict"}}),
            encoding="utf-8",
        )
        assert _most_recent_successful_brief_date(state_path) is None

    def test_run_entry_not_a_dict_skipped(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({"version": 1, "runs": [
                "not a dict",  # corrupted entry
                _run_record("2026-05-10", success=True),
            ]}),
            encoding="utf-8",
        )
        # The good entry still wins; the corrupt one is silently skipped.
        assert _most_recent_successful_brief_date(state_path) == "2026-05-10"


# ---------------------------------------------------------------------------
# _check_last_successful_brief — the four-state probe contract
# ---------------------------------------------------------------------------


class TestLastSuccessfulBriefProbe:
    def test_yesterday_is_ok(self, tmp_path: Path) -> None:
        # The healthy-steady-state case. BIT runs at 05:55, brief
        # daemon runs at 06:00 — at probe time today's brief doesn't
        # exist yet, so yesterday's is the most recent.
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [_run_record(_yesterday_iso())])
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.OK
        assert result.name == "last-successful-brief"
        assert result.data["days_old"] == 1

    def test_today_is_ok(self, tmp_path: Path) -> None:
        # Operator manually ran ``alfred brief generate`` after BIT,
        # OR the probe is run mid-day after the 06:00 daemon fired.
        # Either way: today's date is fresher than yesterday → OK.
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [_run_record(_today_iso())])
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.OK
        assert result.data["days_old"] == 0

    def test_two_days_ago_is_warn(self, tmp_path: Path) -> None:
        # One missed day — could be a transient API blip or a single
        # daemon hiccup. WARN, not FAIL.
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [_run_record(_days_ago_iso(2))])
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.WARN
        assert result.data["days_old"] == 2

    def test_ten_days_ago_is_fail(self, tmp_path: Path) -> None:
        # The bug-of-record shape: 10-day silent failure. The exact
        # incident the probe was added to catch.
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [_run_record(_days_ago_iso(10))])
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.FAIL
        assert result.data["days_old"] == 10
        # The detail string must be operator-actionable — a glance
        # tells the operator "daemon may be silently failing" so they
        # check brief.log not surveyor.log.
        assert "silently failing" in result.detail

    def test_three_days_ago_is_fail(self, tmp_path: Path) -> None:
        # Anything > 2 days = FAIL. Pin the exact threshold.
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [_run_record(_days_ago_iso(3))])
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.FAIL

    def test_no_state_file_is_skip_fresh_install(
        self, tmp_path: Path,
    ) -> None:
        # Fresh install — no brief has ever run. Not an error; SKIP
        # so the operator sees "we didn't check" rather than spurious
        # FAIL on a clean machine.
        state_path = tmp_path / "missing_state.json"
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.SKIP
        assert "fresh install" in result.detail

    def test_state_with_no_successful_runs_is_skip(
        self, tmp_path: Path,
    ) -> None:
        # State file exists but every recorded run had success=False.
        # Treat as "we don't have a positive signal" → SKIP, not FAIL.
        # FAIL would be wrong because the daemon IS running and
        # recording attempts; we just don't have a confirmed success
        # to compare against. The other probes (schedule-time,
        # weather-api) are the right surfaces for that diagnosis.
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [_run_record(_days_ago_iso(1), success=False)])
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.SKIP
        assert "no successful runs" in result.detail

    def test_uses_brief_configured_timezone_not_utc(
        self, tmp_path: Path,
    ) -> None:
        # Timezone-correctness pin per the dispatch reminder. Compute
        # "yesterday" in the brief's configured timezone, NOT UTC.
        # Set up: state has yesterday-in-Halifax, probe runs at 05:55
        # ADT on today's date. UTC interpretation could fence-post
        # by one day depending on hour.
        state_path = tmp_path / "brief_state.json"
        # Yesterday in America/Halifax — what the brief daemon writes.
        yesterday_halifax = (
            datetime.now(ZoneInfo("America/Halifax")).date() - timedelta(days=1)
        ).isoformat()
        _write_state(state_path, [_run_record(yesterday_halifax)])
        raw = _raw(tmp_path, state_path=state_path, tz="America/Halifax")
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.OK

    def test_unparseable_date_is_skip(self, tmp_path: Path) -> None:
        # Corrupt date string in state → degrade gracefully to SKIP,
        # don't crash the BIT run.
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [
            {
                "date": "not-a-date",
                "generated_at": "x",
                "vault_path": "x",
                "sections": [],
                "success": True,
            },
        ])
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.SKIP

    def test_bad_timezone_skips_probe_no_double_fail(
        self, tmp_path: Path,
    ) -> None:
        # If the operator misconfigured ``schedule.timezone``,
        # ``_check_schedule`` already FAILs that probe. The
        # last-successful-brief probe must SKIP (not double-FAIL)
        # so the operator sees the canonical timezone error rather
        # than two redundant ones.
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [_run_record(_yesterday_iso())])
        raw = _raw(tmp_path, state_path=state_path, tz="Invalid/Zone")
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.SKIP
        assert "unresolvable" in result.detail


# ---------------------------------------------------------------------------
# health_check integration — the new probe is wired into the rollup
# ---------------------------------------------------------------------------


class TestHealthCheckIntegration:
    async def test_last_successful_brief_appears_in_results(
        self, tmp_path: Path,
    ) -> None:
        # End-to-end pin: invoking the rollup ``health_check`` produces
        # a CheckResult with ``name == "last-successful-brief"``.
        # Catches the regression class where a probe is implemented but
        # never wired into the results list.
        from alfred.brief.health import health_check

        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [_run_record(_yesterday_iso())])

        raw: dict[str, Any] = {
            "vault": {"path": str(tmp_path)},
            "logging": {"dir": str(tmp_path)},
            "brief": {
                "schedule": {"time": "06:00", "timezone": "America/Halifax"},
                "output": {"directory": "run"},
                "weather": {"stations": []},  # SKIP the weather-api probe
                "state": {"path": str(state_path)},
            },
        }

        rollup = await health_check(raw, mode="quick")
        names = [r.name for r in rollup.results]
        assert "last-successful-brief" in names
        # Confirm it ran healthily on yesterday's data.
        last = next(r for r in rollup.results if r.name == "last-successful-brief")
        assert last.status == Status.OK


# ---------------------------------------------------------------------------
# _read_last_error — defensive dict-walking for the diagnostic field
# (added 2026-05-14)
# ---------------------------------------------------------------------------


class TestReadLastError:
    def test_missing_state_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_last_error(tmp_path / "missing.json") is None

    def test_state_without_last_error_returns_none(
        self, tmp_path: Path,
    ) -> None:
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [])
        # No last_error field in the legacy state shape.
        assert _read_last_error(state_path) is None

    def test_last_error_null_returns_none(self, tmp_path: Path) -> None:
        # After a successful recovery the field is None, not absent.
        state_path = tmp_path / "brief_state.json"
        _write_state_with_error(state_path, [], None)
        assert _read_last_error(state_path) is None

    def test_last_error_populated_returns_dict(self, tmp_path: Path) -> None:
        state_path = tmp_path / "brief_state.json"
        err = {"ts": "2026-05-14T06:00:00+00:00", "message": "KeyError: 'visib'"}
        _write_state_with_error(state_path, [], err)
        loaded = _read_last_error(state_path)
        assert loaded == err

    def test_last_error_not_a_dict_returns_none(self, tmp_path: Path) -> None:
        # Degrade gracefully on a corrupt shape.
        state_path = tmp_path / "brief_state.json"
        _write_state_with_error(state_path, [], "not-a-dict")  # type: ignore[arg-type]
        assert _read_last_error(state_path) is None

    def test_last_error_without_message_returns_none(
        self, tmp_path: Path,
    ) -> None:
        # No actionable message → treat as absent. The probe-side then
        # omits the suffix rather than rendering "; last error: ".
        state_path = tmp_path / "brief_state.json"
        _write_state_with_error(
            state_path, [], {"ts": "2026-05-14T06:00:00+00:00"},
        )
        assert _read_last_error(state_path) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "brief_state.json"
        state_path.write_text("{ not json", encoding="utf-8")
        assert _read_last_error(state_path) is None


# ---------------------------------------------------------------------------
# _check_last_successful_brief — last_error surfacing in WARN/FAIL detail
# (added 2026-05-14)
# ---------------------------------------------------------------------------


class TestLastErrorSurfacing:
    """Pin that when ``last_error`` is populated, the probe's
    WARN/FAIL detail string includes the message so the BIT line
    carries the cause without requiring the operator to grep
    ``data/brief.log``.

    Symmetric: when ``last_error`` is None or absent, the detail
    stays clean (no trailing "; last error: " sentinel).
    """

    def test_fail_includes_last_error_message(self, tmp_path: Path) -> None:
        state_path = tmp_path / "brief_state.json"
        _write_state_with_error(
            state_path,
            [_run_record(_days_ago_iso(10))],
            {"ts": "2026-05-14T06:00:00+00:00", "message": "KeyError: 'visib'"},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.FAIL
        # The headline date threshold message stays present...
        assert "silently failing" in result.detail
        # ...AND the error cause is appended for diagnostic context.
        assert "last error: KeyError: 'visib'" in result.detail

    def test_warn_includes_last_error_message(self, tmp_path: Path) -> None:
        state_path = tmp_path / "brief_state.json"
        _write_state_with_error(
            state_path,
            [_run_record(_days_ago_iso(2))],
            {"ts": "2026-05-14T06:00:00+00:00", "message": "TimeoutError"},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.WARN
        assert "one missed run" in result.detail
        assert "last error: TimeoutError" in result.detail

    def test_fail_without_last_error_omits_suffix(
        self, tmp_path: Path,
    ) -> None:
        # The intentionally-left-blank semantics: when there's no
        # error to surface, don't emit a bare "; last error: " — keep
        # the detail clean.
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [_run_record(_days_ago_iso(10))])
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.FAIL
        assert "last error:" not in result.detail

    def test_warn_without_last_error_omits_suffix(
        self, tmp_path: Path,
    ) -> None:
        state_path = tmp_path / "brief_state.json"
        _write_state(state_path, [_run_record(_days_ago_iso(2))])
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.WARN
        assert "last error:" not in result.detail

    def test_long_message_truncated_to_150_chars(
        self, tmp_path: Path,
    ) -> None:
        # The BIT line is a single-line operator surface — long
        # multi-line tracebacks would wreck readability. Cap at 150
        # chars with an ellipsis sentinel.
        state_path = tmp_path / "brief_state.json"
        long_msg = "TypeError: " + ("x" * 500)
        _write_state_with_error(
            state_path,
            [_run_record(_days_ago_iso(10))],
            {"ts": "2026-05-14T06:00:00+00:00", "message": long_msg},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.FAIL
        # The detail contains a truncated message ending in "...".
        # The truncation point is 147 + "..." = 150 chars total.
        assert "..." in result.detail
        # Pull out the suffix and check the length ceiling.
        suffix = result.detail.split("last error: ", 1)[1]
        assert len(suffix) <= 150

    def test_short_message_not_truncated(self, tmp_path: Path) -> None:
        # Below the cap, message goes through verbatim.
        state_path = tmp_path / "brief_state.json"
        _write_state_with_error(
            state_path,
            [_run_record(_days_ago_iso(10))],
            {"ts": "2026-05-14T06:00:00+00:00", "message": "short msg"},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert "last error: short msg" in result.detail
        assert "..." not in result.detail

    def test_ok_status_does_not_append_error_suffix(
        self, tmp_path: Path,
    ) -> None:
        # Defensive: if somehow last_error is set but the most recent
        # date is fresh (shouldn't happen because add_run clears, but
        # don't let a state-file edit by an operator break the OK
        # path), the OK detail stays clean. Documents the intent that
        # error surfacing is a WARN/FAIL concern, not OK.
        state_path = tmp_path / "brief_state.json"
        _write_state_with_error(
            state_path,
            [_run_record(_yesterday_iso())],
            {"ts": "2026-05-14T06:00:00+00:00", "message": "stale error"},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.status == Status.OK
        assert "last error:" not in result.detail

    def test_payload_carries_last_error_for_json_consumers(
        self, tmp_path: Path,
    ) -> None:
        # JSON consumers of BIT output (operator dashboards, alert
        # routing) get the full structured error in ``result.data``,
        # not just the truncated detail-string suffix.
        state_path = tmp_path / "brief_state.json"
        err = {"ts": "2026-05-14T06:00:00+00:00", "message": "KeyError: 'visib'"}
        _write_state_with_error(
            state_path, [_run_record(_days_ago_iso(10))], err,
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_brief(raw, raw["brief"])
        assert result.data.get("last_error") == err
