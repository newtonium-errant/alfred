"""Tests for the BIT janitor probes — specifically
``last-successful-sweep`` added 2026-05-10 as part of the cross-
daemon BIT probe arc.

Janitor's state stores sweeps as a dict keyed by sweep_id; the
probe takes the max ``timestamp`` across all sweeps PLUS the
top-level ``last_deep_sweep`` field (whichever is newer).

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alfred.health.types import Status
from alfred.janitor.health import (
    _JANITOR_STALE_FAIL_HOURS,
    _JANITOR_STALE_OK_HOURS,
    _check_last_successful_sweep,
    _read_janitor_most_recent_sweep,
    _read_last_error,
    _resolve_janitor_state_path,
)


def _hours_ago_iso(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


def _write_state(
    state_path: Path,
    *,
    sweeps: dict[str, dict] | None = None,
    last_deep_sweep: str | None = None,
    last_error: dict | None = None,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {},
                "sweeps": sweeps or {},
                "fix_log": [],
                "ignored": {},
                "pending_writes": {},
                "last_deep_sweep": last_deep_sweep,
                "previous_sweep_issues": {},
                "triage_ids_seen": [],
                "last_error": last_error,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _raw(tmp_path: Path, *, state_path: Path | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {"janitor": {}}
    if state_path is not None:
        raw["janitor"]["state"] = {"path": str(state_path)}
    return raw


# ---------------------------------------------------------------------------
# _resolve_janitor_state_path
# ---------------------------------------------------------------------------


class TestResolveJanitorStatePath:
    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom" / "janitor_state.json"
        raw = _raw(tmp_path, state_path=explicit)
        assert _resolve_janitor_state_path(raw) == explicit

    def test_fallback_to_default(self) -> None:
        assert str(_resolve_janitor_state_path({"janitor": {}})) == "data/janitor_state.json"

    def test_no_janitor_section_falls_back_to_default(self) -> None:
        assert str(_resolve_janitor_state_path({})) == "data/janitor_state.json"


# ---------------------------------------------------------------------------
# _read_janitor_most_recent_sweep — combines sweeps[*].timestamp + last_deep_sweep
# ---------------------------------------------------------------------------


class TestReadJanitorMostRecentSweep:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_janitor_most_recent_sweep(tmp_path / "missing.json") is None

    def test_empty_state_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path)
        assert _read_janitor_most_recent_sweep(state_path) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text("not valid {{{", encoding="utf-8")
        assert _read_janitor_most_recent_sweep(state_path) is None

    def test_returns_max_of_sweep_timestamps(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, sweeps={
            "sw1": {"timestamp": "2026-05-08T12:00:00+00:00"},
            "sw2": {"timestamp": "2026-05-10T08:00:00+00:00"},
            "sw3": {"timestamp": "2026-05-09T14:00:00+00:00"},
        })
        assert _read_janitor_most_recent_sweep(state_path) == "2026-05-10T08:00:00+00:00"

    def test_last_deep_sweep_considered_alongside(self, tmp_path: Path) -> None:
        # ``last_deep_sweep`` is a top-level field, not a sweep entry.
        # The probe must include it in the max() — otherwise a daemon
        # whose only deep sweep happened recently but has no sweep
        # entries would falsely SKIP.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_deep_sweep="2026-05-10T09:00:00+00:00")
        assert _read_janitor_most_recent_sweep(state_path) == "2026-05-10T09:00:00+00:00"

    def test_max_picks_newer_when_both_present(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            sweeps={"sw1": {"timestamp": "2026-05-09T12:00:00+00:00"}},
            last_deep_sweep="2026-05-10T05:00:00+00:00",
        )
        # last_deep_sweep is newer → wins.
        assert _read_janitor_most_recent_sweep(state_path) == "2026-05-10T05:00:00+00:00"

    def test_sweep_entry_not_dict_skipped(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        # Write directly so we can poison one entry.
        state_path.write_text(
            json.dumps({
                "version": 1,
                "sweeps": {
                    "sw1": "not a dict",  # corrupted
                    "sw2": {"timestamp": "2026-05-10T08:00:00+00:00"},
                },
            }),
            encoding="utf-8",
        )
        # Poisoned entry silently skipped; good entry still wins.
        assert _read_janitor_most_recent_sweep(state_path) == "2026-05-10T08:00:00+00:00"


# ---------------------------------------------------------------------------
# _check_last_successful_sweep — full probe contract
# ---------------------------------------------------------------------------


class TestLastSuccessfulSweepProbe:
    def test_no_state_file_is_skip_fresh_install(self, tmp_path: Path) -> None:
        state_path = tmp_path / "missing.json"
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.SKIP
        assert "fresh install" in result.detail

    def test_empty_state_is_skip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path)
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.SKIP
        assert "no sweeps" in result.detail

    def test_unparseable_timestamp_is_skip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, sweeps={
            "sw1": {"timestamp": "not-a-timestamp"},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.SKIP

    def test_recent_sweep_ok(self, tmp_path: Path) -> None:
        # 6h ago — well under 30h OK threshold.
        state_path = tmp_path / "s.json"
        _write_state(state_path, sweeps={
            "sw1": {"timestamp": _hours_ago_iso(6)},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.OK

    def test_thirty_six_hours_ago_warn(self, tmp_path: Path) -> None:
        # In 30h..48h band — one missed run window.
        state_path = tmp_path / "s.json"
        _write_state(state_path, sweeps={
            "sw1": {"timestamp": _hours_ago_iso(36)},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.WARN
        assert "missed run" in result.detail

    def test_three_days_ago_fail(self, tmp_path: Path) -> None:
        # >48h — multi-day silent failure.
        state_path = tmp_path / "s.json"
        _write_state(state_path, sweeps={
            "sw1": {"timestamp": _hours_ago_iso(72)},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.FAIL
        assert "silently failing" in result.detail

    def test_last_deep_sweep_alone_provides_signal(self, tmp_path: Path) -> None:
        # No sweep entries, only a recent ``last_deep_sweep`` — must
        # still produce OK rather than SKIP. This is the test that
        # would catch a regression where the probe forgets to consider
        # the top-level field.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_deep_sweep=_hours_ago_iso(1))
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.OK

    def test_threshold_constants_match_dispatch(self) -> None:
        assert _JANITOR_STALE_OK_HOURS == 30
        assert _JANITOR_STALE_FAIL_HOURS == 48


# ---------------------------------------------------------------------------
# health_check integration
# ---------------------------------------------------------------------------


class TestHealthCheckIntegration:
    async def test_last_successful_sweep_appears_in_results(
        self, tmp_path: Path,
    ) -> None:
        from alfred.janitor.health import health_check

        state_path = tmp_path / "janitor_state.json"
        _write_state(state_path, sweeps={
            "sw1": {"timestamp": _hours_ago_iso(6)},
        })

        raw: dict[str, Any] = {
            "vault": {"path": str(tmp_path)},
            "agent": {"backend": "openclaw"},  # skip anthropic-auth
            "janitor": {
                "state": {"path": str(state_path)},
            },
        }

        rollup = await health_check(raw, mode="quick")
        names = [r.name for r in rollup.results]
        assert "last-successful-sweep" in names
        last = next(r for r in rollup.results if r.name == "last-successful-sweep")
        assert last.status == Status.OK


# ---------------------------------------------------------------------------
# _read_last_error — defensive dict-walking for the diagnostic field
# (added 2026-05-14)
# ---------------------------------------------------------------------------


class TestReadLastError:
    def test_missing_state_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_last_error(tmp_path / "missing.json") is None

    def test_state_without_last_error_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path)
        assert _read_last_error(state_path) is None

    def test_last_error_null_returns_none(self, tmp_path: Path) -> None:
        # After a successful recovery the field is None, not absent.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_error=None)
        assert _read_last_error(state_path) is None

    def test_last_error_populated_returns_dict(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        err = {"ts": "2026-05-14T06:00:00+00:00", "message": "KeyError: 'foo'"}
        _write_state(state_path, last_error=err)
        assert _read_last_error(state_path) == err

    def test_last_error_not_a_dict_returns_none(self, tmp_path: Path) -> None:
        # Degrade gracefully on a corrupt shape.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_error="not-a-dict")  # type: ignore[arg-type]
        assert _read_last_error(state_path) is None

    def test_last_error_without_message_returns_none(
        self, tmp_path: Path,
    ) -> None:
        # No actionable message → treat as absent. The probe-side then
        # omits the suffix rather than rendering "; last error: ".
        state_path = tmp_path / "s.json"
        _write_state(
            state_path, last_error={"ts": "2026-05-14T06:00:00+00:00"},
        )
        assert _read_last_error(state_path) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text("{ not json", encoding="utf-8")
        assert _read_last_error(state_path) is None


# ---------------------------------------------------------------------------
# _check_last_successful_sweep — last_error surfacing in WARN/FAIL detail
# (added 2026-05-14)
# ---------------------------------------------------------------------------


class TestLastErrorSurfacing:
    """Pin that when ``last_error`` is populated, the probe's
    WARN/FAIL detail string includes the message so the BIT line
    carries the cause without requiring the operator to grep
    ``data/janitor.log``.

    Symmetric: when ``last_error`` is None or absent, the detail
    stays clean (no trailing "; last error: " sentinel).
    """

    def test_fail_includes_last_error_message(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            sweeps={"sw1": {"timestamp": _hours_ago_iso(72)}},
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": "KeyError: 'foo'",
            },
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.FAIL
        assert "silently failing" in result.detail
        assert "last error: KeyError: 'foo'" in result.detail

    def test_warn_includes_last_error_message(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            sweeps={"sw1": {"timestamp": _hours_ago_iso(36)}},
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": "TimeoutError",
            },
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.WARN
        assert "missed run" in result.detail
        assert "last error: TimeoutError" in result.detail

    def test_fail_without_last_error_omits_suffix(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, sweeps={"sw1": {"timestamp": _hours_ago_iso(72)}})
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.FAIL
        assert "last error:" not in result.detail

    def test_warn_without_last_error_omits_suffix(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, sweeps={"sw1": {"timestamp": _hours_ago_iso(36)}})
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.WARN
        assert "last error:" not in result.detail

    def test_long_message_truncated_to_150_chars(self, tmp_path: Path) -> None:
        # The BIT line is a single-line operator surface — long
        # multi-line tracebacks would wreck readability. Cap at 150
        # chars with an ellipsis sentinel.
        state_path = tmp_path / "s.json"
        long_msg = "TypeError: " + ("x" * 500)
        _write_state(
            state_path,
            sweeps={"sw1": {"timestamp": _hours_ago_iso(72)}},
            last_error={"ts": "2026-05-14T06:00:00+00:00", "message": long_msg},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.FAIL
        assert "..." in result.detail
        suffix = result.detail.split("last error: ", 1)[1]
        assert len(suffix) <= 150

    def test_short_message_not_truncated(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            sweeps={"sw1": {"timestamp": _hours_ago_iso(72)}},
            last_error={"ts": "2026-05-14T06:00:00+00:00", "message": "short"},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert "last error: short" in result.detail
        assert "..." not in result.detail

    def test_ok_status_does_not_append_error_suffix(
        self, tmp_path: Path,
    ) -> None:
        # Defensive: if somehow last_error is set but the timestamp is
        # fresh (shouldn't happen because save_sweep_issues clears,
        # but don't let an operator state-file edit break OK output),
        # the OK detail stays clean. Documents the intent that error
        # surfacing is a WARN/FAIL concern, not OK.
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            sweeps={"sw1": {"timestamp": _hours_ago_iso(6)}},
            last_error={"ts": "2026-05-14T06:00:00+00:00", "message": "stale"},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.status == Status.OK
        assert "last error:" not in result.detail

    def test_payload_carries_last_error_for_json_consumers(
        self, tmp_path: Path,
    ) -> None:
        # JSON consumers of BIT output (operator dashboards, alert
        # routing) get the full structured error in ``result.data``,
        # not just the truncated detail-string suffix.
        state_path = tmp_path / "s.json"
        err = {"ts": "2026-05-14T06:00:00+00:00", "message": "KeyError: 'foo'"}
        _write_state(
            state_path,
            sweeps={"sw1": {"timestamp": _hours_ago_iso(72)}},
            last_error=err,
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_sweep(raw)
        assert result.data.get("last_error") == err
