"""Curator state persistence for ``last_agent_failure`` (2026-07-29 incident).

Pins the load-time schema-tolerance contract for the new field: an old-schema
state file (no ``last_agent_failure`` key) loads to ``None`` rather than
crashing, a malformed value degrades to ``None``, and a round-trip through
``to_dict``/``from_dict`` (and ``StateManager`` save/load) preserves the
recorded failure.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from alfred.curator.state import State, StateManager
from alfred.health.agent_failure import SUSTAINED_FAILURE_STREAK


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()


def test_record_agent_failure_shape() -> None:
    s = State()
    s.record_agent_failure(kind="quota_limited", summary="Exit code 1: stdout: hit your weekly limit")
    assert s.last_agent_failure is not None
    assert s.last_agent_failure["kind"] == "quota_limited"
    assert "weekly limit" in s.last_agent_failure["summary_tail"]
    assert isinstance(s.last_agent_failure["ts"], str) and s.last_agent_failure["ts"]


def test_record_agent_failure_empty_kind_defaults_to_other() -> None:
    s = State()
    s.record_agent_failure(kind="", summary="boom")
    assert s.last_agent_failure["kind"] == "other"


def test_record_does_not_touch_last_run() -> None:
    # last_run must stay the LAST SUCCESS so the BIT probe can compare the two.
    s = State()
    s.last_run = "2026-07-25T00:00:00+00:00"
    s.record_agent_failure(kind="quota_limited", summary="x")
    assert s.last_run == "2026-07-25T00:00:00+00:00"


def test_roundtrip_preserves_last_agent_failure() -> None:
    s = State()
    s.record_agent_failure(kind="auth", summary="Exit code 1: not logged in")
    restored = State.from_dict(s.to_dict())
    assert restored.last_agent_failure == s.last_agent_failure


def test_old_schema_file_loads_to_none() -> None:
    """A pre-2026-07-29 state file (no key) must load fine → None (tolerance)."""
    old = {"version": 2, "last_run": "2026-07-01T00:00:00+00:00", "processed": {}}
    restored = State.from_dict(old)
    assert restored.last_agent_failure is None
    assert restored.last_run == "2026-07-01T00:00:00+00:00"


def test_malformed_last_agent_failure_degrades_to_none() -> None:
    for bad in ("a string", 42, ["list"], True):
        restored = State.from_dict({"last_agent_failure": bad})
        assert restored.last_agent_failure is None


# ---------------------------------------------------------------------------
# consecutive-failure STREAK (2026-08-15 sustained-outage escalation)
# ---------------------------------------------------------------------------


def test_first_failure_starts_streak_at_one() -> None:
    s = State()
    s.record_agent_failure(kind="quota_limited", summary="x")
    assert s.last_agent_failure["consecutive"] == 1
    # ``since`` is this failure — there is no earlier one to point at.
    assert s.last_agent_failure["since"] == s.last_agent_failure["ts"]


def test_consecutive_failures_extend_the_streak() -> None:
    s = State()
    for expected in (1, 2, 3, 4):
        s.record_agent_failure(kind="quota_limited", summary="x")
        assert s.last_agent_failure["consecutive"] == expected


def test_streak_carries_since_from_the_first_failure() -> None:
    """``since`` must be the streak's START, not its most recent failure.

    The probe renders "failing since X" from it; before the streak amendment it
    passed the newest ``ts``, so a multi-day outage read as if it had begun
    moments ago.
    """
    s = State()
    s.record_agent_failure(kind="quota_limited", summary="x")
    first_since = s.last_agent_failure["since"]
    for _ in range(3):
        s.record_agent_failure(kind="quota_limited", summary="x")
    assert s.last_agent_failure["since"] == first_since
    # ...and the streak's newest ts has genuinely moved on from it, so the two
    # fields are not trivially equal (this is what makes the pin above real).
    assert s.last_agent_failure["ts"] != first_since


def test_success_between_failures_resets_the_streak() -> None:
    """A success breaks the streak — the next failure is transient again.

    This is the property that makes the counter mean "outage" rather than
    "failures ever seen": without the reset, a tool that fails once a week
    would eventually cross the threshold and claim an outage.
    """
    s = State()
    for _ in range(SUSTAINED_FAILURE_STREAK + 1):
        s.record_agent_failure(kind="quota_limited", summary="x")
    assert s.last_agent_failure["consecutive"] > SUSTAINED_FAILURE_STREAK

    s.mark_processed("a.md", "inbox/a.md", [], [], "claude")
    s.record_agent_failure(kind="quota_limited", summary="x")
    assert s.last_agent_failure["consecutive"] == 1


def test_legacy_failure_record_reads_as_one_and_extends() -> None:
    """A pre-amendment ``last_agent_failure`` has no ``consecutive`` key.

    It evidences exactly ONE failure, so the next failure makes 2 — an outage
    in flight across the deploy keeps counting instead of restarting at 0. And
    ``since`` falls back to the legacy record's own ``ts`` rather than claiming
    the streak began at the moment of the upgrade.
    """
    s = State()
    legacy_ts = _iso(90)
    s.last_agent_failure = {"ts": legacy_ts, "kind": "quota_limited", "summary_tail": "old"}
    s.record_agent_failure(kind="quota_limited", summary="x")
    assert s.last_agent_failure["consecutive"] == 2
    assert s.last_agent_failure["since"] == legacy_ts


def test_corrupt_consecutive_value_degrades_without_raising() -> None:
    """A hand-edited/corrupt count must not crash the daemon's failure path."""
    for bad in ("three", None, [3], {"n": 3}):
        s = State()
        s.last_agent_failure = {"ts": _iso(5), "kind": "other", "consecutive": bad}
        s.record_agent_failure(kind="other", summary="x")
        assert s.last_agent_failure["consecutive"] == 2


# ---------------------------------------------------------------------------
# ILB — the outage transition is logged at BOTH ends
# ---------------------------------------------------------------------------


def test_crossing_the_threshold_logs_once() -> None:
    """The escalation line fires on the CROSSING, not once per failure.

    A per-failure line would put one warning per quarantined email into the log
    for the length of a multi-day outage, which buries the moment intake
    actually stopped — the one fact an operator greps back for.
    """
    s = State()
    with structlog.testing.capture_logs() as captured:
        for _ in range(SUSTAINED_FAILURE_STREAK + 3):
            s.record_agent_failure(kind="quota_limited", summary="hit your weekly limit")
    events = [c for c in captured if c.get("event") == "curator.agent_failure_sustained"]
    assert len(events) == 1
    assert events[0]["consecutive"] == SUSTAINED_FAILURE_STREAK
    assert events[0]["threshold"] == SUSTAINED_FAILURE_STREAK
    assert events[0]["kind"] == "quota_limited"
    assert "weekly limit" in events[0]["summary_tail"]
    assert events[0]["since"]


def test_transient_failures_log_no_escalation() -> None:
    """Below the threshold there is no outage to announce (positive control:
    the same driver DOES emit once one more failure arrives)."""
    s = State()
    with structlog.testing.capture_logs() as captured:
        for _ in range(SUSTAINED_FAILURE_STREAK - 1):
            s.record_agent_failure(kind="quota_limited", summary="x")
    assert [c for c in captured if c.get("event") == "curator.agent_failure_sustained"] == []

    with structlog.testing.capture_logs() as captured:
        s.record_agent_failure(kind="quota_limited", summary="x")
    assert len([c for c in captured if c.get("event") == "curator.agent_failure_sustained"]) == 1


def test_first_success_after_sustained_outage_logs_recovery() -> None:
    s = State()
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary="x")

    with structlog.testing.capture_logs() as captured:
        s.mark_processed("a.md", "inbox/a.md", [], [], "claude")
    events = [c for c in captured if c.get("event") == "curator.agent_failure_recovered"]
    assert len(events) == 1
    assert events[0]["consecutive"] == SUSTAINED_FAILURE_STREAK
    assert events[0]["kind"] == "quota_limited"

    # ...and only the FIRST success says so. Further successes are ordinary.
    with structlog.testing.capture_logs() as captured:
        s.mark_processed("b.md", "inbox/b.md", [], [], "claude")
    assert [c for c in captured if c.get("event") == "curator.agent_failure_recovered"] == []


def test_recovery_from_a_short_streak_is_not_announced() -> None:
    """A hiccup that resolved is not news — only a sustained outage ending is.

    Positive control for the pin above: same call, same driver, and the ONLY
    difference is that the streak never reached the threshold.
    """
    s = State()
    for _ in range(SUSTAINED_FAILURE_STREAK - 1):
        s.record_agent_failure(kind="quota_limited", summary="x")
    with structlog.testing.capture_logs() as captured:
        s.mark_processed("a.md", "inbox/a.md", [], [], "claude")
    assert [c for c in captured if c.get("event") == "curator.agent_failure_recovered"] == []


def test_ordinary_success_with_no_failure_logs_nothing() -> None:
    s = State()
    with structlog.testing.capture_logs() as captured:
        s.mark_processed("a.md", "inbox/a.md", [], [], "claude")
    assert [c for c in captured if c.get("event") == "curator.agent_failure_recovered"] == []


def test_failure_retirement_path_does_not_claim_recovery() -> None:
    """``bump_last_run=False`` is the legacy on_failure escape hatch: the file
    is retired WITHOUT a success. It must not announce a recovery that never
    happened (nor bump ``last_run``, which the probe reads as proof of one)."""
    s = State()
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary="x")
    with structlog.testing.capture_logs() as captured:
        s.mark_processed("a.md", "inbox/a.md", [], [], "claude", bump_last_run=False)
    assert [c for c in captured if c.get("event") == "curator.agent_failure_recovered"] == []
    assert s.last_run == ""


def test_streak_fields_survive_a_disk_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "curator_state.json"
    mgr = StateManager(path)
    mgr.load()
    for _ in range(SUSTAINED_FAILURE_STREAK):
        mgr.state.record_agent_failure(kind="quota_limited", summary="x")
    mgr.save()

    reloaded = StateManager(path).load()
    assert reloaded.last_agent_failure["consecutive"] == SUSTAINED_FAILURE_STREAK
    assert reloaded.last_agent_failure["since"]
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["last_agent_failure"]["consecutive"] == SUSTAINED_FAILURE_STREAK


def test_statemanager_save_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "curator_state.json"
    mgr = StateManager(path)
    mgr.load()
    mgr.state.record_agent_failure(kind="quota_limited", summary="Exit code 1: hit your weekly limit")
    mgr.save()

    # Reload from disk via a fresh manager.
    mgr2 = StateManager(path)
    reloaded = mgr2.load()
    assert reloaded.last_agent_failure is not None
    assert reloaded.last_agent_failure["kind"] == "quota_limited"

    # And the on-disk JSON actually carries the key.
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["last_agent_failure"]["kind"] == "quota_limited"
