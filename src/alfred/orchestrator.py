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
    from alfred.email_classifier.config import load_from_unified as load_classifier
    config = load_from_unified(raw)
    # Per-instance opt-in: when ``email_classifier:`` is absent or
    # ``enabled: false``, this returns a disabled config and the daemon
    # short-circuits the post-processor. KAL-LE's config.kalle.yaml
    # leaves the block out by design.
    classifier_config = load_classifier(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout)
    from alfred.curator.daemon import run
    asyncio.run(run(config, Path(skills_dir), email_classifier_config=classifier_config))


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


def _run_instructor(raw: dict[str, Any], skills_dir: str, suppress_stdout: bool = False) -> None:
    """Instructor watch daemon process entry point.

    Polls the vault for ``alfred_instructions`` directives and executes
    them in-process via the Anthropic SDK. Takes the same 3-arg
    signature as curator/janitor/distiller because the instructor also
    needs a ``skills_dir`` (its SKILL.md lives at
    ``vault-instructor/SKILL.md``).
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/instructor.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.instructor.config import load_from_unified
    from alfred.instructor.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout)
    from alfred.instructor.state import InstructorState
    from alfred.instructor.daemon import run as run_instructor_daemon
    state = InstructorState(config.state.path)
    state.load()
    asyncio.run(run_instructor_daemon(
        config,
        state=state,
        suppress_stdout=suppress_stdout,
        skills_dir=Path(skills_dir),
    ))


_MISSING_DEPS_EXIT = 78  # exit code signaling missing optional dependencies


def _inject_transport_env_vars(raw: dict[str, Any]) -> None:
    """Set ``ALFRED_TRANSPORT_{HOST,PORT,TOKEN}`` in the current process env.

    Child processes inherit the current environment (``fork`` +
    ``multiprocessing.Process``), so setting these here means every
    tool's subprocess sees the values. Matches the ``MAIL_WEBHOOK_TOKEN``
    injection pattern — once injected, `alfred.transport.client`
    picks them up via ``os.environ.get()``.

    Values are read from the substituted config dict. Env vars
    already set (e.g. from ``.env``) are preserved so a manual
    override still wins.
    """
    transport = raw.get("transport", {}) or {}

    server = transport.get("server", {}) or {}
    host = str(server.get("host", "") or "")
    port = server.get("port")
    if host and "ALFRED_TRANSPORT_HOST" not in os.environ:
        os.environ["ALFRED_TRANSPORT_HOST"] = host
    if port and "ALFRED_TRANSPORT_PORT" not in os.environ:
        os.environ["ALFRED_TRANSPORT_PORT"] = str(port)

    # Token — pull from auth.tokens.local.token, the v1 entry.
    auth = transport.get("auth", {}) or {}
    tokens = auth.get("tokens", {}) or {}
    local = tokens.get("local", {}) or {}
    token = str(local.get("token", "") or "")
    # Don't set if it's the unresolved ${VAR} placeholder — that
    # would leak a literal placeholder into child env and confuse the
    # client's "missing token" check.
    if token and not token.startswith("${") and "ALFRED_TRANSPORT_TOKEN" not in os.environ:
        os.environ["ALFRED_TRANSPORT_TOKEN"] = token


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
    # Idle-tick heartbeat — defaulted-on; emits ``mail.idle_tick`` so
    # the operator can distinguish "no traffic" from "daemon dead".
    run_webhook(
        inbox_path,
        token=token,
        idle_tick_enabled=config.idle_tick.enabled,
        idle_tick_interval_seconds=config.idle_tick.interval_seconds,
    )


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


def _run_bit(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """BIT daemon process entry point.

    Spawns the BIT scheduler. The BIT daemon writes to the vault without
    setting ``ALFRED_VAULT_SCOPE`` — unscoped writes pass the scope
    check in ``vault/scope.py`` (empty scope → unrestricted) — and runs
    at ``brief.schedule.time`` minus ``bit.schedule.lead_minutes`` (default
    5 minutes) so the Morning Brief can pick up a fresh BIT record.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/bit.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    from alfred.bit.config import load_from_unified
    # Reuse brief's setup_logging — the signature matches and BIT
    # doesn't need a bespoke logger.
    from alfred.brief.utils import setup_logging
    config = load_from_unified(raw)
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout)
    from alfred.bit.daemon import run_daemon
    asyncio.run(run_daemon(config, raw))


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


def _run_brief_digest_push(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Brief-digest pusher daemon entry point (V.E.R.A. content arc sender).

    Runs on KAL-LE / STAY-C / future specialist instances. Auto-starts
    when ``brief_digest_push:`` is in the unified config AND
    ``enabled: true``. Salem intentionally omits the block — it is the
    receiver, not a sender.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/brief_digest_push.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    # Reuse brief's setup_logging — same signature, no bespoke logger
    # needed. Keeps log format consistent with the receiver side.
    from alfred.brief.utils import setup_logging
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout)
    from alfred.brief.kalle_brief_daemon import (
        load_brief_digest_push_config,
        run_daemon,
    )
    from alfred.transport.config import load_from_unified as load_transport
    config = load_brief_digest_push_config(raw)
    if not config.enabled:
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("kalle.brief_digest.daemon.disabled_in_config")
        sys.exit(78)
    if not config.self_name:
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("kalle.brief_digest.daemon.missing_self_name")
        sys.exit(78)
    transport_config = load_transport(raw)
    asyncio.run(run_daemon(config, transport_config))


def _run_daily_sync(raw: dict[str, Any], suppress_stdout: bool = False) -> None:
    """Daily Sync daemon process entry point.

    Per-instance 09:00 ADT push channel. Reads the unified config's
    ``daily_sync`` block (per email-surfacing c2). The orchestrator
    only spawns this entry point when ``daily_sync`` is in raw AND
    ``enabled: true`` — but we double-check here so a manual run via
    ``alfred up --only daily_sync`` against a misconfigured file
    fails fast with a clear log line instead of looping.
    """
    log_cfg = raw.get("logging", {})
    log_file = f"{log_cfg.get('dir', './data')}/daily_sync.log"
    if suppress_stdout:
        _silence_stdio(log_file)
    # Reuse brief's setup_logging — the signature matches and Daily
    # Sync doesn't need a bespoke logger.
    from alfred.brief.utils import setup_logging
    setup_logging(level=log_cfg.get("level", "INFO"), log_file=log_file, suppress_stdout=suppress_stdout)
    from alfred.daily_sync.config import load_from_unified as load_ds
    from alfred.daily_sync.daemon import run_daemon as run_ds_daemon
    config = load_ds(raw)
    if not config.enabled:
        # Misconfiguration — return immediately rather than spinning
        # the loop. Matches the orchestrator's exit-78 convention so
        # auto-restart won't keep relaunching us.
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("daily_sync.daemon.disabled_in_config")
        sys.exit(78)
    vault_path_str = raw.get("vault", {}).get("path", "./vault")
    telegram_raw = raw.get("telegram", {}) or {}
    allowed = telegram_raw.get("allowed_users") or []
    user_id = 0
    if allowed:
        try:
            user_id = int(allowed[0])
        except (TypeError, ValueError):
            user_id = 0
    if not user_id:
        import sys
        import structlog
        log = structlog.get_logger(__name__)
        log.warning("daily_sync.daemon.no_telegram_user")
        sys.exit(78)
    asyncio.run(run_ds_daemon(config, Path(vault_path_str), user_id))


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
    "instructor": _run_instructor,
    "surveyor": _run_surveyor,
    "mail": _run_mail_webhook,
    "brief": _run_brief,
    "bit": _run_bit,
    "talker": _run_talker,
    "daily_sync": _run_daily_sync,
    "brief_digest_push": _run_brief_digest_push,
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
        # BIT daemon auto-starts when the config has a ``bit`` section
        # OR when the brief is configured (BIT is a brief pre-check —
        # it makes no sense to have brief without BIT). Explicit
        # ``bit:`` section wins if present.
        if "bit" in raw or "brief" in raw:
            tools.append("bit")
        # Only add talker if config section exists — users without a Telegram
        # bot shouldn't have a daemon spinning in a retry loop on 78 exits.
        if "telegram" in raw:
            tools.append("talker")
        # Instructor auto-starts when ``instructor:`` is in config.
        # Without the section, the daemon has no Anthropic API key to
        # work with and would spin in a retry loop on every directive.
        if "instructor" in raw:
            tools.append("instructor")
        # Daily Sync (email-surfacing c2) auto-starts when ``daily_sync:``
        # is in config AND ``enabled: true``. KAL-LE intentionally omits
        # the block so it doesn't fire 09:00 conversations about coding.
        if "daily_sync" in raw and (raw.get("daily_sync") or {}).get("enabled"):
            tools.append("daily_sync")
        # Brief-digest pusher (V.E.R.A. content arc sender) auto-starts
        # when ``brief_digest_push:`` is in config AND ``enabled: true``.
        # KAL-LE turns this on; Salem leaves it absent (Salem is the
        # principal — receiver, not sender).
        if "brief_digest_push" in raw and (raw.get("brief_digest_push") or {}).get("enabled"):
            tools.append("brief_digest_push")

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

    # Resolve transport env vars once — orchestrator injects these
    # into every tool's child environment so any subprocess can call
    # the outbound-push client without looking at config.yaml again.
    _inject_transport_env_vars(raw)

    def start_process(tool: str) -> multiprocessing.Process:
        runner = TOOL_RUNNERS[tool]
        # Tools whose runner signature is ``(raw, suppress_stdout)`` (no
        # skills_dir). BIT has no skill prompts — it drives the
        # aggregator directly — so it lives in this bucket.
        if tool in ("surveyor", "mail", "brief", "bit", "daily_sync", "brief_digest_push"):
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
