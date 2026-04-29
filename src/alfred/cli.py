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


def _scan_entity_linking_coverage(vault_path: Path) -> dict:
    """Walk the vault once, tally related_* frontmatter coverage.

    Returns a summary dict suitable for both human-readable status
    output and machine-readable JSON telemetry. Groups matter counts
    by slug so a tenant can see per-matter link density at a glance.
    """
    from collections import Counter

    try:
        import frontmatter  # only imported here so `alfred status` still
                            # works in environments where frontmatter is
                            # not installed — they just lose the breakdown.
    except Exception:
        return {"available": False}

    totals = {
        "records_with_related_matters": 0,
        "records_with_related_persons": 0,
        "records_with_related_orgs": 0,
        "records_with_related_projects": 0,
        "records_with_any_related": 0,
        "unlinked_non_entity_records": 0,
        "total_records_scanned": 0,
    }
    per_matter: Counter = Counter()
    ENTITY_TYPES = {"matter", "person", "org", "project"}

    for md_path in vault_path.rglob("*.md"):
        if ".git" in md_path.parts:
            continue
        try:
            post = frontmatter.load(md_path)
        except Exception:
            continue
        md = post.metadata
        totals["total_records_scanned"] += 1
        record_type = md.get("type")
        if isinstance(record_type, list):
            record_type = record_type[0] if record_type else None

        touched_any = False
        for field, key in [
            ("related_matters", "records_with_related_matters"),
            ("related_persons", "records_with_related_persons"),
            ("related_orgs", "records_with_related_orgs"),
            ("related_projects", "records_with_related_projects"),
        ]:
            v = md.get(field)
            if isinstance(v, list) and v:
                totals[key] += 1
                touched_any = True
                if field == "related_matters":
                    for p in v:
                        if isinstance(p, str):
                            slug = p.rsplit("/", 1)[-1].removesuffix(".md")
                            per_matter[slug] += 1

        if touched_any:
            totals["records_with_any_related"] += 1
        elif record_type not in ENTITY_TYPES:
            totals["unlinked_non_entity_records"] += 1

    return {
        "available": True,
        **totals,
        "per_matter": dict(per_matter.most_common(20)),
    }


def cmd_status(args: argparse.Namespace) -> None:
    raw = _load_unified_config(args.config)

    # --json: emit a single machine-readable blob instead of the
    # printed status. Used by `alfred status --json | jq .surveyor…`.
    as_json = bool(getattr(args, "json", False))
    payload: dict[str, Any] = {}

    if not as_json:
        print("=" * 60)
        print("ALFRED STATUS")
        print("=" * 60)

    # Daemon status
    pid_path = _resolve_pid_path(raw)
    from alfred.daemon import check_already_running
    running_pid = check_already_running(pid_path)
    if as_json:
        payload["daemon"] = {"running": bool(running_pid), "pid": running_pid}
    elif running_pid:
        print(f"Daemon: running (pid {running_pid})")
    else:
        print("Daemon: not running")

    # Per-tool status. Skip tools whose config block is absent —
    # mirrors the orchestrator's auto-start gate so a per-instance
    # config that doesn't enable a tool doesn't surface an
    # "(unavailable: ...)" line that's actually a state-file collision
    # against another tool's default. Item 4 from the state-path
    # collision sweep (KAL-LE P0 review).
    def _has_block(name: str) -> bool:
        return isinstance(raw.get(name), dict)

    # Curator status
    curator_info: dict = {}
    if not _has_block("curator"):
        curator_info = {"note": "no config block"}
    else:
        try:
            from alfred.curator.config import load_from_unified as curator_cfg
            cfg = curator_cfg(raw)
            from alfred.curator.state import StateManager
            sm = StateManager(cfg.state.path)
            sm.load()
            curator_info = {
                "processed_files": len(sm.state.processed),
                "last_run": sm.state.last_run or None,
            }
        except Exception as e:
            curator_info = {"error": str(e)}
    if as_json:
        payload["curator"] = curator_info
    else:
        print("\n--- Curator ---")
        if "note" in curator_info:
            print(f"  ({curator_info['note']})")
        elif "error" in curator_info:
            print(f"  (unavailable: {curator_info['error']})")
        else:
            print(f"  Processed files: {curator_info['processed_files']}")
            print(f"  Last run: {curator_info['last_run'] or 'never'}")

    # Janitor status
    janitor_info: dict = {}
    if not _has_block("janitor"):
        janitor_info = {"note": "no config block"}
    else:
        try:
            from alfred.janitor.config import load_from_unified as janitor_cfg
            cfg = janitor_cfg(raw)
            from alfred.janitor.state import JanitorState
            st = JanitorState(cfg.state.path, cfg.state.max_sweep_history)
            st.load()
            files_with_issues = sum(1 for fs in st.files.values() if fs.open_issues)
            janitor_info = {
                "tracked_files": len(st.files),
                "files_with_issues": files_with_issues,
                "sweeps_recorded": len(st.sweeps),
            }
        except Exception as e:
            janitor_info = {"error": str(e)}
    if as_json:
        payload["janitor"] = janitor_info
    else:
        print("\n--- Janitor ---")
        if "note" in janitor_info:
            print(f"  ({janitor_info['note']})")
        elif "error" in janitor_info:
            print(f"  (unavailable: {janitor_info['error']})")
        else:
            print(f"  Tracked files: {janitor_info['tracked_files']}")
            print(f"  Files with issues: {janitor_info['files_with_issues']}")
            print(f"  Sweeps recorded: {janitor_info['sweeps_recorded']}")

    # Distiller status
    distiller_info: dict = {}
    if not _has_block("distiller"):
        distiller_info = {"note": "no config block"}
    else:
        try:
            from alfred.distiller.config import load_from_unified as distiller_cfg
            cfg = distiller_cfg(raw)
            from alfred.distiller.state import DistillerState
            st = DistillerState(cfg.state.path, cfg.state.max_run_history)
            st.load()
            total_learns = sum(len(fs.learn_records_created) for fs in st.files.values())
            distiller_info = {
                "tracked_source_files": len(st.files),
                "learn_records_created": total_learns,
                "runs_recorded": len(st.runs),
            }
        except Exception as e:
            distiller_info = {"error": str(e)}
    if as_json:
        payload["distiller"] = distiller_info
    else:
        print("\n--- Distiller ---")
        if "note" in distiller_info:
            print(f"  ({distiller_info['note']})")
        elif "error" in distiller_info:
            print(f"  (unavailable: {distiller_info['error']})")
        else:
            print(f"  Tracked source files: {distiller_info['tracked_source_files']}")
            print(f"  Learn records created: {distiller_info['learn_records_created']}")
            print(f"  Runs recorded: {distiller_info['runs_recorded']}")

    # Surveyor status + entity-linking telemetry (#26)
    surveyor_info: dict = {}
    if not _has_block("surveyor"):
        surveyor_info = {"note": "no config block"}
    else:
        try:
            from alfred.surveyor.config import load_from_unified as surveyor_cfg
            scfg = surveyor_cfg(raw)
            from alfred.surveyor.state import PipelineState
            st = PipelineState(scfg.state.path)
            st.load()
            surveyor_info = {
                "tracked_files": len(st.files),
                "clusters": len(st.clusters),
                "last_run": st.last_run or None,
            }
            # Walk vault frontmatter once for coverage stats. Only run on
            # --json or when vault is small enough that the full scan stays
            # fast — for a 3500-record vault this is ~2s, acceptable.
            vault_cfg = raw.get("vault", {}) or {}
            vault_path_str = vault_cfg.get("path") or os.environ.get("ALFRED_VAULT_PATH")
            if vault_path_str:
                vault_path = Path(vault_path_str).expanduser().resolve()
                if vault_path.is_dir():
                    surveyor_info["entity_linking"] = _scan_entity_linking_coverage(vault_path)
        except Exception as e:
            surveyor_info = {"error": str(e)}
    if as_json:
        payload["surveyor"] = surveyor_info
    else:
        print("\n--- Surveyor ---")
        if "note" in surveyor_info:
            print(f"  ({surveyor_info['note']})")
        elif "error" in surveyor_info:
            print(f"  (unavailable: {surveyor_info['error']})")
        else:
            print(f"  Tracked files: {surveyor_info['tracked_files']}")
            print(f"  Clusters: {surveyor_info['clusters']}")
            print(f"  Last run: {surveyor_info['last_run'] or 'never'}")
            el = surveyor_info.get("entity_linking", {})
            if el.get("available"):
                print(f"  Entity linking:")
                print(f"    Records scanned:       {el['total_records_scanned']}")
                print(f"    Any related_* field:   {el['records_with_any_related']}")
                print(f"    related_matters:       {el['records_with_related_matters']}")
                print(f"    related_persons:       {el['records_with_related_persons']}")
                print(f"    related_orgs:          {el['records_with_related_orgs']}")
                print(f"    related_projects:      {el['records_with_related_projects']}")
                print(f"    Non-entity unlinked:   {el['unlinked_non_entity_records']}")
                top = el.get("per_matter", {})
                if top:
                    print(f"  Top matters by link count:")
                    for slug, n in list(top.items())[:10]:
                        print(f"    {slug:<50} {n:>4}")

    # Instructor status — only show if config section exists, mirroring
    # the orchestrator's auto-start gate.
    if "instructor" in raw and not as_json:
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
    if "telegram" in raw and not as_json:
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

    if as_json:
        print(json.dumps(payload, indent=2, default=str))
    else:
        print()


def cmd_curator(args: argparse.Namespace) -> None:
    import asyncio
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="curator")
    from alfred.curator.config import load_from_unified
    from alfred.email_classifier.config import load_from_unified as load_classifier
    config = load_from_unified(raw)
    classifier_config = load_classifier(raw)
    from alfred.curator.daemon import run
    from alfred._data import get_skills_dir
    try:
        asyncio.run(run(config, get_skills_dir(), email_classifier_config=classifier_config))
    except KeyboardInterrupt:
        print("\nStopped.")


def cmd_email_classifier(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred email-classifier`` subcommands.

    Currently exposes ``backfill`` only — runs the c1 classifier against
    every email-derived note in ``vault/note/`` that's missing a
    ``priority`` frontmatter field. Safe to re-run; resumable; ``--dry-run``
    + ``--limit N`` flags for safety on first invocation.
    """
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="email_classifier")

    subcmd = getattr(args, "email_classifier_cmd", None)
    if subcmd != "backfill":
        print("Usage: alfred email-classifier backfill [--dry-run] [--limit N]")
        sys.exit(1)

    from alfred.email_classifier import EmailClassifierConfig, run_backfill
    from alfred.email_classifier.config import load_from_unified as load_classifier
    config: EmailClassifierConfig = load_classifier(raw)
    if not config.enabled:
        print(
            "email_classifier is not enabled in this config "
            "(missing/disabled email_classifier: block). Aborting backfill.",
        )
        sys.exit(1)

    vault_cfg = raw.get("vault", {}) or {}
    vault_path_str = vault_cfg.get("path")
    if not vault_path_str:
        print("vault.path not set in config. Aborting backfill.")
        sys.exit(1)
    vault_path = Path(vault_path_str)
    if not vault_path.is_dir():
        print(f"vault path does not exist: {vault_path}. Aborting backfill.")
        sys.exit(1)

    summary = run_backfill(
        vault_path=vault_path,
        config=config,
        dry_run=args.dry_run,
        limit=args.limit,
    )

    print()
    print("=== Email-classifier backfill summary ===")
    if args.dry_run:
        print(f"  candidates (would classify): {summary.candidates}")
    else:
        print(f"  classified:                   {summary.classified}")
    print(f"  skipped (already classified): {summary.skipped_already_done}")
    print(f"  skipped (not email-derived):  {summary.skipped_not_email}")
    print(f"  errors:                       {summary.errors}")
    print(f"  elapsed seconds:              {summary.elapsed_seconds:.1f}")
    if summary.error_paths:
        print()
        print("Errored paths (first 10):")
        for p in summary.error_paths[:10]:
            print(f"  - {p}")


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
    elif subcmd == "backfill":
        dcli.cmd_backfill(config, source=args.source, dry_run=args.dry_run)
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
    if subcmd == "tail":
        sys.exit(tcli.cmd_tail(
            raw,
            peer=getattr(args, "peer", None),
            limit=getattr(args, "limit", 50),
            wants_json=wants_json,
        ))
    if subcmd == "propose-person":
        sys.exit(tcli.cmd_propose_person(
            raw,
            peer=args.peer,
            name=args.name,
            fields=list(getattr(args, "field", []) or []),
            source=getattr(args, "source", ""),
            self_name=getattr(args, "self_name", "kal-le"),
            wants_json=wants_json,
        ))

    print(
        "Usage: alfred transport "
        "{status|send-test|queue|dead-letter|rotate|tail|propose-person}"
    )
    sys.exit(1)


def cmd_reviews(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred reviews`` subcommands.

    JSON-on-stdout — same contract as ``alfred vault``. Logs are routed
    to the file sink so the JSON output stays clean.
    """
    try:
        raw = _load_unified_config(args.config)
        _setup_logging_from_config(raw, tool="reviews", suppress_stdout=True)
    except SystemExit:
        raw = {}
    except Exception:
        raw = {}
    from alfred.reviews import cli as rcli
    sys.exit(rcli.dispatch(raw, args))


def cmd_digest(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred digest`` subcommands."""
    try:
        raw = _load_unified_config(args.config)
        _setup_logging_from_config(raw, tool="digest", suppress_stdout=True)
    except SystemExit:
        raw = {}
    except Exception:
        raw = {}
    from alfred.digest import cli as dcli
    sys.exit(dcli.dispatch(raw, args))


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
    # Dispatch to relink subcommand if specified; otherwise default to
    # running the daemon (preserves `alfred surveyor` legacy behaviour).
    subcmd = getattr(args, "surveyor_cmd", None)
    if subcmd == "relink":
        return cmd_surveyor_relink(args)
    # `run` and None both start the daemon.
    return cmd_surveyor_run(args)


def cmd_surveyor_run(args: argparse.Namespace) -> None:
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


def cmd_audit(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred audit`` subcommands.

    Currently exposes only ``infer-marker`` (calibration audit gap c3
    retroactive sweep). Future commands (``list``, etc.) plug into the
    same dispatcher.
    """
    from alfred.audit import cli as audit_cli

    code = audit_cli.dispatch(args)
    sys.exit(code)


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
    filter_peer = getattr(args, "peer", None)
    tools: list[str] | None = None
    if filter_tools:
        tools = [t.strip() for t in filter_tools.split(",") if t.strip()]

    from alfred.health.aggregator import run_all_checks
    from alfred.health.renderer import render_human, render_json
    from alfred.health.types import Status, HealthReport

    if filter_peer:
        # Peer-filtered check: bypass the full aggregator and invoke
        # the transport health-check directly with filter_peer.
        from alfred.transport.health import health_check as transport_health
        from datetime import datetime, timezone

        started_dt = datetime.now(timezone.utc)
        th = asyncio.run(
            transport_health(raw, mode=mode, filter_peer=filter_peer),
        )
        finished_dt = datetime.now(timezone.utc)
        report = HealthReport(
            mode=mode,
            started_at=started_dt.isoformat(),
            finished_at=finished_dt.isoformat(),
            overall_status=th.status,
            tools=[th],
            elapsed_ms=(finished_dt - started_dt).total_seconds() * 1000.0,
        )
    else:
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


def cmd_instance(args: argparse.Namespace) -> None:
    """Stage 3.5: scaffold a new Alfred instance (config + dirs + BotFather checklist).

    Subcommand: ``alfred instance new <name>``.

    Creates:
      - ``config.<name>.yaml`` in the current directory, rendered from
        the universal per-instance template
        (``config.instance.yaml.example``). The template carries the
        subordinate default shape (talker + transport + instructor);
        optional blocks like ``email_classifier``, ``daily_sync``, and
        ``brief.peer_digests`` ship commented-out and can be
        uncommented per instance. For a primary-like instance, see
        ``config.yaml.example`` instead.
      - ``/home/andrew/.alfred/<name>/data/`` directory
      - ``/home/andrew/.alfred/<name>/logs/`` directory

    Prints a BotFather checklist to stdout with the exact env vars
    that need setting. Does NOT write to .env (that's manual — the
    user has to set real token values).
    """
    subcmd = getattr(args, "instance_cmd", None)
    if subcmd != "new":
        print("Usage: alfred instance new <name>")
        sys.exit(1)

    name = args.instance_name.strip().lower()
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        print(
            f"Invalid instance name {name!r} — must be lowercase "
            "alphanumeric with -/_."
        )
        sys.exit(1)

    # Where configs live relative to the cwd at invocation time.
    config_path = Path(f"config.{name}.yaml")
    if config_path.exists() and not getattr(args, "force", False):
        print(
            f"{config_path} already exists. Re-run with --force to overwrite, "
            "or remove it first."
        )
        sys.exit(1)

    instance_dir = Path(f"/home/andrew/.alfred/{name}")
    data_dir = instance_dir / "data"
    logs_dir = instance_dir / "logs"

    # Locate the universal per-instance template. Ships as
    # config.instance.yaml.example next to config.yaml.example.
    # Accepts the legacy name config.kalle.yaml.example as a fallback so
    # in-place upgrades don't break.
    repo_root = Path(__file__).resolve().parent.parent.parent
    candidates = [
        Path("config.instance.yaml.example"),
        Path("config.kalle.yaml.example"),
        repo_root / "config.instance.yaml.example",
        repo_root / "config.kalle.yaml.example",
    ]
    template_path = next((p for p in candidates if p.exists()), None)
    if template_path is None:
        print(
            "Can't find config.instance.yaml.example (or legacy "
            "config.kalle.yaml.example) — are you running from the "
            "alfred project root?"
        )
        sys.exit(1)

    # Load the template, substitute the name token-ish — the template
    # is KAL-LE-shaped, so we do a light rename pass for STAY-C etc.
    # but primarily this is "copy + rename paths".
    template = template_path.read_text(encoding="utf-8")
    # Naive replacement — the template uses KAL-LE/kalle literals.
    # Good enough for scaffolding; operator tunes the identity block.
    substituted = (
        template
        .replace("/home/andrew/.alfred/kalle/", f"/home/andrew/.alfred/{name}/")
        .replace("TELEGRAM_KALLE_BOT_TOKEN", f"TELEGRAM_{name.upper().replace('-', '_')}_BOT_TOKEN")
        .replace("ALFRED_KALLE_TRANSPORT_TOKEN", f"ALFRED_{name.upper().replace('-', '_')}_TRANSPORT_TOKEN")
        .replace("ALFRED_KALLE_PEER_TOKEN", f"ALFRED_{name.upper().replace('-', '_')}_PEER_TOKEN")
    )

    # Create directories.
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Couldn't create {instance_dir}: {exc}")
        sys.exit(1)

    # Write the config file.
    try:
        config_path.write_text(substituted, encoding="utf-8")
    except OSError as exc:
        print(f"Couldn't write {config_path}: {exc}")
        sys.exit(1)

    # BotFather checklist.
    env_prefix = name.upper().replace("-", "_")
    print(f"Scaffolded instance {name!r}:")
    print(f"  config:     {config_path}")
    print(f"  data dir:   {data_dir}")
    print(f"  logs dir:   {logs_dir}")
    print()
    print("Next steps (manual):")
    print("  1. BotFather: @BotFather on Telegram → /newbot → capture token.")
    print("  2. Add to .env (generate each with")
    print("       python -c 'import secrets; print(secrets.token_hex(32))'):")
    print(f"       TELEGRAM_{env_prefix}_BOT_TOKEN=<BotFather token>")
    print(f"       ALFRED_{env_prefix}_TRANSPORT_TOKEN=<64-char hex>")
    print(f"       ALFRED_{env_prefix}_PEER_TOKEN=<64-char hex>")
    print(f"       ALFRED_SALEM_PEER_TOKEN=<64-char hex (if not already set)>")
    print(f"  3. Open Telegram, /start the new bot once.")
    print(f"  4. Review and tune {config_path} — port, instance name,")
    print(f"     allowed_users, vault.path. Uncomment optional blocks")
    print(f"     at the bottom (email_classifier / daily_sync /")
    print(f"     brief.peer_digests) if this instance needs them.")
    print(f"  5. Create a venv at {instance_dir}/.venv and install alfred:")
    print(f"       python -m venv {instance_dir}/.venv")
    print(f"       source {instance_dir}/.venv/bin/activate")
    print(f"       pip install -e {Path.cwd()}")
    print(f"  6. Launch:")
    print(f"       alfred --config {config_path} up --only talker,instructor,brief_digest_push")


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


def cmd_surveyor_relink(args: argparse.Namespace) -> None:
    """One-off entity-link pass across ALL clusters + noise points.

    Rebuilds cluster membership from the current Milvus embeddings, then
    runs the same entity-linking logic the daemon would run — but across
    every semantic cluster instead of only the `changed_semantic` subset.

    No re-embedding, no re-clustering (in the sense of recomputing labels),
    no LLM calls. Pure numpy cosine + VaultWriter frontmatter appends.
    Existing links are preserved — the writer appends new entries to the
    related_* lists, it never removes.

    Intended for one-time use after surveyor v2 rollout to densify link
    coverage on a vault whose state shows most clusters unlabeled because
    only the `changed` subset ever got processed.
    """
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw)

    try:
        from alfred.surveyor.config import load_from_unified
        from alfred.surveyor.daemon import Daemon
        from alfred.surveyor.parser import parse_file
    except ImportError as e:
        print(f"Surveyor dependencies not installed: {e}")
        sys.exit(1)

    import asyncio

    async def _run() -> int:
        config = load_from_unified(raw)
        daemon = Daemon(config)
        daemon.state.load()

        # Pull every embedding currently in Milvus.
        embedding_data = daemon.embedder.get_all_embeddings()
        if embedding_data is None:
            print("No embeddings found — run `alfred surveyor run` first to embed the vault.")
            return 1
        paths, vectors = embedding_data
        print(f"Loaded {len(paths)} embeddings from Milvus.")

        # Parse every record so we know record_type for each path. We need
        # this to tell entities from regulars in the link pass.
        records = {}
        for rel in paths:
            try:
                records[rel] = parse_file(config.vault.path, rel)
            except Exception:
                continue
        print(f"Parsed {len(records)} records.")

        # Reconstruct cluster membership purely from embeddings — we can't
        # rely on state.files[rel].semantic_cluster_id alone because fresh
        # tenants or post-migration vaults may not have those set. So run
        # the clusterer against the current embeddings. HDBSCAN is cheap
        # (~1-3s on 3500 vectors) and we need a valid cluster map anyway.
        result = daemon.clusterer.run(paths, vectors, records)
        cluster_members: dict[int, list[str]] = {}
        for p, cid in result.semantic.items():
            if cid == -1:
                continue
            cluster_members.setdefault(cid, []).append(p)
        noise_paths = [p for p, cid in result.semantic.items() if cid == -1]

        total_clusters = len(cluster_members)
        print(
            f"Clustering: {total_clusters} semantic clusters, "
            f"{len(noise_paths)} noise points."
        )

        if getattr(args, "dry_run", False):
            # Tally how many would be touched without actually writing.
            from alfred.surveyor.labeler import ENTITY_RECORD_TYPES
            with_entities = 0
            with_regulars = 0
            for cid, members in cluster_members.items():
                has_e = any(
                    records[m].record_type in ENTITY_RECORD_TYPES
                    for m in members if m in records
                )
                has_r = any(
                    records[m].record_type not in ENTITY_RECORD_TYPES
                    for m in members if m in records
                )
                if has_e: with_entities += 1
                if has_r and has_e: with_regulars += 1
            print(
                f"[dry-run] {with_entities} clusters contain >=1 entity record; "
                f"{with_regulars} of those would have links written."
            )
            return 0

        # Optional threshold override: some vaults benefit from 0.65 over
        # the default 0.75, especially when matter records are short
        # (structured frontmatter + one-paragraph description) vs.
        # long-form events + transcripts they should link to.
        if getattr(args, "threshold", None) is not None:
            daemon.cfg.entity_link.threshold = float(args.threshold)
            print(f"Threshold override: {daemon.cfg.entity_link.threshold}")

        # Process ALL semantic clusters (not just changed_semantic).
        all_cluster_ids = set(cluster_members.keys())
        print(f"Running entity linking across {total_clusters} clusters…")
        daemon._link_entities_in_clusters(
            all_cluster_ids, cluster_members, records, paths, vectors,
        )

        # And every noise point.
        if noise_paths:
            print(f"Running noise-point linking across {len(noise_paths)} records…")
            daemon._link_noise_points_to_entities(
                noise_paths, records, paths, vectors,
            )

        # Full-vault entity backfill. This is the critical densification
        # pass: for each existing entity (not just new ones from diff.new
        # like #25 does in steady state), walk every non-entity record in
        # the vault and link above threshold regardless of cluster. Catches
        # the common case where a matter M is topically close to a cluster
        # C it isn't actually a member of — cluster-scoped linking misses
        # that relationship forever.
        if not getattr(args, "no_backfill", False):
            from alfred.surveyor.labeler import ENTITY_RECORD_TYPES
            all_entity_paths = [
                p for p, r in records.items()
                if r.record_type in ENTITY_RECORD_TYPES
            ]
            by_type: dict[str, int] = {}
            for p in all_entity_paths:
                t = records[p].record_type
                by_type[t] = by_type.get(t, 0) + 1
            print(
                f"Running full-vault backfill across {len(all_entity_paths)} entities "
                f"({', '.join(f'{v} {k}s' for k, v in sorted(by_type.items()))})…"
            )
            daemon._backfill_new_entities(
                all_entity_paths, records, paths, vectors,
            )

        # Persist updated state (the writer marked frontmatter writes in
        # state.pending_writes; save so the watcher ignores them).
        daemon.state.save()

        print("\nDone. Check `alfred status --json | jq .surveyor.entity_linking`.")
        return 0

    try:
        rc = asyncio.run(_run())
        sys.exit(rc)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)


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
    status_p = sub.add_parser("status", help="Show status from all tools")
    status_p.add_argument(
        "--json", action="store_true", default=False,
        help="Emit a machine-readable JSON blob instead of printed output.",
    )

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
    check_p.add_argument(
        "--peer", default=None,
        help=(
            "Run only the per-peer probes for the named peer "
            "(Stage 3.5 — e.g. --peer kal-le). Skips the local "
            "transport probes."
        ),
    )

    # curator
    sub.add_parser("curator", help="Start curator daemon")

    # email-classifier (backfill + future operational subcommands)
    ec = sub.add_parser(
        "email-classifier",
        help="Email classifier subcommands (backfill — c1.5 retroactive run)",
    )
    ec_sub = ec.add_subparsers(dest="email_classifier_cmd")
    ec_backfill = ec_sub.add_parser(
        "backfill",
        help="Classify existing email-derived notes lacking a priority field",
    )
    ec_backfill.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Count candidates without making LLM calls or writing frontmatter",
    )
    ec_backfill.add_argument(
        "--limit", type=int, default=None,
        help="Cap the number of records actually classified (skips don't count)",
    )

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
    # One-time backfill: extract learn records from an external source dir
    # (e.g. KAL-LE distiller-radar Phase 1 over Salem's vault/session/).
    # Source files are read-only; learn records land in the configured
    # vault path; processed source paths are tracked in
    # distiller_backfill_state.json (sibling of distiller_state.json).
    dist_backfill = dist_sub.add_parser(
        "backfill",
        help="One-time extraction over an external source directory",
    )
    dist_backfill.add_argument(
        "--source", required=True,
        help="Absolute path to a source directory of *.md session/note files",
    )
    dist_backfill.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Report eligible files + counts without extracting or writing",
    )

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
    surv = sub.add_parser("surveyor", help="Surveyor pipeline operations")
    surv_sub = surv.add_subparsers(dest="surveyor_cmd")
    surv_sub.add_parser("run", help="Start surveyor daemon (same as bare `surveyor`)")
    surv_relink = surv_sub.add_parser(
        "relink",
        help="One-off re-run of entity linking across ALL semantic clusters + noise points "
             "+ full-vault entity backfill, using current embeddings. Does NOT re-embed, "
             "re-cluster, or re-label — only writes related_* frontmatter. Preserves "
             "existing links (writer appends).",
    )
    surv_relink.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Scan + report, don't write frontmatter",
    )
    surv_relink.add_argument(
        "--no-backfill", action="store_true", default=False,
        help="Skip the full-vault backfill pass (only walk clusters + noise)",
    )
    surv_relink.add_argument(
        "--threshold", type=float, default=None,
        help="Override entity_link.threshold for this run only (e.g. 0.65 for denser links)",
    )

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

    # instance — Stage 3.5 multi-instance scaffolding
    instance_p = sub.add_parser(
        "instance",
        help="Multi-instance scaffolding (new, list — Stage 3.5)",
    )
    instance_sub = instance_p.add_subparsers(dest="instance_cmd")
    inst_new = instance_sub.add_parser(
        "new", help="Scaffold a new instance (config + data dirs)",
    )
    inst_new.add_argument("instance_name", help="Lowercase instance name, e.g. kalle, stayc")
    inst_new.add_argument(
        "--force", action="store_true", default=False,
        help="Overwrite an existing config.<name>.yaml",
    )

    # mail
    mail_p = sub.add_parser("mail", help="Email fetcher subcommands")
    mail_sub = mail_p.add_subparsers(dest="mail_cmd")
    mail_fetch = mail_sub.add_parser("fetch", help="Fetch new emails from configured accounts")
    mail_fetch.add_argument("--once", action="store_true", default=False, help="Fetch once and exit (no polling)")
    mail_sub.add_parser("status", help="Show mail fetcher state")
    mail_webhook = mail_sub.add_parser("webhook", help="Start webhook receiver for incoming email")
    mail_webhook.add_argument("--port", type=int, default=5005, help="Port to listen on (default: 5005)")
    mail_webhook.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")

    # audit (calibration audit gap, c3 retroactive sweep CLI)
    from alfred.audit import cli as audit_cli
    audit_cli.build_parser(sub)

    # reviews — KAL-LE per-project review files
    from alfred.reviews import cli as reviews_cli
    reviews_cli.build_subparser(sub)

    # digest — KAL-LE cross-project weekly synthesis
    from alfred.digest import cli as digest_cli
    digest_cli.build_subparser(sub)

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
        "email-classifier": cmd_email_classifier,
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
        "instance": cmd_instance,
        "talker": cmd_talker,
        "check": cmd_check,
        "bit": cmd_bit,
        "audit": cmd_audit,
        "reviews": cmd_reviews,
        "digest": cmd_digest,
    }

    handler = handlers.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)
