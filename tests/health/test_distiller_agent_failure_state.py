"""Distiller's agent-failure state, sink threading, and probe (2026-08-22).

The gap this closes: distiller's ``pipeline._call_llm`` classified 246 quota
failures on 2026-08-22, logged every ``kind`` on ``pipeline.llm_failed``, and
dropped it — the BIT read ``distiller ok`` for the whole outage. Distiller is
the awkward one of the three: its agent call is four frames below the daemon
that owns state, so the fix needs a sink threaded down AND applied back up.
Both halves are driven here, plus the structural pin that keeps the sink
parameter from silently acquiring a default.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import inspect
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import structlog

from alfred.distiller import pipeline as distiller_pipeline
from alfred.distiller.health import _check_agent_failure_kind
from alfred.distiller.state import DistillerState
from alfred.health.agent_failure import (
    SUSTAINED_FAILURE_STREAK,
    AgentCallOutcomes,
)
from alfred.health.types import Status

from .._required_kwarg import assert_required_keyword_only

QUOTA_TAIL = "Exit code 1: stdout: You've hit your weekly limit · resets 4am (UTC)"


def _state(tmp_path: Path) -> DistillerState:
    return DistillerState(tmp_path / "distiller_state.json")


def _raw(tmp_path: Path) -> dict:
    return {
        "vault": {"path": str(tmp_path)},
        "distiller": {"state": {"path": str(tmp_path / "distiller_state.json")}},
    }


def _iso(hours_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).isoformat()


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------


def test_fresh_state_has_both_agent_fields(tmp_path: Path) -> None:
    s = _state(tmp_path)
    assert s.last_agent_failure is None
    assert s.last_agent_success == ""


def test_record_agent_failure_shape_and_streak(tmp_path: Path) -> None:
    s = _state(tmp_path)
    for _ in range(3):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    assert s.last_agent_failure["kind"] == "quota_limited"
    assert s.last_agent_failure["consecutive"] == 3
    assert "weekly limit" in s.last_agent_failure["summary_tail"]


def test_failure_does_not_touch_last_agent_success(tmp_path: Path) -> None:
    s = _state(tmp_path)
    s.record_agent_success()
    stamped = s.last_agent_success
    s.record_agent_failure(kind="auth", summary="not logged in")
    assert s.last_agent_success == stamped


def test_sustained_and_recovery_events(tmp_path: Path) -> None:
    s = _state(tmp_path)
    with structlog.testing.capture_logs() as captured:
        for _ in range(SUSTAINED_FAILURE_STREAK + 2):
            s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
        s.record_agent_success()
    sustained = [c for c in captured if c.get("event") == "distiller.agent_failure_sustained"]
    recovered = [c for c in captured if c.get("event") == "distiller.agent_failure_recovered"]
    assert len(sustained) == 1
    assert sustained[0]["consecutive"] == SUSTAINED_FAILURE_STREAK
    assert len(recovered) == 1
    assert recovered[0]["consecutive"] == SUSTAINED_FAILURE_STREAK + 2


def test_short_streak_emits_neither_event(tmp_path: Path) -> None:
    """Passing neighbour for both assertions above."""
    s = _state(tmp_path)
    with structlog.testing.capture_logs() as captured:
        for _ in range(SUSTAINED_FAILURE_STREAK - 1):
            s.record_agent_failure(kind="other", summary="blip")
        s.record_agent_success()
    names = {c.get("event") for c in captured}
    assert "distiller.agent_failure_sustained" not in names
    assert "distiller.agent_failure_recovered" not in names


# ---------------------------------------------------------------------------
# apply_agent_outcomes — ORDER is the point
# ---------------------------------------------------------------------------


def test_outcomes_apply_in_order_so_a_mid_run_success_breaks_the_streak(
    tmp_path: Path,
) -> None:
    """(fail, fail, success, fail) is a streak of ONE, not three.

    This is why the sink is an ordered list rather than a tally: collapsing a
    run to a single verdict would either erase the success or erase the
    failures, and both readings produce a wrong ``consecutive``.
    """
    s = _state(tmp_path)
    o = AgentCallOutcomes()
    o.record_failure("quota_limited", QUOTA_TAIL)
    o.record_failure("quota_limited", QUOTA_TAIL)
    o.record_success()
    o.record_failure("quota_limited", QUOTA_TAIL)
    s.apply_agent_outcomes(o)
    assert s.last_agent_failure["consecutive"] == 1


def test_all_failing_run_accumulates_the_full_streak(tmp_path: Path) -> None:
    """The positive control for the ordering pin: same four events, no success,
    and the streak reaches four."""
    s = _state(tmp_path)
    o = AgentCallOutcomes()
    for _ in range(4):
        o.record_failure("quota_limited", QUOTA_TAIL)
    s.apply_agent_outcomes(o)
    assert s.last_agent_failure["consecutive"] == 4


def test_empty_outcomes_change_nothing(tmp_path: Path) -> None:
    """A run that made no agent calls is neither evidence of health nor of
    failure — it must not silently green a live outage."""
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    before = dict(s.last_agent_failure)
    before_success = s.last_agent_success
    s.apply_agent_outcomes(AgentCallOutcomes())
    assert s.last_agent_failure == before
    assert s.last_agent_success == before_success


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------


def test_round_trip_through_save_and_load(tmp_path: Path) -> None:
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    s.save()
    on_disk = json.loads((tmp_path / "distiller_state.json").read_text())
    assert on_disk["last_agent_failure"]["consecutive"] == SUSTAINED_FAILURE_STREAK
    reloaded = _state(tmp_path)
    reloaded.load()
    assert reloaded.last_agent_failure == s.last_agent_failure


def test_old_schema_file_loads_with_empty_agent_fields(tmp_path: Path) -> None:
    sp = tmp_path / "distiller_state.json"
    sp.write_text(json.dumps({"version": 1, "files": {}, "runs": {}}), encoding="utf-8")
    s = _state(tmp_path)
    s.load()
    assert s.last_agent_failure is None
    assert s.last_agent_success == ""


def test_malformed_agent_fields_degrade_rather_than_crash(tmp_path: Path) -> None:
    sp = tmp_path / "distiller_state.json"
    sp.write_text(
        json.dumps({"version": 1, "last_agent_failure": [1, 2], "last_agent_success": None}),
        encoding="utf-8",
    )
    s = _state(tmp_path)
    s.load()
    assert s.last_agent_failure is None
    assert s.last_agent_success == ""


# ---------------------------------------------------------------------------
# writer -> probe, end to end
# ---------------------------------------------------------------------------


def test_sustained_outage_written_by_the_state_reds_the_probe(tmp_path: Path) -> None:
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    s.save()
    r = _check_agent_failure_kind(_raw(tmp_path))
    assert r.status is Status.FAIL
    assert "quota-limited" in r.detail
    assert "knowledge extraction is stopped" in r.detail
    assert "weekly limit" in r.detail


def test_recovery_written_by_the_state_greens_the_probe(tmp_path: Path) -> None:
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    s.record_agent_success()
    s.save()
    assert _check_agent_failure_kind(_raw(tmp_path)).status is Status.OK


def test_run_timestamps_alone_cannot_launder_an_active_outage(tmp_path: Path) -> None:
    """The reason ``last_agent_success`` exists rather than reusing the run
    clock: ``add_run`` fires at the end of every extraction regardless of what
    the stage calls did, and ``PipelineResult.success`` is set True
    unconditionally at the end of ``run_pipeline``. A run NEWER than the
    failures must not read as recovery."""
    s = _state(tmp_path)
    for _ in range(SUSTAINED_FAILURE_STREAK):
        s.record_agent_failure(kind="quota_limited", summary=QUOTA_TAIL)
    s.last_deep_extraction = _iso(0)
    s.save()
    on_disk = json.loads((tmp_path / "distiller_state.json").read_text())
    on_disk["runs"] = {"r1": {"run_id": "r1", "timestamp": _iso(0)}}
    (tmp_path / "distiller_state.json").write_text(json.dumps(on_disk), encoding="utf-8")
    assert _check_agent_failure_kind(_raw(tmp_path)).status is Status.FAIL


# ---------------------------------------------------------------------------
# the sink parameter — structural, so it cannot silently acquire a default
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn_name",
    [
        "_call_llm",
        "_stage1_extract",
        "_stage3_create",
        "run_pipeline",
        "run_meta_analysis",
        "run_consolidation",
    ],
)
def test_outcomes_is_required_keyword_only_everywhere(fn_name: str) -> None:
    """builder.md's optional-gate trap, in its exact shape.

    ``outcomes`` is the only route from the agent call to the state file. A
    ``= None`` default would be threaded by every test here and defaulted at
    every production call site — every pin in this module would stay green
    while the probe read an empty field forever. Structural assertion, because
    a bare ``pytest.raises(TypeError)`` can be satisfied incidentally by a
    downstream raise (see ``tests/_required_kwarg.py``).
    """
    assert_required_keyword_only(
        getattr(distiller_pipeline, fn_name), "outcomes"
    )


#: ``outcomes=`` followed by a bare IDENTIFIER — a sink bound outside the call,
#: which is the only kind that can be read back afterwards. Deliberately does
#: NOT match ``outcomes=AgentCallOutcomes()``: a freshly-constructed sink
#: satisfies the required-kwarg pin, collects the run's failures, and is
#: discarded on return, which is the original bug wearing the fix's clothes.
#: Measured: a mutation substituting exactly that scored RED 0 against an
#: earlier ``"outcomes=" in args`` version of this pin.
_THREADED_SINK_RE = re.compile(r"outcomes=\s*([A-Za-z_][A-Za-z0-9_]*)\s*[,)]")


def test_every_production_call_site_threads_the_sink() -> None:
    """The other half of the trap: a required parameter proves the callers pass
    SOMETHING, not that they pass the RUN's sink.

    Reads the daemon and CLI sources and asserts each ``run_pipeline`` /
    ``run_meta_analysis`` / ``run_consolidation`` invocation names ``outcomes=``
    with a variable. Source-level because the alternative — driving four daemon
    entry points end to end — would mock away the very wiring under test.

    KNOWN LIMIT: it proves the argument is a NAME, not that the name is
    subsequently applied to state. ``apply_agent_outcomes`` is pinned
    separately, and janitor's equivalent path is driven end to end in
    ``test_agent_failure_daemon_wiring.py``; distiller's full ``run_extraction``
    is not, because standing it up means a vault, a config and four stage
    prompts, and the mocking needed would remove the wiring under test.
    """
    from alfred.distiller import cli as distiller_cli
    from alfred.distiller import daemon as distiller_daemon

    seen = 0
    for module in (distiller_daemon, distiller_cli):
        src = inspect.getsource(module)
        for fn in ("run_pipeline(", "run_meta_analysis(", "run_consolidation("):
            start = 0
            while (idx := src.find(fn, start)) != -1:
                start = idx + len(fn)
                # The call's argument text, up to its closing paren.
                depth, j = 1, start
                while j < len(src) and depth:
                    depth += (src[j] == "(") - (src[j] == ")")
                    j += 1
                args = src[start:j]
                if "import" in src[max(0, idx - 60):idx]:
                    continue  # an import line, not a call
                match = _THREADED_SINK_RE.search(args)
                assert match, (
                    f"{module.__name__} calls {fn}...) without threading a NAMED "
                    f"sink — the agent-health field would never be written, or "
                    f"would be written into an object nobody reads. "
                    f"Args were: {args!r}"
                )
                seen += 1
    assert seen >= 4, (
        f"the scanner found only {seen} call sites; it is meant to cover 3 in "
        f"the daemon and 1 in the CLI. A parser that finds nothing asserts "
        f"nothing."
    )
