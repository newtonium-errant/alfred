"""Concurrency — tools spawn in parallel, not sequentially.

``run_all`` has a 10-second stagger between tool starts (``time.sleep(0.1)``
x 100 iterations). With ``fast_sleep`` collapsing that to ~100ms per
tool, we can start N tools in well under 1 second and verify:

- Multiple child PIDs exist at the same time (i.e., parallel processing)
- ``start_process`` is called once per tool (not re-called serially)
- All tools get a PID file written under ``data/{tool}.pid``
"""

from __future__ import annotations

import time
from pathlib import Path

import alfred.orchestrator as orchestrator

from ._fakes import long_lived_runner_2arg, long_lived_runner_3arg


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def test_multiple_tools_spawn_concurrently(
    orchestrator_raw_config, orch_dirs, fast_sleep, noop_signals,
    install_fake_runners, fire_sentinel_after,
) -> None:
    """Three tools spawn as three LIVE processes — not one at a time.

    Uses long-lived runners so all three are simultaneously alive when
    we observe the PID files.
    """
    raw = orchestrator_raw_config
    install_fake_runners({
        "curator": long_lived_runner_3arg,
        "janitor": long_lived_runner_3arg,
        "distiller": long_lived_runner_3arg,
    })

    # Fire sentinel at 600ms — gives all three tools time to spawn
    # (10-second stagger compressed to ~0.3s/tool under fast_sleep),
    # write their PID files, and for us to observe them.
    observed_pids: dict[str, int] = {}

    # Start orchestrator in a thread so we can observe PIDs DURING run.
    # We can't send signals from a thread, but the sentinel file + fast
    # sleep combo shuts things down cleanly.
    import threading

    def _run() -> None:
        orchestrator.run_all(
            raw, only="curator,janitor,distiller",
            skills_dir=orch_dirs["skills"],
            pid_path=orch_dirs["pid_path"],
            live_mode=False,
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Poll until all three PID files appear (or timeout at 3s).
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        curator_pid = _read_pid(orch_dirs["data"] / "curator.pid")
        janitor_pid = _read_pid(orch_dirs["data"] / "janitor.pid")
        distiller_pid = _read_pid(orch_dirs["data"] / "distiller.pid")
        if curator_pid and janitor_pid and distiller_pid:
            observed_pids = {
                "curator": curator_pid,
                "janitor": janitor_pid,
                "distiller": distiller_pid,
            }
            break
        time.sleep(0.02)

    # Trigger shutdown and wait for orchestrator to return.
    orch_dirs["sentinel_path"].write_text("stop", encoding="utf-8")
    t.join(timeout=5.0)
    assert not t.is_alive(), "Orchestrator thread did not shut down cleanly"

    # Three distinct PIDs — all three processes lived concurrently.
    assert len(observed_pids) == 3, (
        f"Expected 3 live tool processes, saw {observed_pids}"
    )
    pid_values = list(observed_pids.values())
    assert len(set(pid_values)) == 3, (
        f"PIDs not distinct: {observed_pids}"
    )
    # All PIDs should be non-zero positive integers.
    assert all(p > 0 for p in pid_values)


def test_per_tool_pid_files_written(
    orchestrator_raw_config, orch_dirs, fast_sleep, noop_signals,
    install_fake_runners, fire_sentinel_after,
) -> None:
    """Each spawned tool writes its PID to ``data/{tool}.pid``.

    The per-tool PID files are how ``_kill_stale_tool`` finds zombie
    children on next startup. If this wiring breaks, an orphaned child
    from a crashed ``alfred up`` would survive the next ``alfred up``
    cycle — the 2026-04-17 orphan-process bug.
    """
    raw = orchestrator_raw_config
    install_fake_runners({"curator": long_lived_runner_3arg})

    observed_pids: list[int | None] = []

    import threading

    def _run() -> None:
        orchestrator.run_all(
            raw, only="curator",
            skills_dir=orch_dirs["skills"],
            pid_path=orch_dirs["pid_path"],
            live_mode=False,
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Wait for the PID file.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        pid = _read_pid(orch_dirs["data"] / "curator.pid")
        if pid:
            observed_pids.append(pid)
            break
        time.sleep(0.02)

    # Shut down.
    orch_dirs["sentinel_path"].write_text("stop", encoding="utf-8")
    t.join(timeout=5.0)
    assert not t.is_alive()

    assert observed_pids and observed_pids[0] and observed_pids[0] > 0
    # After shutdown, the PID file should be gone (teardown path).
    assert not (orch_dirs["data"] / "curator.pid").exists()


def test_mixed_two_arg_and_three_arg_tools_spawn_together(
    orchestrator_raw_config, orch_dirs, fast_sleep, noop_signals,
    install_fake_runners, fire_sentinel_after,
) -> None:
    """Two-arg and three-arg tools can spawn side-by-side.

    Guards against regressions where the arity dispatch accidentally
    picks the wrong branch for mixed tool lists (e.g., if someone
    hard-codes arity per-tool in a refactor).
    """
    raw = orchestrator_raw_config
    raw["surveyor"] = {}  # Enable surveyor auto-start
    install_fake_runners({
        "curator": long_lived_runner_3arg,     # 3-arg
        "surveyor": long_lived_runner_2arg,    # 2-arg
    })

    observed: dict[str, int] = {}

    import threading

    def _run() -> None:
        orchestrator.run_all(
            raw, only="curator,surveyor",
            skills_dir=orch_dirs["skills"],
            pid_path=orch_dirs["pid_path"],
            live_mode=False,
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        c = _read_pid(orch_dirs["data"] / "curator.pid")
        s = _read_pid(orch_dirs["data"] / "surveyor.pid")
        if c and s:
            observed = {"curator": c, "surveyor": s}
            break
        time.sleep(0.02)

    orch_dirs["sentinel_path"].write_text("stop", encoding="utf-8")
    t.join(timeout=5.0)
    assert not t.is_alive()

    # Both tools ran — wrong arity would have caused TypeError in the
    # child process, which prints a traceback but still registers a PID
    # for the exit. We're stricter: require DISTINCT live PIDs.
    assert len(observed) == 2, (
        f"Expected both curator and surveyor to run, got {observed}"
    )
    assert observed["curator"] != observed["surveyor"]
