"""BIT ``agent-failure-kind`` probe — surfaces the 2026-07-29 weekly-limit class.

The ``claude-cli-auth`` probe (login-only, zero-token) stayed GREEN through a
three-day quota outage — logged in the whole time, just out of budget. This
probe closes that blind spot by reading the ``last_agent_failure`` curator
state writes from REAL failing traffic and mapping it to a severity:

    no failure / recovered → OK ; quota → WARN ; auth → FAIL ; other → WARN

THE incident is pinned end-to-end: a quota_limited failure newer than the last
success → WARN with the CLI banner in the detail.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from alfred.curator.health import (
    _check_agent_failure_kind,
    _read_curator_last_agent_failure,
    health_check,
)
from alfred.health.types import Status

INCIDENT_MESSAGE = "Exit code 1: stdout: You've hit your weekly limit · resets 4am (UTC)"


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _write_state(
    state_path: Path,
    *,
    last_run: str | None = None,
    last_agent_failure: dict[str, Any] | None = None,
) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"version": 2, "processed": {}}
    if last_run is not None:
        doc["last_run"] = last_run
    if last_agent_failure is not None:
        doc["last_agent_failure"] = last_agent_failure
    state_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")


def _raw(state_path: Path) -> dict[str, Any]:
    return {"vault": {"path": str(state_path.parent)}, "curator": {"state": {"path": str(state_path)}}}


# ---------------------------------------------------------------------------
# no-failure / absent-state → OK (intentionally-left-blank)
# ---------------------------------------------------------------------------


def test_no_state_file_is_ok(tmp_path: Path) -> None:
    r = _check_agent_failure_kind(_raw(tmp_path / "curator_state.json"))
    assert r.status is Status.OK
    assert "no recent agent failures" in r.detail


def test_state_without_failure_is_ok(tmp_path: Path) -> None:
    sp = tmp_path / "curator_state.json"
    _write_state(sp, last_run=_iso(1))
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.OK
    assert "no recent agent failures" in r.detail


def test_corrupt_state_file_is_ok(tmp_path: Path) -> None:
    sp = tmp_path / "curator_state.json"
    sp.write_text("{ not json", encoding="utf-8")
    assert _read_curator_last_agent_failure(sp) is None
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.OK


# ---------------------------------------------------------------------------
# active failures → severity by kind
# ---------------------------------------------------------------------------


def test_active_quota_failure_warns_with_message(tmp_path: Path) -> None:
    """THE incident regression pin."""
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(72),  # last success is OLD; failure is recent
        last_agent_failure={"ts": _iso(1), "kind": "quota_limited", "summary_tail": INCIDENT_MESSAGE},
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.WARN
    assert "quota-limited" in r.detail
    assert "You've hit your weekly limit" in r.detail
    assert r.data["kind"] == "quota_limited"


def test_active_quota_failure_with_no_prior_success_warns(tmp_path: Path) -> None:
    # No last_run at all (curator never succeeded) → the failure is active.
    sp = tmp_path / "curator_state.json"
    _write_state(sp, last_agent_failure={"ts": _iso(1), "kind": "quota_limited", "summary_tail": INCIDENT_MESSAGE})
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.WARN


def test_active_auth_failure_fails(tmp_path: Path) -> None:
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(72),
        last_agent_failure={"ts": _iso(1), "kind": "auth", "summary_tail": "Exit code 1: not logged in"},
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.FAIL
    assert "DOWN" in r.detail
    assert "not logged in" in r.detail


def test_active_other_failure_warns(tmp_path: Path) -> None:
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(72),
        last_agent_failure={"ts": _iso(1), "kind": "other", "summary_tail": "Exit code 1: connection reset"},
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.WARN
    assert "connection reset" in r.detail


# ---------------------------------------------------------------------------
# ordering vs last-successful-process — recovery
# ---------------------------------------------------------------------------


def test_failure_older_than_last_success_is_ok(tmp_path: Path) -> None:
    """A success AFTER the failure means the pipeline recovered → OK."""
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(1),  # succeeded 1h ago
        last_agent_failure={"ts": _iso(48), "kind": "quota_limited", "summary_tail": INCIDENT_MESSAGE},
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.OK
    assert "predates last success" in r.detail


def test_failure_newer_than_last_success_is_active(tmp_path: Path) -> None:
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(48),  # succeeded 48h ago
        last_agent_failure={"ts": _iso(1), "kind": "quota_limited", "summary_tail": INCIDENT_MESSAGE},
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.WARN


def test_unparseable_failure_ts_still_surfaces(tmp_path: Path) -> None:
    # Can't prove recovery on a bad ts → treat as active, don't swallow it.
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(1),
        last_agent_failure={"ts": "not-a-date", "kind": "quota_limited", "summary_tail": INCIDENT_MESSAGE},
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.WARN


# ---------------------------------------------------------------------------
# health_check wiring — the probe is registered in the tool rollup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_wired_into_health_check(tmp_path: Path) -> None:
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(72),
        last_agent_failure={"ts": _iso(1), "kind": "quota_limited", "summary_tail": INCIDENT_MESSAGE},
    )
    # Non-claude backend → skips the network anthropic/cli probes; the
    # agent-failure-kind probe runs unconditionally.
    raw = {
        "vault": {"path": str(tmp_path)},
        "agent": {"backend": "disabled"},
        "curator": {"state": {"path": str(sp)}},
    }
    tool = await health_check(raw)
    names = [r.name for r in tool.results]
    assert "agent-failure-kind" in names
    probe = next(r for r in tool.results if r.name == "agent-failure-kind")
    assert probe.status is Status.WARN
    # Tool rollup reflects the WARN (worst of OK/WARN across probes).
    assert tool.status in (Status.WARN, Status.FAIL)
