"""Janitor's agent-failure state + daemon wiring (2026-08-22).

The gap this closes: on 2026-08-22 janitor's backend classified 1,029
quota failures (box, ~12:00 UTC — point-in-time), ``daemon.run_sweep`` logged
every ``kind``, and nothing
persisted any of them — so the BIT read ``janitor ok`` for the whole outage.
These pins DRIVE the writer (fabricate the failure, assert it lands and the
probe reds) and pair each with its passing neighbour.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from alfred.health.agent_failure import SUSTAINED_FAILURE_STREAK
from alfred.health.types import Status
from alfred.janitor.health import _check_agent_failure_kind
from alfred.janitor.state import JanitorState

QUOTA_TAIL = "Exit code 1: stdout: You've hit your weekly limit · resets 4am (UTC)"


def _state(tmp_path: Path) -> JanitorState:
    return JanitorState(tmp_path / "janitor_state.json")


def _raw(tmp_path: Path) -> dict:
    return {
        "vault": {"path": str(tmp_path)},
        "janitor": {"state": {"path": str(tmp_path / "janitor_state.json")}},
    }


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------


def test_fresh_state_has_both_agent_fields(tmp_path: Path) -> None:
    """Always-present fields carrying empty values, per ILB's data-shape half:
    a conditionally-present field's absence cannot be told apart from a
    producer that never ran."""
    s = _state(tmp_path)
    assert s.last_agent_failure is None
    assert s.last_agent_success == ""


def test_record_agent_failure_shape(tmp_path: Path) -> None:
    s = _state(tmp_path)
    s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    assert s.last_agent_failure["kind"] == "quota_limited"
    assert "weekly limit" in s.last_agent_failure["summary_tail"]
    assert s.last_agent_failure["consecutive"] == 1
    assert s.last_agent_failure["ts"]


def test_failure_does_not_touch_last_agent_success(tmp_path: Path) -> None:
    """The recovery comparison is only honest if a failure cannot bump the
    success side — that is the whole mechanism."""
    s = _state(tmp_path)
    s.record_agent_success()
    stamped = s.last_agent_success
    s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    assert s.last_agent_success == stamped


def test_consecutive_failures_extend_the_streak(tmp_path: Path) -> None:
    s = _state(tmp_path)
    for _ in range(4):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    assert s.last_agent_failure["consecutive"] == 4


def test_a_success_between_failures_resets_the_streak(tmp_path: Path) -> None:
    """Positive control for the extend pin above: identical loop, one
    ``record_agent_success`` in the middle, opposite result."""
    s = _state(tmp_path)
    for _ in range(4):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    s.record_agent_success()
    s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    assert s.last_agent_failure["consecutive"] == 1


def test_since_carries_the_streak_start_forward(tmp_path: Path) -> None:
    s = _state(tmp_path)
    s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    first_since = s.last_agent_failure["since"]
    for _ in range(3):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    assert s.last_agent_failure["since"] == first_since
    assert s.last_agent_failure["ts"] != first_since


def test_sustained_crossing_logs_once(tmp_path: Path) -> None:
    """ILB + once-per-outage: the log carries the MOMENT fixes stopped, not one
    line per failure for the rest of a multi-day outage."""
    s = _state(tmp_path)
    with structlog.testing.capture_logs() as captured:
        for _ in range(SUSTAINED_FAILURE_STREAK + 3):
            s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    events = [c for c in captured if c.get("event") == "janitor.agent_failure_sustained"]
    assert len(events) == 1
    assert events[0]["consecutive"] == SUSTAINED_FAILURE_STREAK
    assert events[0]["threshold"] == SUSTAINED_FAILURE_STREAK
    assert events[0]["kind"] == "quota_limited"
    assert "weekly limit" in events[0]["summary_tail"]


def test_short_streak_never_logs_sustained(tmp_path: Path) -> None:
    """Passing neighbour for the pin above — without it, a writer that logged
    unconditionally would still satisfy ``len(events) == 1`` on the first."""
    s = _state(tmp_path)
    with structlog.testing.capture_logs() as captured:
        for _ in range(SUSTAINED_FAILURE_STREAK - 1):
            s.record_agent_failure(kind="other", summary="blip")
    assert [c for c in captured if c.get("event") == "janitor.agent_failure_sustained"] == []


def test_recovery_logs_once_on_the_success_that_ends_the_outage(tmp_path: Path) -> None:
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    with structlog.testing.capture_logs() as captured:
        s.record_agent_success()
        s.record_agent_success()  # a second ordinary success is NOT news
    events = [c for c in captured if c.get("event") == "janitor.agent_failure_recovered"]
    assert len(events) == 1
    assert events[0]["consecutive"] == SUSTAINED_FAILURE_STREAK


def test_recovery_from_a_short_streak_is_silent(tmp_path: Path) -> None:
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK - 1):
        s.record_agent_failure(kind="other", summary="blip")
    with structlog.testing.capture_logs() as captured:
        s.record_agent_success()
    assert [c for c in captured if c.get("event") == "janitor.agent_failure_recovered"] == []


# ---------------------------------------------------------------------------
# persistence — schema tolerance both directions
# ---------------------------------------------------------------------------


def test_round_trip_through_save_and_load(tmp_path: Path) -> None:
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    s.save()

    on_disk = json.loads((tmp_path / "janitor_state.json").read_text())
    assert on_disk["last_agent_failure"]["consecutive"] == SUSTAINED_FAILURE_STREAK
    assert on_disk["last_agent_success"] == ""

    reloaded = _state(tmp_path)
    reloaded.load()
    assert reloaded.last_agent_failure == s.last_agent_failure
    assert reloaded.last_agent_success == s.last_agent_success


def test_old_schema_file_loads_with_empty_agent_fields(tmp_path: Path) -> None:
    """Forward-compat: a state file written before 2026-08-22 has neither key."""
    sp = tmp_path / "janitor_state.json"
    sp.write_text(json.dumps({"version": 1, "files": {}, "sweeps": {}}), encoding="utf-8")
    s = _state(tmp_path)
    s.load()
    assert s.last_agent_failure is None
    assert s.last_agent_success == ""


def test_malformed_agent_fields_degrade_rather_than_crash(tmp_path: Path) -> None:
    sp = tmp_path / "janitor_state.json"
    sp.write_text(
        json.dumps({
            "version": 1,
            "last_agent_failure": "not-a-dict",
            "last_agent_success": 12345,
        }),
        encoding="utf-8",
    )
    s = _state(tmp_path)
    s.load()
    assert s.last_agent_failure is None
    assert s.last_agent_success == ""


# ---------------------------------------------------------------------------
# writer -> probe, end to end through the real state file
# ---------------------------------------------------------------------------


def test_sustained_outage_written_by_the_state_reds_the_probe(tmp_path: Path) -> None:
    """THE regression pin: fabricate the outage through the PRODUCTION writer,
    read it through the PRODUCTION probe. No hand-written fixture in between,
    so a writer/probe schema drift reds here even though both halves' own unit
    pins stay green."""
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    s.save()

    r = _check_agent_failure_kind(_raw(tmp_path))
    assert r.status is Status.FAIL
    assert "quota-limited" in r.detail
    assert "vault issue fixes are not being applied" in r.detail
    assert "weekly limit" in r.detail


def test_recovery_written_by_the_state_greens_the_probe(tmp_path: Path) -> None:
    """Passing neighbour: same writer, one extra call, and the probe goes OK.

    Without it the pin above passes identically against a probe hard-wired to
    FAIL whenever any failure key exists.
    """
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    s.record_agent_success()
    s.save()

    r = _check_agent_failure_kind(_raw(tmp_path))
    assert r.status is Status.OK


def test_probe_is_ok_on_a_janitor_that_never_invoked_an_agent(tmp_path: Path) -> None:
    s = _state(tmp_path)
    s.save()
    assert _check_agent_failure_kind(_raw(tmp_path)).status is Status.OK


def test_sweep_timestamps_alone_cannot_launder_an_active_outage(tmp_path: Path) -> None:
    """The reason ``last_agent_success`` exists rather than reusing the sweep
    clock: ``add_sweep`` runs at the end of every sweep, agent failure or not.

    Here the state carries a sweep and a deep-sweep NEWER than the failure and
    the probe must still call the outage active. If a future change repoints
    the recovery comparison at a sweep timestamp, this reds.
    """
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    s.last_deep_sweep = _iso(0)  # a sweep completed AFTER the failures
    s.save()

    on_disk = json.loads((tmp_path / "janitor_state.json").read_text())
    on_disk["sweeps"] = {"s1": {"timestamp": _iso(0)}}
    (tmp_path / "janitor_state.json").write_text(json.dumps(on_disk), encoding="utf-8")

    r = _check_agent_failure_kind(_raw(tmp_path))
    assert r.status is Status.FAIL, "a sweep is not an agent success"
