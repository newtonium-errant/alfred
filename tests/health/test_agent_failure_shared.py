"""The SHARED half of the agent-failure signal (2026-08-22).

On 2026-08-22 the box's ``claude -p`` weekly quota was exhausted and the BIT
reported ``curator fail``, ``janitor ok``, ``distiller ok`` — while janitor had
logged 1,029 quota failures and distiller 246 (measured on the box 2026-08-22
~12:00 UTC; point-in-time operator state, not checkable from this repo). All three backends classified
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


# ---------------------------------------------------------------------------
# KNOWN DEFECT — curator only, pre-existing, NOT fixed by this arc
# ---------------------------------------------------------------------------


def test_curator_last_run_can_be_bumped_without_an_agent_success(
    tmp_path: Path,
) -> None:
    """PINS A BUG, deliberately, so it cannot be lost. Read before "fixing".

    Curator's recovery comparison uses ``state.last_run``, which is honest on
    the two ``mark_processed`` call sites that matter — the success path, and
    the legacy failure path that passes ``bump_last_run=False`` precisely to
    protect this comparison. There is a THIRD call site:
    ``curator/daemon.py``'s preference-filter branch, ``backend_used=
    "preference_filter_inbox"``, which retires an inbox file that matched a
    user filter. No agent call happens on that path, and it takes the default
    ``bump_last_run=True``.

    So on a quota-limited box, one preference-filtered email arriving during a
    live outage moves ``last_run`` past the recorded failure. Measured, not
    reasoned — this test IS the measurement: a sustained FAIL becomes OK, a
    false ``curator.agent_failure_recovered`` is logged, and the streak resets
    to 1 so the escalation has to climb from scratch.

    NOT fixed here because the correct fix is the one janitor and distiller
    already have — a dedicated ``last_agent_success``, split from ``last_run``
    because the liveness probe (``last-successful-process``) legitimately wants
    the opposite answer: a preference-filtered file IS evidence the daemon is
    alive. Splitting the field changes curator's pinned probe contract and
    deserves its own gate rather than riding this one.

    **If you are reading this because you made it fail: good — delete it.**
    That means curator grew ``last_agent_success`` and the hole is closed.
    """
    from alfred.curator.state import StateManager

    sp = tmp_path / "curator_state.json"
    mgr = StateManager(sp)
    raw = {"vault": {"path": str(tmp_path)}, "curator": {"state": {"path": str(sp)}}}

    for _ in range(SUSTAINED_FAILURE_STREAK):
        mgr.state.record_agent_failure(kind=QUOTA_LIMITED, summary=TAIL)
    mgr.save()
    assert curator_probe(raw).status is Status.FAIL, (
        "precondition: a sustained outage must escalate, or this pin is vacuous"
    )

    # The preference-filter call site, verbatim. No agent is invoked.
    mgr.state.mark_processed(
        filename="filtered.md",
        inbox_path=str(tmp_path / "filtered.md"),
        files_created=[],
        files_modified=[],
        backend_used="preference_filter_inbox",
    )
    mgr.save()

    assert curator_probe(raw).status is Status.OK, (
        "KNOWN DEFECT (see docstring): the outage is still live; a filtered "
        "email is not an agent success"
    )
    mgr.state.record_agent_failure(kind=QUOTA_LIMITED, summary=TAIL)
    assert mgr.state.last_agent_failure["consecutive"] == 1, (
        "KNOWN DEFECT: the streak restarts, so FAIL must be re-earned"
    )


def test_janitor_and_distiller_have_no_such_laundering_path(tmp_path: Path) -> None:
    """The positive control for the defect pin above, and the reason
    ``last_agent_success`` is a separate field rather than a reused clock.

    Janitor and distiller stamp agent health ONLY from an agent call's own
    outcome, so no amount of ordinary bookkeeping can move it. Here the sweep
    and run clocks are advanced to *now* while the outage stands, and both
    probes still say FAIL.
    """
    from alfred.distiller.state import DistillerState
    from alfred.janitor.state import JanitorState

    js = JanitorState(tmp_path / "janitor_state.json")
    ds = DistillerState(tmp_path / "distiller_state.json")
    for state in (js, ds):
        for _ in range(SUSTAINED_FAILURE_STREAK):
            state.record_agent_failure(kind=QUOTA_LIMITED, summary=TAIL)
    js.last_deep_sweep = _iso(0)
    ds.last_deep_extraction = _iso(0)
    js.save()
    ds.save()

    j_raw = {"vault": {"path": str(tmp_path)},
             "janitor": {"state": {"path": str(tmp_path / "janitor_state.json")}}}
    d_raw = {"vault": {"path": str(tmp_path)},
             "distiller": {"state": {"path": str(tmp_path / "distiller_state.json")}}}
    assert janitor_probe(j_raw).status is Status.FAIL
    assert distiller_probe(d_raw).status is Status.FAIL


def test_the_preference_filter_call_site_still_omits_bump_last_run() -> None:
    """PREMISE PIN for the defect above — added in the gate fix round.

    The laundering pin calls ``mark_processed`` itself with the production
    kwargs. That reproduces the SYMPTOM but cannot see a production-side repair:
    the gate applied the obvious alternative fix (``bump_last_run=False`` at the
    preference-filter call site), and both pins stayed green — the suite would
    have gone on asserting ``Status.OK`` and defending a bug that no longer
    existed, directly contradicting the "delete the pin when it reds"
    instruction it carries.

    So assert the premise the symptom pin rests on, structurally, against the
    source: the ``mark_processed`` call whose ``backend_used`` is
    ``"preference_filter_inbox"`` passes no ``bump_last_run``. Fix production
    and THIS reds first, which is the signal to delete both.

    Bound to the CALL's own keywords via AST rather than to text near it — a
    regex over the source would match a ``bump_last_run`` appearing in the
    comment block that call site already carries.
    """
    import ast
    import inspect

    from alfred.curator import daemon as curator_daemon

    tree = ast.parse(inspect.getsource(curator_daemon))
    targets = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        kwargs = {kw.arg: kw for kw in node.keywords if kw.arg}
        backend = kwargs.get("backend_used")
        if backend is None or not isinstance(backend.value, ast.Constant):
            continue
        if backend.value.value == "preference_filter_inbox":
            targets.append(kwargs)

    assert len(targets) == 1, (
        f"expected exactly one preference-filter mark_processed call site, "
        f"found {len(targets)} — the defect pin's premise is about a specific "
        f"call site and can no longer identify it"
    )
    assert "bump_last_run" not in targets[0], (
        "PRODUCTION WAS FIXED: the preference-filter call site now passes "
        "bump_last_run, so curator no longer launders an active outage. Delete "
        "this pin AND test_curator_last_run_can_be_bumped_without_an_agent_"
        "success — the latter now asserts a bug that is gone."
    )


@pytest.mark.parametrize(
    "stored,expected_next",
    [(5, 6), (1, 2), (0, 2), (-3, 2), ("junk", 2), (None, 2)],
    ids=["five", "one", "zero", "negative", "corrupt", "missing"],
)
def test_extend_base_floor_is_measured_not_assumed(stored: Any, expected_next: int) -> None:
    """PREMISE PIN for ``read_streak``'s floor — added in the gate fix round.

    ``read_streak`` floors at 1, and ``next_failure_record`` extends with
    ``read_streak(prior) + 1``. So a stored ``0`` or negative yields 2, where
    curator's pre-2026-08-22 inline ``max(prior_streak, 0) + 1`` yielded 1.
    Every other input matches the old arithmetic exactly — these six rows are
    the measurement, and ``zero``/``negative`` are the only two that moved.

    Kept deliberately: the divergence escalates one failure EARLIER, which is
    the speak-up direction for an instrument allowed to be wrong, and no writer
    produces a non-positive ``consecutive``. Pinned because the docstring makes
    a claim about it, and an unpinned claim about arithmetic is how the
    docstring came to be wrong in the first place.
    """
    prior = {"ts": _iso(1)}
    if stored is not None:
        prior["consecutive"] = stored
    rec = next_failure_record(
        prior=prior, last_success_ts="", kind=QUOTA_LIMITED, summary=TAIL
    )
    assert rec["consecutive"] == expected_next


def test_extend_base_floor_matches_legacy_arithmetic_on_every_sane_input() -> None:
    """The positive control: for every value a WRITER can actually produce,
    the new arithmetic is identical to curator's old inline form.

    Without this the pin above reads as "the arithmetic changed" when what
    happened is "the arithmetic changed on two impossible inputs".
    """
    for stored in range(1, 12):
        legacy = max(stored, 0) + 1
        rec = next_failure_record(
            prior={"ts": _iso(1), "consecutive": stored},
            last_success_ts="", kind=QUOTA_LIMITED, summary=TAIL,
        )
        assert rec["consecutive"] == legacy, f"diverged at a REACHABLE value: {stored}"
