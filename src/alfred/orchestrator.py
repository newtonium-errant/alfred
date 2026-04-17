"""Process manager — `alfred up` starts all daemons via multiprocessing."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from alfred.daemon import is_running, read_pid, remove_pid as _remove_pid_file, write_pid as _write_pid_file


def _silence_stdio(log_file: str | None = None) -> None:
    """Redirect stdout/stderr away from the terminal in child processes for live mode.

    stderr goes to the log file (if given) so uncaught tracebacks are preserved
    for debugging. stdout goes to devnull.
    """
    sys.stdout = open(os.devnull, "w")  # noqa: SIM115 — kept open for process lifetime
    if log_file:
        sys.stderr = open(log_file, "a")  # noqa: SIM115
    else:
        sys.stderr = sys.stdout


def _run_curator(raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False) -> None:
    """Curator daemon process entry point."""
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/curator.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.curator.config import load_from_unified
    from alfred.curator.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout)
    from alfred.curator.daemon import run
    asyncio.run(run(config, Path(skills_dir)))


def _run_janitor(raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False) -> None:
    """Janitor watch daemon process entry point."""
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/janitor.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.janitor.config import load_from_unified
    from alfred.janitor.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout)
    from alfred.janitor.state import JanitorState
    from alfred.janitor.daemon import run_watch
    state = JanitorState(config.state.path, config.state.max_sweep_history)
    state.load()
    asyncio.run(run_watch(config, state, Path(skills_dir)))


def _run_distiller(raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False) -> None:
    """Distiller watch daemon process entry point."""
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/distiller.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.distiller.config import load_from_unified
    from alfred.distiller.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout)
    from alfred.distiller.state import DistillerState
    from alfred.distiller.daemon import run_watch
    state = DistillerState(config.state.path, config.state.max_run_history)
    state.load()
    asyncio.run(run_watch(config, state, Path(skills_dir)))


_MISSING_DEPS_EXIT = 78  # exit code signaling missing optional dependencies


def _run_surveyor(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Surveyor daemon process entry point."""
    log_cfg = raw.get("logging", {})
    if suppress_stdout:
        _silence_stdio(f"{log_cfg.get('dir', './data')}/surveyor.log")
    try:
        from alfred.surveyor.config import load_from_unified
        from alfred.surveyor.utils import setup_logging
        from alfred.surveyor.daemon import Daemon
    except ImportError as e:
        sys.exit(_MISSING_DEPS_EXIT)

    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=f"{log_cfg.get('dir', './data')}/surveyor.log", suppress_stdout=suppress_stdout)
    daemon = Daemon(config)
    asyncio.run(daemon.run())


def _run_mail_webhook(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Mail webhook receiver process entry point."""
    log_cfg = raw.get("logging", {})
    if suppress_stdout:
        _silence_stdio(f"{log_cfg.get('dir', './data')}/mail_webhook.log")
    from alfred.mail.config import load_from_unified
    config = load_from_unified(raw)
    vault_path = Path(raw.get("vault", {}).get("path", "./vault"))
    inbox_path = vault_path / config.inbox_dir
    token = os.environ.get("MAIL_WEBHOOK_TOKEN", "")
    from alfred.mail.webhook import run_webhook
    run_webhook(inbox_path, token=token)


def _run_talker(raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False) -> None:
    """Talker (Telegram) daemon process entry point.

    Mirrors the 3-arg curator runner: the talker needs ``skills_dir`` to
    locate ``vault-talker/SKILL.md`` for the system prompt.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/talker.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.telegram.daemon import run as talker_run
    exit_code = asyncio.run(
        talker_run(raw, skills_dir_str=skills_dir, suppress_stdout=suppress_stdout)
    )
    if exit_code:
        sys.exit(exit_code)


def _run_brief(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Brief daemon process entry point."""
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/brief.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.brief.config import load_from_unified
    from alfred.brief.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout)
    from alfred.brief.daemon import run_daemon
    asyncio.run(run_daemon(config))


# ---------------------------------------------------------------------------
# Per-tool PID tracking — prevents zombie tool processes from surviving
# across alfred down / alfred up cycles.
# ---------------------------------------------------------------------------

def _tool_pid_path(data_dir: Path, tool: str) -> Path:
    """Return the PID file path for a specific tool."""
    return data_dir / f"{tool}.pid"


def _kill_stale_tool(data_dir: Path, tool: str) -> None:
    """If a previous instance of *tool* is still running, kill it.

    This catches zombie child processes that survived a previous
    ``alfred down`` (e.g., because the orchestrator was SIGKILL'd before
    it could terminate its children).
    """
    pid_file = _tool_pid_path(data_dir, tool)
    old_pid = read_pid(pid_file)
    if old_pid is None:
        return
    if old_pid == os.getpid():
        # Stale file pointing at ourselves — just clean up
        _remove_pid_file(pid_file)
        return
    if not is_running(old_pid):
        _remove_pid_file(pid_file)
        return
    # Process is alive — kill it
    print(f"  [{tool}] killing stale process (pid {old_pid})")
    try:
        os.kill(old_pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pid_file(pid_file)
        return
    # Give it a moment to exit gracefully
    for _ in range(30):  # 3 seconds
        time.sleep(0.1)
        if not is_running(old_pid):
            break
    else:
        # Force kill
        try:
            os.kill(old_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    _remove_pid_file(pid_file)


def _record_tool_pid(data_dir: Path, tool: str, pid: int) -> None:
    """Write the tool's child-process PID to its PID file."""
    _write_pid_file(_tool_pid_path(data_dir, tool), pid)


def _cleanup_tool_pid(data_dir: Path, tool: str) -> None:
    """Remove the tool's PID file on shutdown."""
    _remove_pid_file(_tool_pid_path(data_dir, tool))


TOOL_RUNNERS = {
    "curator": _run_curator,
    "janitor": _run_janitor,
    "distiller": _run_distiller,
    "surveyor": _run_surveyor,
    "mail": _run_mail_webhook,
    "brief": _run_brief,
    "talker": _run_talker,
}


def run_all(
    raw: dict[str, Any],
    only: str | None = None,
    skills_dir: Path | None = None,
    pid_path: Path | None = None,
    live_mode: bool = False,
) -> None:
    """Start selected daemons as child processes with auto-restart."""
    if skills_dir is None:
        from alfred._data import get_skills_dir
        skills_dir = get_skills_dir()

    skills_dir_str = str(skills_dir)

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
        # Only add mail webhook if config section exists
        if "mail" in raw:
            tools.append("mail")
        if "brief" in raw:
            tools.append("brief")
        # Only add talker if config section exists — users without a Telegram
        # bot shouldn't have a daemon spinning in a retry loop on 78 exits.
        if "telegram" in raw:
            tools.append("talker")

    # Validate tool names
    for tool in tools:
        if tool not in TOOL_RUNNERS:
            print(f"Unknown tool: {tool}")
            print(f"Available: {', '.join(TOOL_RUNNERS.keys())}")
            sys.exit(1)

    if not live_mode:
        print(f"Starting daemons: {', '.join(tools)}")

    # Resolve data directory for per-tool PID files
    data_dir = Path(raw.get("logging", {}).get("dir", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)

    # Kill any stale tool processes left over from a previous run
    for tool in tools:
        _kill_stale_tool(data_dir, tool)

    processes: dict[str, multiprocessing.Process] = {}
    restart_counts: dict[str, int] = {}

    suppress_stdout = live_mode

    # Sentinel file path — ``alfred down`` creates this to signal shutdown
    sentinel_path = pid_path.parent / "alfred.stop" if pid_path else None

    log_dir = Path(raw.get("logging", {}).get("dir", "./data"))
    workers_json_path = log_dir / "workers.json"
    started_at = datetime.now(timezone.utc).isoformat()

    # ---- Graceful SIGTERM/SIGINT handling --------------------------------
    # Installed BEFORE spawning children so that SIGTERM arriving during the
    # stagger sleep (10s between tool starts) sets the flag instead of killing
    # the orchestrator instantly and orphaning already-started children.
    shutdown_requested = False

    def _handle_shutdown(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True

    signal.signal(signal.SIGTERM, _handle_shutdown)
    signal.signal(signal.SIGINT, _handle_shutdown)

    def start_process(tool: str) -> multiprocessing.Process:
        runner = TOOL_RUNNERS[tool]
        if tool in ("surveyor", "mail", "brief"):
            p = multiprocessing.Process(target=runner, args=(raw, suppress_stdout), name=f"alfred-{tool}")
        else:
            p = multiprocessing.Process(target=runner, args=(raw, skills_dir_str, suppress_stdout), name=f"alfred-{tool}")
        p.daemon = True
        p.start()
        # Record per-tool PID so we can kill zombies on next startup
        _record_tool_pid(data_dir, tool, p.pid)
        if not live_mode:
            print(f"  [{tool}] started (pid {p.pid})")
        return p

    def _write_workers_json() -> None:
        """Write current process status to workers.json for the Ink TUI."""
        data = {
            "pid": os.getpid(),
            "started_at": started_at,
            "tools": {},
        }
        for tool in tools:
            p = processes.get(tool)
            if p is None:
                data["tools"][tool] = {"pid": None, "status": "stopped", "restarts": restart_counts.get(tool, 0)}
                continue
            alive = p.is_alive()
            data["tools"][tool] = {
                "pid": p.pid if alive else None,
                "status": "running" if alive else "stopped",
                "restarts": restart_counts.get(tool, 0),
            }
            if not alive and p.exitcode is not None:
                data["tools"][tool]["exit_code"] = p.exitcode
        try:
            workers_json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    try:
        # Start all — stagger by 10s to avoid thundering herd on shared infra.
        # Stagger sleep uses small increments so SIGTERM is noticed quickly.
        for i, tool in enumerate(tools):
            if i > 0:
                for _ in range(100):  # 10s in 0.1s increments
                    time.sleep(0.1)
                    if shutdown_requested:
                        break
                if shutdown_requested:
                    break
            processes[tool] = start_process(tool)
            restart_counts[tool] = 0

        if shutdown_requested:
            print("Shutdown requested during startup, stopping...")

        # Write initial workers.json
        _write_workers_json()
        last_workers_write = time.monotonic()

        if not shutdown_requested and live_mode:
            # Live TUI dashboard mode — prefer Textual, fall back to Rich Live
            # NOTE: Both TUI implementations check the sentinel file internally
            # (Textual via set_interval, Rich Live in its 0.25s loop).  The
            # SIGTERM handler + try/finally here ensures cleanup still runs if
            # the signal arrives while the TUI event loop is active.
            try:
                from alfred.tui import run_textual_dashboard
                run_textual_dashboard(
                    tools=tools,
                    processes=processes,
                    restart_counts=restart_counts,
                    start_process=start_process,
                    sentinel_path=sentinel_path,
                    log_dir=log_dir,
                    state_dir=log_dir,
                )
            except ImportError:
                from alfred.dashboard import run_live_dashboard
                run_live_dashboard(
                    tools=tools,
                    processes=processes,
                    restart_counts=restart_counts,
                    start_process=start_process,
                    sentinel_path=sentinel_path,
                    log_dir=log_dir,
                    state_dir=log_dir,
                )
        elif not shutdown_requested:
            # Plain text monitor loop
            try:
                while True:
                    # Sleep in small increments so the loop responds to
                    # SIGTERM within ~100ms instead of waiting up to 5s.
                    for _ in range(50):
                        time.sleep(0.1)
                        if shutdown_requested:
                            break

                    if shutdown_requested:
                        print("SIGTERM received, stopping...")
                        break

                    # Periodically write workers.json for the Ink TUI
                    now = time.monotonic()
                    if now - last_workers_write >= 2:
                        _write_workers_json()
                        last_workers_write = now

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
                                tools = [t for t in tools if t != tool]

                    if not tools:
                        print("All daemons failed, exiting.")
                        break
            except KeyboardInterrupt:
                print("\nShutting down...")
    finally:
        # Terminate child processes and clean up per-tool PID files.
        # This block runs on every exit path: normal break, SIGTERM,
        # KeyboardInterrupt, or unhandled exception.
        #
        # Strategy: SIGTERM all children at once, give them a brief window
        # to exit, then SIGKILL any survivors.  We must finish within the
        # ~5s window that ``_stop_unix`` allows before it SIGKILLs us.
        alive = {tool: p for tool, p in processes.items() if p.is_alive()}

        # Phase 1: SIGTERM all children simultaneously
        for tool, p in alive.items():
            p.terminate()

        # Phase 2: brief wait for graceful exit (1s total, not per-child)
        deadline = time.monotonic() + 1.0
        for tool, p in alive.items():
            remaining = max(0, deadline - time.monotonic())
            p.join(timeout=remaining)

        # Phase 3: SIGKILL any survivors
        for tool, p in alive.items():
            if p.is_alive():
                p.kill()
                p.join(timeout=0.5)
            print(f"  [{tool}] stopped")

        for tool in processes:
            _cleanup_tool_pid(data_dir, tool)
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
        try:
            workers_json_path.unlink(missing_ok=True)
        except OSError:
            pass
