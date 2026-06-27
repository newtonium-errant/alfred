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
    _check_openrouter,
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


# ---------------------------------------------------------------------------
# _check_openrouter — env-substituted key resolution (KAL-LE flag FIX 1)
# ---------------------------------------------------------------------------
#
# The health-check must resolve the api_key the SAME way the labeler does
# (${VAR} → os.environ[VAR]). The canonical config is
# ``surveyor.openrouter.api_key: "${GROQ_API_KEY}"`` + Groq base_url; when
# GROQ_API_KEY is set the labeler labels fine, so the check must PASS — it
# previously WARNed on the raw ${...} placeholder (false cry-wolf →
# posture yellow on Salem + KAL-LE).


class TestCheckOpenrouterKeyResolution:
    def test_groq_configured_key_set_passes(self, monkeypatch) -> None:
        """REGRESSION PIN (KAL-LE flag): a Groq-configured surveyor
        (api_key=${GROQ_API_KEY}) with the env var SET passes the
        openrouter-key check — it must NOT false-WARN on the placeholder.
        This is the live Salem + KAL-LE config; the labeler resolves +
        labels fine, so the check must agree."""
        monkeypatch.setenv("GROQ_API_KEY", "DUMMY_GROQ_TEST_KEY")
        result = _check_openrouter(
            {"api_key": "${GROQ_API_KEY}", "model": "llama-3.3-70b-versatile"}
        )
        assert result.status == Status.OK, (
            f"Groq-configured key (env set) should PASS, got "
            f"{result.status}: {result.detail}"
        )
        assert result.data["has_key"] is True

    def test_unset_placeholder_warns(self, monkeypatch) -> None:
        """A ${VAR} that resolves to empty (env var unset) → WARN — the
        labeler WOULD skip, so the warn is correct here (not a
        false-positive)."""
        monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
        result = _check_openrouter(
            {"api_key": "${OPENROUTER_API_KEY}", "model": "x"}
        )
        assert result.status == Status.WARN
        assert result.data["has_key"] is False

    def test_empty_key_warns(self) -> None:
        result = _check_openrouter({"api_key": "", "model": "x"})
        assert result.status == Status.WARN
        assert result.data["has_key"] is False

    def test_literal_key_passes(self) -> None:
        result = _check_openrouter(
            {"api_key": "DUMMY_LITERAL_TEST_KEY", "model": "x"}
        )
        assert result.status == Status.OK
        assert result.data["has_key"] is True

    def test_key_set_but_model_missing_warns(self, monkeypatch) -> None:
        """The model-missing WARN must still fire AFTER the key resolves
        (i.e. key-resolution doesn't mask the separate model check)."""
        monkeypatch.setenv("GROQ_API_KEY", "DUMMY_GROQ_TEST_KEY")
        result = _check_openrouter({"api_key": "${GROQ_API_KEY}", "model": ""})
        assert result.status == Status.WARN
        assert "model" in result.detail.lower()
