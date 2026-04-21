"""Top-level argparse CLI dispatcher for Alfred."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml


def _load_env_file(env_path: Path | None = None) -> None:
    """Load a .env file into os.environ (without overriding existing vars).

    Supports lines of the form KEY=VALUE (with optional quoting).
    Skips blank lines and comments (#).
    """
    if env_path is None:
        env_path = Path(".env")
    if not env_path.is_file():
        return
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Remove surrounding quotes if present
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            # Only set if not already in environment (don't override)
            if key not in os.environ:
                os.environ[key] = value


def _load_unified_config(config_path: str) -> dict[str, Any]:
    """Load and return raw unified config dict."""
    path = Path(config_path)
    if not path.exists():
        print(f"Config file not found: {path}")
        print("Run `alfred quickstart` to create one.")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_pid_path(raw: dict[str, Any]) -> Path:
    """Return the configured PID path, honouring multi-instance overrides.

    Priority (Stage 3.5 multi-instance plumbing):
      1. ``daemon.pid_path`` top-level config field (explicit per-instance
         override — e.g. KAL-LE ships ``/home/andrew/.alfred/kalle/data/alfred.pid``)
      2. ``logging.dir`` + ``alfred.pid`` (legacy default — Salem keeps
         this unchanged)

    Extracting this into a helper means ``up``/``down``/``status``/``tui``
    all read from the same code path; accidentally diverging would have
    Salem writing a PID to one place and ``alfred down`` looking for it
    elsewhere, which would orphan the daemon on teardown.
    """
    daemon_cfg = raw.get("daemon", {}) or {}
    explicit = daemon_cfg.get("pid_path")
    if explicit:
        return Path(explicit)
    log_cfg = raw.get("logging", {})
    log_dir = log_cfg.get("dir", "./data")
    return Path(log_dir) / "alfred.pid"


def _setup_logging_from_config(raw: dict[str, Any], tool: str = "alfred", suppress_stdout: bool = False) -> None:
    """Set up logging from the unified config's logging section.

    ``tool`` selects the per-tool log file: ``data/<tool>.log``. Default
    ``"alfred"`` preserves backward compatibility for the daemon launcher
    (``cmd_up``) and any handler that legitimately wants the shared log.

    ``suppress_stdout`` prevents adding a StreamHandler to stdout, which is
    load-bearing for the vault CLI (its stdout is a JSON contract).

    Each tool's ``utils.setup_logging`` helper has an identical signature,
    so the choice to import curator's here is arbitrary — any of them work.
    """
    log_cfg = raw.get("logging", {})
    level = log_cfg.get("level", "INFO")
    log_dir = log_cfg.get("dir", "./data")
    from alfred.curator.utils import setup_logging
    setup_logging(level=level, log_file=f"{log_dir}/{tool}.log", suppress_stdout=suppress_stdout)


# --- Subcommand handlers ---

def cmd_quickstart(args: argparse.Namespace) -> None:
    from alfred.quickstart import run_quickstart
    run_quickstart()


def cmd_up(args: argparse.Namespace) -> None:
    raw = _load_unified_config(args.config)
    log_cfg = raw.get("logging", {})
    log_dir = log_cfg.get("dir", "./data")
    pid_path = _resolve_pid_path(raw)

    # Check if already running
    from alfred.daemon import check_already_running
    existing = check_already_running(pid_path)
    if existing:
        print(f"Alfred is already running (pid {existing}).")
        print("Use `alfred down` to stop it first.")
        sys.exit(1)

    # Optional preflight gate — ``alfred up --preflight`` runs a quick
    # BIT sweep before spawning daemons. OK/WARN/SKIP continues; FAIL
    # aborts with a non-zero exit so scripts can detect the gate failing.
    # Per plan Part 11 Q3: WARN does not block, only FAIL does.
    if getattr(args, "preflight", False):
        import asyncio
        from alfred.health.aggregator import run_all_checks
        from alfred.health.renderer import render_human
        from alfred.health.types import Status
        print("Preflight BIT sweep (quick mode)...")
        report = asyncio.run(run_all_checks(raw, mode="quick"))
        for line in render_human(report):
            print(line)
        if report.overall_status == Status.FAIL:
            print("\nPreflight FAILED — not starting daemons.")
            print("Re-run without --preflight to start anyway, or fix the issues above.")
            sys.exit(1)
        print("\nPreflight passed. Starting daemons...\n")

    live_mode = getattr(args, "live", False)
    foreground = getattr(args, "_internal_foreground", False) or getattr(args, "foreground", False) or live_mode

    if foreground:
        # Run in foreground (current behavior) — used by --foreground, --live, and --_internal-foreground
        if not live_mode:
            _setup_logging_from_config(raw)
        from alfred.orchestrator import run_all
        from alfred._data import get_skills_dir
        run_all(raw, only=args.only, skills_dir=get_skills_dir(), pid_path=pid_path, live_mode=live_mode)
    else:
        # Daemon mode: re-exec as detached background process
        from alfred.daemon import spawn_daemon
        log_file = f"{log_dir}/alfred.log"
        pid = spawn_daemon(config_path=args.config, only=args.only, log_file=log_file)
        print(f"Alfred started (pid {pid}). Logs: {log_file}")
        print("Stop with: alfred down")


def cmd_down(args: argparse.Namespace) -> None:
    raw = _load_unified_config(args.config)
    pid_path = _resolve_pid_path(raw)

    from alfred.daemon import stop_daemon
    if stop_daemon(pid_path):
        print("Alfred stopped.")
    else:
        print("Alfred is not running.")


def cmd_status(args: argparse.Namespace) -> None:
    raw = _load_unified_config(args.config)

    print("=" * 60)
    print("ALFRED STATUS")
    print("=" * 60)

    # Daemon status
    pid_path = _resolve_pid_path(raw)
    from alfred.daemon import check_already_running
    running_pid = check_already_running(pid_path)
    if running_pid:
        print(f"Daemon: running (pid {running_pid})")
    else:
        print("Daemon: not running")

    # Curator status
    print("\n--- Curator ---")
    try:
        from alfred.curator.config import load_from_unified as curator_cfg
        cfg = curator_cfg(raw)
        from alfred.curator.state import StateManager
        sm = StateManager(cfg.state.path)
        sm.load()
        print(f"  Processed files: {len(sm.state.processed)}")
        print(f"  Last run: {sm.state.last_run or 'never'}")
    except Exception as e:
        print(f"  (unavailable: {e})")

    # Janitor status
    print("\n--- Janitor ---")
    try:
        from alfred.janitor.config import load_from_unified as janitor_cfg
        cfg = janitor_cfg(raw)
        from alfred.janitor.state import JanitorState
        st = JanitorState(cfg.state.path, cfg.state.max_sweep_history)
        st.load()
        files_with_issues = sum(1 for fs in st.files.values() if fs.open_issues)
        print(f"  Tracked files: {len(st.files)}")
        print(f"  Files with issues: {files_with_issues}")
        print(f"  Sweeps recorded: {len(st.sweeps)}")
    except Exception as e:
        print(f"  (unavailable: {e})")

    # Distiller status
    print("\n--- Distiller ---")
    try:
        from alfred.distiller.config import load_from_unified as distiller_cfg
        cfg = distiller_cfg(raw)
        from alfred.distiller.state import DistillerState
        st = DistillerState(cfg.state.path, cfg.state.max_run_history)
        st.load()
        total_learns = sum(len(fs.learn_records_created) for fs in st.files.values())
        print(f"  Tracked source files: {len(st.files)}")
        print(f"  Learn records created: {total_learns}")
        print(f"  Runs recorded: {len(st.runs)}")
    except Exception as e:
        print(f"  (unavailable: {e})")

    # Surveyor status
    print("\n--- Surveyor ---")
    try:
        from alfred.surveyor.config import load_from_unified as surveyor_cfg
        cfg = surveyor_cfg(raw)
        from alfred.surveyor.state import PipelineState
        st = PipelineState(cfg.state.path)
        st.load()
        print(f"  Tracked files: {len(st.files)}")
        print(f"  Clusters: {len(st.clusters)}")
        print(f"  Last run: {st.last_run or 'never'}")
    except Exception as e:
        print(f"  (unavailable: {e})")

    # Instructor status — only show if config section exists, mirroring
    # the orchestrator's auto-start gate.
    if "instructor" in raw:
        print("\n--- Instructor ---")
        try:
            from alfred.instructor.config import load_from_unified as instructor_cfg
            cfg = instructor_cfg(raw)
            from alfred.instructor.state import InstructorState
            st = InstructorState(cfg.state.path)
            st.load()
            pending = {k: v for k, v in st.retry_counts.items() if v > 0}
            print(f"  Tracked records: {len(st.file_hashes)}")
            print(f"  Retries pending: {len(pending)}")
            print(f"  Last run:        {st.last_run_ts or 'never'}")
        except Exception as e:
            print(f"  (unavailable: {e})")

    # Talker status — only show if config section exists, mirroring the
    # orchestrator's auto-start gate.
    if "telegram" in raw:
        print("\n--- Talker ---")
        try:
            from alfred.telegram.config import load_from_unified as talker_cfg
            from alfred.telegram.state import StateManager as TalkerState
            cfg = talker_cfg(raw)
            sm = TalkerState(cfg.session.state_path)
            sm.load()
            active = sm.state.get("active_sessions", {}) or {}
            closed = sm.state.get("closed_sessions", []) or []
            print(f"  Active sessions: {len(active)}")
            print(f"  Closed sessions: {len(closed)}")
        except Exception as e:
            print(f"  (unavailable: {e})")

    print()


def cmd_curator(args: argparse.Namespace) -> None:
    import asyncio
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="curator")
    from alfred.curator.config import load_from_unified
    config = load_from_unified(raw)
    from alfred.curator.daemon import run
    from alfred._data import get_skills_dir
    try:
        asyncio.run(run(config, get_skills_dir()))
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_janitor(args: argparse.Namespace) -> None:
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="janitor")
    from alfred.janitor.config import load_from_unified
    config = load_from_unified(raw)
    from alfred._data import get_skills_dir
    skills_dir = get_skills_dir()

    from alfred.janitor import cli as jcli
    subcmd = args.janitor_cmd

    if subcmd == "scan":
        jcli.cmd_scan(config, skills_dir)
    elif subcmd == "fix":
        jcli.cmd_fix(config, skills_dir)
    elif subcmd == "watch":
        jcli.cmd_watch(config, skills_dir)
    elif subcmd == "status":
        jcli.cmd_status(config)
    elif subcmd == "history":
        jcli.cmd_history(config, limit=args.limit)
    elif subcmd == "drift":
        jcli.cmd_drift(config)
    elif subcmd == "ignore":
        jcli.cmd_ignore(config, args.file, reason=args.reason)
    else:
        print(f"Unknown janitor subcommand: {subcmd}")
        sys.exit(1)


def cmd_distiller(args: argparse.Namespace) -> None:
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="distiller")
    from alfred.distiller.config import load_from_unified
    config = load_from_unified(raw)
    from alfred._data import get_skills_dir
    skills_dir = get_skills_dir()

    from alfred.distiller import cli as dcli
    subcmd = args.distiller_cmd

    if subcmd == "scan":
        dcli.cmd_scan(config, skills_dir, project=args.project)
    elif subcmd == "run":
        dcli.cmd_run(config, skills_dir, project=args.project)
    elif subcmd == "watch":
        dcli.cmd_watch(config, skills_dir)
    elif subcmd == "status":
        dcli.cmd_status(config)
    elif subcmd == "history":
        dcli.cmd_history(config, limit=args.limit)
    elif subcmd == "consolidate":
        dcli.cmd_consolidate(config, skills_dir)
    else:
        print(f"Unknown distiller subcommand: {subcmd}")
        sys.exit(1)


def cmd_instructor(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred instructor`` subcommands (scan/run/status)."""
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="instructor")
    from alfred.instructor.config import load_from_unified
    config = load_from_unified(raw)

    from alfred.instructor import cli as icli
    subcmd = getattr(args, "instructor_cmd", None)

    if subcmd == "scan":
        icli.cmd_scan(config)
    elif subcmd == "run":
        icli.cmd_run(config)
    elif subcmd == "status":
        icli.cmd_status(config)
    else:
        print("Usage: alfred instructor {scan|run|status}")
        sys.exit(1)


def cmd_transport(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred transport`` subcommands."""
    raw = _load_unified_config(args.config)
    # Transport CLI may emit JSON; suppress stdout logging so the
    # JSON contract stays clean.
    wants_json = bool(getattr(args, "json", False))
    _setup_logging_from_config(
        raw, tool="transport", suppress_stdout=wants_json,
    )

    from alfred.transport import cli as tcli
    subcmd = getattr(args, "transport_cmd", None)

    if subcmd == "status":
        sys.exit(tcli.cmd_status(raw, wants_json=wants_json))
    if subcmd == "send-test":
        sys.exit(tcli.cmd_send_test(
            raw, user_id=args.user_id, text=args.text, wants_json=wants_json,
        ))
    if subcmd == "queue":
        sys.exit(tcli.cmd_queue(raw, wants_json=wants_json))
    if subcmd == "dead-letter":
        sys.exit(tcli.cmd_dead_letter(
            raw,
            action=args.action,
            entry_id=getattr(args, "entry_id", None),
            wants_json=wants_json,
        ))
    if subcmd == "rotate":
        sys.exit(tcli.cmd_rotate(raw))

    print("Usage: alfred transport {status|send-test|queue|dead-letter|rotate}")
    sys.exit(1)


def cmd_vault(args: argparse.Namespace) -> None:
    # Route logs to a dedicated file sink. The vault CLI emits JSON on stdout
    # that calling agents parse, so logging MUST NOT leak to stdout.
    # suppress_stdout=True is load-bearing for the JSON contract.
    try:
        raw = _load_unified_config(args.config)
        _setup_logging_from_config(raw, tool="vault", suppress_stdout=True)
    except SystemExit:
        # _load_unified_config calls sys.exit on missing config; swallow so
        # vault CLI still works in environments without config.yaml.
        pass
    except Exception:
        # Never let logging setup break the vault CLI contract (JSON stdout).
        pass

    from alfred.vault.cli import handle_vault_command
    handle_vault_command(args)


def cmd_exec(args: argparse.Namespace) -> None:
    """Run a command with vault env vars set up automatically."""
    import os
    import subprocess

    from alfred.vault.mutation_log import (
        append_to_audit_log,
        cleanup_session_file,
        create_session_file,
        read_mutations,
    )
    from alfred.vault.scope import SCOPE_RULES

    raw = _load_unified_config(args.config)
    vault_cfg = raw.get("vault", {})
    vault_path = str(Path(vault_cfg.get("path", "./vault")).resolve())

    scope = args.scope
    if scope and scope not in SCOPE_RULES:
        print(f"Unknown scope: '{scope}'. Valid: {', '.join(sorted(SCOPE_RULES))}")
        sys.exit(1)

    session_file = create_session_file()

    env = {
        **os.environ,
        "ALFRED_VAULT_PATH": vault_path,
        "ALFRED_VAULT_SESSION": session_file,
    }
    if scope:
        env["ALFRED_VAULT_SCOPE"] = scope

    command = args.exec_command
    # Strip leading '--' separator if present
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("No command provided. Usage: alfred exec [--scope SCOPE] -- <command...>")
        cleanup_session_file(session_file)
        sys.exit(1)

    try:
        result = subprocess.run(command, env=env)
    except FileNotFoundError:
        print(f"Command not found: {command[0]}")
        cleanup_session_file(session_file)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        result = None

    # Report mutations
    mutations = read_mutations(session_file)
    total = sum(len(v) for v in mutations.values())

    # Audit log
    if total > 0:
        log_cfg = raw.get("logging", {})
        audit_path = Path(log_cfg.get("dir", "./data")) / "vault_audit.log"
        append_to_audit_log(str(audit_path), "exec", mutations, detail=" ".join(command))

    if total > 0:
        print(f"\n--- Vault mutations ({total}) ---")
        for path in mutations["files_created"]:
            print(f"  + {path}")
        for path in mutations["files_modified"]:
            print(f"  ~ {path}")
        for path in mutations["files_deleted"]:
            print(f"  - {path}")

    cleanup_session_file(session_file)
    sys.exit(result.returncode if result else 1)


def cmd_ingest(args: argparse.Namespace) -> None:
    """Split a bulk conversation export into individual inbox files."""
    from alfred.curator.ingest import ingest_file

    json_path = Path(args.file).resolve()
    if not json_path.exists():
        print(f"File not found: {json_path}")
        sys.exit(1)

    raw = _load_unified_config(args.config)
    vault_cfg = raw.get("vault", {})
    vault_path = Path(vault_cfg.get("path", "./vault")).resolve()
    curator_cfg = raw.get("curator", {})
    inbox_dir = curator_cfg.get("inbox_dir", "inbox")
    processed_dir = curator_cfg.get("processed_dir", "inbox/processed")
    inbox_path = vault_path / inbox_dir
    processed_path = vault_path / processed_dir

    try:
        count = ingest_file(
            json_path=json_path,
            inbox_path=inbox_path,
            processed_path=processed_path,
            dry_run=args.dry_run,
        )
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Error: {e}")
        sys.exit(1)

    if not args.dry_run and count > 0:
        print(f"\nDone. The curator daemon will pick up the {count} files automatically.")


def cmd_process(args: argparse.Namespace) -> None:
    """Batch-process all unprocessed inbox files with progress display."""
    import asyncio
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="curator")
    from alfred.curator.config import load_from_unified
    config = load_from_unified(raw)
    from alfred.curator.process import run_batch
    from alfred._data import get_skills_dir
    try:
        asyncio.run(run_batch(config, get_skills_dir(), limit=args.limit, dry_run=args.dry_run, concurrency=args.jobs))
    except KeyboardInterrupt:
        pass


def cmd_tui(args: argparse.Namespace) -> None:
    """Launch the Ink TUI dashboard (reads data/ files produced by daemons)."""
    import shutil
    import subprocess

    raw = _load_unified_config(args.config)
    log_cfg = raw.get("logging", {})
    log_dir = Path(log_cfg.get("dir", "./data")).resolve()

    # Check if daemons are running (warn only)
    pid_path = _resolve_pid_path(raw)
    from alfred.daemon import check_already_running
    if not check_already_running(pid_path):
        print("Note: Alfred daemons are not running. The TUI will show last-known state.")
        print("Start daemons with: alfred up\n")

    # Locate bundled JS
    from alfred._data import get_tui_js_path
    js_path = get_tui_js_path()
    if not js_path.exists():
        print(f"TUI bundle not found at {js_path}")
        print("Rebuild with: cd tui-ink && npm run build")
        sys.exit(1)

    # Check node is available
    node = shutil.which("node")
    if not node:
        print("Node.js is required for the Ink TUI but was not found on PATH.")
        print("Install Node.js 18+ from https://nodejs.org/")
        sys.exit(1)

    # Get version
    version = "0.2.1"
    try:
        from importlib.metadata import version as pkg_version
        version = pkg_version("alfred-vault")
    except Exception:
        pass

    env = {
        **os.environ,
        "ALFRED_DATA_DIR": str(log_dir),
        "ALFRED_VERSION": version,
    }

    try:
        subprocess.run([node, str(js_path)], env=env)
    except KeyboardInterrupt:
        pass


def cmd_temporal(args: argparse.Namespace) -> None:
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="temporal")

    try:
        from alfred.temporal import cli as tcli
    except ImportError:
        print("Temporal is not installed. Alfred's workflow engine won't work without it.")
        print("Install with: pip install alfred-vault[temporal]")
        sys.exit(1)

    subcmd = getattr(args, "temporal_cmd", None)
    if subcmd == "worker":
        tcli.cmd_worker(args, raw)
    elif subcmd == "run":
        tcli.cmd_run(args, raw)
    elif subcmd == "schedule":
        tcli.cmd_schedule(args, raw)
    elif subcmd == "list":
        tcli.cmd_list(args, raw)
    else:
        print("Usage: alfred temporal {worker|run|schedule|list}")
        print("Run `alfred temporal --help` for details.")
        sys.exit(1)


def cmd_surveyor(args: argparse.Namespace) -> None:
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="surveyor")

    try:
        from alfred.surveyor.config import load_from_unified
        from alfred.surveyor.daemon import Daemon
    except ImportError as e:
        print(f"Surveyor dependencies not installed: {e}")
        print("Install with: pip install alfred-vault[all]")
        sys.exit(1)

    import asyncio
    config = load_from_unified(raw)
    daemon = Daemon(config)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_brief(args: argparse.Namespace) -> None:
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="brief")

    from alfred.brief.config import load_from_unified
    from alfred.brief import cli as bcli

    config = load_from_unified(raw)
    subcmd = getattr(args, "brief_cmd", None)
    if subcmd == "weather":
        bcli.cmd_weather(config)
    elif subcmd == "status":
        bcli.cmd_status(config)
    elif subcmd == "history":
        bcli.cmd_history(config, limit=args.limit)
    elif subcmd == "watch":
        bcli.cmd_watch(config)
    elif subcmd == "generate":
        bcli.cmd_generate(config, refresh=args.refresh)
    else:
        # Default: generate
        bcli.cmd_generate(config)


def cmd_talker(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred talker`` subcommands."""
    raw = _load_unified_config(args.config)
    subcmd = getattr(args, "talker_cmd", None)

    # JSON-emitting subcommands suppress stdout logging so the JSON stream
    # stays clean for downstream parsers. Same contract as the vault CLI.
    wants_json = bool(getattr(args, "json", False))
    _setup_logging_from_config(
        raw,
        tool="talker",
        suppress_stdout=wants_json,
    )

    if subcmd == "watch":
        import asyncio
        from alfred.telegram.daemon import run as talker_run
        from alfred._data import get_skills_dir
        try:
            code = asyncio.run(
                talker_run(
                    raw,
                    skills_dir_str=str(get_skills_dir()),
                    suppress_stdout=False,
                )
            )
        except KeyboardInterrupt:
            print("\nStopped.")
            return
        sys.exit(code)

    # The remaining subcommands all touch state — share the load.
    from alfred.telegram.config import load_from_unified as talker_cfg_loader
    from alfred.telegram.state import StateManager
    config = talker_cfg_loader(raw)
    sm = StateManager(config.session.state_path)
    sm.load()

    if subcmd == "status":
        active = sm.state.get("active_sessions", {}) or {}
        closed = sm.state.get("closed_sessions", []) or []
        if wants_json:
            payload = {
                "active_sessions": [
                    {
                        "chat_id": int(cid) if str(cid).lstrip("-").isdigit() else cid,
                        "session_id": s.get("session_id"),
                        "started_at": s.get("started_at"),
                        "last_message_at": s.get("last_message_at"),
                        "turn_count": len(s.get("transcript") or []),
                    }
                    for cid, s in active.items()
                ],
                "closed_count": len(closed),
            }
            print(json.dumps(payload, indent=2))
            return
        print("=" * 60)
        print("TALKER STATUS")
        print("=" * 60)
        if not active:
            print("Active sessions: none")
        else:
            print(f"Active sessions: {len(active)}")
            for cid, s in active.items():
                turns = len(s.get("transcript") or [])
                print(f"  - chat_id={cid}")
                print(f"      started_at:      {s.get('started_at', '?')}")
                print(f"      last_message_at: {s.get('last_message_at', '?')}")
                print(f"      turn_count:      {turns}")
        print(f"Closed sessions: {len(closed)}")
        return

    if subcmd == "end":
        chat_id = args.chat_id
        active_dict = sm.get_active(chat_id)
        if active_dict is None:
            print(f"No active session for chat_id={chat_id}")
            sys.exit(1)
        from alfred.telegram import session as tsession
        user_path = (
            active_dict.get("_user_vault_path")
            or (config.primary_users[0] if config.primary_users else None)
        )
        stt_model = active_dict.get("_stt_model_used") or config.stt.model
        vault_root = active_dict.get("_vault_path_root") or config.vault.path
        try:
            rel_path = tsession.close_session(
                sm,
                vault_path_root=vault_root,
                chat_id=int(chat_id),
                reason="cli_manual",
                user_vault_path=user_path,
                stt_model_used=stt_model,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"Failed to close session: {exc}")
            sys.exit(1)
        if wants_json:
            print(json.dumps({"chat_id": chat_id, "record_path": rel_path}, indent=2))
        else:
            print(f"Closed session for chat_id={chat_id}")
            print(f"  Record: {rel_path}")
        return

    if subcmd == "history":
        closed = list(sm.state.get("closed_sessions", []) or [])
        limit = getattr(args, "limit", 10) or 10
        tail = closed[-limit:]
        if wants_json:
            print(json.dumps(tail, indent=2))
            return
        if not tail:
            print("No closed sessions recorded.")
            return
        print(f"Showing last {len(tail)} closed session(s):")
        for s in tail:
            print(
                f"  {s.get('ended_at', '?')}  chat={s.get('chat_id', '?')}  "
                f"turns={s.get('message_count', 0)}  ops={s.get('vault_ops', 0)}  "
                f"reason={s.get('reason', '?')}"
            )
            rp = s.get("record_path")
            if rp:
                print(f"      {rp}")
        return

    print("Usage: alfred talker {watch|status|end|history}")
    sys.exit(1)


def cmd_bit(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred bit`` subcommands (run-now / status / history)."""
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="bit")

    from alfred.bit.config import load_from_unified
    from alfred.bit import cli as bcli

    config = load_from_unified(raw)
    subcmd = getattr(args, "bit_cmd", None)
    wants_json = bool(getattr(args, "json", False))

    if subcmd == "run-now":
        code = bcli.cmd_run_now(config, raw, wants_json=wants_json)
    elif subcmd == "status":
        code = bcli.cmd_status(config, wants_json=wants_json)
    elif subcmd == "history":
        limit = getattr(args, "limit", 10) or 10
        code = bcli.cmd_history(config, limit=limit, wants_json=wants_json)
    else:
        print("Usage: alfred bit {run-now|status|history}")
        sys.exit(1)

    sys.exit(code)


def cmd_check(args: argparse.Namespace) -> None:
    """Run Alfred's built-in test (BIT) and report health.

    Two output modes:
      * default — streaming human-readable lines, line-by-line so a
        slow probe doesn't leave the user waiting
      * ``--json`` — batch JSON written to stdout, suitable for piping
        to ``jq`` or for machine consumption

    Exit code:
      * 0  when overall_status is OK, WARN, or SKIP (WARN is not a
        blocker — operators see it and decide, see plan Part 11 Q3)
      * 1  when any tool reports FAIL
    """
    import asyncio

    raw = _load_unified_config(args.config)

    # Logging to the alfred.log sink — human-readable output goes to
    # stdout. We suppress stdout logging to keep the BIT output clean.
    _setup_logging_from_config(raw, tool="alfred", suppress_stdout=True)

    mode = "full" if getattr(args, "full", False) else "quick"
    wants_json = bool(getattr(args, "json", False))
    filter_tools = getattr(args, "tools", None)
    tools: list[str] | None = None
    if filter_tools:
        tools = [t.strip() for t in filter_tools.split(",") if t.strip()]

    from alfred.health.aggregator import run_all_checks
    from alfred.health.renderer import render_human, render_json
    from alfred.health.types import Status

    report = asyncio.run(run_all_checks(raw, mode=mode, tools=tools))

    if wants_json:
        # Batch output — JSON is useless to stream line-by-line.
        print(render_json(report))
    else:
        # Streaming human output — push each line to stdout as it's
        # produced. The renderer already ships a ``write`` hook for
        # this pattern.
        render_human(report, write=print)

    sys.exit(1 if report.overall_status == Status.FAIL else 0)


def cmd_mail(args: argparse.Namespace) -> None:
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="mail")

    from alfred.mail.config import load_from_unified
    from alfred.mail.fetcher import fetch_all
    from alfred.mail.state import StateManager

    config = load_from_unified(raw)
    vault_path = Path(raw.get("vault", {}).get("path", "./vault"))

    subcmd = getattr(args, "mail_cmd", None)
    if subcmd == "fetch":
        if not config.accounts:
            print("No mail accounts configured. Add a 'mail' section to config.yaml.")
            print("See config.yaml.example for the format.")
            sys.exit(1)
        total = fetch_all(config, vault_path)
        print(f"Fetched {total} new email(s).")
        if not args.once:
            import time
            print(f"Polling every {config.poll_interval}s. Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(config.poll_interval)
                    total = fetch_all(config, vault_path)
                    if total:
                        print(f"Fetched {total} new email(s).")
            except KeyboardInterrupt:
                print("\nStopped.")
    elif subcmd == "webhook":
        from alfred.mail.webhook import run_webhook
        token = os.environ.get("MAIL_WEBHOOK_TOKEN", "")
        inbox_path = vault_path / config.inbox_dir
        run_webhook(inbox_path, host=args.host, port=args.port, token=token)
    elif subcmd == "status":
        sm = StateManager(config.state_path)
        sm.load()
        for name, ids in sm.state.seen_ids.items():
            print(f"  {name}: {len(ids)} emails fetched")
        if not sm.state.seen_ids:
            print("  No emails fetched yet.")
    else:
        print("Usage: alfred mail {fetch|webhook|status}")
        sys.exit(1)


# --- Argument parser ---

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alfred",
        description="Alfred — unified vault operations suite",
    )
    parser.add_argument(
        "--config", default="config.yaml",
        help="Path to config.yaml (default: config.yaml)",
    )
    sub = parser.add_subparsers(dest="command")

    # quickstart
    sub.add_parser("quickstart", help="Interactive setup wizard")

    # up
    up_parser = sub.add_parser("up", help="Start all daemons (background by default)")
    up_parser.add_argument(
        "--only", type=str, default=None,
        help="Comma-separated list of tools to start (e.g. curator,janitor)",
    )
    up_parser.add_argument(
        "--foreground", action="store_true", default=False,
        help="Stay attached to the terminal (for development/debugging)",
    )
    up_parser.add_argument(
        "--_internal-foreground", dest="_internal_foreground",
        action="store_true", default=False,
        help=argparse.SUPPRESS,
    )
    up_parser.add_argument(
        "--live", action="store_true", default=False,
        help="Show live TUI dashboard (implies --foreground)",
    )
    up_parser.add_argument(
        "--preflight", action="store_true", default=False,
        help="Run BIT quick check before starting daemons; abort if any tool FAILs",
    )

    # down
    sub.add_parser("down", help="Stop the background daemon")

    # status
    sub.add_parser("status", help="Show status from all tools")

    # check — run BIT (built-in test) across every tool
    check_p = sub.add_parser(
        "check",
        help="Run built-in test (health checks across all tools)",
    )
    check_p.add_argument(
        "--full", action="store_true", default=False,
        help="Run deeper probes (15s per tool vs. 5s quick mode)",
    )
    check_p.add_argument(
        "--json", action="store_true", default=False,
        help="Emit JSON instead of human-readable streaming output",
    )
    check_p.add_argument(
        "--tools", default=None,
        help="Comma-separated subset of tools to check (default: all)",
    )

    # curator
    sub.add_parser("curator", help="Start curator daemon")

    # janitor
    jan = sub.add_parser("janitor", help="Vault janitor subcommands")
    jan_sub = jan.add_subparsers(dest="janitor_cmd")
    jan_sub.add_parser("scan", help="Run structural scan")
    jan_sub.add_parser("fix", help="Scan + agent fix")
    jan_sub.add_parser("watch", help="Daemon mode")
    jan_sub.add_parser("status", help="Show sweep status")
    jan_sub.add_parser("drift", help="Run semantic drift scan")
    jan_hist = jan_sub.add_parser("history", help="Show sweep history")
    jan_hist.add_argument("--limit", type=int, default=10)
    jan_ignore = jan_sub.add_parser("ignore", help="Ignore a file")
    jan_ignore.add_argument("file", help="Relative file path to ignore")
    jan_ignore.add_argument("--reason", default="", help="Reason for ignoring")

    # distiller
    dist = sub.add_parser("distiller", help="Vault distiller subcommands")
    dist_sub = dist.add_subparsers(dest="distiller_cmd")
    dist_scan = dist_sub.add_parser("scan", help="Scan for candidates")
    dist_scan.add_argument("--project", "-p", default=None, help="Filter by project name")
    dist_run = dist_sub.add_parser("run", help="Scan + extract")
    dist_run.add_argument("--project", "-p", default=None, help="Filter by project name")
    dist_sub.add_parser("watch", help="Daemon mode")
    dist_sub.add_parser("status", help="Show extraction status")
    dist_sub.add_parser("consolidate", help="Consolidation sweep: merge duplicates, resolve contradictions")
    dist_hist = dist_sub.add_parser("history", help="Show run history")
    dist_hist.add_argument("--limit", type=int, default=10)

    # instructor
    inst = sub.add_parser(
        "instructor",
        help="Vault instructor subcommands (alfred_instructions watcher)",
    )
    inst_sub = inst.add_subparsers(dest="instructor_cmd")
    inst_sub.add_parser("scan", help="One-shot scan: list pending directives, don't execute")
    inst_sub.add_parser("run", help="Run the poll loop in foreground until Ctrl-C")
    inst_sub.add_parser("status", help="Show tracked files, pending retries, last run")

    # transport
    from alfred.transport.cli import build_subparser as build_transport_subparser
    build_transport_subparser(sub)

    # vault
    from alfred.vault.cli import build_vault_parser
    build_vault_parser(sub)

    # exec
    exec_parser = sub.add_parser(
        "exec",
        help="Run a command with vault env vars (ALFRED_VAULT_PATH, etc.)",
    )
    exec_parser.add_argument(
        "--scope", default=None,
        help="Agent scope: curator, janitor, distiller (default: unrestricted)",
    )
    exec_parser.add_argument(
        "exec_command", nargs=argparse.REMAINDER,
        help="Command to run (use -- before the command)",
    )

    # ingest
    ingest_parser = sub.add_parser(
        "ingest",
        help="Split a bulk conversation export (ChatGPT/Anthropic) into individual inbox files",
    )
    ingest_parser.add_argument(
        "file",
        help="Path to a conversations JSON export",
    )
    ingest_parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Show what would be created without writing files",
    )

    # process
    process_parser = sub.add_parser(
        "process",
        help="Batch-process all unprocessed inbox files with progress display",
    )
    process_parser.add_argument(
        "--limit", "-n", type=int, default=None,
        help="Process only N files (for testing)",
    )
    process_parser.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Show what would be processed without running",
    )
    process_parser.add_argument(
        "--jobs", "-j", type=int, default=4,
        help="Number of concurrent workers (default: 4)",
    )

    # temporal
    temp = sub.add_parser("temporal", help="Temporal workflow engine (requires: pip install alfred-vault[temporal])")
    temp_sub = temp.add_subparsers(dest="temporal_cmd")
    temp_sub.add_parser("worker", help="Start the Temporal worker")
    temp_run = temp_sub.add_parser("run", help="Trigger a workflow")
    temp_run.add_argument("workflow_name")
    temp_run.add_argument("--params", default=None, help="JSON params")
    temp_run.add_argument("--id", default=None, help="Workflow ID")
    temp_sched = temp_sub.add_parser("schedule", help="Manage schedules")
    temp_sched_sub = temp_sched.add_subparsers(dest="schedule_cmd")
    temp_sched_register = temp_sched_sub.add_parser("register", help="Register from file")
    temp_sched_register.add_argument("file")
    temp_sched_sub.add_parser("list", help="List schedules")
    temp_sub.add_parser("list", help="List discovered workflows")

    # surveyor
    sub.add_parser("surveyor", help="Start surveyor pipeline")

    # tui
    sub.add_parser("tui", help="Launch interactive Ink TUI dashboard (requires Node.js)")

    # brief
    brief_p = sub.add_parser("brief", help="Morning brief subcommands")
    brief_sub = brief_p.add_subparsers(dest="brief_cmd")
    brief_gen = brief_sub.add_parser("generate", help="Generate a brief now")
    brief_gen.add_argument("--refresh", action="store_true", default=False, help="Overwrite today's existing brief")
    brief_sub.add_parser("weather", help="Update weather in today's brief with fresh data")
    brief_sub.add_parser("status", help="Show brief status")
    brief_hist = brief_sub.add_parser("history", help="Show brief history")
    brief_hist.add_argument("--limit", type=int, default=10)
    brief_sub.add_parser("watch", help="Daemon mode (generate on schedule)")

    # talker
    talker_p = sub.add_parser("talker", help="Telegram voice/text chat with Alfred")
    talker_sub = talker_p.add_subparsers(dest="talker_cmd")
    talker_watch = talker_sub.add_parser("watch", help="Start the Telegram bot daemon")
    talker_watch.add_argument(
        "--json", action="store_true", default=False,
        help=argparse.SUPPRESS,
    )
    talker_status = talker_sub.add_parser("status", help="Show active/closed session counts")
    talker_status.add_argument(
        "--json", action="store_true", default=False,
        help="Emit JSON instead of human-readable text",
    )
    talker_end = talker_sub.add_parser("end", help="Close an active session and write its vault record")
    talker_end.add_argument("chat_id", help="Telegram chat_id of the session to end")
    talker_end.add_argument(
        "--json", action="store_true", default=False,
        help="Emit JSON instead of human-readable text",
    )
    talker_history = talker_sub.add_parser("history", help="Show recent closed sessions")
    talker_history.add_argument("--limit", type=int, default=10)
    talker_history.add_argument(
        "--json", action="store_true", default=False,
        help="Emit JSON instead of human-readable text",
    )

    # bit — built-in test daemon
    bit_p = sub.add_parser("bit", help="Alfred built-in test (BIT) subcommands")
    bit_sub = bit_p.add_subparsers(dest="bit_cmd")
    bit_run = bit_sub.add_parser("run-now", help="Run one BIT sweep now and write a record")
    bit_run.add_argument("--json", action="store_true", default=False, help="Emit JSON")
    bit_status = bit_sub.add_parser("status", help="Show BIT schedule + latest run")
    bit_status.add_argument("--json", action="store_true", default=False, help="Emit JSON")
    bit_hist = bit_sub.add_parser("history", help="Show recent BIT runs")
    bit_hist.add_argument("--limit", type=int, default=10)
    bit_hist.add_argument("--json", action="store_true", default=False, help="Emit JSON")

    # mail
    mail_p = sub.add_parser("mail", help="Email fetcher subcommands")
    mail_sub = mail_p.add_subparsers(dest="mail_cmd")
    mail_fetch = mail_sub.add_parser("fetch", help="Fetch new emails from configured accounts")
    mail_fetch.add_argument("--once", action="store_true", default=False, help="Fetch once and exit (no polling)")
    mail_sub.add_parser("status", help="Show mail fetcher state")
    mail_webhook = mail_sub.add_parser("webhook", help="Start webhook receiver for incoming email")
    mail_webhook.add_argument("--port", type=int, default=5005, help="Port to listen on (default: 5005)")
    mail_webhook.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")

    return parser


def main() -> None:
    _load_env_file()

    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    handlers = {
        "quickstart": cmd_quickstart,
        "up": cmd_up,
        "down": cmd_down,
        "status": cmd_status,
        "curator": cmd_curator,
        "janitor": cmd_janitor,
        "distiller": cmd_distiller,
        "instructor": cmd_instructor,
        "transport": cmd_transport,
        "vault": cmd_vault,
        "exec": cmd_exec,
        "ingest": cmd_ingest,
        "process": cmd_process,
        "temporal": cmd_temporal,
        "surveyor": cmd_surveyor,
        "tui": cmd_tui,
        "brief": cmd_brief,
        "mail": cmd_mail,
        "talker": cmd_talker,
        "check": cmd_check,
        "bit": cmd_bit,
    }

    handler = handlers.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)
