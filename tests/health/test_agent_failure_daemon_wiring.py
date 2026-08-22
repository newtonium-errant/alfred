"""The WIRING — production entry points, not helpers called directly.

Every other pin in this arc exercises a piece: the classifier, the streak
arithmetic, the probe, the state writer. All of those were ALREADY green on
2026-08-22 while janitor and distiller reported ``ok`` through a 1,275-failure
outage, because the pieces were never connected. So these are the pins that
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
