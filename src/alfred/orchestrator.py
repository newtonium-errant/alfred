"""Process manager — `alfred up` starts all daemons via multiprocessing."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any


def _run_curator(raw: dict[str, Any], base_dir: str) -> None:
    """Curator daemon process entry point."""
    from alfred.curator.config import load_from_unified
    from alfred.curator.utils import setup_logging
    config = load_from_unified(raw)
    log_cfg = raw.get("logging", {})
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=f"{log_cfg.get('dir', './data')}/curator.log")
    from alfred.curator.daemon import run
    asyncio.run(run(config, Path(base_dir)))


def _run_janitor(raw: dict[str, Any], base_dir: str) -> None:
    """Janitor watch daemon process entry point."""
    from alfred.janitor.config import load_from_unified
    from alfred.janitor.utils import setup_logging
    config = load_from_unified(raw)
    log_cfg = raw.get("logging", {})
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=f"{log_cfg.get('dir', './data')}/janitor.log")
    from alfred.janitor.state import JanitorState
    from alfred.janitor.daemon import run_watch
    state = JanitorState(config.state.path, config.state.max_sweep_history)
    state.load()
    asyncio.run(run_watch(config, state, Path(base_dir)))


def _run_distiller(raw: dict[str, Any], base_dir: str) -> None:
    """Distiller watch daemon process entry point."""
    from alfred.distiller.config import load_from_unified
    from alfred.distiller.utils import setup_logging
    config = load_from_unified(raw)
    log_cfg = raw.get("logging", {})
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=f"{log_cfg.get('dir', './data')}/distiller.log")
    from alfred.distiller.state import DistillerState
    from alfred.distiller.daemon import run_watch
    state = DistillerState(config.state.path, config.state.max_run_history)
    state.load()
    asyncio.run(run_watch(config, state, Path(base_dir)))


_MISSING_DEPS_EXIT = 78  # exit code signaling missing optional dependencies


def _run_surveyor(raw: dict[str, Any]) -> None:
    """Surveyor daemon process entry point."""
    try:
        from alfred.surveyor.config import load_from_unified
        from alfred.surveyor.utils import setup_logging
        from alfred.surveyor.daemon import Daemon
    except ImportError as e:
        print(f"  [surveyor] ERROR: missing dependencies: {e}")
        print(f"  [surveyor] Install with: pip install -e '.[all]'")
        sys.exit(_MISSING_DEPS_EXIT)

    config = load_from_unified(raw)
    log_cfg = raw.get("logging", {})
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=f"{log_cfg.get('dir', './data')}/surveyor.log")
    daemon = Daemon(config)
    daemon.run()


TOOL_RUNNERS = {
    "curator": _run_curator,
    "janitor": _run_janitor,
    "distiller": _run_distiller,
    "surveyor": _run_surveyor,
}


def run_all(
    raw: dict[str, Any],
    only: str | None = None,
    base_dir: Path | None = None,
    pid_path: Path | None = None,
) -> None:
    """Start selected daemons as child processes with auto-restart."""
    if base_dir is None:
        base_dir = Path(__file__).resolve().parent.parent.parent

    base_dir_str = str(base_dir)

    # Write PID file so ``alfred down`` can find us
    if pid_path is not None:
        from alfred.daemon import write_pid
        write_pid(pid_path, os.getpid())

    # Determine which tools to run
    if only:
        tools = [t.strip() for t in only.split(",")]
    else:
        tools = ["curator", "janitor", "distiller"]
        # Only add surveyor if config section exists
        if "surveyor" in raw:
            tools.append("surveyor")

    # Validate tool names
    for tool in tools:
        if tool not in TOOL_RUNNERS:
            print(f"Unknown tool: {tool}")
            print(f"Available: {', '.join(TOOL_RUNNERS.keys())}")
            sys.exit(1)

    print(f"Starting daemons: {', '.join(tools)}")

    processes: dict[str, multiprocessing.Process] = {}
    restart_counts: dict[str, int] = {}

    def start_process(tool: str) -> multiprocessing.Process:
        runner = TOOL_RUNNERS[tool]
        if tool == "surveyor":
            p = multiprocessing.Process(target=runner, args=(raw,), name=f"alfred-{tool}")
        else:
            p = multiprocessing.Process(target=runner, args=(raw, base_dir_str), name=f"alfred-{tool}")
        p.daemon = True
        p.start()
        print(f"  [{tool}] started (pid {p.pid})")
        return p

    # Start all
    for tool in tools:
        processes[tool] = start_process(tool)
        restart_counts[tool] = 0

    # Sentinel file path — ``alfred down`` creates this to signal shutdown
    sentinel_path = pid_path.parent / "alfred.stop" if pid_path else None

    # Monitor loop
    try:
        while True:
            time.sleep(5)

            # Check for shutdown sentinel
            if sentinel_path and sentinel_path.exists():
                print("Shutdown sentinel detected, stopping...")
                break

            for tool in list(tools):
                p = processes[tool]
                if not p.is_alive():
                    exit_code = p.exitcode
                    if exit_code == _MISSING_DEPS_EXIT:
                        print(f"  [{tool}] missing dependencies, not restarting")
                        tools = [t for t in tools if t != tool]
                        continue
                    restart_counts[tool] += 1
                    if restart_counts[tool] <= 5:
                        print(f"  [{tool}] exited ({exit_code}), restarting ({restart_counts[tool]}/5)...")
                        processes[tool] = start_process(tool)
                    else:
                        print(f"  [{tool}] exceeded restart limit, giving up")
    except KeyboardInterrupt:
        print("\nShutting down...")

    # Terminate child processes
    for tool, p in processes.items():
        if p.is_alive():
            p.terminate()
            p.join(timeout=5)
            if p.is_alive():
                p.kill()
            print(f"  [{tool}] stopped")
    print("All daemons stopped.")

    # Clean up PID file and sentinel
    if pid_path:
        from alfred.daemon import remove_pid
        remove_pid(pid_path)
    if sentinel_path:
        try:
            sentinel_path.unlink(missing_ok=True)
        except OSError:
            pass
