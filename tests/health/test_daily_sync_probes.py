"""Tests for the BIT daily_sync probes — specifically the
``last-successful-fire`` daemon-liveness probe added 2026-05-10 to
close the cross-daemon silent-failure observability sweep.

Brief / curator / surveyor / janitor / distiller all got their
last-successful-* probes earlier today; daily_sync mirrors the same
pattern. Per ``feedback_intentionally_left_blank.md``: silence
(daily_sync daemon idle, no morning-message log activity, operator
notices missing 09:00 ADT Telegram message) is ambiguous between
healthy-quiet and broken; the probe disambiguates.

Tests run unconditionally per
``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alfred.daily_sync.health import (
    _check_last_successful_fire,
    _check_state_path,
    _read_last_fired_date,
    _resolve_state_path,
    health_check,
)
from alfred.health.types import Status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_state(state_path: Path, last_fired_date: str | None = None, **extra: Any) -> None:
    """Write a daily_sync_state.json with the given ``last_fired_date``.

    Daily Sync's state is a flat dict (no dataclass wrapper today) —
    mirror that shape so the test fixtures match production.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    if last_fired_date is not None:
        payload["last_fired_date"] = last_fired_date
    payload.update(extra)
    state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _today_iso(tz_name: str = "America/Halifax") -> str:
    return datetime.now(ZoneInfo(tz_name)).date().isoformat()


def _yesterday_iso(tz_name: str = "America/Halifax") -> str:
    return (datetime.now(ZoneInfo(tz_name)).date() - timedelta(days=1)).isoformat()


def _days_ago_iso(n: int, tz_name: str = "America/Halifax") -> str:
    return (datetime.now(ZoneInfo(tz_name)).date() - timedelta(days=n)).isoformat()


def _config(
    tmp_path: Path,
    *,
    state_path: Path | None = None,
    enabled: bool = True,
    tz: str = "America/Halifax",
    schedule_time: str = "09:00",
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "vault": {"path": str(tmp_path)},
        "logging": {"dir": str(tmp_path)},
        "daily_sync": {
            "enabled": enabled,
            "schedule": {"time": schedule_time, "timezone": tz},
        },
    }
    if state_path is not None:
        raw["daily_sync"]["state"] = {"path": str(state_path)}
    return raw


# ---------------------------------------------------------------------------
# _resolve_state_path — mirrors daily_sync.config.load_from_unified
# ---------------------------------------------------------------------------


class TestResolveStatePath:
    def test_explicit_path_wins(self, tmp_path: Path) -> None:
        explicit = tmp_path / "custom" / "daily_sync_state.json"
        raw = _config(tmp_path, state_path=explicit)
        assert _resolve_state_path(raw["daily_sync"]) == explicit

    def test_fallback_to_default(self) -> None:
        # Mirrors the dataclass default — pin so a config-default
        # rename surfaces as a test failure.
        assert str(_resolve_state_path({})) == "data/daily_sync_state.json"

    def test_empty_state_section_falls_back(self) -> None:
        # Defensive: ``state: {}`` (block present, key absent) must
        # still fall back cleanly.
        assert str(_resolve_state_path({"state": {}})) == "data/daily_sync_state.json"


# ---------------------------------------------------------------------------
# _read_last_fired_date — schema-tolerant inline dict-walk
# ---------------------------------------------------------------------------


class TestReadLastFiredDate:
    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _read_last_fired_date(tmp_path / "missing.json") is None

    def test_present_field_round_trips(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_fired_date="2026-05-10")
        assert _read_last_fired_date(state_path) == "2026-05-10"

    def test_missing_field_returns_none(self, tmp_path: Path) -> None:
        # State file exists but doesn't yet have last_fired_date —
        # daemon hasn't fired even once.
        state_path = tmp_path / "s.json"
        _write_state(state_path)  # no last_fired_date
        assert _read_last_fired_date(state_path) is None

    def test_corrupt_json_returns_none(self, tmp_path: Path) -> None:
        # JSONDecodeError must NOT crash the BIT run — degrade
        # gracefully to "we don't know" and let the probe SKIP.
        state_path = tmp_path / "s.json"
        state_path.write_text("not valid json {{{", encoding="utf-8")
        assert _read_last_fired_date(state_path) is None

    def test_top_level_not_a_dict_returns_none(self, tmp_path: Path) -> None:
        # Defensive against future schema migrations or accidental
        # JSON-array writes.
        state_path = tmp_path / "s.json"
        state_path.write_text(json.dumps(["array", "not", "dict"]), encoding="utf-8")
        assert _read_last_fired_date(state_path) is None

    def test_last_fired_date_not_string_returns_none(self, tmp_path: Path) -> None:
        # Defensive: int / null / object types in the field don't
        # crash the loader.
        state_path = tmp_path / "s.json"
        state_path.write_text(
            json.dumps({"last_fired_date": 12345}),
            encoding="utf-8",
        )
        assert _read_last_fired_date(state_path) is None

    def test_empty_string_returns_none(self, tmp_path: Path) -> None:
        # Defensive: ``"last_fired_date": ""`` is treated the same as
        # absent. Without this branch, an empty string would parse
        # successfully through ``date.fromisoformat`` and crash —
        # the inline dict-walk filters it before that.
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_fired_date="")
        assert _read_last_fired_date(state_path) is None


# ---------------------------------------------------------------------------
# _check_state_path — fresh-install / readable / writable cases
# ---------------------------------------------------------------------------


class TestCheckStatePath:
    def test_present_readable_is_ok(self, tmp_path: Path) -> None:
        state_path = tmp_path / "s.json"
        _write_state(state_path, last_fired_date="2026-05-10")
        result = _check_state_path(state_path)
        assert result.status == Status.OK
        assert result.data["exists"] is True

    def test_missing_with_writable_parent_is_ok_fresh_install(
        self, tmp_path: Path,
    ) -> None:
        # Fresh-install path: state file doesn't exist yet but the
        # parent dir is ready for the daemon to write into.
        state_path = tmp_path / "s.json"
        result = _check_state_path(state_path)
        assert result.status == Status.OK
        assert result.data["exists"] is False
        assert "will be created" in result.detail

    def test_missing_with_missing_parent_is_warn(self, tmp_path: Path) -> None:
        # Parent dir doesn't exist — orchestrator/daemon will create
        # it, but BIT-time observability is "this isn't quite ready."
        state_path = tmp_path / "missing_parent" / "s.json"
        result = _check_state_path(state_path)
        assert result.status == Status.WARN


# ---------------------------------------------------------------------------
# _check_last_successful_fire — full probe contract
# ---------------------------------------------------------------------------


class TestLastSuccessfulFireProbe:
    def test_today_is_ok(self, tmp_path: Path) -> None:
        # Operator manually re-fired daily_sync after BIT, OR the
        # probe is run mid-day after the 09:00 daemon fired.
        state_path = tmp_path / "daily_sync_state.json"
        _write_state(state_path, last_fired_date=_today_iso())
        raw = _config(tmp_path, state_path=state_path)
        result = _check_last_successful_fire(raw, raw["daily_sync"])
        assert result.status == Status.OK
        assert result.data["days_old"] == 0

    def test_yesterday_is_ok(self, tmp_path: Path) -> None:
        # The healthy-steady-state case at BIT time. BIT runs at
        # 05:55 ADT before daily_sync at 09:00 ADT — yesterday's
        # fire is the freshest possible value at probe time.
        state_path = tmp_path / "daily_sync_state.json"
        _write_state(state_path, last_fired_date=_yesterday_iso())
        raw = _config(tmp_path, state_path=state_path)
        result = _check_last_successful_fire(raw, raw["daily_sync"])
        assert result.status == Status.OK
        assert result.data["days_old"] == 1

    def test_two_days_ago_is_warn(self, tmp_path: Path) -> None:
        state_path = tmp_path / "daily_sync_state.json"
        _write_state(state_path, last_fired_date=_days_ago_iso(2))
        raw = _config(tmp_path, state_path=state_path)
        result = _check_last_successful_fire(raw, raw["daily_sync"])
        assert result.status == Status.WARN
        assert result.data["days_old"] == 2
        assert "missed run" in result.detail

    def test_three_days_ago_is_fail(self, tmp_path: Path) -> None:
        # Threshold pin — anything > 2 days = FAIL. Mirrors the
        # brief precedent exactly.
        state_path = tmp_path / "daily_sync_state.json"
        _write_state(state_path, last_fired_date=_days_ago_iso(3))
        raw = _config(tmp_path, state_path=state_path)
        result = _check_last_successful_fire(raw, raw["daily_sync"])
        assert result.status == Status.FAIL
        assert "silently failing" in result.detail

    def test_seven_days_ago_is_fail(self, tmp_path: Path) -> None:
        # Multi-day silent failure — the bug class the cross-daemon
        # arc closed.
        state_path = tmp_path / "daily_sync_state.json"
        _write_state(state_path, last_fired_date=_days_ago_iso(7))
        raw = _config(tmp_path, state_path=state_path)
        result = _check_last_successful_fire(raw, raw["daily_sync"])
        assert result.status == Status.FAIL
        assert result.data["days_old"] == 7

    def test_no_state_file_is_skip_fresh_install(self, tmp_path: Path) -> None:
        state_path = tmp_path / "missing_state.json"
        raw = _config(tmp_path, state_path=state_path)
        result = _check_last_successful_fire(raw, raw["daily_sync"])
        assert result.status == Status.SKIP
        assert "fresh install" in result.detail

    def test_state_file_without_last_fired_date_is_skip(
        self, tmp_path: Path,
    ) -> None:
        # State file exists (daemon may have written other fields like
        # ``last_batch``) but ``last_fired_date`` was never written.
        # Treat as "we don't have a positive signal" → SKIP.
        state_path = tmp_path / "daily_sync_state.json"
        _write_state(state_path, last_batch={})  # no last_fired_date
        raw = _config(tmp_path, state_path=state_path)
        result = _check_last_successful_fire(raw, raw["daily_sync"])
        assert result.status == Status.SKIP
        assert "no last_fired_date" in result.detail

    def test_unparseable_date_is_skip(self, tmp_path: Path) -> None:
        # Corrupt date string in state → degrade gracefully to SKIP,
        # don't crash the BIT run.
        state_path = tmp_path / "daily_sync_state.json"
        _write_state(state_path, last_fired_date="not-a-date")
        raw = _config(tmp_path, state_path=state_path)
        result = _check_last_successful_fire(raw, raw["daily_sync"])
        assert result.status == Status.SKIP
        assert "unparseable" in result.detail

    def test_bad_timezone_skips_probe_no_double_fail(
        self, tmp_path: Path,
    ) -> None:
        # If ``schedule.timezone`` is misconfigured, the
        # schedule-timezone probe already FAILs that surface. The
        # last-successful-fire probe MUST SKIP (not double-FAIL).
        state_path = tmp_path / "daily_sync_state.json"
        _write_state(state_path, last_fired_date=_yesterday_iso())
        raw = _config(tmp_path, state_path=state_path, tz="Invalid/Zone")
        result = _check_last_successful_fire(raw, raw["daily_sync"])
        assert result.status == Status.SKIP
        assert "unresolvable" in result.detail

    def test_uses_configured_timezone_not_utc(self, tmp_path: Path) -> None:
        # Timezone-correctness pin per the dispatch reminder. daily_sync
        # writes ``today.isoformat()`` in the configured timezone (NOT
        # UTC) at daemon.py:297 — the probe must compute today/yesterday
        # the same way or fence-post by one day at certain hours.
        state_path = tmp_path / "daily_sync_state.json"
        yesterday_halifax = (
            datetime.now(ZoneInfo("America/Halifax")).date() - timedelta(days=1)
        ).isoformat()
        _write_state(state_path, last_fired_date=yesterday_halifax)
        raw = _config(tmp_path, state_path=state_path, tz="America/Halifax")
        result = _check_last_successful_fire(raw, raw["daily_sync"])
        assert result.status == Status.OK


# ---------------------------------------------------------------------------
# health_check rollup — the two short-circuit gates + integration
# ---------------------------------------------------------------------------


class TestHealthCheckGates:
    async def test_no_daily_sync_section_is_skip(self, tmp_path: Path) -> None:
        # Hypatia case: instance config doesn't have a daily_sync
        # block at all. Must SKIP cleanly without crashing.
        raw: dict[str, Any] = {"vault": {"path": str(tmp_path)}}
        rollup = await health_check(raw, mode="quick")
        assert rollup.tool == "daily_sync"
        assert rollup.status == Status.SKIP
        assert "no daily_sync section" in rollup.detail
        # No probe rows when the gate fires — explicit empty-list.
        assert rollup.results == []

    async def test_daily_sync_not_a_dict_is_skip(self, tmp_path: Path) -> None:
        # Defensive against a malformed config where daily_sync was
        # set to e.g. a string by accident.
        raw: dict[str, Any] = {
            "vault": {"path": str(tmp_path)},
            "daily_sync": "not a dict",
        }
        rollup = await health_check(raw, mode="quick")
        assert rollup.status == Status.SKIP
        assert "not a dict" in rollup.detail

    async def test_enabled_false_is_skip(self, tmp_path: Path) -> None:
        # Operator opted out via daily_sync.enabled = false. The
        # rollup MUST SKIP (not OK) so a green status doesn't
        # misrepresent intent.
        raw = _config(tmp_path, enabled=False)
        rollup = await health_check(raw, mode="quick")
        assert rollup.status == Status.SKIP
        assert "explicitly disabled" in rollup.detail

    async def test_enabled_default_false_is_skip(self, tmp_path: Path) -> None:
        # daily_sync.enabled defaults to False per
        # DailySyncConfig.enabled = False — the rollup must respect
        # the absence of an explicit ``enabled: true`` and SKIP.
        raw: dict[str, Any] = {
            "vault": {"path": str(tmp_path)},
            "daily_sync": {
                "schedule": {"time": "09:00", "timezone": "America/Halifax"},
            },
        }
        rollup = await health_check(raw, mode="quick")
        assert rollup.status == Status.SKIP
        assert "explicitly disabled" in rollup.detail


class TestHealthCheckIntegration:
    async def test_enabled_with_yesterday_fire_rolls_up_ok(
        self, tmp_path: Path,
    ) -> None:
        # End-to-end pin: enabled + yesterday's fire + valid schedule
        # → rollup is OK, all 4 probe rows present
        # (schedule-time, schedule-timezone, state-path,
        # last-successful-fire).
        state_path = tmp_path / "daily_sync_state.json"
        _write_state(state_path, last_fired_date=_yesterday_iso())
        raw = _config(tmp_path, state_path=state_path, enabled=True)
        rollup = await health_check(raw, mode="quick")
        assert rollup.tool == "daily_sync"
        assert rollup.status == Status.OK
        names = [r.name for r in rollup.results]
        # All four probes present — catches a regression where one is
        # implemented but never appended to results.
        assert "schedule-time" in names
        assert "schedule-timezone" in names
        assert "state-path" in names
        assert "last-successful-fire" in names
        # Last-successful-fire fired green.
        last = next(r for r in rollup.results if r.name == "last-successful-fire")
        assert last.status == Status.OK

    async def test_enabled_with_old_fire_rolls_up_fail(
        self, tmp_path: Path,
    ) -> None:
        # End-to-end pin that a FAIL on last-successful-fire
        # propagates to the rollup status.
        state_path = tmp_path / "daily_sync_state.json"
        _write_state(state_path, last_fired_date=_days_ago_iso(7))
        raw = _config(tmp_path, state_path=state_path, enabled=True)
        rollup = await health_check(raw, mode="quick")
        assert rollup.status == Status.FAIL
        last = next(r for r in rollup.results if r.name == "last-successful-fire")
        assert last.status == Status.FAIL


# ---------------------------------------------------------------------------
# Aggregator registration — pin that the new module is in the registry
# ---------------------------------------------------------------------------


class TestAggregatorRegistration:
    def test_daily_sync_registered_in_known_tool_modules(self) -> None:
        # The whole point of side 1: KNOWN_TOOL_MODULES MUST include
        # daily_sync, otherwise the aggregator never imports the
        # health module and the register_check() call never fires.
        # Pin to catch a regression that drops the entry.
        from alfred.health.aggregator import KNOWN_TOOL_MODULES
        assert "daily_sync" in KNOWN_TOOL_MODULES
        assert KNOWN_TOOL_MODULES["daily_sync"] == "alfred.daily_sync.health"
