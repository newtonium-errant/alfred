"""Tests for the BIT distiller probes — specifically
``last-successful-extraction`` added 2026-05-10 as part of the
cross-daemon BIT probe arc.

Distiller's deep extraction (the only path that writes state) fires
once per day at 03:30 ADT per ``extraction.deep_extraction_schedule``;
thresholds (30h OK / 48h FAIL) mirror janitor's daily-sweep shape.
Recalibrated 2026-05-10 after smoke-test FAIL on healthy daemon
revealed the original 90min/240min values assumed an hourly cadence
that doesn't exist — see commit message for the misconfig story.

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alfred.distiller.health import (
    _DISTILLER_STALE_FAIL_HOURS,
    _DISTILLER_STALE_OK_HOURS,
    _check_last_successful_extraction,
    _read_distiller_most_recent_run,
    _read_last_error,
    _resolve_distiller_state_path,
)
from alfred.health.types import Status


def _hours_ago_iso(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


def _write_state(
    state_path: Path,
    *,
    runs: dict[str, dict] | None = None,
    last_deep_extraction: str | None = None,
    last_error: dict | None = None,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "files": {},
                "runs": runs or {},
                "extraction_log": [],
                "pending_writes": {},
                "last_deep_extraction": last_deep_extraction,
                "last_error": last_error,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _raw(tmp_path: Path, *, state_path: Path | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {"distiller": {}}
    if state_path is not None:
        raw["distiller"]["state"] = {"path": str(state_path)}
    return raw


# ---------------------------------------------------------------------------
# _resolve_distiller_state_path
# ---------------------------------------------------------------------------


class TestResolveDistillerStatePath:
    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom" / "distiller_state.json"
        raw = _raw(tmp_path, state_path=explicit)
        assert _resolve_distiller_state_path(raw) == explicit

    def test_fallback_to_default(self) -> None:
        assert str(_resolve_distiller_state_path({"distiller": {}})) == "data/distiller_state.json"

    def test_no_distiller_section_falls_back_to_default(self) -> None:
        assert str(_resolve_distiller_state_path({})) == "data/distiller_state.json"


# ---------------------------------------------------------------------------
# _read_distiller_most_recent_run
# ---------------------------------------------------------------------------


class TestReadDistillerMostRecentRun:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_distiller_most_recent_run(tmp_path / "missing.json") is None

    def test_empty_state_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path)
        assert _read_distiller_most_recent_run(state_path) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text("not valid {{{", encoding="utf-8")
        assert _read_distiller_most_recent_run(state_path) is None

    def test_returns_max_of_run_timestamps(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, runs={
            "r1": {"run_id": "r1", "timestamp": "2026-05-10T08:00:00+00:00"},
            "r2": {"run_id": "r2", "timestamp": "2026-05-10T10:00:00+00:00"},
            "r3": {"run_id": "r3", "timestamp": "2026-05-10T09:00:00+00:00"},
        })
        assert _read_distiller_most_recent_run(state_path) == "2026-05-10T10:00:00+00:00"

    def test_last_deep_extraction_alone_provides_signal(
        self, tmp_path: Path,
    ) -> None:
        # Same regression-shape pin as janitor's last_deep_sweep test:
        # if the probe forgets to consider the top-level field, a
        # daemon whose only recent activity was a deep extraction
        # would falsely SKIP.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_deep_extraction="2026-05-10T09:00:00+00:00")
        assert _read_distiller_most_recent_run(state_path) == "2026-05-10T09:00:00+00:00"

    def test_max_picks_newer_when_both_present(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            runs={"r1": {"run_id": "r1", "timestamp": "2026-05-10T08:00:00+00:00"}},
            last_deep_extraction="2026-05-10T11:00:00+00:00",
        )
        assert _read_distiller_most_recent_run(state_path) == "2026-05-10T11:00:00+00:00"

    def test_run_entry_not_dict_skipped(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({
                "version": 1,
                "runs": {
                    "r1": "not a dict",
                    "r2": {"run_id": "r2", "timestamp": "2026-05-10T10:00:00+00:00"},
                },
            }),
            encoding="utf-8",
        )
        assert _read_distiller_most_recent_run(state_path) == "2026-05-10T10:00:00+00:00"


# ---------------------------------------------------------------------------
# _check_last_successful_extraction — full probe contract
# ---------------------------------------------------------------------------


class TestLastSuccessfulExtractionProbe:
    def test_no_state_file_is_skip_fresh_install(self, tmp_path: Path) -> None:
        state_path = tmp_path / "missing.json"
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.SKIP
        assert "fresh install" in result.detail

    def test_empty_state_is_skip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path)
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.SKIP
        assert "no extraction runs" in result.detail

    def test_unparseable_timestamp_is_skip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, runs={
            "r1": {"run_id": "r1", "timestamp": "not-a-timestamp"},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.SKIP

    def test_six_hours_ago_ok(self, tmp_path: Path) -> None:
        # Well within the 30h OK threshold — typical mid-day probe a
        # few hours after the 03:30 ADT deep extraction.
        state_path = tmp_path / "s.json"
        _write_state(state_path, runs={
            "r1": {"run_id": "r1", "timestamp": _hours_ago_iso(6)},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.OK

    def test_thirty_six_hours_ago_warn(self, tmp_path: Path) -> None:
        # 36h — in the 30..48h WARN window. One missed daily run; could
        # be a transient API blip on the 03:30 ADT fire.
        state_path = tmp_path / "s.json"
        _write_state(state_path, runs={
            "r1": {"run_id": "r1", "timestamp": _hours_ago_iso(36)},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.WARN
        assert "missed run" in result.detail

    def test_seventy_two_hours_ago_fail(self, tmp_path: Path) -> None:
        # 72h > 48h → FAIL. The bug-of-record shape: distiller daemon
        # swallowing exceptions and not surfacing — multi-day silent
        # failure pattern.
        state_path = tmp_path / "s.json"
        _write_state(state_path, runs={
            "r1": {"run_id": "r1", "timestamp": _hours_ago_iso(72)},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.FAIL
        assert "silently failing" in result.detail

    def test_twelve_hours_ago_still_ok(self, tmp_path: Path) -> None:
        # Regression-pin against the original misconfig: 12.5h was
        # FAILing under the old 240min threshold even though it's a
        # healthy mid-day-after-03:30-extraction state. Must be OK
        # under the recalibrated 30h threshold. This test exists
        # specifically so a future regression that restores the
        # minute-shaped thresholds fails loudly.
        state_path = tmp_path / "s.json"
        _write_state(state_path, runs={
            "r1": {"run_id": "r1", "timestamp": _hours_ago_iso(12.5)},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.OK

    def test_last_deep_extraction_alone_provides_signal(
        self, tmp_path: Path,
    ) -> None:
        # No run entries, only a recent ``last_deep_extraction`` —
        # must produce OK rather than SKIP. Catches the same regression
        # shape pinned in the janitor probe (ignoring top-level field).
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_deep_extraction=_hours_ago_iso(1))
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.OK

    def test_threshold_constants_match_dispatch(self) -> None:
        # Recalibrated 2026-05-10 from minute-shaped to hour-shaped
        # after smoke-test FAIL on healthy daemon. See commit message.
        assert _DISTILLER_STALE_OK_HOURS == 30
        assert _DISTILLER_STALE_FAIL_HOURS == 48


# ---------------------------------------------------------------------------
# health_check integration
# ---------------------------------------------------------------------------


class TestHealthCheckIntegration:
    async def test_last_successful_extraction_appears_in_results(
        self, tmp_path: Path,
    ) -> None:
        from alfred.distiller.health import health_check

        state_path = tmp_path / "distiller_state.json"
        _write_state(state_path, runs={
            "r1": {"run_id": "r1", "timestamp": _hours_ago_iso(6)},
        })

        raw: dict[str, Any] = {
            "vault": {"path": str(tmp_path)},
            "agent": {"backend": "openclaw"},  # skip anthropic-auth
            "distiller": {
                "state": {"path": str(state_path)},
            },
        }

        rollup = await health_check(raw, mode="quick")
        names = [r.name for r in rollup.results]
        assert "last-successful-extraction" in names
        last = next(r for r in rollup.results if r.name == "last-successful-extraction")
        assert last.status == Status.OK

    async def test_skips_when_distiller_section_absent(
        self, tmp_path: Path,
    ) -> None:
        """KAL-LE peer-digest regression-pin (2026-05-16).

        Instances that don't run distiller must surface a tool-level
        SKIP rather than running probes against a config that has no
        distiller section. Mirrors the gating in surveyor / brief /
        mail / etc.

        KAL-LE actually DOES run distiller in production, but
        Hypatia / future instances may not — and the bug class is the
        same: tool absent → probe must not consult an ageing state
        file via the dataclass default path.
        """
        from alfred.distiller.health import health_check

        rollup = await health_check({}, mode="quick")
        assert rollup.status == Status.SKIP
        assert rollup.results == []
        assert "no distiller section" in (rollup.detail or "")

    async def test_skips_when_only_other_sections_present(
        self, tmp_path: Path,
    ) -> None:
        """Hypatia-shape config: vault + telegram present, distiller absent."""
        from alfred.distiller.health import health_check

        raw: dict[str, Any] = {
            "vault": {"path": str(tmp_path)},
            "telegram": {"bot_token": "test"},
        }
        rollup = await health_check(raw, mode="quick")
        assert rollup.status == Status.SKIP
        assert rollup.results == []


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
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_error="not-a-dict")  # type: ignore[arg-type]
        assert _read_last_error(state_path) is None

    def test_last_error_without_message_returns_none(
        self, tmp_path: Path,
    ) -> None:
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
# _check_last_successful_extraction — last_error surfacing in WARN/FAIL detail
# (added 2026-05-14)
# ---------------------------------------------------------------------------


class TestLastErrorSurfacing:
    """Pin that when ``last_error`` is populated, the probe's
    WARN/FAIL detail string includes the message so the BIT line
    carries the cause without requiring the operator to grep
    ``data/distiller.log``.

    Symmetric: when ``last_error`` is None or absent, the detail
    stays clean (no trailing "; last error: " sentinel).
    """

    def test_fail_includes_last_error_message(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            runs={"r1": {"run_id": "r1", "timestamp": _hours_ago_iso(72)}},
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": "KeyError: 'foo'",
            },
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.FAIL
        assert "silently failing" in result.detail
        assert "last error: KeyError: 'foo'" in result.detail

    def test_warn_includes_last_error_message(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            runs={"r1": {"run_id": "r1", "timestamp": _hours_ago_iso(36)}},
            last_error={
                "ts": "2026-05-14T06:00:00+00:00",
                "message": "TimeoutError",
            },
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.WARN
        assert "missed run" in result.detail
        assert "last error: TimeoutError" in result.detail

    def test_fail_without_last_error_omits_suffix(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            runs={"r1": {"run_id": "r1", "timestamp": _hours_ago_iso(72)}},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.FAIL
        assert "last error:" not in result.detail

    def test_warn_without_last_error_omits_suffix(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            runs={"r1": {"run_id": "r1", "timestamp": _hours_ago_iso(36)}},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.WARN
        assert "last error:" not in result.detail

    def test_long_message_truncated_to_150_chars(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        long_msg = "TypeError: " + ("x" * 500)
        _write_state(
            state_path,
            runs={"r1": {"run_id": "r1", "timestamp": _hours_ago_iso(72)}},
            last_error={"ts": "2026-05-14T06:00:00+00:00", "message": long_msg},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.FAIL
        assert "..." in result.detail
        suffix = result.detail.split("last error: ", 1)[1]
        assert len(suffix) <= 150

    def test_short_message_not_truncated(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            runs={"r1": {"run_id": "r1", "timestamp": _hours_ago_iso(72)}},
            last_error={"ts": "2026-05-14T06:00:00+00:00", "message": "short"},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert "last error: short" in result.detail
        assert "..." not in result.detail

    def test_ok_status_does_not_append_error_suffix(
        self, tmp_path: Path,
    ) -> None:
        # Defensive: stale last_error with a fresh timestamp shouldn't
        # leak into OK detail. add_run clears, so this only happens on
        # operator hand-edit.
        state_path = tmp_path / "s.json"
        _write_state(
            state_path,
            runs={"r1": {"run_id": "r1", "timestamp": _hours_ago_iso(6)}},
            last_error={"ts": "2026-05-14T06:00:00+00:00", "message": "stale"},
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.OK
        assert "last error:" not in result.detail

    def test_payload_carries_last_error_for_json_consumers(
        self, tmp_path: Path,
    ) -> None:
        # JSON consumers of BIT output get the full structured error.
        state_path = tmp_path / "s.json"
        err = {"ts": "2026-05-14T06:00:00+00:00", "message": "KeyError: 'foo'"}
        _write_state(
            state_path,
            runs={"r1": {"run_id": "r1", "timestamp": _hours_ago_iso(72)}},
            last_error=err,
        )
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.data.get("last_error") == err
