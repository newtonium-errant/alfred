"""Auto-start gating for the cloudflared daemon.

Mirrors the per-tool autostart pinning in ``test_auto_start.py``:

- Block absent → daemon NOT registered
- ``enabled: false`` → daemon NOT registered
- ``enabled: true`` → daemon REGISTERED + spawned

Two-arg dispatcher branch is already pinned by
``test_dispatcher_two_arg_branch_matches_two_arg_tools``; this file
covers the gate logic specific to the cloudflared block.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

import alfred.orchestrator as orchestrator

from ._fakes import fake_runner_2arg


ALL_FAKES = {
    "curator": "fake_runner_3arg",
    "janitor": "fake_runner_3arg",
    "distiller": "fake_runner_3arg",
    "instructor": "fake_runner_3arg",
    "surveyor": "fake_runner_2arg",
    "mail": "fake_runner_2arg",
    "brief": "fake_runner_2arg",
    "talker": "fake_runner_3arg",
    "cloudflared": "fake_runner_2arg",
}


def _read_started_tools(data_dir: Path) -> set[str]:
    started: set[str] = set()
    for entry in data_dir.iterdir():
        if entry.name.startswith("started_"):
            started.add(entry.name.removeprefix("started_"))
    return started


@pytest.fixture
def install_per_tool_fakes(
    monkeypatch: pytest.MonkeyPatch, orch_dirs: dict[str, Path],
):
    """Re-imports the same pattern from test_auto_start.py."""
    import tests.orchestrator._fakes as fakes_module

    def _install(tools_with_arity: dict[str, int]) -> None:
        for tool, arity in tools_with_arity.items():
            if arity == 2:
                def _runner(raw, suppress_stdout=False, _tool=tool):
                    raw = dict(raw)
                    raw["_fake_runner"] = {
                        **raw.get("_fake_runner", {}),
                        "tool": _tool,
                    }
                    fakes_module._shared_exit(raw)
                monkeypatch.setitem(
                    orchestrator.TOOL_RUNNERS, tool, _runner,
                )
            else:
                def _runner(raw, skills_dir, suppress_stdout=False, _tool=tool):
                    raw = dict(raw)
                    raw["_fake_runner"] = {
                        **raw.get("_fake_runner", {}),
                        "tool": _tool,
                    }
                    fakes_module._shared_exit(raw)
                monkeypatch.setitem(
                    orchestrator.TOOL_RUNNERS, tool, _runner,
                )

    return _install


def _wire_touch_files(
    raw: dict, orch_dirs: dict, tools: list[str],
) -> dict[str, str]:
    tf: dict[str, str] = {}
    for tool in tools:
        tf[tool] = str(orch_dirs["data"] / f"started_{tool}")
    raw.setdefault("_fake_runner", {})["touch_files"] = tf
    return tf


def test_cloudflared_block_absent_skips_start(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """No ``cloudflared:`` block → daemon never starts.

    Matches Hypatia / KAL-LE behavior: they have no tunnel to expose,
    so they leave the block out entirely. Even with curator running,
    cloudflared should NOT spawn.
    """
    raw = orchestrator_raw_config
    raw["curator"] = {}  # ensures we observe SOMETHING started so
                         # absence of cloudflared is not just "no run"
    assert "cloudflared" not in raw

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({
        t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
        for t in ALL_FAKES
    })

    fire_sentinel_after(0.4)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if "curator" in started:
            break
        time.sleep(0.05)

    assert "curator" in started, "curator should have started"
    assert "cloudflared" not in started, (
        f"cloudflared should be skipped when block is absent, got {started}"
    )


def test_cloudflared_enabled_false_skips_start(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """``cloudflared: { enabled: false }`` → daemon NOT registered.

    Distinct from "block absent": the explicit-disable case used by
    instances that copied a template config but want the tunnel off.
    Symmetric to distiller's enabled=false gate.
    """
    raw = orchestrator_raw_config
    raw["curator"] = {}
    raw["cloudflared"] = {"enabled": False, "tunnel_id": "abc"}

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({
        t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
        for t in ALL_FAKES
    })

    fire_sentinel_after(0.4)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if "curator" in started:
            break
        time.sleep(0.05)

    assert "curator" in started
    assert "cloudflared" not in started, (
        f"cloudflared should be skipped via enabled=false, got {started}"
    )


def test_cloudflared_enabled_true_triggers_start(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """``cloudflared: { enabled: true }`` → daemon registered + spawned.

    Live gate. The fake runner short-circuits before actually running
    the binary, but observing the touch file confirms the orchestrator
    routed correctly.
    """
    raw = orchestrator_raw_config
    raw["cloudflared"] = {
        "enabled": True,
        "tunnel_id": "5e44e541-b24c-4caa-8246-105559dd8744",
    }

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({
        t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
        for t in ALL_FAKES
    })

    fire_sentinel_after(0.5)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if "cloudflared" in started:
            break
        time.sleep(0.05)

    assert "cloudflared" in started, (
        f"cloudflared should auto-start when enabled=true, got {started}"
    )


def test_cloudflared_enabled_without_explicit_true_skips_start(
    orchestrator_raw_config, orch_dirs, fast_sleep,
    install_per_tool_fakes, fire_sentinel_after,
) -> None:
    """Block present but missing ``enabled`` key → does NOT start.

    Conservative gate (digest/daily_sync convention, not mail's
    enabled-by-presence convention). The cloudflared block needs an
    explicit ``enabled: true`` because a misconfigured tunnel spins in
    a 5-retry loop on credential errors and we don't want that to fire
    accidentally on template copies.
    """
    raw = orchestrator_raw_config
    raw["curator"] = {}
    raw["cloudflared"] = {"tunnel_id": "abc"}  # no `enabled` key

    _wire_touch_files(raw, orch_dirs, list(ALL_FAKES))
    install_per_tool_fakes({
        t: (3 if t in {"curator", "janitor", "distiller", "instructor", "talker"} else 2)
        for t in ALL_FAKES
    })

    fire_sentinel_after(0.4)
    orchestrator.run_all(
        raw, only=None, skills_dir=orch_dirs["skills"],
        pid_path=orch_dirs["pid_path"], live_mode=False,
    )

    for _ in range(30):
        started = _read_started_tools(orch_dirs["data"])
        if "curator" in started:
            break
        time.sleep(0.05)

    assert "curator" in started
    assert "cloudflared" not in started, (
        f"cloudflared should not start without explicit enabled=true, got {started}"
    )
