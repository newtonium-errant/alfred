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

from alfred.vault.mutation_log import append_to_audit_log, cleanup_session_file, create_session_file, read_mutations

from .backends import BaseBackend, BackendResult, build_issue_report
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
        if any(part in ignore for part in rel.parts):
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
            )

            mutations = read_mutations(session_path)
            created = mutations["files_created"]
            modified = mutations["files_modified"]
            deleted = mutations["files_deleted"]
            cleanup_session_file(session_path)

            # Audit log
            audit_mutations = {"files_created": created, "files_modified": modified, "files_deleted": deleted}
            audit_path = str(Path(config.state.path).parent / "vault_audit.log")
            append_to_audit_log(audit_path, "janitor", audit_mutations, detail=sweep_id)

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
                    )

                    # Determine what changed
                    if use_mutation_log and session_path:
                        mutations = read_mutations(session_path)
                        created = mutations["files_created"]
                        modified = mutations["files_modified"]
                        deleted = mutations["files_deleted"]
                        cleanup_session_file(session_path)
                    else:
                        after = snapshot_vault(vault_path, config.vault.ignore_dirs)
                        created, modified, deleted = diff_vault(before, after)

                    # Audit log
                    audit_mutations = {"files_created": created, "files_modified": modified, "files_deleted": deleted}
                    audit_path = str(Path(config.state.path).parent / "vault_audit.log")
                    append_to_audit_log(audit_path, "janitor", audit_mutations, detail=sweep_id)

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
    deep_interval_hours = config.sweep.deep_sweep_interval_hours
    structural_only = config.sweep.structural_only

    # Start with epoch so the first sweep is always a deep sweep (fix mode)
    last_deep = datetime.min.replace(tzinfo=timezone.utc)
    last_drift = datetime.min.replace(tzinfo=timezone.utc)
    drift_interval_hours = config.sweep.drift_sweep_interval_hours

    log.info(
        "daemon.starting",
        interval=interval,
        deep_interval_hours=deep_interval_hours,
        drift_interval_hours=drift_interval_hours,
    )

    while True:
        now = datetime.now(timezone.utc)
        hours_since_deep = (now - last_deep).total_seconds() / 3600

        try:
            if hours_since_deep >= deep_interval_hours:
                # Deep sweep with agent
                log.info("daemon.deep_sweep")
                await run_sweep(config, state, skills_dir, structural_only=False, fix_mode=True)
                last_deep = now
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
