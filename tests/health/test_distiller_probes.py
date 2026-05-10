"""Tests for the BIT distiller probes — specifically
``last-successful-extraction`` added 2026-05-10 as part of the
cross-daemon BIT probe arc.

Distiller's expected interval is hourly
(``extraction.interval_seconds = 3600``); thresholds (90min OK /
240min FAIL) reflect that with one full grace cycle before WARN
and three before FAIL.

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alfred.distiller.health import (
    _DISTILLER_STALE_FAIL_MINUTES,
    _DISTILLER_STALE_OK_MINUTES,
    _check_last_successful_extraction,
    _read_distiller_most_recent_run,
    _resolve_distiller_state_path,
)
from alfred.health.types import Status


def _minutes_ago_iso(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=n)).isoformat()


def _write_state(
    state_path: Path,
    *,
    runs: dict[str, dict] | None = None,
    last_deep_extraction: str | None = None,
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

    def test_thirty_min_ago_ok(self, tmp_path: Path) -> None:
        # Well within the 90min OK threshold (one extraction interval
        # plus 30min grace).
        state_path = tmp_path / "s.json"
        _write_state(state_path, runs={
            "r1": {"run_id": "r1", "timestamp": _minutes_ago_iso(30)},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.OK

    def test_two_hours_ago_warn(self, tmp_path: Path) -> None:
        # 120min — in the 90-240min WARN window.
        state_path = tmp_path / "s.json"
        _write_state(state_path, runs={
            "r1": {"run_id": "r1", "timestamp": _minutes_ago_iso(120)},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.WARN
        assert "missed run" in result.detail

    def test_six_hours_ago_fail(self, tmp_path: Path) -> None:
        # 360min > 240min → FAIL. The bug-of-record shape: distiller
        # daemon swallowing exceptions and not surfacing — three full
        # intervals have elapsed without any extraction.
        state_path = tmp_path / "s.json"
        _write_state(state_path, runs={
            "r1": {"run_id": "r1", "timestamp": _minutes_ago_iso(360)},
        })
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.FAIL
        assert "silently failing" in result.detail

    def test_last_deep_extraction_alone_provides_signal(
        self, tmp_path: Path,
    ) -> None:
        # No run entries, only a recent ``last_deep_extraction`` —
        # must produce OK rather than SKIP. Catches the same regression
        # shape pinned in the janitor probe (ignoring top-level field).
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_deep_extraction=_minutes_ago_iso(15))
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_extraction(raw)
        assert result.status == Status.OK

    def test_threshold_constants_match_dispatch(self) -> None:
        assert _DISTILLER_STALE_OK_MINUTES == 90
        assert _DISTILLER_STALE_FAIL_MINUTES == 240


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
            "r1": {"run_id": "r1", "timestamp": _minutes_ago_iso(30)},
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
