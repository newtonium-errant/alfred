"""Top-level argparse CLI dispatcher for Alfred."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def _load_env_file(env_path: Path | None = None) -> None:
    """Load a .env file into os.environ (without overriding existing vars).

    Thin shim over the canonical ``alfred._env.auto_load_dotenv`` so
    parser semantics stay byte-identical with the orchestrator's
    ``_auto_load_dotenv_for_config`` path. Pre-consolidation
    (2026-05-05), this function had its own ``KEY=VALUE`` parser that
    silently set ``os.environ["export FOO"] = "bar"`` for every
    ``export FOO=bar`` line — same .env file produced two different
    environments depending on which loader fired first. See
    ``orchestrator._auto_load_dotenv_for_config`` for the canonical
    contract (path resolution, override=False semantics, structured
    logs). This loader stays CWD-defaulted because cli.py doesn't have
    a config path until argparse runs; the orchestrator path picks up
    anything cli.py missed (e.g. when ``--config`` points at a
    non-CWD directory) via ``override=False`` — both fire, second one
    fills gaps, neither clobbers parent-shell exports.
    """
    from alfred._env import auto_load_dotenv

    if env_path is None:
        env_path = Path(".env")
    auto_load_dotenv(env_path, override=False)


def _load_unified_config(config_path: str) -> dict[str, Any]:
    """Load and return raw unified config dict.

    The resolved absolute path is stamped onto the dict as a synthetic
    ``_config_path`` key so subprocess daemons (talker etc.) can re-read
    the SAME file when they need to lazy-load a sibling config block
    (e.g. ``transport`` from inside the talker conversation loop). Without
    this, lazy loaders default to ``config.yaml`` and a Hypatia daemon
    launched with ``--config config.hypatia.yaml`` silently picks up
    Salem's config — see ``TalkerConfig.config_path`` for the rationale.
    """
    path = Path(config_path)
    if not path.exists():
        print(f"Config file not found: {path}")
        print("Run `alfred quickstart` to create one.")
        sys.exit(1)
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    raw["_config_path"] = str(path.resolve())
    return raw


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

    Rotation kwargs (``max_bytes`` / ``backup_count``) are pulled from
    ``logging.rotation`` in the unified config and threaded through —
    same plumbing as the orchestrator's per-daemon runners, so CLI
    invocations and daemon runs honor the same rotation policy.
    """
    log_cfg = raw.get("logging", {})
    level = log_cfg.get("level", "INFO")
    log_dir = log_cfg.get("dir", "./data")
    from alfred.curator.utils import setup_logging
    from alfred.common.logging_handler import extract_rotation_config
    max_bytes, backup_count = extract_rotation_config(log_cfg)
    setup_logging(
        level=level,
        log_file=f"{log_dir}/{tool}.log",
        suppress_stdout=suppress_stdout,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


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

    # Optional schema-check gate — ``alfred up --check-schemas`` runs
    # the Anthropic tool-schema validator before spawning daemons.
    # Closes the same bug class as ``alfred check-tool-schemas`` but at
    # the operator's natural deploy point (right before restart) rather
    # than as a separate manual step. Per the 2026-05-05 oneOf-at-top-
    # level P0: schemas can pass every local test and STILL get rejected
    # by Anthropic's server-side validator on first real conversation,
    # 36 hours after restart. The ``count_tokens`` probe is zero-cost
    # and surfaces the rejection in 1-2 seconds.
    #
    # Exit-code mapping from ``_run_schema_check``:
    #   0  all tools accepted → continue spawning daemons
    #   1  schema rejected   → BLOCK restart (the bug we're catching)
    #   2  fatal infra error → log + continue (network blip / no api_key
    #                          shouldn't block daemon startup; the
    #                          standalone ``check-tool-schemas`` exit-2
    #                          is preserved for scripts that want
    #                          fatal-vs-rejected distinction).
    #
    # Opt-in (mirrors ``--preflight``) — not default-on because it
    # requires an Anthropic API round-trip; some test/dev environments
    # don't have api_key configured. Operators in production should
    # adopt the flag in their restart wrapper / systemd unit.
    if getattr(args, "check_schemas", False):
        print("Pre-restart tool-schema validation...")
        rc = _run_schema_check(raw)
        if rc == 1:
            print(
                "\nSchema check FAILED — not starting daemons. "
                "Fix the rejected schemas above, then re-run."
            )
            sys.exit(1)
        if rc == 2:
            # Fatal infra: warn but continue. The operator still wants
            # the daemon up; the schema check failing to RUN doesn't
            # mean the schemas themselves are broken. The standalone
            # ``alfred check-tool-schemas`` subcommand surfaces this
            # explicitly when the operator wants the strict version.
            print(
                "\nSchema check could not run (infra issue, see above). "
                "Continuing daemon startup; re-run "
                "``alfred check-tool-schemas`` later to verify."
            )
        else:
            print()

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
        print(
            "Usage: alfred email-classifier backfill "
            "[--dry-run] [--limit N] [--reclassify]"
        )
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
        reclassify=args.reclassify,
    )

    print()
    print("=== Email-classifier backfill summary ===")
    if args.reclassify:
        print(f"  mode:                         reclassify (overwrite existing priority)")
    if args.dry_run:
        print(f"  candidates (would classify): {summary.candidates}")
    else:
        print(f"  classified:                   {summary.classified}")
    print(f"  skipped (already classified): {summary.skipped_already_done}")
    print(f"  skipped (not email-derived):  {summary.skipped_not_email}")
    if args.reclassify:
        # Verdict-change count is only meaningful in reclassify mode;
        # in default mode it's always zero (no record gets a second look).
        print(f"  verdict changes:              {summary.reclassified_verdict_changes}")
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

    # Resolve audit-log path so promote-proposal / discard-proposal
    # can write to the unified vault audit log. Mirrors the cmd_vault
    # (issue #64) precedent: ``<logging.dir>/vault_audit.log`` from
    # the unified config. Respect a caller-set
    # ``ALFRED_VAULT_AUDIT_LOG`` override (test harnesses, one-off
    # invocations). Only resolve on subcommands that actually mutate
    # — other distiller subcommands don't need the audit context.
    #
    # V1 of the env-var → function-arg refactor: build a
    # :class:`VaultContext` and pass to the handlers; also mirror to
    # env for backward-compat with not-yet-migrated consumers. V2
    # drops the env-var write.
    from alfred.vault.context import (
        ENV_VAULT_AUDIT_LOG,
        ENV_VAULT_PATH,
        ENV_VAULT_SCOPE,
        ENV_VAULT_SESSION,
        VaultContext,
    )

    distiller_ctx: VaultContext | None = None
    if args.distiller_cmd in ("promote-proposal", "discard-proposal"):
        log_cfg = raw.get("logging", {}) or {}
        log_dir = log_cfg.get("dir", "./data")
        resolved_audit = os.environ.get(ENV_VAULT_AUDIT_LOG) or str(
            Path(log_dir) / "vault_audit.log"
        )
        if not os.environ.get(ENV_VAULT_AUDIT_LOG):
            # Mirror to env for backward-compat (V1).
            os.environ[ENV_VAULT_AUDIT_LOG] = resolved_audit
        distiller_ctx = VaultContext(
            vault_path=os.environ.get(ENV_VAULT_PATH) or None,
            scope=os.environ.get(ENV_VAULT_SCOPE) or None,
            session_path=os.environ.get(ENV_VAULT_SESSION) or None,
            audit_log_path=resolved_audit,
        )

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
    elif subcmd == "rank-week":
        dcli.cmd_rank_week(
            config,
            top_n=args.top_n,
            window_days=args.window_days,
            dry_run=args.dry_run,
        )
    elif subcmd == "rank-day":
        dcli.cmd_rank_day(
            config,
            top_n=args.top_n,
            min_score=args.min_score,
            digests_dir=args.digests_dir,
            state_dir=args.state_dir,
            dry_run=args.dry_run,
        )
    elif subcmd == "mine-patterns":
        dcli.cmd_mine_patterns(
            config,
            config_path=args.config,
            dry_run=args.dry_run,
            min_cluster_size=args.min_cluster_size,
            top=args.top,
        )
    elif subcmd == "promote-proposal":
        dcli.cmd_promote_proposal(
            config,
            slug=args.slug,
            to=args.to,
            strip_scaffolding=not args.no_strip_scaffolding,
            fingerprint=args.fingerprint,
            vault_context=distiller_ctx,
        )
    elif subcmd == "discard-proposal":
        dcli.cmd_discard_proposal(
            config,
            slug=args.slug,
            reason=args.reason,
            fingerprint=args.fingerprint,
            vault_context=distiller_ctx,
        )
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
        # ``--self`` defaults to None at the argparse layer (per the
        # Tier A #1.1 hardcoded-literal sweep); if absent, derive the
        # self-name from the loaded config's ``telegram.instance.name``
        # so the dispatcher fails loud when neither the CLI flag nor
        # the config supplies one, instead of silently impersonating a
        # specific instance via a hardcoded fallback. See
        # feedback_hardcoding_and_alfred_naming.md.
        self_name_arg = getattr(args, "self_name", None)
        if not self_name_arg:
            from alfred.transport.health import _infer_self_name
            try:
                self_name_arg = _infer_self_name(raw)
            except RuntimeError as exc:
                print(
                    "alfred transport propose-person: --self not given "
                    "and could not derive from config: "
                    f"{exc}",
                    file=sys.stderr,
                )
                sys.exit(2)
        sys.exit(tcli.cmd_propose_person(
            raw,
            peer=args.peer,
            name=args.name,
            fields=list(getattr(args, "field", []) or []),
            source=getattr(args, "source", ""),
            self_name=self_name_arg,
            wants_json=wants_json,
        ))

    print(
        "Usage: alfred transport "
        "{status|send-test|queue|dead-letter|rotate|tail|propose-person}"
    )
    sys.exit(1)


def cmd_gcal(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred gcal`` subcommands.

    Phase A+ inter-instance comms: Google Calendar integration. The
    four subcommands (authorize / status / test-write / backfill) are
    operator tools — Salem's daemon doesn't invoke them directly.
    They live behind the main CLI so the operator setup flow is
    uniform with every other Alfred capability.

    JSON output is supported for ``status`` / ``test-write`` /
    ``backfill`` so a setup script can pipe the result through ``jq``
    for validation.
    """
    raw = _load_unified_config(args.config)
    wants_json = bool(getattr(args, "json", False))
    _setup_logging_from_config(
        raw, tool="gcal", suppress_stdout=wants_json,
    )

    from alfred.integrations import gcal_cli

    subcmd = getattr(args, "gcal_cmd", None)
    if subcmd == "authorize":
        sys.exit(gcal_cli.cmd_authorize(raw))
    if subcmd == "status":
        sys.exit(gcal_cli.cmd_status(raw, wants_json=wants_json))
    if subcmd == "test-write":
        sys.exit(gcal_cli.cmd_test_write(
            raw,
            cleanup=not getattr(args, "no_cleanup", False),
            wants_json=wants_json,
        ))
    if subcmd == "backfill":
        sys.exit(gcal_cli.cmd_backfill(
            raw,
            dry_run=bool(getattr(args, "dry_run", False)),
            from_date=getattr(args, "from_date", None),
            infer_times=bool(getattr(args, "infer_times", False)),
            wants_json=wants_json,
        ))
    if subcmd == "collapse":
        sys.exit(gcal_cli.cmd_collapse(
            raw,
            collapse_key=getattr(args, "key", ""),
            group_date=getattr(args, "date", ""),
            wants_json=wants_json,
        ))

    print(
        "Usage: alfred gcal {authorize|status|test-write|backfill|collapse}"
    )
    sys.exit(1)


def cmd_fiction(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred fiction`` subcommands.

    Hypatia Phase 2.5 fiction posture support. Two subcommands:

      * ``scaffold "<title>"`` — scaffolds the project directory +
        per-element files. Prints JSON for SKILL consumption
      * ``slug "<title>"`` — prints just the canonical slug

    Both subcommands route through
    :mod:`alfred.telegram.fiction` so the on-disk shape matches what
    the ``/fiction`` slash command produces — same Python helper,
    same slug rules, same directory shape. Hypatia's SKILL revision
    invokes ``alfred fiction scaffold`` via bash for natural-
    language scaffolding ("let's start a fiction project called
    X"); the JSON output gives the SKILL the slug + path + file
    list it needs to confirm to Andrew.

    JSON output on stdout means logging MUST go to the file sink to
    keep stdout clean for SKILL parsing. Same convention as ``alfred
    vault``.
    """
    try:
        raw = _load_unified_config(args.config)
        _setup_logging_from_config(raw, tool="fiction", suppress_stdout=True)
    except SystemExit:
        raw = {}
    except Exception:
        raw = {}

    from alfred.telegram import fiction_cli

    subcmd = getattr(args, "fiction_cmd", None)
    if subcmd == "scaffold":
        sys.exit(fiction_cli.cmd_scaffold(raw, args.title))
    if subcmd == "slug":
        sys.exit(fiction_cli.cmd_slug(args.title))

    print("Usage: alfred fiction {scaffold|slug} \"<title>\"")
    sys.exit(2)


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


def cmd_prefs(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred prefs`` subcommands.

    V1 ships ``rebuild-index`` only — projects active Shape A
    preferences into ``data/operator_preferences.json`` (atomic write).
    Future subcommands (``list``, ``inspect``, ``validate``) extend
    the same dispatch shape.
    """
    raw: dict[str, Any] = {}
    try:
        raw = _load_unified_config(args.config)
        _setup_logging_from_config(raw, tool="prefs", suppress_stdout=False)
    except SystemExit:
        raw = {}
    except Exception:
        raw = {}

    subcmd = getattr(args, "prefs_cmd", None)
    if subcmd == "rebuild-index":
        from alfred.preferences.index import rebuild_index

        vault_path = (raw.get("vault") or {}).get("path")
        if not vault_path:
            print(
                "error: vault.path not configured in config.yaml",
                file=sys.stderr,
            )
            sys.exit(2)

        # Default index output path: <logging.dir>/operator_preferences.json
        # (mirrors state-file defaults). Override via --output.
        output_path = args.output
        if output_path is None:
            logging_dir = (raw.get("logging") or {}).get("dir", "./data")
            output_path = str(
                Path(logging_dir) / "operator_preferences.json"
            )

        # Pull the instance name if available (telegram.instance.name).
        # None is tolerated — the index just stamps null. Per
        # ``feedback_hardcoding_and_alfred_naming.md`` we never default
        # this to a literal — let the index carry None when absent.
        instance = (
            ((raw.get("telegram") or {}).get("instance") or {}).get("name")
        )

        payload = rebuild_index(
            vault_path=vault_path,
            output_path=output_path,
            instance=instance,
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "output_path": output_path,
                    "active_count": len(payload.get("active_preferences", [])),
                    "instance": instance,
                },
                indent=2,
            )
        )
        sys.exit(0)

    # Unknown / missing subcommand: print help shape.
    print(
        "usage: alfred prefs <subcommand>\n"
        "Subcommands:\n"
        "  rebuild-index  Rebuild data/operator_preferences.json from "
        "vault preference/ records.",
        file=sys.stderr,
    )
    sys.exit(2)


def cmd_vault(args: argparse.Namespace) -> None:
    # Route logs to a dedicated file sink. The vault CLI emits JSON on stdout
    # that calling agents parse, so logging MUST NOT leak to stdout.
    # suppress_stdout=True is load-bearing for the JSON contract.
    raw: dict[str, Any] = {}
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

    # Issue #64 — direct CLI invocations bypassed ``vault_audit.log``
    # because mutation logging required ``ALFRED_VAULT_SESSION`` (only
    # set by agent backends). Build a :class:`VaultContext` here so
    # ``vault/cli.py``'s ``_log_or_audit`` helper can append-to-audit-
    # log when no session is active. Mirrors the ``cmd_exec``
    # precedent (cli.py:942): ``logging.dir`` is the per-instance-
    # correct parent (Salem -> ``./data``, KAL-LE ->
    # ``/home/andrew/.alfred/kalle/data``).
    #
    # V1 of the env-var → function-arg refactor (see
    # ``src/alfred/vault/context.py`` module docstring): the
    # ``VaultContext`` threads the resolved audit path down to the
    # handler as a typed kwarg. We also write the env var here for
    # backward-compat — subprocess consumers and not-yet-migrated
    # in-process consumers still need it. V2 drops the env-var write
    # once the consumer migration tail closes.
    from alfred.vault.context import (
        ENV_VAULT_AUDIT_LOG,
        ENV_VAULT_PATH,
        ENV_VAULT_SCOPE,
        ENV_VAULT_SESSION,
        VaultContext,
    )

    # Resolve audit-log path from config when available.
    audit_log_path_str: str | None = None
    if raw:
        log_cfg = raw.get("logging", {}) or {}
        log_dir = log_cfg.get("dir", "./data")
        audit_log_path_str = str(Path(log_dir) / "vault_audit.log")

    # Respect a caller-set ALFRED_VAULT_AUDIT_LOG override (lets tests /
    # one-off invocations point at a different path without rewriting
    # config). Convention matches ALFRED_VAULT_PATH / ALFRED_VAULT_SCOPE
    # / ALFRED_VAULT_SESSION precedence.
    env_override = os.environ.get(ENV_VAULT_AUDIT_LOG)
    if env_override:
        audit_log_path_str = env_override
    elif audit_log_path_str:
        # Mirror to env for backward-compat (V1). Subprocess consumers
        # and not-yet-migrated in-process consumers still read from
        # env. V2 drops this write once all consumers thread through
        # ``VaultContext`` directly.
        os.environ[ENV_VAULT_AUDIT_LOG] = audit_log_path_str

    ctx = VaultContext(
        vault_path=os.environ.get(ENV_VAULT_PATH) or None,
        scope=os.environ.get(ENV_VAULT_SCOPE) or None,
        session_path=os.environ.get(ENV_VAULT_SESSION) or None,
        audit_log_path=audit_log_path_str,
    )

    from alfred.vault.cli import handle_vault_command
    handle_vault_command(args, vault_context=ctx)


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
    # Dispatch to relink / cleanup subcommand if specified; otherwise
    # default to running the daemon (preserves `alfred surveyor`
    # legacy behaviour).
    subcmd = getattr(args, "surveyor_cmd", None)
    if subcmd == "relink":
        return cmd_surveyor_relink(args)
    if subcmd == "cleanup-contamination":
        return cmd_surveyor_cleanup_contamination(args)
    if subcmd == "cleanup-alfred-tags":
        return cmd_surveyor_cleanup_alfred_tags(args)
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

    if subcmd == "skill-audit":
        # SKILL capability-audit detector. Stateless — read the config,
        # compare the runtime tool registry against the bundled SKILL.md,
        # report missing-advertisement findings. Exit 1 on findings so
        # CI / operator scripts can gate on it; exit 0 when clean.
        from alfred.telegram.skill_audit import audit_skill, render_audit
        try:
            result = audit_skill(raw)
        except Exception as exc:  # noqa: BLE001
            # Should be rare — audit_skill swallows operator-input issues.
            # A raise here typically means the conversation module failed
            # to import (e.g. broken deps). Surface it loudly.
            if wants_json:
                print(json.dumps({
                    "error": f"{exc.__class__.__name__}: {exc}",
                }, indent=2))
            else:
                print(f"ERROR: skill-audit failed: {exc.__class__.__name__}: {exc}")
            sys.exit(2)
        if wants_json:
            payload = {
                "instance_name": result.instance_name,
                "tool_set": result.tool_set,
                "skill_bundle": result.skill_bundle,
                "skill_path": str(result.skill_path),
                "skill_missing": result.skill_missing,
                "registered_tools": list(result.registered_tools),
                "advertised": list(result.advertised),
                "missing_advertisements": list(result.missing_advertisements),
                "is_clean": result.is_clean,
            }
            print(json.dumps(payload, indent=2))
        else:
            print(render_audit(result))
        sys.exit(0 if result.is_clean else 1)

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

    print("Usage: alfred talker {watch|status|end|history|skill-audit}")
    sys.exit(1)


def cmd_voice(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred voice`` subcommands.

    Currently exposes ``train backfill`` (Ticket #59, 2026-05-08) —
    walks the vault for raw essay/source records that never went
    through the extraction worker and enqueues extraction jobs for
    them. Recovery path for partially-shipped /train invocations,
    operator-authored essay records, or post-fix retries on
    extraction-failed records.
    """
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="talker", suppress_stdout=False)

    subcmd = getattr(args, "voice_cmd", None)
    train_cmd = getattr(args, "voice_train_cmd", None)

    if subcmd == "train" and train_cmd == "backfill":
        from alfred.telegram import voice_train as _voice_train
        from alfred.telegram.bot import _resolve_queue_path
        from alfred.telegram.config import load_from_unified as talker_cfg_loader

        config = talker_cfg_loader(raw)
        if config.voice_train is None:
            print(
                "voice_train block missing from config — backfill needs "
                "telegram.voice_train.command_enabled: true (or at least "
                "the block to exist) so the queue path can be resolved."
            )
            sys.exit(1)

        vault_path = Path(config.vault.path)
        if not vault_path.is_dir():
            print(f"Vault path not found: {vault_path}")
            sys.exit(1)

        instance_name = config.instance.name or ""
        queue_path = _resolve_queue_path(config)

        jobs, skipped_voice, skipped_method = (
            _voice_train.collect_backfill_jobs(
                vault_path=vault_path,
                instance=instance_name,
            )
        )
        voice_count = sum(1 for j in jobs if j.kind == "voice")
        method_count = sum(1 for j in jobs if j.kind == "method")
        skipped_total = skipped_voice + skipped_method

        dry_run = bool(getattr(args, "dry_run", False))

        if dry_run:
            if not jobs:
                # Per ``feedback_intentionally_left_blank.md``: emit an
                # explicit "ran, nothing to do" signal so dry-run with
                # zero work is distinguishable from a broken walk.
                print(
                    "Dry-run: 0 jobs to enqueue "
                    f"(skipped {skipped_total} already-extracted)."
                )
                return
            print(
                f"Dry-run: would enqueue {voice_count} voice + "
                f"{method_count} method job(s) "
                f"(skipped {skipped_total} already-extracted):"
            )
            for job in jobs:
                print(
                    f"  [{job.kind:6}] {job.raw_rel_path} "
                    f"(cluster={job.cluster or '-'})"
                )
            return

        # Real enqueue path — append each job to the JSONL queue. Same
        # path as the bot uses, so the worker picks them up on the next
        # poll tick (8s default).
        if not jobs:
            print(
                "Enqueued 0 voice jobs + 0 method jobs "
                f"(skipped {skipped_total} already-extracted)."
            )
            return
        for job in jobs:
            try:
                _voice_train.enqueue_job(queue_path, job)
            except Exception as exc:  # noqa: BLE001
                print(
                    f"Failed to enqueue job for {job.raw_rel_path}: {exc}"
                )
                sys.exit(1)
        print(
            f"Enqueued {voice_count} voice jobs + {method_count} method "
            f"jobs (skipped {skipped_total} already-extracted)."
        )
        print(f"Queue: {queue_path}")
        return

    print("Usage: alfred voice train backfill [--dry-run]")
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


def cmd_scaffold(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred scaffold`` subcommands.

    Currently exposes only ``sync`` (Build #38). Mirrors ``cmd_vault`` /
    ``cmd_distiller`` env-var injection: when the subcommand actually
    mutates the vault (``--apply``), resolve ``<logging.dir>/vault_audit
    .log`` and set ``ALFRED_VAULT_AUDIT_LOG`` so the canonical
    ``append_to_audit_log`` helper can find the per-instance-correct
    audit log path without each handler re-reading the config.

    Gated to ``--apply`` only — ``--dry-run`` is a no-op on the
    filesystem and therefore produces no audit rows; setting the env
    var on dry-runs would be a write of process-global state without
    a downstream consumer, the exact "test-hygiene contract" violation
    CLAUDE.md flags for env-var-mutating dispatchers.

    Include / exclude precedence (Stage 2 follow-up to Build #38,
    closes the structural gap surfaced by KAL-LE + Hypatia apply
    cycles 2026-05-12): the unified config dict is threaded down to
    ``cmd_sync`` via ``scaffold_cli.dispatch(args, raw)`` so the
    handler can read ``raw["scaffold"]["include"]`` /
    ``raw["scaffold"]["exclude"]`` via :func:`alfred.scaffold.config
    .load_from_unified`. Three layers, highest wins:

      1. CLI ``--include`` / ``--exclude`` (operator override)
      2. Per-instance config ``scaffold.include`` / ``scaffold.exclude``
      3. Module-level ``DEFAULT_INCLUDE`` / ``DEFAULT_EXCLUDE``
         (Salem-shape fallback)

    The resolution logic lives in :func:`alfred.scaffold.cli
    ._resolve_filter` so all three layers compose in one place.
    """
    raw: dict[str, Any] = {}
    try:
        raw = _load_unified_config(args.config)
        _setup_logging_from_config(raw, tool="scaffold")
    except SystemExit:
        # Allow --vault-path to bypass the config requirement; the
        # downstream handler emits its own error if neither config
        # nor --vault-path provides a vault root.
        pass
    except Exception:
        pass

    # Resolve audit-log path only on mutating subcommands.
    # ``--dry-run`` wins over ``--apply`` if both passed (matches the
    # precedence in cmd_sync), so we re-check both flags here for
    # parity. ``scaffold_cmd == "sync"`` is the only mutating path
    # today; future ``alfred scaffold ...`` subcommands that mutate
    # should be added to the tuple.
    #
    # V1 of the env-var → function-arg refactor: build a
    # :class:`VaultContext` and thread it down through
    # ``scaffold_cli.dispatch``. Also mirror to env for backward-compat
    # with the legacy ``os.environ.get("ALFRED_VAULT_AUDIT_LOG")`` path
    # in case any out-of-tree consumer still reads it. V2 drops the
    # env-var write.
    from alfred.vault.context import (
        ENV_VAULT_AUDIT_LOG,
        ENV_VAULT_PATH,
        ENV_VAULT_SCOPE,
        ENV_VAULT_SESSION,
        VaultContext,
    )

    sub = getattr(args, "scaffold_cmd", None)
    apply_flag = bool(getattr(args, "apply", False))
    dry_run_flag = bool(getattr(args, "dry_run", False))
    will_mutate = sub == "sync" and apply_flag and not dry_run_flag
    scaffold_ctx: VaultContext | None = None
    if will_mutate and raw:
        log_cfg = raw.get("logging", {}) or {}
        log_dir = log_cfg.get("dir", "./data")
        resolved_audit = os.environ.get(ENV_VAULT_AUDIT_LOG) or str(
            Path(log_dir) / "vault_audit.log"
        )
        if not os.environ.get(ENV_VAULT_AUDIT_LOG):
            os.environ[ENV_VAULT_AUDIT_LOG] = resolved_audit
        scaffold_ctx = VaultContext(
            vault_path=os.environ.get(ENV_VAULT_PATH) or None,
            scope=os.environ.get(ENV_VAULT_SCOPE) or None,
            session_path=os.environ.get(ENV_VAULT_SESSION) or None,
            audit_log_path=resolved_audit,
        )

    from alfred.scaffold import cli as scaffold_cli

    code = scaffold_cli.dispatch(args, raw, vault_context=scaffold_ctx)
    sys.exit(code)


def cmd_routine(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred routine`` subcommands.

    Phase 1: ``done`` (log completion), ``run-now`` (force-build today's
    aggregator note), ``status`` (last run + schedule). Phase 2B B3
    (2026-05-30): ``item add/remove/edit`` for item-level CRUD on
    existing routine records. All commands are Salem-only —
    non-Salem instances raise ScopeError per the
    feature_routine_phase1 contract.
    """
    raw = _load_unified_config(args.config)
    wants_json = bool(getattr(args, "json", False))
    _setup_logging_from_config(raw, tool="routine", suppress_stdout=wants_json)

    from alfred.routine.config import load_from_unified
    from alfred.routine import cli as rcli
    from alfred.vault.scope import ScopeError

    config = load_from_unified(raw)
    subcmd = getattr(args, "routine_cmd", None)

    try:
        if subcmd == "done":
            # Phase 2B B1 (2026-05-30) — arg routing:
            #   * Both args supplied → (record_name, item_text)
            #     (the operator named both — the strict-then-fuzzy
            #     cascade applies inside cmd_done)
            #   * Only first arg supplied → ("", item_text) →
            #     vault-wide fuzzy match
            # ``args.item`` is None when the operator passed just one
            # positional; argparse's nargs='?' default.
            record_or_item = getattr(args, "record_or_item", "")
            item = getattr(args, "item", None)
            if item is None:
                # Single-positional form: treat the first arg as the
                # item text + empty record (triggers vault-wide fuzzy).
                record_name_arg = ""
                item_text_arg = record_or_item
            else:
                record_name_arg = record_or_item
                item_text_arg = item
            code = rcli.cmd_done(
                config,
                record_name=record_name_arg,
                item_text=item_text_arg,
                wants_json=wants_json,
                completed_at=getattr(args, "completed_at", None),
            )
        elif subcmd == "undone":
            # Inverse of ``done`` — identical two-positional routing.
            record_or_item = getattr(args, "record_or_item", "")
            item = getattr(args, "item", None)
            if item is None:
                record_name_arg = ""
                item_text_arg = record_or_item
            else:
                record_name_arg = record_or_item
                item_text_arg = item
            code = rcli.cmd_undone(
                config,
                record_name=record_name_arg,
                item_text=item_text_arg,
                date=getattr(args, "date", None),
                wants_json=wants_json,
            )
        elif subcmd == "run-now":
            code = rcli.cmd_run_now(config, wants_json=wants_json)
        elif subcmd == "status":
            code = rcli.cmd_status(config, wants_json=wants_json)
        elif subcmd == "item":
            # Phase 2B B3 (2026-05-30) — item-level CRUD subverb tree.
            # Three actions (add / remove / edit) discriminated by
            # ``routine_item_action`` argparse dest.
            action = getattr(args, "routine_item_action", None)
            if action == "add":
                code = rcli.cmd_item_add(
                    config,
                    record_name=getattr(args, "record", ""),
                    item_text=getattr(args, "text", ""),
                    wants_json=wants_json,
                    priority=getattr(args, "priority", None),
                    target_cadence_days=getattr(
                        args, "target_cadence_days", None,
                    ),
                    surface_at_days=getattr(
                        args, "surface_at_days", None,
                    ),
                    escalate_at_days=getattr(
                        args, "escalate_at_days", None,
                    ),
                    due_pattern=getattr(args, "due_pattern", None),
                    self_care=getattr(args, "self_care", None),
                )
            elif action in ("remove", "edit"):
                # Both share the two-positional-form pattern from B1's
                # ``alfred routine done`` — first positional is either
                # the record name (when --item present) or the item
                # text (vault-wide fuzzy mode).
                record_or_item = getattr(args, "record_or_item", "")
                item_arg = getattr(args, "item", None)
                if item_arg is None:
                    record_name_arg = ""
                    item_text_arg = record_or_item
                else:
                    record_name_arg = record_or_item
                    item_text_arg = item_arg
                if action == "remove":
                    code = rcli.cmd_item_remove(
                        config,
                        record_name=record_name_arg,
                        item_text=item_text_arg,
                        wants_json=wants_json,
                    )
                else:  # edit
                    code = rcli.cmd_item_edit(
                        config,
                        record_name=record_name_arg,
                        item_text=item_text_arg,
                        wants_json=wants_json,
                        new_text=getattr(args, "new_text", None),
                        priority=getattr(args, "priority", None),
                        target_cadence_days=getattr(
                            args, "target_cadence_days", None,
                        ),
                        surface_at_days=getattr(
                            args, "surface_at_days", None,
                        ),
                        escalate_at_days=getattr(
                            args, "escalate_at_days", None,
                        ),
                        due_pattern=getattr(args, "due_pattern", None),
                        self_care=getattr(args, "self_care", None),
                        clear_due_pattern=getattr(
                            args, "clear_due_pattern", False,
                        ),
                        clear_target_cadence_days=getattr(
                            args, "clear_target_cadence_days", False,
                        ),
                    )
            else:
                print(
                    "Usage: alfred routine item {add|remove|edit} ..."
                )
                sys.exit(1)
        else:
            print(
                "Usage: alfred routine {done|undone|run-now|status|item}"
            )
            sys.exit(1)
    except ScopeError as exc:
        if wants_json:
            import json
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"Refused: {exc}", file=sys.stderr)
        sys.exit(1)

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


def cmd_ticket_forward(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred ticket-forward`` subcommands (pipeline c4).

    ``run-once`` is the testable single tick AND the c6 probe surface
    — stdout stays explicit (per-ticket outcome lines + the ILB tick
    summary). ``status`` summarizes the forwarder state (linked /
    pending) plus a live vault scan (open / eligible-now).
    """
    raw = _load_unified_config(args.config)
    wants_json = bool(getattr(args, "json", False))
    _setup_logging_from_config(
        raw, tool="ticket_forward", suppress_stdout=wants_json,
    )

    import asyncio

    from alfred.transport.ticket_forward import (
        TicketForwardState,
        load_ticket_forward_config,
        run_forward_once,
        scan_tickets,
    )

    config = load_ticket_forward_config(raw)
    subcmd = getattr(args, "ticket_forward_cmd", None)

    if subcmd == "run-once":
        if not config.vault_path:
            print(
                "ticket-forward: no vault path configured "
                "(set ticket_forward.vault_path or vault.path)",
                file=sys.stderr,
            )
            sys.exit(1)
        if not config.self_name:
            print(
                "ticket-forward: self_name missing from the "
                "ticket_forward config block",
                file=sys.stderr,
            )
            sys.exit(1)
        result = asyncio.run(run_forward_once(config, raw))
        if wants_json:
            print(json.dumps(result, indent=2))
        else:
            for r in result.get("results", []):
                bits = [
                    r.get("outcome", "?"),
                    r.get("uid", ""),
                    r.get("relpath", ""),
                ]
                if r.get("issue_number") is not None:
                    bits.append(f"issue #{r['issue_number']}")
                print("  " + " · ".join(str(b) for b in bits if b))
            if not result.get("results"):
                # Intentionally-left-blank: zero work is an explicit line.
                print("  (no eligible tickets — nothing to forward)")
            tail = (
                " aborted=peer_not_upgraded" if result.get("aborted") else ""
            )
            print(
                f"tick: scanned={result['scanned']} "
                f"eligible={result['eligible']} "
                f"forwarded={result['forwarded']} "
                f"pending={result['pending']} "
                f"failed={result['failed']}{tail}"
            )
        sys.exit(
            0 if not result.get("failed") and not result.get("aborted") else 1
        )
    elif subcmd == "status":
        state = TicketForwardState.load(config.state_path)
        linked = sum(
            1 for e in state.entries.values() if e.issue_number is not None
        )
        pending = len(state.entries) - linked
        open_count = eligible_now = 0
        if config.vault_path:
            scanned, eligible, _held_rrts = scan_tickets(
                Path(config.vault_path), state,
            )
            open_count = scanned
            eligible_now = len(eligible)
        out = {
            "enabled": config.enabled,
            "target_peer": config.target_peer,
            "interval_minutes": config.interval_minutes,
            "vault_path": config.vault_path,
            "state_path": config.state_path,
            "tracked": len(state.entries),
            "linked": linked,
            "pending": pending,
            "tickets_scanned": open_count,
            "eligible_now": eligible_now,
        }
        if wants_json:
            print(json.dumps(out, indent=2))
        else:
            for key, value in out.items():
                print(f"{key}: {value}")
        sys.exit(0)
    else:
        print("Usage: alfred ticket-forward {run-once|status}")
        sys.exit(1)


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


def _run_schema_check(raw: dict) -> int:
    """Validate this instance's tool schemas; return CLI-style exit code.

    Extracted from ``cmd_check_tool_schemas`` so both the standalone
    subcommand AND the ``alfred up --check-schemas`` pre-restart gate
    share one implementation. Prints the per-tool table to stdout as a
    side effect (same shape as the standalone subcommand).

    Exit codes:
      * 0  all tools accepted (or empty tool list — "ran, nothing to do")
      * 1  one or more tools rejected (operator must fix schema)
      * 2  fatal error (no api_key, missing SDK, network failure) —
           validator couldn't run, status of tools unknown

    Pre-2026-05-09 this logic lived inline in ``cmd_check_tool_schemas``
    with bare ``sys.exit`` calls; refactored to return an int so
    ``cmd_up``'s pre-restart gate can short-circuit on rejection without
    forking the implementation.
    """
    import asyncio

    # Load talker config to get api_key + model + tool_set + instance name.
    from alfred.telegram.config import load_from_unified as talker_cfg_loader
    config = talker_cfg_loader(raw)

    # Mirror the talker's runtime tool selection. ``tools_for_set`` reads
    # ``gcal_enabled`` to decide whether to surface the GCal read tool;
    # we lazy-resolve from the loaded config the same way ``run_turn``
    # does (see ``conversation._resolve_gcal_enabled_for_run_turn``).
    from alfred.telegram.conversation import (
        _resolve_gcal_enabled_for_run_turn,
        tools_for_set,
    )
    gcal_enabled = _resolve_gcal_enabled_for_run_turn(config)
    tool_set = (
        config.instance.tool_set
        if config.instance and config.instance.tool_set
        else "talker"
    )
    tools = tools_for_set(tool_set, gcal_enabled=gcal_enabled)

    instance_name = (
        config.instance.name
        if config.instance and config.instance.name
        else "(unnamed)"
    )
    print(
        f"Validating {instance_name} tool schemas "
        f"({tool_set} set, {len(tools)} tools) against api.anthropic.com..."
    )

    from alfred.health.tool_schema_validator import validate_tool_schemas
    report = asyncio.run(
        validate_tool_schemas(
            api_key=config.anthropic.api_key,
            model=config.anthropic.model,
            tools=tools,
            instance_name=instance_name,
            tool_set=tool_set,
        )
    )

    if report.fatal_error:
        # Per ``feedback_intentionally_left_blank.md``: distinguish
        # "couldn't run" (fatal) from "ran and tools failed" (rejected).
        # Fatal → exit 2 so CI / scripts can tell them apart.
        print(f"FATAL: {report.fatal_error}")
        print("Tool validation could not run; status of all tools is UNKNOWN.")
        return 2

    if not report.results:
        # Empty tool list — explicit "ran, nothing to do" signal.
        print(
            f"No tools surfaced for tool_set={tool_set!r} "
            f"(gcal_enabled={gcal_enabled}). Nothing to validate."
        )
        return 0

    # Per-tool table. Pad tool names so the ✓/✗ column lines up.
    name_width = max(len(r.tool_name) for r in report.results)
    for r in report.results:
        if r.accepted:
            print(f"  - {r.tool_name:<{name_width}}: ✓ accepted")
        else:
            print(f"  - {r.tool_name:<{name_width}}: ✗ REJECTED")
            # Indent the error so it's visually grouped with the tool.
            for line in r.error_text.splitlines() or [r.error_text]:
                print(f"      {line}")

    print()
    if report.all_accepted:
        print(f"All {len(report.results)} tools validated. Safe to restart.")
        return 0
    rejected = report.rejected_count
    print(
        f"{rejected} of {len(report.results)} tools failed validation. "
        f"DO NOT restart — fix schema first."
    )
    return 1


def cmd_check_tool_schemas(args: argparse.Namespace) -> None:
    """Validate this instance's tool schemas against Anthropic's request validator.

    Closes the bug class surfaced 2026-05-05 by the ``oneOf``-at-top-level
    P0 (commit ``0d7e7a6``): schema passed every local test but Anthropic's
    server-side request validator rejected with HTTP 400 on first real
    conversation. This subcommand exercises the SAME validator pre-deploy
    via ``client.messages.count_tokens`` (zero cost) so a schema break
    surfaces BEFORE restart, not 36 hours after.

    Operator workflow:
        $ alfred --config config.yaml check-tool-schemas
        Validating Salem tool schemas against api.anthropic.com...
        - vault_search:      ✓ accepted
        - vault_read:        ✓ accepted
        - vault_create:      ✓ accepted
        - vault_edit:        ✓ accepted
        - gcal_list_events:  ✓ accepted
        All 5 tools validated. Safe to restart.

    On rejection:
        - vault_edit: ✗ REJECTED
            tools.0.custom.input_schema: input_schema does not support
            oneOf, allOf, or anyOf at the top level
        1 of 4 tools failed validation. DO NOT restart — fix schema first.
        exit 1

    Exit codes:
      * 0  all tools accepted
      * 1  one or more tools rejected (operator must fix schema)
      * 2  fatal error (no api_key, missing SDK, network failure) —
           validator couldn't run, status of tools unknown

    Per-tool isolation: each tool gets its own probe so the operator
    sees the tool NAME in errors, not Anthropic's "tools.N" index.

    Implementation: thin wrapper around ``_run_schema_check`` so the
    ``alfred up --check-schemas`` pre-restart gate shares the same
    validator path. Refactored 2026-05-09 (Batch B) to enable the
    pre-restart wiring without forking the per-tool report shape.
    """
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="alfred", suppress_stdout=True)
    sys.exit(_run_schema_check(raw))


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
    # Phase 1 Algernon platform wrapper (2026-05-28): the ``instance``
    # namespace gained ``up``/``down``/``status`` sub-verbs for
    # fan-out across all registered instances. Dispatch them to
    # their dedicated handlers; the existing ``new`` flow stays
    # unchanged below.
    if subcmd == "up":
        return cmd_instance_up_all(args)
    if subcmd == "down":
        return cmd_instance_down_all(args)
    if subcmd == "status":
        return cmd_instance_status_all(args)
    if subcmd != "new":
        print(
            "Usage: alfred instance new <name>\n"
            "       alfred instance up\n"
            "       alfred instance down\n"
            "       alfred instance status [--verbose] [--json]"
        )
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


# ---------------------------------------------------------------------------
# Algernon platform wrapper — fan-out across all instances (Phase 1, 2026-05-28)
# ---------------------------------------------------------------------------
#
# ``alfred instance up | down | status`` fans the corresponding verb out
# across every enabled instance in the registry (``~/.alfred/instances.yaml``).
# Suppressed top-level aliases ``up-all`` / ``down-all`` / ``status-all``
# preserve quick muscle-memory typing without cluttering ``--help``.
#
# Per the Phase 1 design (Plan agent, 2026-05-28): subprocess-shells the
# per-instance command. Idempotency-friendly — pre-check on PID file
# presence treats ``already-running`` as success per ratified decision
# #1. Failure on one instance doesn't block the rest; the wrapper
# continues fan-out best-effort and reports an aggregate pass/fail
# summary at the end.


def _load_registry_or_exit(args: argparse.Namespace):
    """Load the instance registry or exit cleanly with operator-actionable
    error.

    Handles two failure shapes:
      * Missing registry file — points the operator at the
        ``instances.yaml.example`` starter.
      * Malformed YAML / missing fields — surfaces the
        ValueError message verbatim so the operator can fix
        the bad row.
    """
    from alfred.instance_set import load_registry
    registry_path = getattr(args, "registry", None)
    path = Path(registry_path) if registry_path else None
    try:
        return load_registry(path)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(
            "        Bootstrap the registry: "
            "``cp instances.yaml.example ~/.alfred/instances.yaml`` "
            "from the alfred project root.",
            file=sys.stderr,
        )
        sys.exit(2)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)


def cmd_instance_up_all(args: argparse.Namespace) -> None:
    """``alfred instance up`` — start every enabled instance.

    Per ratified Phase 1 decision: ``already-running`` counts as
    OK. The per-line summary distinguishes ``started`` from
    ``already-running`` so the operator can see the state
    distribution at a glance.
    """
    from alfred.instance_set import (
        format_summary_sentinel,
        run_verb_across_set,
    )
    instances = _load_registry_or_exit(args)
    results, exit_code = run_verb_across_set(instances, "up")
    for _, summary in results:
        print(summary)
    print(format_summary_sentinel("up", results))
    sys.exit(exit_code)


def cmd_instance_down_all(args: argparse.Namespace) -> None:
    """``alfred instance down`` — stop every enabled instance.

    Per-line summary distinguishes ``stopped`` (was running →
    stopped) from ``was not running`` (idle, no change) so the
    operator sees which instances actually shed PIDs.
    """
    from alfred.instance_set import (
        format_summary_sentinel,
        run_verb_across_set,
    )
    instances = _load_registry_or_exit(args)
    results, exit_code = run_verb_across_set(instances, "down")
    for _, summary in results:
        print(summary)
    print(format_summary_sentinel("down", results))
    sys.exit(exit_code)


def cmd_instance_status_all(args: argparse.Namespace) -> None:
    """``alfred instance status`` — report running state across all
    enabled instances.

    Default: one-line-per-instance summary. ``--verbose``: concatenate
    full ``alfred status`` output per instance with section headers
    (``=== Salem ===``). ``--json``: aggregate per-instance ``--json``
    blobs into one top-level dict keyed by instance name.
    """
    verbose = bool(getattr(args, "verbose", False))
    as_json = bool(getattr(args, "json", False))
    if verbose and as_json:
        print(
            "error: --verbose and --json are mutually exclusive "
            "(--verbose prints human-readable; --json prints "
            "machine-readable).",
            file=sys.stderr,
        )
        sys.exit(2)

    instances = _load_registry_or_exit(args)
    enabled = [i for i in instances if i.enabled]

    if as_json:
        # Aggregate per-instance ``alfred status --json`` blobs.
        # Each subprocess returns its own JSON object; we wrap
        # them by instance name. Failures land as ``{"error": ...}``
        # entries so the operator can spot per-instance issues.
        # ``json`` is imported at module level (line 6); the local
        # import was redundant — dropped 2026-05-28 per reviewer NOTE.
        from alfred.instance_set import _build_subprocess_cmd
        payload: dict[str, Any] = {}
        any_timed_out = False
        for inst in enabled:
            cmd = _build_subprocess_cmd(inst, "status", ["--json"])
            # Subprocess timeout per the canonical pattern in
            # ``instance_set.run_verb`` (instance_set.py:267-289):
            # a wedged config-load on one instance would otherwise
            # hang the whole fan-out. 30s is generous (``status``
            # reads PID file + config in <1s normally); a timeout
            # firing means corrupt config / locked file / network
            # call inside config load and the operator wants to
            # know NOW. Best-effort fan-out continues to the next
            # instance after the timeout entry lands in payload.
            #
            # Diagnostic pivot per the canonical comment block:
            # FAILED has stderr to inspect (subprocess returned
            # non-zero); TIMEOUT has cwd + config path to inspect
            # (subprocess never returned, so stderr is empty).
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                payload[inst.name] = {
                    "timeout": True,
                    "config": inst.config,
                    "message": (
                        f"30s wedge — check {inst.config}"
                    ),
                }
                any_timed_out = True
                continue
            if proc.returncode == 0 and proc.stdout.strip():
                try:
                    payload[inst.name] = json.loads(proc.stdout)
                except json.JSONDecodeError as exc:
                    payload[inst.name] = {
                        "error": f"JSON parse failure: {exc}",
                        "stdout_tail": proc.stdout[-500:],
                    }
            else:
                payload[inst.name] = {
                    "error": (
                        proc.stderr.splitlines()[0]
                        if proc.stderr.strip()
                        else f"exit code {proc.returncode}"
                    ),
                }
        print(json.dumps(payload, indent=2, default=str))
        if any_timed_out:
            sys.exit(1)
        return

    if verbose:
        # Concatenate full ``alfred status`` per instance with headers.
        from alfred.instance_set import _build_subprocess_cmd
        any_timed_out = False
        for inst in enabled:
            cmd = _build_subprocess_cmd(inst, "status", [])
            # Subprocess timeout per the canonical pattern in
            # ``instance_set.run_verb`` (instance_set.py:267-289).
            # Same wedge surface as the --json branch above.
            # Diagnostic pivot: FAILED has stderr (subprocess
            # returned non-zero); TIMEOUT has cwd + config path
            # (subprocess never returned).
            try:
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=30,
                )
            except subprocess.TimeoutExpired:
                # Emit a distinct TIMEOUT header rather than the
                # standard ``=== <Display> ===`` block — operator
                # scanning the verbose output spots wedged instances
                # immediately without re-reading the section body.
                print(
                    f"=== {inst.display}: TIMEOUT "
                    f"(30s wedge — check {inst.config}) ==="
                )
                print()
                any_timed_out = True
                continue
            print(f"=== {inst.display} ===")
            if proc.stdout:
                print(proc.stdout.rstrip())
            if proc.returncode != 0:
                # Verbose mode keeps stderr visible too — operator
                # debugging needs all the diagnostic surface.
                stderr = (proc.stderr or "").rstrip()
                if stderr:
                    print(f"[stderr]\n{stderr}")
            print()
        if any_timed_out:
            sys.exit(1)
        return

    # Default: one-line-per-instance.
    from alfred.instance_set import (
        format_summary_sentinel,
        run_verb_across_set,
    )
    results, _ = run_verb_across_set(instances, "status")
    for _, summary in results:
        print(summary)
    print(format_summary_sentinel("status", results))


def cmd_scribe(args: argparse.Namespace) -> None:
    """``alfred scribe attest`` — the ONLY sanctioned clinical_note attest path.

    Runs the scribe.attest orchestrator (authorize_attestation → triad write
    under the privileged stayc_clinical_attest scope → durable PHI-free attest
    audit). Fail-closed: an empty ``scribe.clinicians`` list means no valid
    attester; a self-attest / forward-only / non-clinician violation raises.
    """
    subcmd = getattr(args, "scribe_cmd", None)
    if subcmd == "presets":
        _cmd_scribe_presets(args)
        return
    if subcmd == "bugs":
        _cmd_scribe_bugs(args)
        return
    if subcmd != "attest":
        print("Usage: alfred scribe {attest <note> --attester <clinician> | "
              "presets {list|audit|delete} | bugs {list|show|resolve}}")
        sys.exit(1)

    raw = _load_unified_config(args.config)
    from alfred.scribe.config import load_from_unified as load_scribe_config
    from alfred.scribe.attest import attest as scribe_attest
    from alfred.sovereign import (
        SovereignBoundaryError,
        install_sovereign_http_guard,
        validate_sovereign_boundary,
    )

    # The attest CLI is a PRIVILEGED writer to PHI-bearing clinical_notes, invoked
    # from the operator's interactive shell (which may carry cloud creds — no
    # barrier-c) with NO daemon/orchestrator around it. Enforce the SAME sovereign
    # boundary + arm the SAME egress guard the daemon does (daemon.py:76-79), so a
    # sovereign instance's attest runs behind the no-egress boundary and any
    # future/transitive egress from the attest path is guard-refused. Fail-closed:
    # a boundary breach REFUSES the attest.
    #
    # validate_sovereign_boundary is unconditional (it already no-ops unless
    # sovereign.enabled). The guard install is GATED on sovereign.enabled for
    # precision — mirror the boundary's own no-op condition so a NON-sovereign
    # instance's attest does not monkeypatch the transport (harmless today — attest
    # has no network surface and the guard only ever blocks — but the guard is a
    # sovereign-scope control, so it should only arm for a sovereign instance).
    sovereign = raw.get("sovereign") or {}
    sovereign_enabled = isinstance(sovereign, dict) and bool(sovereign.get("enabled"))
    try:
        validate_sovereign_boundary(raw)
    except SovereignBoundaryError as e:
        print(f"Attest REFUSED — sovereign boundary breach: {e}")
        sys.exit(1)
    if sovereign_enabled:
        install_sovereign_http_guard()

    cfg = load_scribe_config(raw)
    vault_path = Path((raw.get("vault") or {}).get("path", "./vault"))
    log_dir = Path((raw.get("logging") or {}).get("dir", "./data"))
    audit_path = log_dir / "clinical_attest_audit.jsonl"
    # #58-D2 — the free-text --reason for a forced override lands HERE (the general
    # vault mutation-provenance trail), keeping clinical_attest_audit.jsonl PHI-free.
    vault_audit_path = log_dir / "vault_audit.log"

    try:
        result = scribe_attest(
            vault_path,
            args.note,
            new_status=args.new_status,
            attester=args.attester,
            clinician_ids=set(cfg.clinicians),
            audit_path=audit_path,
            # #58 D1 — the audited override (available in all modes). An empty
            # --reason with --force-incomplete surfaces as a clean force_without_reason
            # refusal below (non-zero exit, no triad written).
            allow_incomplete=args.force_incomplete,
            override_reason=args.reason,
            vault_audit_path=vault_audit_path,
            # P4-5 — the voice-enrollment capture sink (self-correcting attest_outcome
            # rows). Empty (dormant enrollment) → the attest capture is a no-op.
            enrollment_dir=cfg.diarize.enrollment_dir,
        )
    except Exception as e:  # noqa: BLE001 — surface any attest refusal to the operator
        print(f"Attest REFUSED: {e}")
        sys.exit(1)
    print(
        f"Attested: {result['path']} → {args.new_status} by {args.attester} "
        f"(audit: {audit_path})"
    )


def _cmd_scribe_bugs(args: argparse.Namespace) -> None:
    """``alfred scribe bugs list|show|resolve`` — triage box-local bug reports (task #4).

    Local file ops only (no vault write, no egress): reads/moves ``<ts>-<hex>.md`` reports
    (opaque id — the summary lives only in the file body) under the resolved bug dir. Promotion
    to Forgejo bug-intake / VERA is a HUMAN act after
    on-box read + scrub — this CLI does NOT forward. ``resolve`` moves a report to
    ``resolved/`` (v1 keeps them; retention is owned by task #13)."""
    raw = _load_unified_config(args.config)
    from alfred.scribe.config import load_from_unified as load_scribe_config
    from alfred.scribe import bug as bug_mod

    cfg = load_scribe_config(raw)
    bcmd = getattr(args, "bugs_cmd", None)

    if bcmd == "list":
        rows = bug_mod.list_bugs(cfg, include_resolved=getattr(args, "all", False))
        if not rows:
            print("No bug reports.")                 # intentionally-left-blank
            return
        for r in rows:
            flag = "  [resolved]" if r["resolved"] else ""
            print(f"{r['id']}  {r['created']}  {r['summary']!r}{flag}")
        return

    if bcmd == "show":
        text = bug_mod.read_bug(cfg, args.bug_id)
        if text is None:
            print(f"No such bug report: {args.bug_id!r}")
            sys.exit(1)
        print(text)
        return

    if bcmd == "resolve":
        if bug_mod.resolve_bug(cfg, args.bug_id):
            print(f"Resolved {args.bug_id} (moved to resolved/).")
        else:
            print(f"No such bug report: {args.bug_id!r}")
            sys.exit(1)
        return

    print("Usage: alfred scribe bugs {list [--all] | show <id> | resolve <id>}")
    sys.exit(1)


def _cmd_scribe_presets(args: argparse.Namespace) -> None:
    """``alfred scribe presets list|audit|delete`` — operate the voice-enrollment store.

    Local file ops only (no vault write, no egress) — reads/tombstones preset files +
    the enroll audit.log under ``scribe.diarize.enrollment_dir``. ``audit`` joins names
    from the preset files at DISPLAY time (the audit.log itself is preset_id-only,
    PHI-free); ``list`` flags ORPHANED biometrics (a user subdir no longer in
    ``scribe.clinicians``)."""
    raw = _load_unified_config(args.config)
    from alfred.scribe.config import load_from_unified as load_scribe_config
    from alfred.scribe import embed_voice, enroll_learning
    from alfred.scribe import enrollment as en

    cfg = load_scribe_config(raw)
    enroll_dir = cfg.diarize.enrollment_dir
    if not enroll_dir:
        print("Voice enrollment is not configured (scribe.diarize.enrollment_dir is empty).")
        sys.exit(1)
    root = Path(enroll_dir)
    clinicians = set(cfg.clinicians)
    fp = embed_voice.engine_fingerprint(cfg)

    def _enrolled_users() -> list[str]:
        if not root.is_dir():
            return []
        return sorted(p.name for p in root.iterdir() if p.is_dir() and en.valid_user(p.name))

    pcmd = getattr(args, "presets_cmd", None)
    if pcmd == "list":
        users = [args.user] if args.user else _enrolled_users()
        if not users:
            print("No enrolled users.")            # intentionally-left-blank
            return
        for user in users:
            orphan = "" if user in clinicians else "  ⚠ ORPHANED (not in scribe.clinicians)"
            print(f"User {user!r}{orphan}")
            entries = en.list_user_presets(enroll_dir, user, fp)
            if not entries:
                print("  (no presets)")            # intentionally-left-blank
                continue
            for e in entries:
                p = e.preset
                name = p.name if p else "(unreadable)"
                ver = p.centroid_version if p else "?"
                print(f"  {e.path.stem}  [{e.classification}]  v{ver}  {name!r}")
        return

    if pcmd == "audit":
        audit_path = root / enroll_learning.AUDIT_NAME
        # Build preset_id → name at display time (audit.log is id-only, PHI-free).
        names: dict[str, str] = {}
        for user in _enrolled_users():
            for pf in sorted((root / user).iterdir()):
                if pf.is_file() and pf.suffix == ".json" and en.PRESET_ID_RE.fullmatch(pf.stem):
                    preset, _ = en.load_preset(pf)
                    if preset is not None:
                        names[preset.preset_id] = preset.name
        try:
            audit_lines = (audit_path.read_text(encoding="utf-8").splitlines()
                           if audit_path.is_file() else [])
        except Exception:  # noqa: BLE001 — a TORN audit.log (invalid UTF-8) must not
            # traceback the operator CLI; report it and continue to the orphan check.
            print("Enroll audit is UNREADABLE (corrupt/torn) — skipping.")
            audit_lines = []
        if not audit_lines:
            print("No enroll audit events yet.")    # intentionally-left-blank
        else:
            print("Enroll audit:")
            for line in audit_lines:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001 — skip a bad line, never crash
                    continue
                pid = row.get("preset_id")
                name = names.get(pid, "?") if pid else "-"
                print(f"  {row.get('ts','')}  {row.get('event',''):<18}  {pid or '-'}  {name!r}")
        orphans = [u for u in _enrolled_users() if u not in clinicians]
        if orphans:
            print("\n⚠ ORPHANED biometrics (user no longer in scribe.clinicians):")
            for o in orphans:
                print(f"  {o}")
        else:
            print("\nNo orphaned biometrics (every enrolled user is a current clinician).")
        return

    if pcmd == "delete":
        try:
            en.revoke_preset(enroll_dir, args.user, args.preset, reason="cli_delete")
        except en.EnrollmentError as ex:
            print(f"Delete failed: {ex}")
            sys.exit(1)
        enroll_learning.audit(enroll_dir, "preset_deleted", preset_id=args.preset, user=args.user)
        print(f"Deleted (revoked + tombstoned) preset {args.preset} for user {args.user!r}. "
              f"Notes already written are unaffected.")
        return

    print("Usage: alfred scribe presets {list [--user U] | audit | delete --user U --preset ID}")
    sys.exit(1)


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


def cmd_surveyor_cleanup_contamination(args: argparse.Namespace) -> None:
    """Phase 2 contamination cleanup — body-text-anchor heuristic.

    Bulk-removes contaminated entity links (``related_persons`` /
    ``related_orgs`` / ``related_matters`` / ``related_projects``)
    from records where the linked entity has no textual presence in
    the record's body / title / description / related list. See
    ``alfred.surveyor.cleanup`` for the heuristic + scope rationale.

    Always defaults to dry-run unless ``--apply`` is passed —
    operator must opt in to the actual mutation. Per the Phase 2
    ticket: this script ships, operator runs dry-run, reviews,
    approves, runs for-real.
    """
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="surveyor")

    try:
        from alfred.surveyor.cleanup import cleanup_entity_link_contamination
        from alfred.surveyor.config import load_from_unified
    except ImportError as e:
        print(f"Surveyor dependencies not installed: {e}")
        sys.exit(1)

    config = load_from_unified(raw)
    vault_path = Path(config.vault.path)

    # Default targets: the four signatures from the QA finding. Operator
    # can pass ``--target`` to override / extend (one ``--target`` per
    # path). Empty default-set with explicit ``--target`` is honoured.
    targets: list[str] = list(getattr(args, "target", None) or [])
    if not targets:
        targets = [
            "person/Ben McMillan.md",
            "person/Jamie.md",
            "org/TIXR.md",
            "org/Halifax Music Fest.md",
        ]

    apply = bool(getattr(args, "apply", False))
    dry_run = not apply

    # Audit-log path mirrors the daemon's derivation
    # (``cfg.state.path``-sibling). Only used in non-dry-run mode.
    audit_log_path = Path(config.state.path).parent / "vault_audit.log"

    print(f"Surveyor contamination cleanup — {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  Vault:    {vault_path}")
    print(f"  Targets:  {len(targets)} entity path(s)")
    for t in targets:
        print(f"    • {t}")
    print()

    report = cleanup_entity_link_contamination(
        vault_path=vault_path,
        targets=targets,
        dry_run=dry_run,
        audit_log_path=audit_log_path if not dry_run else None,
    )

    # Per-target table (the operator-actionable summary).
    verb = "Would remove" if dry_run else "Removed"
    print(f"{verb} the following:")
    for t in report.targets:
        print(
            f"  {t.target_path:<40s} "
            f"removed: {len(t.removed_from):>5d}  "
            f"preserved: {len(t.preserved_in):>5d}  "
            f"(not present in {t.not_present_in} records)"
        )
    print()
    print(f"Total mutations: {report.total_removed} across {report.affected_record_count} records")
    if report.failed_records:
        print(f"Failures:        {len(report.failed_records)} (see report JSON for details)")

    # Save the report so operator has a deterministic record of what
    # the dry-run found (for diff-against-live-run sanity).
    from datetime import date as _date
    report_path = (
        Path(config.state.path).parent
        / f"surveyor_cleanup_{'dryrun_' if dry_run else ''}{_date.today().isoformat()}.json"
    )
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"\nFull report: {report_path}")
    except OSError as exc:
        print(f"\n(could not write report file: {exc})")

    if dry_run:
        print("\nRe-run with --apply to mutate the vault.")


def cmd_surveyor_cleanup_alfred_tags(args: argparse.Namespace) -> None:
    """Phase 2 (tag side) — alfred_tags contamination cleanup CLI.

    Walks the vault, finds records whose ``alfred_tags`` frontmatter
    list contains tags whose anchor term has no textual presence in
    the record's body / title / description / related list. Removes
    those tags. See :func:`alfred.surveyor.cleanup.cleanup_alfred_tags_contamination`
    for the heuristic + scope rationale.

    Always defaults to dry-run unless ``--apply`` is passed — operator
    must opt in to the actual mutation. Same shape as the link-side
    ``cleanup-contamination`` handler above.
    """
    raw = _load_unified_config(args.config)
    _setup_logging_from_config(raw, tool="surveyor")

    try:
        from alfred.surveyor.cleanup import cleanup_alfred_tags_contamination
        from alfred.surveyor.config import load_from_unified
    except ImportError as e:
        print(f"Surveyor dependencies not installed: {e}")
        sys.exit(1)

    config = load_from_unified(raw)
    vault_path = Path(config.vault.path)

    apply = bool(getattr(args, "apply", False))
    dry_run = not apply

    audit_log_path = Path(config.state.path).parent / "vault_audit.log"

    print(f"Surveyor alfred_tags cleanup — {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"  Vault:    {vault_path}")
    print()

    report = cleanup_alfred_tags_contamination(
        vault_path=vault_path,
        dry_run=dry_run,
        audit_log_path=audit_log_path if not dry_run else None,
    )

    # Per-record table — only modified records appear (unmodified
    # records roll up into the aggregate counts). Aligns with the
    # spec's "per-record table for modified records" shape.
    verb = "Would remove" if dry_run else "Removed"
    if report.per_record_modifications:
        print(f"{verb} the following:")
        for m in report.per_record_modifications:
            print(
                f"  {m.record_path:<60s}  "
                f"removed: {len(m.tags_removed):>3d}  "
                f"kept: {len(m.tags_kept):>3d}"
            )
            # Show the actual removed tags so operator can spot-check.
            print(f"    tags removed: {', '.join(m.tags_removed)}")
        print()
    else:
        # Per ``feedback_intentionally_left_blank.md``: explicit
        # "ran, nothing to do" so silence is distinguishable from a
        # broken walker. Surface as a one-liner instead of an empty
        # table.
        print("No records require modification — every alfred_tags entry")
        print("has textual anchor support in its record's content.")
        print()

    print(
        f"Records scanned:  {report.records_scanned}\n"
        f"  with alfred_tags: {report.records_with_tags}\n"
        f"  modified:         {report.records_modified}\n"
        f"Tags removed:     {report.tags_removed_total}"
    )
    if report.failed_records:
        print(f"Failures:         {len(report.failed_records)} (see report JSON for details)")

    # Save the report so operator has a deterministic record of the
    # dry-run findings (for diff-against-live-run sanity).
    from datetime import date as _date
    report_path = (
        Path(config.state.path).parent
        / f"surveyor_cleanup_alfred_tags_{'dryrun_' if dry_run else ''}{_date.today().isoformat()}.json"
    )
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        print(f"\nFull report: {report_path}")
    except OSError as exc:
        print(f"\n(could not write report file: {exc})")

    if dry_run:
        print("\nRe-run with --apply to mutate the vault.")


# ---------------------------------------------------------------------------
# alfred msg — inter-project message bus (V1)
# ---------------------------------------------------------------------------


def _msg_send(args: argparse.Namespace, config, raw: dict) -> None:
    """`alfred msg send` — mint an id + drop a valid message into the spool.

    Structural-validate only (NOT against the registry — the sender need
    not hold the full registry; the router quarantines an unknown ``to`` as
    undeliverable). A fresh thread mints a ``correlation_id``; ``--reply-to``
    + an echoed ``--correlation-id`` thread a reply."""
    import uuid

    from alfred.msgbus.record import (
        MessageRecord,
        _now_iso,
        message_filename,
        validate_record,
        write_message_file,
    )
    from alfred.msgbus.router import mint_message_id

    from_project = getattr(args, "from_project", "") or config.self_project
    if not from_project:
        print(
            "alfred msg send: --from required (or set message_bus.self_project)",
            file=sys.stderr,
        )
        sys.exit(1)

    body = ""
    body_file = getattr(args, "body_file", "") or ""
    body_inline = getattr(args, "body", "") or ""
    if body_file:
        body = sys.stdin.read() if body_file == "-" else Path(body_file).read_text(encoding="utf-8")
    elif body_inline:
        body = sys.stdin.read() if body_inline == "-" else body_inline

    correlation_id = (
        getattr(args, "correlation_id", "") or f"cnv-{uuid.uuid4().hex[:12]}"
    )
    created = _now_iso()
    record = MessageRecord(
        from_project=from_project,
        to_project=args.to,
        kind=args.kind,
        correlation_id=correlation_id,
        created=created,
        subject=args.subject,
        reply_to=getattr(args, "reply_to", "") or "",
        precedence=getattr(args, "precedence", "") or "R",
        body=body,
    )
    record.id = mint_message_id(
        from_project, args.to, created, args.subject, body,
    )

    errors = validate_record(record)  # structural only (no registry)
    if errors:
        print(
            "alfred msg send: invalid message — " + "; ".join(errors),
            file=sys.stderr,
        )
        sys.exit(1)

    dest = Path(config.spool_path) / message_filename(record)
    write_message_file(dest, record)
    print(f"queued {record.id} → {args.to} ({args.kind}) [{correlation_id}]")

    # Route-on-send (Path B): --route/--now sweeps the spool immediately
    # (under the concurrency lock) so the message lands in the peer inbox now
    # instead of waiting for the 5-min cron. Absent → cron-only, unchanged.
    if getattr(args, "route", False):
        from alfred.msgbus.router import route_now

        result = route_now(config, raw)
        if result.get("skipped_locked"):
            print("routed now: a sweep is already running — it will route this file")
        else:
            print(
                f"routed now: routed={result['routed']} "
                f"contracts_applied={result['contracts_applied']} "
                f"failed={result['failed']}"
            )


def _msg_inbox(args: argparse.Namespace, config) -> None:
    """`alfred msg inbox [<project>] {list|read <id>|drain}`.

    ``<project>`` defaults to ``message_bus.self_project`` (honors the
    config doc — the no-arg form uses this instance's own project)."""
    from alfred.msgbus.inbox import (
        count_unread,
        drain_inbox,
        list_inbox,
        read_message,
    )

    project = (getattr(args, "project", "") or "") or config.self_project
    if not project:
        print(
            "alfred msg inbox: project required "
            "(or set message_bus.self_project)",
            file=sys.stderr,
        )
        sys.exit(1)
    registry = config.registry()
    inbox = registry.inbox_for(project)
    if inbox is None:
        print(
            f"alfred msg inbox: unknown project {project!r} "
            f"(registry: {registry.names()})",
            file=sys.stderr,
        )
        sys.exit(1)

    action = args.inbox_action
    if action == "list":
        records = list_inbox(inbox)
        if not records:
            # Intentionally-left-blank — explicit empty line.
            print(f"  (inbox empty — 0 unread for {project})")
        for r in records:
            print(f"  {r.id}  [{r.kind}] {r.from_project} → {r.subject}")
        print(f"unread: {count_unread(inbox)}")
    elif action == "read":
        mid = getattr(args, "message_id", "") or ""
        if not mid:
            print("alfred msg inbox read: message id required", file=sys.stderr)
            sys.exit(1)
        rec = read_message(inbox, mid)
        if rec is None:
            print(f"not found: {mid}", file=sys.stderr)
            sys.exit(1)
        print(
            f"# {rec.subject}\n"
            f"id: {rec.id}  from: {rec.from_project}  kind: {rec.kind}  "
            f"correlation: {rec.correlation_id}\n"
        )
        print(rec.body)
    elif action == "drain":
        drained = drain_inbox(inbox, mark_read=True)
        if getattr(args, "json", False):
            # Path B loop surface: full records INCLUDING body, machine-
            # parseable, so a live-coordination tick can read + respond. asdict
            # (not to_summary_dict) so the body travels.
            from dataclasses import asdict

            print(json.dumps([asdict(r) for r in drained], indent=2))
            return
        if not drained:
            print(f"  (nothing to drain — 0 unread for {project})")
        for r in drained:
            print(f"  drained {r.id}  [{r.kind}] {r.subject}")
        print(f"drained: {len(drained)}")


def _msg_route_once(
    args: argparse.Namespace, config, raw: dict, wants_json: bool
) -> None:
    """`alfred msg route-once` — run one routing tick (the probe surface)."""
    import asyncio

    from alfred.msgbus.router import run_route_once

    if not config.spool_path:
        print("alfred msg route-once: no spool_path configured", file=sys.stderr)
        sys.exit(1)
    if not config.projects:
        print("alfred msg route-once: no projects registered", file=sys.stderr)
        sys.exit(1)

    result = asyncio.run(run_route_once(config, raw))
    if wants_json:
        print(json.dumps(result, indent=2))
    else:
        for r in result.get("results", []):
            bits = [r.get("outcome", "?"), r.get("id", ""), r.get("to", "")]
            print("  " + " · ".join(str(b) for b in bits if b))
        if not result.get("results"):
            # Intentionally-left-blank — explicit zero-work line.
            print("  (no messages to route)")
        print(
            f"tick: scanned={result['scanned']} routed={result['routed']} "
            f"skipped_dup={result['skipped_dup']} "
            f"malformed={result['malformed']} "
            f"undeliverable={result['undeliverable']} "
            f"failed={result['failed']}"
        )
    sys.exit(0 if not result.get("failed") else 1)


def _msg_status(args: argparse.Namespace, config, wants_json: bool) -> None:
    """`alfred msg status` — bus state + per-project unread counts."""
    from alfred.msgbus.inbox import count_unread
    from alfred.msgbus.state import MessageBusState

    state = MessageBusState.load(config.state_path)
    registry = config.registry()
    per_project = {
        name: count_unread(registry.inbox_for(name))
        for name in registry.names()
        if registry.inbox_for(name) is not None
    }
    summary = {
        "enabled": config.enabled,
        "spool_path": config.spool_path,
        "routed_lifetime": len(state.entries),
        "unread_by_project": per_project,
    }
    if wants_json:
        print(json.dumps(summary, indent=2))
        return
    print(f"message bus: enabled={config.enabled} spool={config.spool_path}")
    print(f"  routed (lifetime): {len(state.entries)}")
    if per_project:
        for name, n in sorted(per_project.items()):
            print(f"  {name}: {n} unread")
    else:
        # Intentionally-left-blank — explicit empty-registry line.
        print("  (no projects registered)")


def cmd_contract(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred contract`` — Layer-2 contract negotiation.

    ``check`` is the script build-gate (its exit code propagates), so this
    handler ``sys.exit``s with the subcommand's return code."""
    raw = _load_unified_config(args.config)
    wants_json = bool(getattr(args, "json", False))
    _setup_logging_from_config(
        raw, tool="contracts", suppress_stdout=wants_json,
    )
    from alfred.contracts.cli import dispatch
    sys.exit(dispatch(args, raw))


def cmd_msg(args: argparse.Namespace) -> None:
    """Dispatcher for ``alfred msg`` — the inter-project message bus.

    ``send`` mints + drops a message into the spool; ``inbox <project>
    {list|read|drain}`` inspects/drains a project inbox; ``route-once`` is
    the daemon's single-tick probe; ``status`` summarizes bus state."""
    raw = _load_unified_config(args.config)
    wants_json = bool(getattr(args, "json", False))
    _setup_logging_from_config(
        raw, tool="message_bus", suppress_stdout=wants_json,
    )
    from alfred.msgbus.config import load_message_bus_config

    config = load_message_bus_config(raw)
    subcmd = getattr(args, "msg_cmd", None)
    if subcmd == "send":
        _msg_send(args, config, raw)
    elif subcmd == "inbox":
        _msg_inbox(args, config)
    elif subcmd == "route-once":
        _msg_route_once(args, config, raw, wants_json)
    elif subcmd == "status":
        _msg_status(args, config, wants_json)
    else:
        print(
            "usage: alfred msg {send|inbox|route-once|status}",
            file=sys.stderr,
        )
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
    up_parser.add_argument(
        "--check-schemas", dest="check_schemas",
        action="store_true", default=False,
        help=(
            "Run Anthropic tool-schema validation before starting "
            "daemons; abort if any schema is rejected. Same probe as "
            "``alfred check-tool-schemas`` but wired into the deploy "
            "step. Catches the 2026-05-05 oneOf-at-top-level bug "
            "class (schema passes local tests but Anthropic server "
            "rejects on first conversation). Network call required."
        ),
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

    # check-tool-schemas — pre-deploy Anthropic schema validator
    # (closes the bug class surfaced 2026-05-05 by the oneOf-at-top-level
    # P0). Probes each tool individually via count_tokens (zero cost) so
    # operator can verify schemas pre-restart.
    sub.add_parser(
        "check-tool-schemas",
        help=(
            "Validate this instance's tool schemas against Anthropic's "
            "request validator (zero-cost count_tokens probe per tool). "
            "Run before restarting daemons after a tool-schema change."
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
    ec_backfill.add_argument(
        "--reclassify", action="store_true", default=False,
        help=(
            "Process records EVEN when priority is already set; overwrite "
            "priority + action_hint + reasoning with new classification. "
            "Use this after a corpus / few-shot prompt fix to retroactively "
            "re-evaluate historical records. Composes with --dry-run + --limit."
        ),
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

    # scribe (STAY-C sovereign scribe) — P2-a: attest a clinical_note ai_draft.
    # The ONLY sanctioned path to flip a clinical_note's status/attested_by:
    # runs scribe.authorize_attestation (forward-only + distinct-human-clinician
    # + non-empty-creator) then writes the triad under the privileged
    # stayc_clinical_attest scope. A raw ``alfred vault edit`` under
    # stayc_clinical can NEVER flip the triad (structurally denied).
    scribe_p = sub.add_parser("scribe", help="Sovereign scribe subcommands")
    scribe_sub = scribe_p.add_subparsers(dest="scribe_cmd")
    scribe_attest = scribe_sub.add_parser(
        "attest", help="Attest a clinical_note ai_draft (a human clinician signs)",
    )
    scribe_attest.add_argument("note", help="Relative path of the clinical_note record")
    scribe_attest.add_argument(
        "--attester", required=True,
        help=(
            "Human clinician identity performing the attestation. Must be in "
            "scribe.clinicians AND distinct from the scribe drafter — the AI "
            "cannot attest its own draft."
        ),
    )
    scribe_attest.add_argument(
        "--new-status", default="attested", choices=["attested", "amended"],
        help="Target status (forward-only lifecycle; default: attested)",
    )
    # #58 — audited override of the completeness precondition (available in ALL
    # modes, incl. clinical — it is opt-in; default is strict-refuse). Bypasses
    # ONLY the completeness gate; the lifecycle + distinct-clinician attester
    # remain absolute. Requires --reason (empty/absent → refused).
    scribe_attest.add_argument(
        "--force-incomplete", action="store_true",
        help=(
            "Attest an INCOMPLETE encounter (no/false completeness marker) — an "
            "audited clinician override. Requires --reason. Bypasses ONLY the "
            "completeness precondition (lifecycle + attester still enforced)."
        ),
    )
    scribe_attest.add_argument(
        "--reason", default=None,
        help=(
            "Justification for --force-incomplete (REQUIRED with it). Recorded in "
            "the VAULT AUDIT (data/vault_audit.log), NOT the PHI-free attest audit "
            "— keep it PHI-free where possible."
        ),
    )
    # P4-5 — voice-preset management (local file ops under scribe.diarize.enrollment_dir).
    scribe_presets = scribe_sub.add_parser(
        "presets", help="Manage voice-enrollment presets (list / audit / delete)",
    )
    presets_sub = scribe_presets.add_subparsers(dest="presets_cmd")
    presets_list = presets_sub.add_parser(
        "list", help="List voice presets + classification; flags orphaned biometrics",
    )
    presets_list.add_argument(
        "--user", default=None,
        help="Filter to one clinician (default: every enrolled user)",
    )
    presets_sub.add_parser(
        "audit", help="Show the enroll audit log (names joined at display) + orphaned biometrics",
    )
    presets_delete = presets_sub.add_parser(
        "delete", help="Delete (revoke + tombstone) a preset — notes already written are unaffected",
    )
    presets_delete.add_argument("--user", required=True, help="The preset's clinician (user subdir)")
    presets_delete.add_argument("--preset", required=True, help="The preset id (pst-...)")

    # Task #4 — box-local bug-report triage (local file ops under the resolved bug dir).
    scribe_bugs = scribe_sub.add_parser(
        "bugs", help="Triage box-local STAY-C bug reports (list / show / resolve)",
    )
    bugs_sub = scribe_bugs.add_subparsers(dest="bugs_cmd")
    bugs_list = bugs_sub.add_parser(
        "list", help="List bug reports (unresolved by default; --all adds resolved)",
    )
    bugs_list.add_argument("--all", action="store_true", help="Include resolved reports")
    bugs_show = bugs_sub.add_parser("show", help="Print a bug report by id")
    bugs_show.add_argument("bug_id", help="The report id (filename stem)")
    bugs_resolve = bugs_sub.add_parser(
        "resolve", help="Mark a report resolved (move to resolved/)",
    )
    bugs_resolve.add_argument("bug_id", help="The report id (filename stem)")

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

    # KAL-LE distiller-radar Phase 2 — manual inspection of the
    # synthesis ranker's top-N. Read-only; useful for tuning the score
    # formula post-deploy. Reads from ``distiller.vault.path`` (so for
    # KAL-LE this picks up aftermath-lab/synthesis|decision|contradiction).
    dist_rank_week = dist_sub.add_parser(
        "rank-week",
        help="Print synthesis ranker top-N for tuning (read-only)",
    )
    dist_rank_week.add_argument(
        "--top-n", type=int, default=12,
        help="Show this many ranked records (default 12)",
    )
    dist_rank_week.add_argument(
        "--window-days", type=int, default=7,
        help="Recency cliff in days (default 7); records older lose only the recency term",
    )
    dist_rank_week.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Accepted for symmetry; the command is read-only either way",
    )

    # KAL-LE distiller-radar Phase 3a — daily continuous radar.
    # Wraps rank_synthesis_records on a 1-day window, dedups against the
    # rolling surfaced log, writes <digests_dir>/daily/YYYY-MM-DD.md, and
    # appends each surfaced record to <state_dir>/radar_surfaced.jsonl.
    # Defaults pick up vault/digests + state.path's parent so the typical
    # KAL-LE invocation is just `alfred --config config.kalle.yaml
    # distiller rank-day` with no flags.
    dist_rank_day = dist_sub.add_parser(
        "rank-day",
        help="Daily radar: rank 1-day window + dedup + write daily file",
    )
    dist_rank_day.add_argument(
        "--top-n", type=int, default=5,
        help="Max items to surface per day (default 5)",
    )
    dist_rank_day.add_argument(
        "--min-score", type=float, default=None,
        help="Optional score floor; items below are skipped (default no floor)",
    )
    dist_rank_day.add_argument(
        "--digests-dir", default=None,
        help="Override target dir; default <vault>/digests",
    )
    dist_rank_day.add_argument(
        "--state-dir", default=None,
        help="Override surfaced-log dir; default parent of distiller state.path",
    )
    dist_rank_day.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Compute + log without writing the daily file or surfaced log",
    )

    # KAL-LE distiller-radar Phase 4 — embedding-pattern miner.
    # Reads the surveyor pipeline's labeled-cluster output, gates each
    # cluster against the four-part rule (labeled / substantive / no
    # canonical match / label-quality), and surfaces survivors as
    # inbox proposals for new architecture/ or principles/ records.
    # Per-instance opt-in via distiller.pattern_miner.enabled in config.
    dist_mine = dist_sub.add_parser(
        "mine-patterns",
        help="Phase 4 embedding-pattern miner: surface unnamed-theme inbox proposals",
    )
    dist_mine.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Evaluate + render counts without writing proposal files or state",
    )
    dist_mine.add_argument(
        "--min-cluster-size", type=int, default=None,
        help="Override the gate's size threshold for one run (default 3 from config)",
    )
    dist_mine.add_argument(
        "--top", type=int, default=None,
        help="Cap on new proposals per run (default unlimited; useful for bulk-mine)",
    )

    # Phase 4 operator-promote tracking (2026-05-11). Closes 3 deferred
    # follow-ups from project_phase4_drafter_prompt_tuning.md: slug-
    # rename-on-promote silently miscounted as discarded by the
    # reconcile sweep; no audit trail for promote/discard actions; no
    # scaffolding-strip automation. The CLI commands set state status
    # explicitly + write per-action fields + write to vault_audit.log
    # + strip scaffolding on promote. Reconcile sweep stays as the
    # backstop for direct-filesystem operator actions.
    dist_promote = dist_sub.add_parser(
        "promote-proposal",
        help="Promote a Phase 4 inbox proposal to a canonical record",
    )
    dist_promote.add_argument(
        "slug",
        help=(
            "proposed_slug of the inbox proposal (e.g. "
            "python-frontmatter-parsing-behaviors). Use --fingerprint "
            "to disambiguate when multiple proposals share a slug."
        ),
    )
    dist_promote.add_argument(
        "--to", default=None,
        help=(
            "Canonical target path within the vault (e.g. "
            "architecture/python-frontmatter.md). Defaults to "
            "<proposed_canonical_type>/<proposed_slug>.md derived "
            "from state."
        ),
    )
    dist_promote.add_argument(
        "--no-strip-scaffolding", action="store_true", default=False,
        help=(
            "Leave the proposal body verbatim (frontmatter + banner + "
            "footer + empty fences). Default behavior strips all four "
            "categories and prepends a canonical promotion banner."
        ),
    )
    dist_promote.add_argument(
        "--fingerprint", default=None,
        help=(
            "Cluster fingerprint to disambiguate ambiguous slugs. "
            "Required when multiple state entries share the same slug "
            "(e.g. after a collision-resolve pass)."
        ),
    )

    dist_discard = dist_sub.add_parser(
        "discard-proposal",
        help="Discard a Phase 4 inbox proposal (record + delete file)",
    )
    dist_discard.add_argument(
        "slug",
        help=(
            "proposed_slug of the inbox proposal. Use --fingerprint to "
            "disambiguate when multiple proposals share a slug."
        ),
    )
    dist_discard.add_argument(
        "--reason", default=None,
        help=(
            "Optional operator context (e.g. 'overlaps with existing "
            "principles/foo.md'). Recorded on the state entry as "
            "discarded_reason."
        ),
    )
    dist_discard.add_argument(
        "--fingerprint", default=None,
        help="Cluster fingerprint to disambiguate ambiguous slugs.",
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

    # cleanup-contamination — Phase 2 of the QA contamination fix.
    # Bulk-removes contaminated related_* entries via body-text-anchor
    # heuristic. Always defaults to dry-run; --apply opts in to mutation.
    surv_cleanup = surv_sub.add_parser(
        "cleanup-contamination",
        help=(
            "Bulk-remove contaminated related_persons / related_orgs / "
            "related_matters / related_projects entries from records "
            "where the linked entity has no textual presence in the "
            "body / title / description / related list. Default is "
            "dry-run; pass --apply to actually mutate. Default targets "
            "are the 4 known signatures (Ben McMillan / Jamie / TIXR / "
            "Halifax Music Fest); pass --target to override."
        ),
    )
    surv_cleanup.add_argument(
        "--apply", action="store_true", default=False,
        help=(
            "Actually mutate the vault. Default (without --apply) is "
            "dry-run: report what would be removed without writing."
        ),
    )
    surv_cleanup.add_argument(
        "--target", action="append", default=None, dest="target",
        help=(
            "Vault path of an entity to clean (e.g. 'person/Ben McMillan.md'). "
            "Pass --target multiple times for multiple entities. When "
            "omitted, the 4 known QA-finding signatures are used."
        ),
    )

    # cleanup-alfred-tags — Phase 2 (tag side) of the QA contamination
    # fix. Bulk-removes unanchored tags from records' alfred_tags
    # frontmatter via the body-text-anchor heuristic. Companion to
    # cleanup-contamination — same dry-run-by-default + --apply pattern;
    # no --target because tag contamination is general (whole-vault
    # walk).
    surv_cleanup_tags = surv_sub.add_parser(
        "cleanup-alfred-tags",
        help=(
            "Bulk-remove unanchored tags from alfred_tags frontmatter "
            "lists across the whole vault. Per-tag predicate: tag's "
            "anchor term must appear (word-boundary) in the record's "
            "body / title / description / related list. Default is "
            "dry-run; pass --apply to actually mutate."
        ),
    )
    surv_cleanup_tags.add_argument(
        "--apply", action="store_true", default=False,
        help=(
            "Actually mutate the vault. Default (without --apply) is "
            "dry-run: report what would be removed without writing."
        ),
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
    # skill-audit — SKILL capability-audit detector. Compares the
    # talker's runtime tool registry (per instance.tool_set + gcal.enabled)
    # against the advertised tool surface in the instance's bundled
    # SKILL.md. Exits 1 when missing advertisements are found so CI /
    # operator scripts can gate on it. See
    # ``src/alfred/telegram/skill_audit.py`` for the rationale.
    talker_skill_audit = talker_sub.add_parser(
        "skill-audit",
        help=(
            "Audit instance SKILL.md against the runtime tool registry "
            "(detects features wired in code but not advertised to the agent)"
        ),
    )
    talker_skill_audit.add_argument(
        "--json", action="store_true", default=False,
        help="Emit JSON instead of human-readable text",
    )

    # voice — voice/method-source training pipeline subcommands
    # (Ticket #59, 2026-05-08). Currently exposes ``train backfill``;
    # future commands (``train list``, ``train status``, etc.) plug
    # into the same dispatcher.
    voice_p = sub.add_parser(
        "voice",
        help="Voice/method-source training pipeline subcommands",
    )
    voice_sub = voice_p.add_subparsers(dest="voice_cmd")
    voice_train_p = voice_sub.add_parser(
        "train",
        help="Voice training (/train) subcommands",
    )
    voice_train_sub = voice_train_p.add_subparsers(dest="voice_train_cmd")
    voice_train_backfill = voice_train_sub.add_parser(
        "backfill",
        help=(
            "Enqueue extraction jobs for raw essay/source records that "
            "never went through the worker (recovery path for partial "
            "/train invocations or operator-authored essay records)."
        ),
    )
    voice_train_backfill.add_argument(
        "--dry-run", dest="dry_run", action="store_true", default=False,
        help="Print what would be enqueued without writing to the queue.",
    )

    # routine — Salem-only daily routine tracker (Phase 1)
    routine_p = sub.add_parser(
        "routine",
        help="Salem daily-routine tracker (done / run-now / status / item)",
    )
    routine_sub = routine_p.add_subparsers(dest="routine_cmd")
    routine_done = routine_sub.add_parser(
        "done",
        help="Log a routine item as completed (default: today)",
    )
    # Phase 2B B1 (2026-05-30): make ``record`` optional. When omitted,
    # the CLI does a vault-wide fuzzy match on the item text across all
    # active routines. Two forms accepted:
    #   alfred routine done "For Self Health" "Dog Walk"
    #   alfred routine done "Dog Walk"          # vault-wide fuzzy
    # Argparse can't natively express "optional positional with a
    # required second positional", so the first arg is named
    # ``record_or_item``; the cmd_routine dispatcher routes by
    # presence of ``item``.
    routine_done.add_argument(
        "record_or_item",
        help=(
            "Routine record name (e.g. 'For Self Health') OR — when "
            "<item> is omitted — the item text to fuzzy-match vault-wide."
        ),
    )
    routine_done.add_argument(
        "item",
        nargs="?",
        default=None,
        help=(
            "Item text within the routine (e.g. 'Dog Walk'). Omit to "
            "treat <record_or_item> as the item text and do a vault-wide "
            "fuzzy match."
        ),
    )
    routine_done.add_argument(
        "--completed-at",
        dest="completed_at",
        default=None,
        help=(
            "Back-date the completion to this YYYY-MM-DD date. "
            "Defaults to today (in config.schedule.timezone). "
            "Future dates rejected."
        ),
    )
    routine_done.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )
    # Surgical single-date un-log — the inverse of ``done``. Same
    # two-positional shape (record + item, OR item-alone for vault-wide
    # fuzzy). Removes ONE date from completion_log[item]; a date that
    # isn't logged is an explicit no-op (not_logged canary, exit 0).
    routine_undone = routine_sub.add_parser(
        "undone",
        help="Remove one logged completion date (inverse of done; default: today)",
    )
    routine_undone.add_argument(
        "record_or_item",
        help=(
            "Routine record name (e.g. 'For Self Health') OR — when "
            "<item> is omitted — the item text to fuzzy-match vault-wide."
        ),
    )
    routine_undone.add_argument(
        "item",
        nargs="?",
        default=None,
        help=(
            "Item text within the routine (e.g. 'Dog Walk'). Omit to "
            "treat <record_or_item> as the item text and do a vault-wide "
            "fuzzy match."
        ),
    )
    routine_undone.add_argument(
        "--date",
        dest="date",
        default=None,
        help=(
            "The YYYY-MM-DD completion date to remove. Defaults to today "
            "(in config.schedule.timezone). A date that isn't logged is a "
            "no-op (nothing removed)."
        ),
    )
    routine_undone.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )
    routine_run = routine_sub.add_parser(
        "run-now",
        help="Force-build today's daily aggregator note now",
    )
    routine_run.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )
    routine_status = routine_sub.add_parser(
        "status",
        help="Show last aggregator run + schedule summary",
    )
    routine_status.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )

    # Phase 2B B3 (2026-05-30) — ``alfred routine item <action>``
    # tree. Three actions (add / remove / edit) for item-level
    # operations on existing routine records. Each shares the
    # canary-on---json + Salem-only + fuzzy-match contract from B1's
    # ``alfred routine done``.
    routine_item_p = routine_sub.add_parser(
        "item",
        help="Item-level CRUD on existing routines (add / remove / edit)",
    )
    routine_item_sub = routine_item_p.add_subparsers(
        dest="routine_item_action",
    )

    # --- item add ---
    item_add = routine_item_sub.add_parser(
        "add",
        help="Append a new item to a routine's items list",
    )
    item_add.add_argument(
        "record",
        help=(
            "Routine record name (REQUIRED for add — vault-wide fuzzy "
            "doesn't apply when adding a NEW item that doesn't exist "
            "anywhere)."
        ),
    )
    item_add.add_argument(
        "text",
        help="New item's text (e.g. 'Walk dog').",
    )
    item_add.add_argument(
        "--priority",
        choices=["critical", "tracked", "aspirational"],
        default=None,
        help="Item priority. Defaults to 'tracked' when omitted.",
    )
    item_add.add_argument(
        "--target-cadence-days",
        dest="target_cadence_days",
        type=int, default=None,
        help=(
            "Soft cadence target — item surfaces in T3 auto-suggest "
            "when days_since_last_completed >= N. Mutually exclusive "
            "with --due-pattern."
        ),
    )
    item_add.add_argument(
        "--due-pattern",
        dest="due_pattern",
        default=None,
        help=(
            "Hard cadence shape as JSON. Example: "
            "'{\"type\":\"weekly\",\"day\":\"thu\"}'. See "
            "alfred.routine.config.DUE_PATTERN_TYPES for the six "
            "valid types. Mutually exclusive with "
            "--target-cadence-days."
        ),
    )
    item_add.add_argument(
        "--surface-at-days",
        dest="surface_at_days",
        type=int, default=None,
        help=(
            "T2 ramp threshold (days before due). Requires "
            "--due-pattern."
        ),
    )
    item_add.add_argument(
        "--escalate-at-days",
        dest="escalate_at_days",
        type=int, default=None,
        help=(
            "T1 escalation threshold (days before due). 0 = T1 fires "
            "on the due date itself. Requires --due-pattern."
        ),
    )
    item_add.add_argument(
        "--self-care", dest="self_care",
        action="store_true", default=None,
        help=(
            "Mark the item as self-care (routes to the T3 self-care lane "
            "in the tier view — intrinsic, never deadline-escalates). "
            "Default off; omit for a non-self-care item."
        ),
    )
    item_add.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )

    # --- item remove ---
    item_remove = routine_item_sub.add_parser(
        "remove",
        help=(
            "Remove an item from a routine. Strips completion_log "
            "entries for that item atomically."
        ),
    )
    item_remove.add_argument(
        "record_or_item",
        help=(
            "Routine record name (e.g. 'For Self Health') OR — when "
            "<item> is omitted — the item text to fuzzy-match "
            "vault-wide. Mirrors B1's two-positional form."
        ),
    )
    item_remove.add_argument(
        "item",
        nargs="?",
        default=None,
        help=(
            "Item text within the routine. Omit to treat "
            "<record_or_item> as the item text and do a vault-wide "
            "fuzzy match."
        ),
    )
    item_remove.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )

    # --- item edit ---
    item_edit = routine_item_sub.add_parser(
        "edit",
        help=(
            "Edit one item's fields. Rename (--text NEW) migrates "
            "completion_log atomically."
        ),
    )
    item_edit.add_argument(
        "record_or_item",
        help="Routine record name OR (when <item> omitted) item text.",
    )
    item_edit.add_argument(
        "item",
        nargs="?",
        default=None,
        help=(
            "Item text within the routine. Omit to treat "
            "<record_or_item> as the item text and do a vault-wide "
            "fuzzy match."
        ),
    )
    item_edit.add_argument(
        "--text",
        dest="new_text",
        default=None,
        help=(
            "Rename the item to this text. Migrates completion_log "
            "key from old text to new atomically."
        ),
    )
    item_edit.add_argument(
        "--priority",
        choices=["critical", "tracked", "aspirational"],
        default=None,
    )
    item_edit.add_argument(
        "--target-cadence-days",
        dest="target_cadence_days",
        type=int, default=None,
    )
    item_edit.add_argument(
        "--due-pattern",
        dest="due_pattern",
        default=None,
    )
    item_edit.add_argument(
        "--surface-at-days",
        dest="surface_at_days",
        type=int, default=None,
    )
    item_edit.add_argument(
        "--escalate-at-days",
        dest="escalate_at_days",
        type=int, default=None,
    )
    item_edit.add_argument(
        "--clear-due-pattern",
        dest="clear_due_pattern",
        action="store_true", default=False,
        help=(
            "Strip due_pattern + escalate_at_days + surface_at_days "
            "from the item. Required when switching hard → soft "
            "cadence (operator opt-in for the mode change)."
        ),
    )
    item_edit.add_argument(
        "--clear-target-cadence-days",
        dest="clear_target_cadence_days",
        action="store_true", default=False,
        help=(
            "Strip target_cadence_days from the item. Required when "
            "switching soft → hard cadence."
        ),
    )
    item_edit.add_argument(
        "--self-care", dest="self_care",
        action=argparse.BooleanOptionalAction, default=None,
        help=(
            "Mark (--self-care) or unmark (--no-self-care) the item as "
            "self-care → the T3 self-care lane. Omit to leave unchanged."
        ),
    )
    item_edit.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
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

    # ticket-forward — VERA→KAL-LE ticket pipeline forwarder (c4)
    tf_p = sub.add_parser(
        "ticket-forward",
        help=(
            "VERA ticket forwarder — scan open tickets, push to the "
            "intake peer as kind=ticket"
        ),
    )
    tf_sub = tf_p.add_subparsers(dest="ticket_forward_cmd")
    tf_run = tf_sub.add_parser(
        "run-once",
        help="Run one forward tick now (the pipeline's probe surface)",
    )
    tf_run.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )
    tf_status = tf_sub.add_parser(
        "status", help="Show forwarder state summary (linked/pending)",
    )
    tf_status.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )

    # msg — inter-project message bus (V1): send / inbox / route-once / status
    msg_p = sub.add_parser(
        "msg",
        help=(
            "Inter-project message bus — send a message, drain a project "
            "inbox, run the router, or show bus status"
        ),
    )
    msg_sub = msg_p.add_subparsers(dest="msg_cmd")
    msg_send = msg_sub.add_parser(
        "send", help="Mint an id + drop a message into the spool",
    )
    msg_send.add_argument("--to", required=True, help="Destination project slug")
    msg_send.add_argument(
        "--kind", required=True,
        choices=["handover", "request", "fyi", "reply"],
    )
    msg_send.add_argument("--subject", required=True, help="One-line title")
    msg_send.add_argument(
        "--from", dest="from_project", default="",
        help="Sender project slug (default: message_bus.self_project)",
    )
    msg_send.add_argument(
        "--correlation-id", dest="correlation_id", default="",
        help="Thread id (default: mint a fresh one)",
    )
    msg_send.add_argument(
        "--reply-to", dest="reply_to", default="",
        help="id of the message being answered (on kind=reply)",
    )
    msg_send.add_argument(
        "--precedence", default="R", choices=["Z", "O", "P", "R"],
    )
    msg_send.add_argument(
        "--route", "--now", dest="route", action="store_true", default=False,
        help="Route to the peer inbox immediately (skip the cron) — Path B",
    )
    msg_body = msg_send.add_mutually_exclusive_group()
    msg_body.add_argument(
        "--body-file", dest="body_file", default="",
        help="Read the body from a file ('-' for stdin)",
    )
    msg_body.add_argument(
        "--body", default="", help="Inline body ('-' for stdin)",
    )
    msg_inbox = msg_sub.add_parser(
        "inbox", help="Inspect/drain a project inbox",
    )
    # project FIRST + optional (the `inbox <project> {list|read|drain}`
    # surface). argparse assigns the lone token of `inbox list` to the
    # required choices-positional, so project defaults to self_project —
    # verified across all forms (project-first is the ONLY ordering that
    # keeps `inbox <project> list` AND `inbox list` both parsing).
    msg_inbox.add_argument(
        "project", nargs="?", default="",
        help="Project slug (default: message_bus.self_project)",
    )
    msg_inbox.add_argument(
        "inbox_action", choices=["list", "read", "drain"],
    )
    msg_inbox.add_argument(
        "message_id", nargs="?", default="", help="Message id (for read)",
    )
    msg_inbox.add_argument(
        "--json", action="store_true", default=False,
        help="drain: emit full records (incl. body) as JSON — Path B loop surface",
    )
    msg_route = msg_sub.add_parser(
        "route-once", help="Run one routing tick now (the probe surface)",
    )
    msg_route.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )
    msg_status = msg_sub.add_parser(
        "status", help="Show bus state + per-project unread counts",
    )
    msg_status.add_argument(
        "--json", action="store_true", default=False, help="Emit JSON",
    )

    # contract — Layer-2 contract negotiation (the meet-in-the-middle gate)
    from alfred.contracts.cli import build_subparser as _build_contract_subparser
    _build_contract_subparser(sub)

    # instance — Stage 3.5 multi-instance scaffolding + Algernon
    # platform wrapper (Phase 1, 2026-05-28)
    instance_p = sub.add_parser(
        "instance",
        help=(
            "Multi-instance management — new (scaffold) + "
            "up/down/status (fan-out across registered instances)"
        ),
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

    # Phase 1 Algernon platform wrapper sub-verbs. The ``--registry``
    # flag is on each subcommand so the operator can override the
    # default ``~/.alfred/instances.yaml`` for testing or alt-installs.
    inst_up = instance_sub.add_parser(
        "up", help="Start every enabled instance in the registry",
    )
    inst_up.add_argument(
        "--registry", default=None,
        help="Override the registry path (default: ~/.alfred/instances.yaml)",
    )
    inst_down = instance_sub.add_parser(
        "down", help="Stop every enabled instance in the registry",
    )
    inst_down.add_argument(
        "--registry", default=None,
        help="Override the registry path (default: ~/.alfred/instances.yaml)",
    )
    inst_status = instance_sub.add_parser(
        "status",
        help="Show running state per enabled instance",
    )
    inst_status.add_argument(
        "--registry", default=None,
        help="Override the registry path (default: ~/.alfred/instances.yaml)",
    )
    inst_status.add_argument(
        "--verbose", action="store_true", default=False,
        help=(
            "Concatenate full ``alfred status`` output per instance "
            "with section headers instead of the one-line summary"
        ),
    )
    inst_status.add_argument(
        "--json", action="store_true", default=False,
        help=(
            "Aggregate per-instance ``alfred status --json`` blobs "
            "into one top-level dict keyed by instance name"
        ),
    )

    # Suppressed top-level aliases — preserve muscle-memory typing
    # for ``alfred up-all`` / ``alfred down-all`` / ``alfred status-all``
    # without cluttering ``--help`` per ratified Phase 1 decision #2.
    # These dispatch to the same handlers as the canonical
    # ``alfred instance ...`` form.
    up_all = sub.add_parser("up-all", help=argparse.SUPPRESS)
    up_all.add_argument("--registry", default=None, help=argparse.SUPPRESS)
    down_all = sub.add_parser("down-all", help=argparse.SUPPRESS)
    down_all.add_argument("--registry", default=None, help=argparse.SUPPRESS)
    status_all = sub.add_parser("status-all", help=argparse.SUPPRESS)
    status_all.add_argument("--registry", default=None, help=argparse.SUPPRESS)
    status_all.add_argument("--verbose", action="store_true", default=False, help=argparse.SUPPRESS)
    status_all.add_argument("--json", action="store_true", default=False, help=argparse.SUPPRESS)

    # mail
    mail_p = sub.add_parser("mail", help="Email fetcher subcommands")
    mail_sub = mail_p.add_subparsers(dest="mail_cmd")
    mail_fetch = mail_sub.add_parser("fetch", help="Fetch new emails from configured accounts")
    mail_fetch.add_argument("--once", action="store_true", default=False, help="Fetch once and exit (no polling)")
    mail_sub.add_parser("status", help="Show mail fetcher state")
    mail_webhook = mail_sub.add_parser("webhook", help="Start webhook receiver for incoming email")
    mail_webhook.add_argument("--port", type=int, default=5005, help="Port to listen on (default: 5005)")
    mail_webhook.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Host to bind (default: 127.0.0.1, loopback). The Cloudflare "
            "tunnel proxies to localhost:5005, so loopback is the correct "
            "bind; pass 0.0.0.0 only if fronting with another reverse proxy."
        ),
    )

    # gcal (Phase A+ inter-instance comms — Google Calendar integration)
    gcal_p = sub.add_parser(
        "gcal",
        help="Google Calendar integration (authorize / status / test-write)",
    )
    gcal_sub = gcal_p.add_subparsers(dest="gcal_cmd")
    gcal_sub.add_parser(
        "authorize",
        help="One-time OAuth flow — opens browser, saves token to disk",
    )
    gcal_status_p = gcal_sub.add_parser(
        "status",
        help="Show GCal config + token state + next-24h event counts",
    )
    gcal_status_p.add_argument(
        "--json", action="store_true", default=False,
        help="Emit machine-readable JSON instead of human-readable output",
    )
    gcal_test_p = gcal_sub.add_parser(
        "test-write",
        help="Create a throwaway test event on Andrew's Calendar (S.A.L.E.M.)",
    )
    gcal_test_p.add_argument(
        "--no-cleanup", action="store_true", default=False,
        help="Leave the test event in place (visible on your phone)",
    )
    gcal_test_p.add_argument(
        "--json", action="store_true", default=False,
        help="Emit machine-readable JSON instead of human-readable output",
    )
    gcal_backfill_p = gcal_sub.add_parser(
        "backfill",
        help=(
            "Push existing vault event records (without gcal_event_id) "
            "to Andrew's Calendar (S.A.L.E.M.); writes back the GCal IDs"
        ),
    )
    gcal_backfill_p.add_argument(
        "--dry-run", action="store_true", default=False,
        help="Report what would happen without making API calls or vault writes",
    )
    gcal_backfill_p.add_argument(
        "--from-date", default=None,
        help=(
            "ISO YYYY-MM-DD cutoff — events with start.date() before this "
            "are skipped (default: today; pass an earlier date to backfill "
            "historical events)"
        ),
    )
    gcal_backfill_p.add_argument(
        "--infer-times", dest="infer_times",
        action="store_true", default=False,
        help=(
            "Opt-in: for legacy records with date+time fields but no ISO "
            "start/end, combine into ISO datetimes (Halifax tz) using a "
            "duration heuristic on the title, write back to vault, then "
            "sync. Default-off preserves the 'refuse to fabricate "
            "timestamps' safety."
        ),
    )
    gcal_backfill_p.add_argument(
        "--json", action="store_true", default=False,
        help="Emit machine-readable JSON instead of human-readable output",
    )

    # gcal collapse — §3 same-day umbrella. Recompute ONE collapse group
    # (events sharing gcal_collapse_key on --date) into a single GCal entry
    # in one pass — the churn-free batch/backfill path (the talker also
    # collapses incrementally via vault_edit).
    gcal_collapse_p = gcal_sub.add_parser(
        "collapse",
        help=(
            "Reconcile a same-day collapse group (gcal_collapse_key + date) "
            "into one umbrella GCal entry"
        ),
    )
    gcal_collapse_p.add_argument(
        "--key", required=True,
        help="The gcal_collapse_key series label (e.g. 'rTMS')",
    )
    gcal_collapse_p.add_argument(
        "--date", required=True,
        help="The group date (YYYY-MM-DD)",
    )
    gcal_collapse_p.add_argument(
        "--json", action="store_true", default=False,
        help="Emit machine-readable JSON instead of human-readable output",
    )

    # fiction — Hypatia Phase 2.5 fiction posture (scaffold/slug
    # helpers; both invoke alfred.telegram.fiction so the SKILL's
    # natural-language scaffolding path produces the same on-disk
    # shape as the /fiction slash command).
    fiction_p = sub.add_parser(
        "fiction",
        help="Hypatia fiction-posture helpers (scaffold a project / derive a slug)",
    )
    fiction_sub = fiction_p.add_subparsers(dest="fiction_cmd")
    fiction_scaffold_p = fiction_sub.add_parser(
        "scaffold",
        help=(
            "Scaffold a fiction project directory + per-element files. "
            "Prints JSON for SKILL consumption: "
            "{slug, path, files_created, already_existed}."
        ),
    )
    fiction_scaffold_p.add_argument(
        "title",
        help='Project title (quote it: alfred fiction scaffold "The Glass Forest")',
    )
    fiction_slug_p = fiction_sub.add_parser(
        "slug",
        help=(
            "Print the canonical slug for a title. Useful for the "
            "SKILL when constructing a wikilink before invoking scaffold."
        ),
    )
    fiction_slug_p.add_argument(
        "title",
        help='Project title (quote it: alfred fiction slug "The Glass Forest")',
    )

    # audit (calibration audit gap, c3 retroactive sweep CLI)
    from alfred.audit import cli as audit_cli
    audit_cli.build_parser(sub)

    # scaffold — Build #38, diff-and-copy bundled scaffold into existing vaults
    from alfred.scaffold import cli as scaffold_cli
    scaffold_cli.build_parser(sub)

    # reviews — KAL-LE per-project review files
    from alfred.reviews import cli as reviews_cli
    reviews_cli.build_subparser(sub)

    # digest — KAL-LE cross-arc weekly synthesis
    from alfred.digest import cli as digest_cli
    digest_cli.build_subparser(sub)

    # prefs — operator-preference V1 (project_operator_preferences_v1).
    # Manages the JSON index that curator + brief consumers read for
    # Shape A action gates. Voice preferences (Shape B) don't need an
    # index — the talker reads them directly from the vault on each
    # session start.
    prefs_p = sub.add_parser(
        "prefs",
        help=(
            "Operator-preference management — rebuild the action-gate "
            "index (Shape A preferences). Voice preferences (Shape B) "
            "are read directly from the vault by the talker."
        ),
    )
    prefs_sub = prefs_p.add_subparsers(dest="prefs_cmd")
    prefs_rebuild = prefs_sub.add_parser(
        "rebuild-index",
        help=(
            "Rebuild data/operator_preferences.json from the active "
            "preference/ records in the vault. Atomic write — old "
            "index stays in place if the rebuild crashes."
        ),
    )
    prefs_rebuild.add_argument(
        "--output",
        default=None,
        help=(
            "Override the index output path. Default: "
            "<logging.dir>/operator_preferences.json (mirrors how "
            "state files resolve their default location)."
        ),
    )

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
        # Algernon platform wrapper Phase 1 (2026-05-28) — suppressed
        # top-level aliases for ``alfred instance up | down | status``.
        # Dispatch to the same handlers as the canonical form per
        # ratified Phase 1 decision #2.
        "up-all": cmd_instance_up_all,
        "down-all": cmd_instance_down_all,
        "status-all": cmd_instance_status_all,
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
        "scribe": cmd_scribe,
        "instance": cmd_instance,
        "talker": cmd_talker,
        "voice": cmd_voice,
        "check": cmd_check,
        "check-tool-schemas": cmd_check_tool_schemas,
        "bit": cmd_bit,
        "routine": cmd_routine,
        "ticket-forward": cmd_ticket_forward,
        "msg": cmd_msg,
        "contract": cmd_contract,
        "audit": cmd_audit,
        "scaffold": cmd_scaffold,
        "reviews": cmd_reviews,
        "digest": cmd_digest,
        "gcal": cmd_gcal,
        "fiction": cmd_fiction,
        "prefs": cmd_prefs,
    }

    handler = handlers.get(args.command)
    if handler:
        handler(args)
    else:
        parser.print_help()
        sys.exit(1)
