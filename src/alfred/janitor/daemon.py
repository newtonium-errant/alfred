"""Sweep orchestrator — two-phase scan + fix pipeline.

For OpenClaw backends, uses a 3-stage pipeline (pipeline.py) for better quality:
  Stage 1: AUTOFIX (pure Python) — deterministic fixes
  Stage 2: LINK REPAIR (LLM per-file) — broken wikilinks
  Stage 3: ENRICH (LLM per-file) — stub records

For other backends, falls back to the legacy single-LLM-call approach.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from alfred.common.heartbeat import Heartbeat
from alfred.common.schedule import compute_next_fire
from alfred.vault.mutation_log import append_to_audit_log, cleanup_session_file, create_session_file, read_mutations
from alfred.vault.ops import is_ignored_path

from .backends import BaseBackend, BackendResult, build_issue_report
from .triage import collect_open_triage_tasks, format_open_triage_block
from .backends.cli import ClaudeBackend
from .backends.http import ZoBackend
from .backends.openclaw import OpenClawBackend
from .config import JanitorConfig
from .context import build_vault_context
from .issues import FixLogEntry, Issue, SweepResult, Severity
from .parser import parse_file
from .pipeline import run_pipeline
from .scanner import run_structural_scan
from .state import JanitorState
from .utils import file_hash, get_logger

log = get_logger(__name__)

# Module-level idle-tick heartbeat — see ``alfred.common.heartbeat`` for
# the rationale ("intentionally left blank" pattern). Counter is bumped
# in :func:`run_sweep` after a sweep that fixed/deleted issues — the
# meaningful signal, not noisy ``issues_found`` from clean scans. The
# heartbeat task is spawned in :func:`run_watch` only when
# ``config.idle_tick.enabled`` is True.
heartbeat: Heartbeat = Heartbeat(daemon_name="janitor", log=log)


def _use_pipeline(config: JanitorConfig) -> bool:
    """Check if the 3-stage pipeline should be used (OpenClaw backend only)."""
    return config.agent.backend == "openclaw"


def _load_skill(skills_dir: Path) -> str:
    """Load SKILL.md and all reference templates into a single text block."""
    skill_path = skills_dir / "vault-janitor" / "SKILL.md"
    if not skill_path.exists():
        log.warning("daemon.skill_not_found", path=str(skill_path))
        return ""

    parts: list[str] = [skill_path.read_text(encoding="utf-8")]

    refs_dir = skills_dir / "vault-janitor" / "references"
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.glob("*.md")):
            content = ref_file.read_text(encoding="utf-8")
            parts.append(f"\n---\n### Reference Template: {ref_file.name}\n```\n{content}\n```")

    return "\n".join(parts)


def _create_backend(config: JanitorConfig) -> BaseBackend:
    """Instantiate the configured backend."""
    backend_name = config.agent.backend
    if backend_name == "claude":
        return ClaudeBackend(config.agent.claude)
    elif backend_name == "zo":
        return ZoBackend(config.agent.zo)
    elif backend_name == "openclaw":
        return OpenClawBackend(config.agent.openclaw)
    else:
        raise ValueError(f"Unknown backend: {backend_name}")


def snapshot_vault(vault_path: Path, ignore_dirs: list[str] | None = None) -> dict[str, str]:
    """Capture SHA-256 checksums of all .md files in the vault."""
    ignore = set(ignore_dirs or [])
    checksums: dict[str, str] = {}

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        if is_ignored_path(rel, ignore):
            continue
        try:
            content = md_file.read_bytes()
            checksums[str(rel).replace("\\", "/")] = hashlib.sha256(content).hexdigest()
        except OSError:
            continue

    return checksums


def diff_vault(
    before: dict[str, str],
    after: dict[str, str],
) -> tuple[list[str], list[str], list[str]]:
    """Compare two vault snapshots. Returns (created, modified, deleted)."""
    created: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []

    for path, checksum in after.items():
        if path not in before:
            created.append(path)
        elif before[path] != checksum:
            modified.append(path)

    for path in before:
        if path not in after:
            deleted.append(path)

    return created, modified, deleted


def _build_affected_records(
    issues: list[Issue],
    vault_path: Path,
) -> str:
    """Read affected files and format for agent prompt."""
    seen: set[str] = set()
    parts: list[str] = []

    for issue in issues:
        if issue.file in seen:
            continue
        seen.add(issue.file)

        full_path = vault_path / issue.file
        try:
            content = full_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            content = "(unreadable)"

        parts.append(f"### {issue.file}\n```\n{content}\n```\n")

    return "\n".join(parts)


def _record_triage_ids_from_created(
    created_paths: list[str],
    vault_path: Path,
    state: JanitorState,
) -> None:
    """Scan newly-created task files for triage frontmatter and record IDs.

    Hard idempotency layer: any task created with ``alfred_triage: true`` has
    its ``alfred_triage_id`` recorded in ``state.triage_ids_seen`` so that
    closed/deleted triage tasks cannot be re-surfaced by a future sweep.
    Non-triage tasks are silently skipped.
    """
    for rel_path in created_paths:
        if not rel_path.startswith("task/") or not rel_path.endswith(".md"):
            continue
        full_path = vault_path / rel_path
        try:
            post = frontmatter.load(str(full_path))
        except Exception as exc:  # noqa: BLE001 — skip unreadable
            log.warning(
                "daemon.triage_parse_failed",
                path=rel_path,
                error=str(exc)[:200],
            )
            continue

        fm = post.metadata or {}
        if not fm.get("alfred_triage"):
            continue

        triage_id = str(fm.get("alfred_triage_id", "")).strip()
        if not triage_id:
            log.warning(
                "daemon.triage_create_missing_id",
                path=rel_path,
            )
            continue

        state.mark_triage_seen(triage_id)
        log.info(
            "daemon.triage_id_recorded",
            triage_id=triage_id,
            path=rel_path,
        )


async def run_sweep(
    config: JanitorConfig,
    state: JanitorState,
    skills_dir: Path,
    structural_only: bool = False,
    fix_mode: bool = False,
) -> SweepResult:
    """Run a complete sweep: Phase 1 structural scan + optional Phase 2 agent fix."""
    sweep_id = str(uuid.uuid4())[:8]
    log.info("sweep.start", sweep_id=sweep_id, fix_mode=fix_mode, structural_only=structural_only)

    # Phase 1: Structural scan
    issues = run_structural_scan(config, state)

    result = SweepResult(
        sweep_id=sweep_id,
        files_scanned=len(state.files),
        issues_found=len(issues),
        issues=issues,
        structural_only=structural_only,
    )

    # Count by severity
    for issue in issues:
        sev = issue.severity.value
        result.issues_by_severity[sev] = result.issues_by_severity.get(sev, 0) + 1

    if not issues:
        log.info("sweep.clean", sweep_id=sweep_id)
        state.add_sweep(result)
        state.save()
        return result

    # Phase 2: Fix (only if fix_mode and not structural_only)
    if fix_mode and not structural_only:
        if _use_pipeline(config):
            # 3-stage pipeline for OpenClaw backend
            session_path = create_session_file()

            pipeline_result = await run_pipeline(
                issues=issues,
                config=config,
                session_path=session_path,
                state=state,
            )

            mutations = read_mutations(session_path)
            created = mutations["files_created"]
            modified = mutations["files_modified"]
            deleted = mutations["files_deleted"]

            # Layer 3: record any newly-created triage task IDs in state so
            # they cannot be re-surfaced on the next sweep even if closed.
            # Heartbeat log below makes every fix-mode sweep visible in
            # janitor.log even when `created` is empty, so a "no activity"
            # scenario shows up as an absence of this event rather than an
            # absence of the downstream `daemon.triage_id_recorded` event.
            log.info("daemon.triage_scan", created_count=len(created), sweep_id=sweep_id)
            _record_triage_ids_from_created(created, config.vault.vault_path, state)

            # Audit log
            audit_mutations = {"files_created": created, "files_modified": modified, "files_deleted": deleted}
            audit_path = str(Path(config.state.path).parent / "vault_audit.log")
            append_to_audit_log(audit_path, "janitor", audit_mutations, detail=sweep_id)

            # Cleanup is the LAST session-related operation — read mutations,
            # act on them, then cleanup. Avoids brittleness if future changes
            # want to read session-derived data during the helper.
            cleanup_session_file(session_path)

            result.files_fixed += len(modified) + len(created)
            result.files_deleted += len(deleted)
            result.agent_invoked = True

            for f in modified:
                state.add_fix_log(FixLogEntry(
                    sweep_id=sweep_id,
                    action="fixed",
                    file=f,
                    detail=f"Pipeline: {pipeline_result.summary[:200]}",
                ))
            for f in created:
                state.add_fix_log(FixLogEntry(
                    sweep_id=sweep_id,
                    action="fixed",
                    file=f,
                    detail="Created by pipeline",
                ))
            for f in deleted:
                state.add_fix_log(FixLogEntry(
                    sweep_id=sweep_id,
                    action="deleted",
                    file=f,
                    detail="Deleted by pipeline",
                ))

            if not pipeline_result.success:
                log.error(
                    "sweep.pipeline_failed",
                    sweep_id=sweep_id,
                    summary=pipeline_result.summary[:500],
                )
        else:
            # Legacy path for Claude and Zo backends
            skill_text = _load_skill(skills_dir)
            if not skill_text:
                log.warning("sweep.no_skill", msg="No SKILL.md found — skipping agent fix")
            else:
                backend = _create_backend(config)
                vault_path = config.vault.vault_path
                use_mutation_log = isinstance(backend, (ClaudeBackend, OpenClawBackend))

                # Batch issues if too many
                max_per_call = config.sweep.max_files_per_agent_call
                affected_files = list({i.file for i in issues})

                # Layer 3: surface existing open triage tasks so the agent
                # can skip already-queued candidates. Computed once per sweep.
                open_triage_tasks = collect_open_triage_tasks(vault_path)
                open_triage_block = format_open_triage_block(
                    open_triage_tasks,
                    seen_ids=state.triage_ids_seen,
                )

                for batch_start in range(0, len(affected_files), max_per_call):
                    batch_files = set(affected_files[batch_start:batch_start + max_per_call])
                    batch_issues = [i for i in issues if i.file in batch_files]

                    issue_report = build_issue_report(batch_issues)
                    affected_records = _build_affected_records(batch_issues, vault_path)

                    session_path = None
                    if use_mutation_log:
                        session_path = create_session_file()
                        backend.env_overrides = {
                            "ALFRED_VAULT_PATH": str(vault_path),
                            "ALFRED_VAULT_SCOPE": "janitor",
                            "ALFRED_VAULT_SESSION": session_path,
                        }
                    else:
                        before = snapshot_vault(vault_path, config.vault.ignore_dirs)

                    # Invoke agent
                    log.info(
                        "sweep.agent_invoke",
                        sweep_id=sweep_id,
                        batch_files=len(batch_files),
                        batch_issues=len(batch_issues),
                    )
                    agent_result = await backend.process(
                        skill_text=skill_text,
                        issue_report=issue_report,
                        affected_records=affected_records,
                        vault_path=str(vault_path),
                        open_triage_block=open_triage_block,
                    )

                    # Determine what changed
                    if use_mutation_log and session_path:
                        mutations = read_mutations(session_path)
                        created = mutations["files_created"]
                        modified = mutations["files_modified"]
                        deleted = mutations["files_deleted"]
                    else:
                        after = snapshot_vault(vault_path, config.vault.ignore_dirs)
                        created, modified, deleted = diff_vault(before, after)

                    # Layer 3: record any newly-created triage task IDs in
                    # state so they cannot be re-surfaced on the next sweep
                    # even if the human closes or deletes them. Handles the
                    # empty-created case naturally (loop is a no-op).
                    # Heartbeat log below makes every fix-mode sweep visible
                    # in janitor.log even when `created` is empty.
                    log.info("daemon.triage_scan", created_count=len(created), sweep_id=sweep_id)
                    _record_triage_ids_from_created(created, vault_path, state)

                    # Audit log
                    audit_mutations = {"files_created": created, "files_modified": modified, "files_deleted": deleted}
                    audit_path = str(Path(config.state.path).parent / "vault_audit.log")
                    append_to_audit_log(audit_path, "janitor", audit_mutations, detail=sweep_id)

                    # Cleanup is the LAST session-related operation — read
                    # mutations, act on them, then cleanup. Avoids brittleness
                    # if future changes read session-derived data in helpers.
                    if use_mutation_log and session_path:
                        cleanup_session_file(session_path)

                    result.files_fixed += len(modified) + len(created)
                    result.files_deleted += len(deleted)
                    result.agent_invoked = True

                    # Log actions
                    for f in modified:
                        state.add_fix_log(FixLogEntry(
                            sweep_id=sweep_id,
                            action="fixed",
                            file=f,
                            detail="Modified by agent",
                        ))
                    for f in deleted:
                        state.add_fix_log(FixLogEntry(
                            sweep_id=sweep_id,
                            action="deleted",
                            file=f,
                            detail="Deleted by agent",
                        ))
                    for f in created:
                        state.add_fix_log(FixLogEntry(
                            sweep_id=sweep_id,
                            action="fixed",
                            file=f,
                            detail="Created by agent",
                        ))

                    if not agent_result.success:
                        log.error(
                            "sweep.agent_failed",
                            sweep_id=sweep_id,
                            summary=agent_result.summary[:500],
                        )

    log.info(
        "sweep.complete",
        sweep_id=sweep_id,
        issues=len(issues),
        fixed=result.files_fixed,
        deleted=result.files_deleted,
    )

    # Idle-tick counter — one issue fixed (or deleted) counts as one
    # event. Sweeps that found nothing broken (issues_found == 0 or
    # fix_mode disabled) add zero, so the heartbeat reflects the
    # meaningful signal rather than scan noise.
    fixed_or_deleted = result.files_fixed + result.files_deleted
    for _ in range(fixed_or_deleted):
        heartbeat.record_event()

    state.add_sweep(result)
    state.save()
    return result


async def run_watch(
    config: JanitorConfig,
    state: JanitorState,
    skills_dir: Path,
) -> None:
    """Daemon mode — sweep on interval until interrupted."""
    interval = config.sweep.interval_seconds
    deep_schedule = config.sweep.deep_sweep_schedule
    structural_only = config.sweep.structural_only

    # Persist last deep sweep time across restarts. Without this, every
    # daemon restart reset last_deep to epoch and triggered a full deep
    # sweep (LLM-heavy) on boot — a runaway-cost bug in upstream. On
    # first boot we seed ``last_deep`` to "now" so the first fire is at
    # the next scheduled window (e.g. 02:30 Halifax) rather than
    # immediately on start.
    now_utc_init = datetime.now(timezone.utc)
    if state.last_deep_sweep:
        try:
            last_deep = datetime.fromisoformat(state.last_deep_sweep)
        except (ValueError, TypeError):
            last_deep = now_utc_init
    else:
        last_deep = now_utc_init
    last_drift = datetime.min.replace(tzinfo=timezone.utc)
    drift_interval_hours = config.sweep.drift_sweep_interval_hours

    log.info(
        "daemon.starting",
        interval=interval,
        deep_sweep_time=deep_schedule.time,
        deep_sweep_tz=deep_schedule.timezone,
        deep_sweep_day_of_week=deep_schedule.day_of_week,
        drift_interval_hours=drift_interval_hours,
    )

    # Idle-tick heartbeat task — emits ``janitor.idle_tick`` every
    # ``config.idle_tick.interval_seconds``. Default 60s, on by default.
    # See ``alfred.common.heartbeat`` for the "intentionally left blank"
    # rationale. Spawned only when enabled — disabled path is silent.
    heartbeat_shutdown = asyncio.Event()
    heartbeat_task: asyncio.Task | None = None
    if config.idle_tick.enabled:
        heartbeat_task = asyncio.create_task(
            heartbeat.run(
                interval_seconds=config.idle_tick.interval_seconds,
                shutdown_event=heartbeat_shutdown,
            ),
            name="janitor-heartbeat",
        )
        log.info(
            "daemon.heartbeat_started",
            interval_seconds=config.idle_tick.interval_seconds,
        )

    while True:
        now = datetime.now(timezone.utc)

        # Clock-aligned deep sweep gate: fire when we've crossed the
        # next scheduled fire time relative to the last one. The helper
        # returns the next fire strictly after its input, so we never
        # double-fire within the same scheduled window — restarts that
        # happen between 02:30 and the next 02:30 don't re-fire because
        # ``last_deep`` was updated on the previous successful fire.
        next_fire_after_last = compute_next_fire(deep_schedule, last_deep)
        # ``now`` is UTC; the helper returns in the schedule's tz. Both
        # are tz-aware, so Python's datetime comparison handles the
        # offset correctly.
        deep_due = now >= next_fire_after_last

        try:
            if deep_due:
                # Event-driven deep sweep (upstream #15). Do a cheap
                # structural scan first and compare the resulting issue
                # set against the previous sweep's snapshot. If no new
                # issue codes AND no file content has changed since last
                # time, skip the expensive LLM fix pipeline entirely —
                # but still bump last_deep so we do not spin here every
                # interval retrying the same check.
                log.info("daemon.deep_sweep_check")
                pre_scan_issues = run_structural_scan(config, state)
                current_issue_map: dict[str, list[str]] = {}
                for iss in pre_scan_issues:
                    current_issue_map.setdefault(iss.file, []).append(iss.code.value)

                # Detect content changes since the stored snapshot. A
                # change resets that file's Stage 3 enrichment staleness
                # so a newly-edited stub becomes eligible for enrichment
                # again.
                changed_files: set[str] = set()
                for rel_path, fs in state.files.items():
                    full = config.vault.vault_path / rel_path
                    if full.exists():
                        try:
                            cur_md5 = file_hash(full)
                        except OSError:
                            continue
                        if cur_md5 != fs.md5:
                            changed_files.add(rel_path)
                            state.reset_enrichment_staleness(rel_path)

                new_issues = state.get_new_issues(current_issue_map)

                if not new_issues and not changed_files:
                    # Emit the fix_mode=False heartbeat so operators can
                    # grep `deep_sweep_fix_mode` and see every deep-sweep
                    # tick's fix-mode decision without inferring it from
                    # the `skipped` event's absence of downstream
                    # ``sweep.start`` / ``sweep.agent_invoke`` events.
                    log.info(
                        "daemon.deep_sweep_fix_mode",
                        fix_mode=False,
                        reason="skipped_no_new_issues_or_changes",
                    )
                    log.info(
                        "daemon.deep_sweep_skipped",
                        msg="no new issues and no content changes; skipping fix pipeline",
                    )
                else:
                    log.info(
                        "daemon.deep_sweep",
                        new_issue_files=len(new_issues),
                        changed_files=len(changed_files),
                    )
                    # Operator-visible heartbeat: every deep-sweep tick
                    # that proceeds to the LLM-fix pipeline emits this
                    # with ``fix_mode=True``. Paired with the skipped
                    # branch's fix_mode=False event, this gives a single
                    # grep to answer "did the deep sweep actually engage
                    # fix mode on date X?". Previously operators had to
                    # infer the answer from the presence/absence of
                    # downstream ``sweep.agent_invoke`` events.
                    log.info(
                        "daemon.deep_sweep_fix_mode",
                        fix_mode=True,
                        new_issue_files=len(new_issues),
                        changed_files=len(changed_files),
                    )
                    await run_sweep(
                        config, state, skills_dir,
                        structural_only=False, fix_mode=True,
                    )

                # Store snapshot for next comparison regardless of whether
                # the pipeline ran — otherwise the first non-skip sweep
                # would never see a baseline to diff against.
                state.save_sweep_issues(current_issue_map)
                last_deep = now
                state.last_deep_sweep = now.isoformat()
                state.save()
                log.info(
                    "daemon.deep_sweep_next",
                    next_fire=compute_next_fire(
                        deep_schedule, last_deep,
                    ).isoformat(),
                )
            else:
                # Structural-only sweep
                await run_sweep(config, state, skills_dir, structural_only=True, fix_mode=False)

            # Drift sweep (weekly by default)
            hours_since_drift = (now - last_drift).total_seconds() / 3600
            if hours_since_drift >= drift_interval_hours:
                log.info("daemon.drift_sweep")
                from .scanner import run_drift_scan
                drift_issues = run_drift_scan(config, state)
                if drift_issues:
                    log.info("daemon.drift_issues_found", count=len(drift_issues))
                    for issue in drift_issues:
                        existing = state.files.get(issue.file)
                        md5 = existing.md5 if existing else ""
                        current_codes = list(existing.open_issues) if existing else []
                        if issue.code.value not in current_codes:
                            current_codes.append(issue.code.value)
                        state.update_file(issue.file, md5, current_codes)
                state.save()
                last_drift = now
        except Exception:
            log.exception("daemon.sweep_error")

        await asyncio.sleep(interval)
