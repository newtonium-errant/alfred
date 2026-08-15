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
from alfred.health.agent_failure import SUSTAINED_FAILURE_STREAK
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
# SUSTAINED outage escalates WARN → FAIL (2026-08-15)
# ---------------------------------------------------------------------------
#
# The severity IS the escalation: ``brief.feed_producer.health_feed_items``
# promotes a FAIL health card to needs-you attention, so this boundary is what
# decides whether a multi-day agent-backend outage reaches the operator or sits
# in the brief as an FYI glance line. Both sides of the threshold are pinned —
# an escalation that fires one failure early is a doorbell for every hiccup.


def _quota_failure(streak: int, *, since_minutes: float = 240) -> dict[str, Any]:
    return {
        "ts": _iso(1),
        "kind": "quota_limited",
        "summary_tail": INCIDENT_MESSAGE,
        "consecutive": streak,
        "since": _iso(since_minutes / 60.0),
    }


def test_streak_one_below_threshold_stays_warn(tmp_path: Path) -> None:
    """N-1 is a hiccup. The lower half of the boundary."""
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(72),
        last_agent_failure=_quota_failure(SUSTAINED_FAILURE_STREAK - 1),
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.WARN
    assert r.data["sustained"] is False


def test_streak_at_threshold_fails(tmp_path: Path) -> None:
    """N is an outage. The upper half of the same boundary."""
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(72),
        last_agent_failure=_quota_failure(SUSTAINED_FAILURE_STREAK),
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.FAIL
    assert r.data["sustained"] is True
    assert r.data["consecutive"] == SUSTAINED_FAILURE_STREAK


def test_sustained_card_body_names_class_and_consequence(tmp_path: Path) -> None:
    """The operator-facing copy. This IS the deliverable of the escalation —
    the card has to say what broke, what it costs, and (via the CLI tail) when
    it resets, or a needs-you ring just says 'something is wrong'."""
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(72),
        last_agent_failure=_quota_failure(4),
    )
    detail = _check_agent_failure_kind(_raw(sp)).detail
    assert "quota-limited" in detail                    # the failure CLASS
    assert "4 consecutive agent failures" in detail     # why it is an outage
    assert "email intake is stopped" in detail          # the CONSEQUENCE
    assert "quarantined" in detail                      # ...and that mail is not lost
    assert "You've hit your weekly limit" in detail     # the reset date rides the tail


def test_sustained_other_kind_also_escalates(tmp_path: Path) -> None:
    """Escalation is STRUCTURAL — it keys on the streak, not on the error text.

    An unclassified backend failure repeating N times is as much an outage as a
    recognised quota banner; only the wording differs.
    """
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(72),
        last_agent_failure={
            "ts": _iso(1), "kind": "other", "summary_tail": "Exit code 1: connection reset",
            "consecutive": SUSTAINED_FAILURE_STREAK, "since": _iso(4),
        },
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.FAIL
    assert "failing (other)" in r.detail
    assert "email intake is stopped" in r.detail


def test_sustained_streak_recovered_by_success_is_ok(tmp_path: Path) -> None:
    """Recovery DOWNGRADES: a success after the outage clears it to OK, which
    makes the card go absent and lets reconcile retire it.

    Positive control is the pin above — same streak, same kind; the only
    difference is that ``last_run`` now post-dates the failure.
    """
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(0.5),  # success AFTER the failure below
        last_agent_failure={
            "ts": _iso(1), "kind": "quota_limited", "summary_tail": INCIDENT_MESSAGE,
            "consecutive": SUSTAINED_FAILURE_STREAK + 5, "since": _iso(72),
        },
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.OK
    assert "predates last success" in r.detail


def test_legacy_record_without_streak_stays_warn(tmp_path: Path) -> None:
    """A pre-amendment record evidences ONE failure → transient, not an outage.

    The upgrade must not re-read old state as an instant outage; the streak
    rebuilds from live traffic.
    """
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(72),
        last_agent_failure={"ts": _iso(1), "kind": "quota_limited", "summary_tail": INCIDENT_MESSAGE},
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.WARN
    assert r.data["consecutive"] == 1


def test_corrupt_streak_degrades_downward(tmp_path: Path) -> None:
    """A corrupt count must not manufacture an outage (nor crash the sweep)."""
    for bad in ("many", None, [9], -4):
        sp = tmp_path / "curator_state.json"
        _write_state(
            sp,
            last_run=_iso(72),
            last_agent_failure={
                "ts": _iso(1), "kind": "quota_limited",
                "summary_tail": INCIDENT_MESSAGE, "consecutive": bad,
            },
        )
        r = _check_agent_failure_kind(_raw(sp))
        assert r.status is Status.WARN, f"corrupt streak {bad!r} escalated"


def test_since_reports_streak_start_not_latest_failure(tmp_path: Path) -> None:
    """"failing since X" must mean since. The bug this replaces rendered the
    NEWEST failure's timestamp, so a three-day outage read as minutes old."""
    sp = tmp_path / "curator_state.json"
    streak_start = _iso(72 * 60)  # 72 hours ago
    _write_state(
        sp,
        last_run=_iso(96 * 60),
        last_agent_failure={
            "ts": _iso(1), "kind": "quota_limited", "summary_tail": INCIDENT_MESSAGE,
            "consecutive": SUSTAINED_FAILURE_STREAK, "since": streak_start,
        },
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert f"since {streak_start}" in r.detail
    assert r.data["since"] == streak_start


def test_auth_failure_still_fails_at_streak_one(tmp_path: Path) -> None:
    """The streak ADDS an escalation path; it must not gate the existing one.

    ``auth`` means the pipeline is down on the first failure — waiting for a
    third would delay a FAIL the probe already knew about in 2026-07.
    """
    sp = tmp_path / "curator_state.json"
    _write_state(
        sp,
        last_run=_iso(72),
        last_agent_failure={
            "ts": _iso(1), "kind": "auth",
            "summary_tail": "Exit code 1: not logged in", "consecutive": 1,
        },
    )
    r = _check_agent_failure_kind(_raw(sp))
    assert r.status is Status.FAIL
    assert "DOWN" in r.detail


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
