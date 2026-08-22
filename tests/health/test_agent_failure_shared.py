"""The SHARED half of the agent-failure signal (2026-08-22).

On 2026-08-22 the box's ``claude -p`` weekly quota was exhausted and the BIT
reported ``curator fail``, ``janitor ok``, ``distiller ok`` — while janitor had
logged 1,029 quota failures and distiller 246. All three backends classified
every one of those failures identically (the PRODUCING half was already
shared); only curator kept the answer. This module pins the CONSUMING half now
that all three have it: the streak arithmetic, the severity mapping, and — the
pin that would have caught the original gap — that the three tools agree.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from alfred.curator.health import (
    CURATOR_AGENT_CONSEQUENCE,
    _check_agent_failure_kind as curator_probe,
)
from alfred.distiller.health import (
    DISTILLER_AGENT_CONSEQUENCE,
    _check_agent_failure_kind as distiller_probe,
)
from alfred.health.agent_failure import (
    AUTH,
    OTHER,
    QUOTA_LIMITED,
    SUSTAINED_FAILURE_STREAK,
    AgentCallOutcomes,
    agent_failure_check,
    is_sustained,
    next_failure_record,
    read_streak,
)
from alfred.health.types import Status
from alfred.janitor.health import (
    JANITOR_AGENT_CONSEQUENCE,
    _check_agent_failure_kind as janitor_probe,
)

CONSEQUENCE = "the widget line has stopped."
TAIL = "Exit code 1: stdout: You've hit your weekly limit · resets 4am (UTC)"


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


def _failure(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ts": _iso(1),
        "kind": QUOTA_LIMITED,
        "summary_tail": TAIL,
        "consecutive": 1,
        "since": _iso(1),
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# read_streak / is_sustained — the schema-tolerant reader
# ---------------------------------------------------------------------------


def test_read_streak_none_is_zero() -> None:
    """``None`` means "never failed", which is NOT the same as "failed once".

    Positive control below: a real record reads >= 1, so this zero is a
    distinction the reader draws rather than a broken reader returning 0.
    """
    assert read_streak(None) == 0
    assert read_streak(_failure(consecutive=1)) == 1


def test_read_streak_reads_consecutive() -> None:
    assert read_streak(_failure(consecutive=7)) == 7


def test_read_streak_legacy_record_without_key_reads_one() -> None:
    """A pre-streak record evidences exactly ONE failure.

    Reading it as 0 would silently un-escalate an outage that was already
    running when the streak amendment deployed.
    """
    legacy = {"ts": _iso(1), "kind": QUOTA_LIMITED, "summary_tail": TAIL}
    assert read_streak(legacy) == 1


@pytest.mark.parametrize("bad", ["three", None, [], {}, 3.5j])
def test_read_streak_corrupt_degrades_to_one(bad: Any) -> None:
    assert read_streak(_failure(consecutive=bad)) == 1


@pytest.mark.parametrize("bad", [0, -1, -99])
def test_read_streak_nonpositive_degrades_to_one(bad: int) -> None:
    """Degrading DOWNWARD can only delay an escalation, never manufacture one."""
    assert read_streak(_failure(consecutive=bad)) == 1


def test_is_sustained_boundary() -> None:
    assert is_sustained(SUSTAINED_FAILURE_STREAK) is True
    assert is_sustained(SUSTAINED_FAILURE_STREAK - 1) is False
    assert is_sustained(SUSTAINED_FAILURE_STREAK + 10) is True


# ---------------------------------------------------------------------------
# next_failure_record — reset vs extend
# ---------------------------------------------------------------------------


def test_first_failure_starts_streak_at_one() -> None:
    rec = next_failure_record(
        prior=None, last_success_ts="", kind=QUOTA_LIMITED, summary=TAIL
    )
    assert rec["consecutive"] == 1
    assert rec["kind"] == QUOTA_LIMITED
    assert rec["since"] == rec["ts"]
    assert TAIL[-40:] in rec["summary_tail"]


def test_second_failure_with_no_intervening_success_extends() -> None:
    first = next_failure_record(
        prior=None, last_success_ts="", kind=QUOTA_LIMITED, summary=TAIL
    )
    second = next_failure_record(
        prior=first, last_success_ts="", kind=QUOTA_LIMITED, summary=TAIL
    )
    assert second["consecutive"] == 2
    # ``since`` is carried forward — "failing since" has to mean since.
    assert second["since"] == first["since"]


def test_intervening_success_resets_the_streak() -> None:
    """The positive control for the extend case above: same call, one field
    different (a success AFTER the prior failure), opposite outcome."""
    prior = _failure(ts=_iso(5), since=_iso(9), consecutive=6)
    extended = next_failure_record(
        prior=prior, last_success_ts=_iso(6), kind=QUOTA_LIMITED, summary=TAIL
    )
    assert extended["consecutive"] == 7, "success PREDATES the failure — extends"

    reset = next_failure_record(
        prior=prior, last_success_ts=_iso(2), kind=QUOTA_LIMITED, summary=TAIL
    )
    assert reset["consecutive"] == 1, "success POSTDATES the failure — resets"
    assert reset["since"] == reset["ts"]


def test_unparseable_timestamps_extend_rather_than_reset() -> None:
    """Cannot PROVE recovery → keep the streak running. Swallowing a live
    outage costs multi-day silence; carrying a stale one costs a line."""
    prior = _failure(ts="not-a-timestamp", consecutive=4)
    rec = next_failure_record(
        prior=prior, last_success_ts=_iso(0.5), kind=QUOTA_LIMITED, summary=TAIL
    )
    assert rec["consecutive"] == 5


def test_legacy_prior_without_since_falls_back_to_its_ts() -> None:
    prior = {"ts": _iso(30), "kind": QUOTA_LIMITED, "summary_tail": TAIL}
    rec = next_failure_record(
        prior=prior, last_success_ts="", kind=QUOTA_LIMITED, summary=TAIL
    )
    assert rec["consecutive"] == 2
    assert rec["since"] == prior["ts"]


def test_empty_kind_defaults_to_other_and_summary_is_bounded() -> None:
    rec = next_failure_record(
        prior=None, last_success_ts="", kind="", summary="x" * 5000
    )
    assert rec["kind"] == OTHER
    assert len(rec["summary_tail"]) == 300


def test_now_ts_is_injectable_for_determinism() -> None:
    rec = next_failure_record(
        prior=None,
        last_success_ts="",
        kind=AUTH,
        summary="nope",
        now_ts="2026-08-22T12:00:00+00:00",
    )
    assert rec["ts"] == "2026-08-22T12:00:00+00:00"
    assert rec["since"] == "2026-08-22T12:00:00+00:00"


# ---------------------------------------------------------------------------
# agent_failure_check — the severity mapping
# ---------------------------------------------------------------------------


def test_no_failure_is_ok_and_says_so() -> None:
    """ILB: healthy is never silent absence."""
    r = agent_failure_check(
        failure=None, last_success_ts=_iso(1),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert r.status is Status.OK
    assert "no recent agent failures" in r.detail
    assert r.name == "agent-failure-kind"


@pytest.mark.parametrize("malformed", ["", 0, [], "a string", 42])
def test_malformed_failure_value_is_ok(malformed: Any) -> None:
    r = agent_failure_check(
        failure=malformed, last_success_ts="",
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert r.status is Status.OK


def test_recovered_failure_is_ok() -> None:
    r = agent_failure_check(
        failure=_failure(ts=_iso(9)), last_success_ts=_iso(2),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert r.status is Status.OK
    assert "predates last success" in r.detail


def test_active_failure_is_the_positive_control_for_recovery() -> None:
    """Same failure record, success moved to BEFORE it → WARN, not OK.

    Without this neighbour the recovery pin above passes identically against a
    probe that returns OK unconditionally.
    """
    r = agent_failure_check(
        failure=_failure(ts=_iso(1)), last_success_ts=_iso(9),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert r.status is Status.WARN
    assert "quota-limited" in r.detail


def test_no_success_at_all_is_active_not_recovered() -> None:
    r = agent_failure_check(
        failure=_failure(), last_success_ts="",
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert r.status is Status.WARN


def test_auth_fails_at_streak_one() -> None:
    """``auth`` means DOWN on the first failure — the streak ADDS an escalation
    path, it must not gate the one that already existed."""
    r = agent_failure_check(
        failure=_failure(kind=AUTH, consecutive=1, summary_tail="not logged in"),
        last_success_ts=_iso(9),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert r.status is Status.FAIL
    assert CONSEQUENCE in r.detail
    assert "not logged in" in r.detail


def test_streak_below_threshold_warns_at_threshold_fails() -> None:
    """The escalation boundary, both sides, in one test."""
    below = agent_failure_check(
        failure=_failure(consecutive=SUSTAINED_FAILURE_STREAK - 1),
        last_success_ts=_iso(9),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    at = agent_failure_check(
        failure=_failure(consecutive=SUSTAINED_FAILURE_STREAK),
        last_success_ts=_iso(9),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert below.status is Status.WARN
    assert below.data["sustained"] is False
    assert at.status is Status.FAIL
    assert at.data["sustained"] is True


def test_sustained_card_names_class_streak_consequence_and_tail() -> None:
    """The operator-facing copy IS the deliverable of the escalation."""
    r = agent_failure_check(
        failure=_failure(consecutive=4, since=_iso(50)),
        last_success_ts=_iso(99),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert r.status is Status.FAIL
    assert "quota-limited" in r.detail            # the failure CLASS
    assert "4 consecutive agent failures" in r.detail
    assert CONSEQUENCE in r.detail                # what it COSTS
    assert "weekly limit" in r.detail             # the reset date rides the tail


def test_sustained_unclassified_kind_also_escalates() -> None:
    """Escalation is STRUCTURAL — it keys on the streak, not the error text."""
    r = agent_failure_check(
        failure=_failure(kind=OTHER, consecutive=SUSTAINED_FAILURE_STREAK,
                         summary_tail="connection reset"),
        last_success_ts=_iso(9),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert r.status is Status.FAIL
    assert "failing (other)" in r.detail
    assert CONSEQUENCE in r.detail


def test_sustained_streak_recovered_is_ok() -> None:
    """Recovery DOWNGRADES even a long streak, so the card goes absent and
    reconcile can retire it. Positive control: the same record with the success
    moved earlier is a FAIL (``test_streak_below_threshold_...`` at-case)."""
    r = agent_failure_check(
        failure=_failure(ts=_iso(9), consecutive=SUSTAINED_FAILURE_STREAK + 5),
        last_success_ts=_iso(1),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert r.status is Status.OK


def test_since_reports_streak_start_not_latest_failure() -> None:
    start = _iso(72)
    r = agent_failure_check(
        failure=_failure(ts=_iso(1), since=start,
                         consecutive=SUSTAINED_FAILURE_STREAK),
        last_success_ts=_iso(99),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert start in r.detail
    assert r.data["since"] == start


def test_unclassified_single_failure_warns_without_consequence() -> None:
    """A single unrecognised failure is a hiccup, not an outage — it surfaces
    the CLI tail and deliberately does NOT claim the tool has stopped."""
    r = agent_failure_check(
        failure=_failure(kind=OTHER, consecutive=1, summary_tail="connection reset"),
        last_success_ts=_iso(9),
        consequence=CONSEQUENCE, state_path="/tmp/x.json",
    )
    assert r.status is Status.WARN
    assert "connection reset" in r.detail
    assert CONSEQUENCE not in r.detail


def test_state_path_rides_in_data_on_every_branch() -> None:
    for failure, success in (
        (None, ""),
        (_failure(ts=_iso(9)), _iso(1)),
        (_failure(), _iso(9)),
        (_failure(kind=AUTH), _iso(9)),
        (_failure(consecutive=9), _iso(9)),
    ):
        r = agent_failure_check(
            failure=failure, last_success_ts=success,
            consequence=CONSEQUENCE, state_path="/tmp/probe.json",
        )
        assert r.data["state_path"] == "/tmp/probe.json"


# ---------------------------------------------------------------------------
# AgentCallOutcomes — the sink
# ---------------------------------------------------------------------------


def test_outcomes_records_in_order_with_tallies() -> None:
    o = AgentCallOutcomes()
    assert o.events == [] and o.failures == 0 and o.successes == 0
    o.record_failure(QUOTA_LIMITED, "boom")
    o.record_success()
    o.record_failure("", "")
    assert o.events == [
        (False, QUOTA_LIMITED, "boom"),
        (True, "", ""),
        (False, OTHER, ""),
    ]
    assert o.failures == 2
    assert o.successes == 1


def test_outcomes_instances_do_not_share_a_list() -> None:
    """A mutable default on a dataclass field is the classic version of this
    bug; ``field(default_factory=list)`` is what prevents it."""
    a, b = AgentCallOutcomes(), AgentCallOutcomes()
    a.record_failure(AUTH, "x")
    assert b.events == []


# ---------------------------------------------------------------------------
# CROSS-TOOL PARITY — the pin that would have caught the 2026-08-22 gap
# ---------------------------------------------------------------------------


def _write_tool_state(state_path: Path, failure: dict | None, success: str) -> None:
    doc: dict[str, Any] = {"version": 1}
    if failure is not None:
        doc["last_agent_failure"] = failure
    doc["last_agent_success"] = success
    # Curator reads its success from ``last_run``, the others from
    # ``last_agent_success``; writing BOTH lets one fixture drive all three.
    doc["last_run"] = success
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(doc), encoding="utf-8")


#: (tool, probe callable, config-section key, state filename, consequence)
_TOOLS = [
    ("curator", curator_probe, "curator", "curator_state.json",
     CURATOR_AGENT_CONSEQUENCE),
    ("janitor", janitor_probe, "janitor", "janitor_state.json",
     JANITOR_AGENT_CONSEQUENCE),
    ("distiller", distiller_probe, "distiller", "distiller_state.json",
     DISTILLER_AGENT_CONSEQUENCE),
]


@pytest.mark.parametrize(
    "failure,success,expected",
    [
        (None, "", Status.OK),
        (_failure(ts=_iso(9)), _iso(1), Status.OK),
        (_failure(ts=_iso(1), consecutive=1), _iso(9), Status.WARN),
        (_failure(ts=_iso(1), kind=AUTH, consecutive=1), _iso(9), Status.FAIL),
        (_failure(ts=_iso(1), consecutive=SUSTAINED_FAILURE_STREAK), _iso(9),
         Status.FAIL),
    ],
    ids=["clean", "recovered", "single-quota", "auth", "sustained"],
)
def test_all_three_tools_agree_on_severity(
    tmp_path: Path, failure: dict | None, success: str, expected: Status,
) -> None:
    """The SAME recorded failure produces the SAME status in all three tools.

    This is the pin the 2026-08-22 outage needed and did not have. Every tool
    classified the failure identically (shared producer) and two of them threw
    the answer away, so ``janitor ok`` and ``distiller ok`` were not a
    disagreement about severity — they were an ABSENCE of the consumer. A pin
    that only exercised curator could not tell those apart; this one reds if
    any tool loses its probe, its state field, or its severity mapping.
    """
    seen: dict[str, Status] = {}
    for tool, probe, section, filename, _consequence in _TOOLS:
        sp = tmp_path / tool / filename
        _write_tool_state(sp, failure, success)
        raw = {"vault": {"path": str(tmp_path)}, section: {"state": {"path": str(sp)}}}
        seen[tool] = probe(raw).status
    assert seen == {t: expected for t, _, _, _, _ in _TOOLS}


@pytest.mark.parametrize("tool,probe,section,filename,consequence", _TOOLS)
def test_each_tool_names_its_own_consequence_on_a_sustained_outage(
    tmp_path: Path, tool: str, probe: Any, section: str, filename: str,
    consequence: str,
) -> None:
    """Severity is shared; the CONSEQUENCE is not, and must not be.

    "email intake is stopped and new mail is being quarantined" on a distiller
    card would send the operator to the wrong place. Membership in the shared
    family proves wiring, not that the card says something true about THIS tool.
    """
    sp = tmp_path / tool / filename
    _write_tool_state(
        sp,
        _failure(ts=_iso(1), consecutive=SUSTAINED_FAILURE_STREAK, since=_iso(30)),
        _iso(99),
    )
    raw = {"vault": {"path": str(tmp_path)}, section: {"state": {"path": str(sp)}}}
    r = probe(raw)
    assert r.status is Status.FAIL
    assert consequence in r.detail
    # ...and does NOT carry a sibling's consequence.
    for other, _, _, _, other_consequence in _TOOLS:
        if other != tool:
            assert other_consequence not in r.detail


def test_the_three_consequences_are_distinct() -> None:
    """A copy-paste that left two tools sharing a sentence would satisfy every
    pin above except this one."""
    consequences = [c for _, _, _, _, c in _TOOLS]
    assert len(set(consequences)) == len(_TOOLS)


@pytest.mark.parametrize("tool,probe,section,filename,consequence", _TOOLS)
def test_corrupt_state_file_does_not_crash_the_bit_run(
    tmp_path: Path, tool: str, probe: Any, section: str, filename: str,
    consequence: str,
) -> None:
    """A corrupt state file degrades to OK rather than raising mid-sweep.

    DELIBERATE and inherited from curator's ``test_corrupt_state_file_is_ok``
    (2026-07-29), not an oversight of the "cannot determine → don't say ok"
    direction: the alternative is a probe that can take down the whole BIT run
    on a truncated write. It is uniform across all three tools as of
    2026-08-22, so no two of them can disagree about it. If the direction is
    ever revisited, it is one change in ``agent_failure_check``, not three.
    """
    sp = tmp_path / tool / filename
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("{ not json", encoding="utf-8")
    raw = {"vault": {"path": str(tmp_path)}, section: {"state": {"path": str(sp)}}}
    r = probe(raw)
    assert r.status is Status.OK
