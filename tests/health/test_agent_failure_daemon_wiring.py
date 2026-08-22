"""The WIRING — production entry points, not helpers called directly.

Every other pin in this arc exercises a piece: the classifier, the streak
arithmetic, the probe, the state writer. All of those were ALREADY green on
2026-08-22 while janitor and distiller reported ``ok`` through 1,275 failures
between them (box, ~12:00 UTC — point-in-time), because the pieces were never
connected. So these are the pins that
would actually have caught it:

  * ``run_sweep`` — janitor's real sweep, with a failing backend, writing to a
    real state file, read back by the real probe.
  * ``health_check`` — the probe is IN each tool's rollup, and the rollup
    reflects it. A probe nobody registers is a probe nobody reads.

Each red assertion is paired with its passing neighbour: a *succeeding* backend
through the same path must leave the probe green, or the pin would pass just as
well against a build where the whole sweep is broken.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from textwrap import dedent

import pytest

from alfred.curator.health import health_check as curator_health_check
from alfred.distiller.health import health_check as distiller_health_check
from alfred.health.agent_failure import SUSTAINED_FAILURE_STREAK
from alfred.health.types import Status
from alfred.janitor.backends import BackendResult
from alfred.janitor.config import (
    JanitorConfig,
    StateConfig,
    SweepConfig,
    VaultConfig,
)
from alfred.janitor.health import health_check as janitor_health_check
from alfred.janitor.state import JanitorState

QUOTA_STDOUT = "You've hit your weekly limit · resets 4am (UTC)"
QUOTA_SUMMARY = f"Exit code 1: stdout: {QUOTA_STDOUT}"


# ---------------------------------------------------------------------------
# janitor: run_sweep -> state -> probe, through the real daemon path
# ---------------------------------------------------------------------------


class _StubBackend:
    """Stands in for ``janitor.backends.cli.ClaudeBackend``.

    Returns a ``BackendResult`` already carrying the ``kind`` its real
    counterpart's ``classify_agent_failure`` call would set — the classifier
    itself is pinned in ``tests/health/test_agent_failure.py``; what is under
    test here is whether anything downstream KEEPS the answer.
    """

    def __init__(self, result: BackendResult) -> None:
        self._result = result
        self.env_overrides: dict[str, str] = {}
        self.calls = 0

    async def process(self, **_kwargs: object) -> BackendResult:
        self.calls += 1
        return self._result


def _janitor_fixture(tmp_path: Path):
    """A vault with one record carrying an issue the agent path handles."""
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    (vault / "person" / "Broken Link.md").write_text(
        dedent(
            """\
            ---
            type: person
            name: Broken Link
            status: active
            created: 2026-01-01
            tags: []
            ---

            Works with [[person/Does Not Exist]].
            """
        ),
        encoding="utf-8",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    config = JanitorConfig(
        vault=VaultConfig(
            path=str(vault),
            ignore_dirs=[".obsidian", "_templates", "_bases"],
            ignore_files=[".gitkeep"],
        ),
        sweep=SweepConfig(),
        state=StateConfig(path=str(state_dir / "janitor_state.json")),
    )
    state = JanitorState(config.state.path, config.state.max_sweep_history)
    skills_dir = tmp_path / "skills"
    (skills_dir / "vault-janitor").mkdir(parents=True)
    (skills_dir / "vault-janitor" / "SKILL.md").write_text("# t\n", encoding="utf-8")
    return config, state, skills_dir


def _janitor_raw(config: JanitorConfig) -> dict:
    return {
        "vault": {"path": config.vault.path},
        "agent": {"backend": "disabled"},  # skip the network auth probes
        "janitor": {"state": {"path": config.state.path}},
    }


def _run_sweep_with(backend: _StubBackend, config, state, skills_dir) -> None:
    from alfred.janitor import daemon as daemon_mod

    original = daemon_mod._create_backend
    daemon_mod._create_backend = lambda _cfg: backend
    try:
        asyncio.run(
            daemon_mod.run_sweep(
                config, state, skills_dir, structural_only=False, fix_mode=True,
            )
        )
    finally:
        daemon_mod._create_backend = original


def test_failing_agent_in_a_real_sweep_reaches_the_probe(tmp_path: Path) -> None:
    """THE regression pin for janitor's half of the 2026-08-22 outage.

    Drives ``run_sweep`` with a quota-failing backend. Before this arc the
    ``kind`` reached ``log.error("sweep.agent_failed", ...)`` and stopped;
    nothing wrote it, so the probe had nothing to read and the BIT said ok.
    """
    config, state, skills_dir = _janitor_fixture(tmp_path)
    backend = _StubBackend(
        BackendResult(success=False, summary=QUOTA_SUMMARY, kind="quota_limited")
    )
    _run_sweep_with(backend, config, state, skills_dir)

    assert backend.calls >= 1, "the sweep never invoked the agent — pin is vacuous"

    on_disk = json.loads(Path(config.state.path).read_text())
    assert on_disk["last_agent_failure"]["kind"] == "quota_limited"
    assert QUOTA_STDOUT in on_disk["last_agent_failure"]["summary_tail"]

    probe = asyncio.run(janitor_health_check(_janitor_raw(config)))
    agent = next(r for r in probe.results if r.name == "agent-failure-kind")
    assert agent.status is Status.WARN, "one failure is a hiccup, not yet an outage"
    assert "quota-limited" in agent.detail


def test_succeeding_agent_in_a_real_sweep_leaves_the_probe_green(
    tmp_path: Path,
) -> None:
    """The passing neighbour. Identical path, ``success=True``.

    Without it the pin above passes just as well against a build where
    ``run_sweep`` writes a failure unconditionally — or where the sweep is so
    broken that everything looks failed.
    """
    config, state, skills_dir = _janitor_fixture(tmp_path)
    backend = _StubBackend(BackendResult(success=True, summary="fixed 1 link"))
    _run_sweep_with(backend, config, state, skills_dir)

    assert backend.calls >= 1
    on_disk = json.loads(Path(config.state.path).read_text())
    assert on_disk["last_agent_failure"] is None
    assert on_disk["last_agent_success"], "a successful agent call must stamp the clock"

    probe = asyncio.run(janitor_health_check(_janitor_raw(config)))
    agent = next(r for r in probe.results if r.name == "agent-failure-kind")
    assert agent.status is Status.OK


def test_repeated_failing_sweeps_escalate_to_fail(tmp_path: Path) -> None:
    """The streak accumulates ACROSS sweeps, which is what makes it an outage
    signal rather than a per-sweep error flag."""
    config, state, skills_dir = _janitor_fixture(tmp_path)
    backend = _StubBackend(
        BackendResult(success=False, summary=QUOTA_SUMMARY, kind="quota_limited")
    )
    for _ in range(SUSTAINED_FAILURE_STREAK):
        _run_sweep_with(backend, config, state, skills_dir)

    probe = asyncio.run(janitor_health_check(_janitor_raw(config)))
    agent = next(r for r in probe.results if r.name == "agent-failure-kind")
    assert agent.status is Status.FAIL
    assert agent.data["consecutive"] >= SUSTAINED_FAILURE_STREAK
    assert "vault issue fixes are not being applied" in agent.detail
    # ...and the TOOL rollup carries it, which is what reaches the operator.
    assert probe.status is Status.FAIL


def test_a_recovering_sweep_clears_the_escalation(tmp_path: Path) -> None:
    """Recovery downgrades, so the health card goes absent and reconcile can
    retire it. Passing neighbour for the escalation pin above."""
    config, state, skills_dir = _janitor_fixture(tmp_path)
    failing = _StubBackend(
        BackendResult(success=False, summary=QUOTA_SUMMARY, kind="quota_limited")
    )
    for _ in range(SUSTAINED_FAILURE_STREAK):
        _run_sweep_with(failing, config, state, skills_dir)
    _run_sweep_with(
        _StubBackend(BackendResult(success=True, summary="ok")),
        config, state, skills_dir,
    )

    probe = asyncio.run(janitor_health_check(_janitor_raw(config)))
    agent = next(r for r in probe.results if r.name == "agent-failure-kind")
    assert agent.status is Status.OK


# ---------------------------------------------------------------------------
# the probe is REGISTERED in each tool's rollup
# ---------------------------------------------------------------------------


def _sustained_state(path: Path, *, success_key: str) -> None:
    """A state file carrying an active sustained outage, in the shape the
    named tool's probe reads its success timestamp from."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "version": 1,
            success_key: "2026-08-01T00:00:00+00:00",
            "last_agent_failure": {
                "ts": "2026-08-20T00:00:00+00:00",
                "since": "2026-08-18T00:00:00+00:00",
                "kind": "quota_limited",
                "summary_tail": QUOTA_SUMMARY,
                "consecutive": SUSTAINED_FAILURE_STREAK,
            },
        }),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "tool,check,section,filename,success_key",
    [
        ("curator", curator_health_check, "curator", "curator_state.json", "last_run"),
        ("janitor", janitor_health_check, "janitor", "janitor_state.json",
         "last_agent_success"),
        ("distiller", distiller_health_check, "distiller", "distiller_state.json",
         "last_agent_success"),
    ],
)
def test_probe_is_wired_into_the_tool_rollup(
    tmp_path: Path, tool: str, check, section: str, filename: str, success_key: str,
) -> None:
    """A probe that is written but never appended to ``results`` is invisible.

    That is not hypothetical — it is precisely the shape of the 2026-08-22 gap
    one layer up (a classification computed and never consumed), so the pin
    asserts registration AND that the rollup takes the severity.
    """
    sp = tmp_path / tool / filename
    _sustained_state(sp, success_key=success_key)
    raw = {
        "vault": {"path": str(tmp_path)},
        "agent": {"backend": "disabled"},
        section: {"state": {"path": str(sp)}},
    }
    result = asyncio.run(check(raw))
    names = [r.name for r in result.results]
    assert "agent-failure-kind" in names, f"{tool} does not register the probe"
    agent = next(r for r in result.results if r.name == "agent-failure-kind")
    assert agent.status is Status.FAIL
    assert result.status is Status.FAIL, (
        f"{tool}'s rollup did not take the probe's FAIL — the card never reaches "
        f"the needs-you column"
    )


@pytest.mark.parametrize(
    "tool,check,section,filename",
    [
        ("curator", curator_health_check, "curator", "curator_state.json"),
        ("janitor", janitor_health_check, "janitor", "janitor_state.json"),
        ("distiller", distiller_health_check, "distiller", "distiller_state.json"),
    ],
)
def test_clean_state_leaves_the_agent_probe_ok(
    tmp_path: Path, tool: str, check, section: str, filename: str,
) -> None:
    """Passing neighbour for the registration pin: with no recorded failure the
    same probe is OK, so the FAIL above is a response to the fixture rather
    than the probe's only output."""
    sp = tmp_path / tool / filename
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"version": 1}), encoding="utf-8")
    raw = {
        "vault": {"path": str(tmp_path)},
        "agent": {"backend": "disabled"},
        section: {"state": {"path": str(sp)}},
    }
    result = asyncio.run(check(raw))
    agent = next(r for r in result.results if r.name == "agent-failure-kind")
    assert agent.status is Status.OK


@pytest.mark.parametrize(
    "tool,check,section",
    [
        ("curator", curator_health_check, "curator"),
        ("janitor", janitor_health_check, "janitor"),
        ("distiller", distiller_health_check, "distiller"),
    ],
)
def test_unconfigured_tool_still_skips_at_the_tool_level(
    tmp_path: Path, tool: str, check, section: str,
) -> None:
    """Adding a probe must not break the "not configured on this instance"
    gate — KAL-LE runs neither curator nor janitor, and a SKIP there is a
    standing config fact, not news."""
    result = asyncio.run(check({"vault": {"path": str(tmp_path)}}))
    assert result.status is Status.SKIP
    assert result.results == []


# ---------------------------------------------------------------------------
# distiller: run_extraction -> state -> probe, through the real daemon path
# ---------------------------------------------------------------------------
#
# Added in the gate fix round. The consuming half was the ONE unpinned link in
# this arc, and it is the incident's exact shape: removing
# ``state.apply_agent_outcomes(agent_outcomes)`` from ``run_extraction``
# reintroduces classified-then-discarded and scored RED 0 — no test under
# tests/ drove ``run_extraction`` at all. Janitor's equivalent link scored RED 2
# on the same mutation class, so janitor was defended and distiller was not.
#
# Only the AGENT CALL is faked. The candidate scan, the batching, the sink's
# construction and threading, ``apply_agent_outcomes``, and the save are all
# real — the fixture record below scores 0.95 against the default 0.6 threshold,
# so ``scan_candidates`` genuinely qualifies it rather than being stubbed.


def _add_distiller_source(vault: Path, n: int = 0) -> Path:
    """Write one source record that really clears ``candidate_threshold``.

    Measured 0.95 against the default 0.6 — the keyword and section signals are
    load-bearing, not decoration. A FRESH record per run is required rather than
    cosmetic: ``run_extraction`` calls ``state.update_file`` for every source in
    the batch REGARDLESS of whether the pipeline's agent calls succeeded, so a
    quota-failed run still consumes its candidates and the next run over the
    same vault finds none. The ``calls >= 1`` guards below caught exactly that
    when this fixture reused one record.
    """
    path = vault / "session" / f"Quota Outage Review {n}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        dedent(
            f"""\
            ---
            type: session
            name: Quota Outage Review {n}
            status: active
            created: 2026-08-22
            tags: []
            ---

            ## Context
            We are assuming the weekly quota resets on schedule, and the budget
            limit means we cannot simply retry. The deadline is Friday.

            ## Outcome
            We decided to persist the classified failure kind into state. We
            agreed that the janitor and distiller probes should read it. We
            can't rely on `claude auth status` — it stays green while
            quota-limited. Run {n}.
            """
        ),
        encoding="utf-8",
    )
    return path


def _distiller_fixture(tmp_path: Path):
    """A vault with one qualifying record, plus a real config and state."""
    from alfred.distiller.config import load_from_unified
    from alfred.distiller.state import DistillerState

    vault = tmp_path / "vault"
    vault.mkdir()
    _add_distiller_source(vault, 0)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config = load_from_unified({
        "vault": {"path": str(vault)},
        "distiller": {"state": {"path": str(data_dir / "distiller_state.json")}},
    })
    state = DistillerState(config.state.path)
    skills_dir = tmp_path / "skills"
    (skills_dir / "vault-distiller").mkdir(parents=True)
    return config, state, skills_dir


def _distiller_raw(config) -> dict:
    return {
        "vault": {"path": str(config.vault.vault_path)},
        "agent": {"backend": "disabled"},
        "distiller": {"state": {"path": str(config.state.path)}},
    }


def _run_extraction_with_agent(outcome_kind: str | None, config, state, skills_dir):
    """Drive the real ``run_extraction``, faking ONLY the agent call.

    The fake writes into the very sink ``run_extraction`` constructed and handed
    down, which is what makes this a test of the threading rather than of the
    sink class. Returns the number of times the pipeline was invoked, so a
    fixture that silently stops qualifying cannot pass this vacuously.
    """
    from alfred.distiller import daemon as daemon_mod
    from alfred.distiller.pipeline import PipelineResult

    calls = {"n": 0}

    async def _fake_run_pipeline(*, batch, config, session_path, outcomes):
        calls["n"] += 1
        if outcome_kind is None:
            outcomes.record_success()
        else:
            outcomes.record_failure(outcome_kind, QUOTA_SUMMARY)
        return PipelineResult(success=True, candidates_processed=1)

    original = daemon_mod.run_pipeline
    daemon_mod.run_pipeline = _fake_run_pipeline
    try:
        asyncio.run(daemon_mod.run_extraction(config, state, skills_dir))
    finally:
        daemon_mod.run_pipeline = original
    return calls["n"]


def test_failing_agent_in_a_real_extraction_reaches_the_probe(tmp_path: Path) -> None:
    """THE regression pin for distiller's half of the 2026-08-22 outage.

    Before this arc the ``kind`` reached ``pipeline.llm_failed`` and stopped.
    This drives the whole surviving path: sink constructed in
    ``run_extraction``, threaded into the pipeline, applied to state, saved,
    read back by the real probe.
    """
    config, state, skills_dir = _distiller_fixture(tmp_path)
    calls = _run_extraction_with_agent("quota_limited", config, state, skills_dir)
    assert calls >= 1, (
        "the extraction never invoked the pipeline — the fixture record stopped "
        "qualifying, so this pin would pass vacuously"
    )

    on_disk = json.loads(Path(config.state.path).read_text())
    assert on_disk["last_agent_failure"]["kind"] == "quota_limited"
    assert QUOTA_STDOUT in on_disk["last_agent_failure"]["summary_tail"]

    probe = asyncio.run(distiller_health_check(_distiller_raw(config)))
    agent = next(r for r in probe.results if r.name == "agent-failure-kind")
    assert agent.status is Status.WARN
    assert "quota-limited" in agent.detail


def test_succeeding_agent_in_a_real_extraction_leaves_the_probe_green(
    tmp_path: Path,
) -> None:
    """The passing neighbour. Identical path, the agent succeeds.

    Without it the pin above passes equally against a build that records a
    failure unconditionally, or one where extraction is broken end to end.
    """
    config, state, skills_dir = _distiller_fixture(tmp_path)
    calls = _run_extraction_with_agent(None, config, state, skills_dir)
    assert calls >= 1

    on_disk = json.loads(Path(config.state.path).read_text())
    assert on_disk["last_agent_failure"] is None
    assert on_disk["last_agent_success"], (
        "a successful agent call must stamp the clock the recovery predicate reads"
    )

    probe = asyncio.run(distiller_health_check(_distiller_raw(config)))
    agent = next(r for r in probe.results if r.name == "agent-failure-kind")
    assert agent.status is Status.OK


def test_repeated_failing_extractions_escalate_to_fail(tmp_path: Path) -> None:
    """The streak accumulates ACROSS runs, so a multi-day outage escalates."""
    config, state, skills_dir = _distiller_fixture(tmp_path)
    for run in range(SUSTAINED_FAILURE_STREAK):
        # A fresh source per run — see ``_add_distiller_source``.
        _add_distiller_source(Path(config.vault.vault_path), run + 1)
        assert _run_extraction_with_agent(
            "quota_limited", config, state, skills_dir
        ) >= 1

    probe = asyncio.run(distiller_health_check(_distiller_raw(config)))
    agent = next(r for r in probe.results if r.name == "agent-failure-kind")
    assert agent.status is Status.FAIL
    assert agent.data["consecutive"] >= SUSTAINED_FAILURE_STREAK
    assert "knowledge extraction is stopped" in agent.detail
    assert probe.status is Status.FAIL


def test_extraction_with_no_candidates_leaves_agent_health_untouched(
    tmp_path: Path,
) -> None:
    """The early-return path: no candidates means no agent calls, which is
    neither evidence of health nor of failure. A live outage must survive it.

    This is the branch that returns before the sink is even constructed, so it
    also proves the ILB "no agent calls" signal is reachable rather than
    theoretical.
    """
    config, state, skills_dir = _distiller_fixture(tmp_path)
    vault = Path(config.vault.vault_path)
    for run in range(SUSTAINED_FAILURE_STREAK):
        _add_distiller_source(vault, run + 1)
        _run_extraction_with_agent("quota_limited", config, state, skills_dir)
    assert asyncio.run(distiller_health_check(_distiller_raw(config))).status is Status.FAIL

    # Add nothing new: every existing source is already marked distilled, so
    # the next run legitimately finds no candidates and returns early.
    calls = _run_extraction_with_agent("quota_limited", config, state, skills_dir)
    assert calls == 0, "fixture no longer exercises the no-candidates branch"

    probe = asyncio.run(distiller_health_check(_distiller_raw(config)))
    agent = next(r for r in probe.results if r.name == "agent-failure-kind")
    assert agent.status is Status.FAIL, "a quiet run must not green a live outage"
