"""Tests for the BIT surveyor probes — specifically
``last-successful-cycle`` added 2026-05-10 as part of the cross-
daemon BIT probe arc.

Surveyor runs a continuous loop (no fixed schedule). Stale-cycle
detection uses sub-hourly thresholds (2h OK, 2-6h WARN, >6h FAIL)
that reflect the expected cadence.

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alfred.health.types import Status
from alfred.surveyor.health import (
    _SURVEYOR_STALE_FAIL_HOURS,
    _SURVEYOR_STALE_OK_HOURS,
    _check_last_successful_cycle,
    _read_surveyor_last_run,
    _resolve_surveyor_state_path,
)


def _write_state(state_path: Path, last_run: str) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {"version": 1, "last_run": last_run, "files": {}, "clusters": {}},
            indent=2,
        ),
        encoding="utf-8",
    )


def _hours_ago_iso(n: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=n)).isoformat()


def _raw(tmp_path: Path, *, state_path: Path | None = None) -> dict[str, Any]:
    raw: dict[str, Any] = {"surveyor": {}}
    if state_path is not None:
        raw["surveyor"]["state"] = {"path": str(state_path)}
    return raw


# ---------------------------------------------------------------------------
# _resolve_surveyor_state_path
# ---------------------------------------------------------------------------


class TestResolveSurveyorStatePath:
    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom" / "surveyor_state.json"
        raw = _raw(tmp_path, state_path=explicit)
        assert _resolve_surveyor_state_path(raw) == explicit

    def test_fallback_to_default(self) -> None:
        assert str(_resolve_surveyor_state_path({"surveyor": {}})) == "data/surveyor_state.json"

    def test_no_surveyor_section_falls_back_to_default(self) -> None:
        # Defensive: probe must not crash on configs that omit
        # the surveyor section (Hypatia / instances without surveyor
        # enabled).
        assert str(_resolve_surveyor_state_path({})) == "data/surveyor_state.json"


# ---------------------------------------------------------------------------
# _read_surveyor_last_run
# ---------------------------------------------------------------------------


class TestReadSurveyorLastRun:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_surveyor_last_run(tmp_path / "missing.json") is None

    def test_empty_last_run_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run="")
        assert _read_surveyor_last_run(state_path) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        state_path.write_text("not valid {{{", encoding="utf-8")
        assert _read_surveyor_last_run(state_path) is None

    def test_round_trip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        ts = "2026-05-10T14:00:00+00:00"
        _write_state(state_path, last_run=ts)
        assert _read_surveyor_last_run(state_path) == ts

    def test_last_run_not_string_returns_none(self, tmp_path: Path) -> None:
        # Defensive against future schema migrations.
        state_path = tmp_path / "s.json"
        state_path.write_text(json.dumps({"last_run": 12345}), encoding="utf-8")
        assert _read_surveyor_last_run(state_path) is None


# ---------------------------------------------------------------------------
# _check_last_successful_cycle — full probe contract
# ---------------------------------------------------------------------------


class TestLastSuccessfulCycleProbe:
    def test_no_state_file_is_skip_fresh_install(self, tmp_path: Path) -> None:
        state_path = tmp_path / "missing.json"
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_cycle(raw)
        assert result.status == Status.SKIP
        assert "fresh install" in result.detail

    def test_empty_last_run_is_skip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run="")
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_cycle(raw)
        assert result.status == Status.SKIP
        assert "no last_run" in result.detail

    def test_unparseable_last_run_is_skip(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run="not-a-timestamp")
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_cycle(raw)
        assert result.status == Status.SKIP

    def test_recent_cycle_is_ok(self, tmp_path: Path) -> None:
        # 30min ago — well within the 2h OK window.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run=_hours_ago_iso(0.5))
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_cycle(raw)
        assert result.status == Status.OK

    def test_three_hours_ago_is_warn(self, tmp_path: Path) -> None:
        # In the 2h..6h band — slow-loop hiccup window.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run=_hours_ago_iso(3))
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_cycle(raw)
        assert result.status == Status.WARN

    def test_eight_hours_ago_is_fail(self, tmp_path: Path) -> None:
        # >6h — loop wedged. The bug-of-record shape for surveyor.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_run=_hours_ago_iso(8))
        raw = _raw(tmp_path, state_path=state_path)
        result = _check_last_successful_cycle(raw)
        assert result.status == Status.FAIL
        assert "wedged" in result.detail

    def test_threshold_constants_match_dispatch_calibration(self) -> None:
        # Pin the thresholds Andrew specified — so a future tune
        # surfaces in the test diff.
        assert _SURVEYOR_STALE_OK_HOURS == 2
        assert _SURVEYOR_STALE_FAIL_HOURS == 6


# ---------------------------------------------------------------------------
# health_check integration
# ---------------------------------------------------------------------------


class TestHealthCheckIntegration:
    async def test_last_successful_cycle_appears_in_results(
        self, tmp_path: Path,
    ) -> None:
        from alfred.surveyor.health import health_check

        state_path = tmp_path / "surveyor_state.json"
        _write_state(state_path, last_run=_hours_ago_iso(0.5))

        # Minimal surveyor config — ollama check will WARN on
        # unreachable but probe wiring is what we're verifying.
        raw: dict[str, Any] = {
            "surveyor": {
                "ollama": {"base_url": "http://localhost:11434"},
                "milvus": {"uri": str(tmp_path / "milvus.db")},
                "openrouter": {},
                "state": {"path": str(state_path)},
            },
        }

        rollup = await health_check(raw, mode="quick")
        names = [r.name for r in rollup.results]
        assert "last-successful-cycle" in names
        last = next(r for r in rollup.results if r.name == "last-successful-cycle")
        assert last.status == Status.OK
