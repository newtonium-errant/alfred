"""Extraction orchestrator — two-phase scan + agent extraction pipeline."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from alfred.vault.mutation_log import append_to_audit_log, cleanup_session_file, create_session_file, read_mutations

from .backends import (
    BaseBackend,
    BackendResult,
    build_extraction_prompt,
    format_existing_learns,
    format_source_records,
)
from .backends.cli import ClaudeBackend
from .backends.http import ZoBackend
from .backends.openclaw import OpenClawBackend
from .candidates import (
    ExtractionBatch,
    ScoredCandidate,
    collect_existing_learns,
    group_by_project,
    scan_candidates,
)
from .config import DistillerConfig
from .parser import parse_file
from .state import DistillerState, ExtractionLogEntry, RunResult
from .utils import get_logger

log = get_logger(__name__)


def _load_skill(skills_dir: Path) -> str:
    """Load SKILL.md and all reference templates into a single text block."""
    skill_path = skills_dir / "vault-distiller" / "SKILL.md"
    if not skill_path.exists():
        log.warning("daemon.skill_not_found", path=str(skill_path))
        return ""

    parts: list[str] = [skill_path.read_text(encoding="utf-8")]

    refs_dir = skills_dir / "vault-distiller" / "references"
    if refs_dir.is_dir():
        for ref_file in sorted(refs_dir.glob("*.md")):
            content = ref_file.read_text(encoding="utf-8")
            parts.append(
                f"\n---\n### Reference Template: {ref_file.name}\n```\n{content}\n```"
            )

    return "\n".join(parts)


def _create_backend(config: DistillerConfig) -> BaseBackend:
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


def snapshot_vault(
    vault_path: Path, ignore_dirs: list[str] | None = None
) -> dict[str, str]:
    """Capture SHA-256 checksums of all .md files in the vault."""
    ignore = set(ignore_dirs or [])
    checksums: dict[str, str] = {}

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        if any(part in ignore for part in rel.parts):
            continue
        try:
            content = md_file.read_bytes()
            checksums[str(rel).replace("\\", "/")] = hashlib.sha256(
                content
            ).hexdigest()
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


def _get_project_description(
    vault_path: Path, project_name: str | None
) -> str:
    """Try to read project description from the vault."""
    if not project_name:
        return ""
    project_file = vault_path / "project" / f"{project_name}.md"
    if project_file.exists():
        try:
            rec = parse_file(vault_path, f"project/{project_name}.md")
            return rec.frontmatter.get("description", "")
        except Exception:
            pass
    return ""


def _build_batches(
    config: DistillerConfig,
    candidates: list[ScoredCandidate],
    vault_path: Path,
) -> list[ExtractionBatch]:
    """Group candidates into extraction batches with dedup context."""
    groups = group_by_project(candidates)
    batches: list[ExtractionBatch] = []

    for project_name, group_candidates in groups.items():
        # Cap batch size
        batch_candidates = group_candidates[: config.extraction.max_sources_per_batch]

        # Collect existing learns for dedup
        existing = collect_existing_learns(
            vault_path,
            config.vault.ignore_dirs,
            config.extraction.learn_types,
            project_name,
        )

        batches.append(
            ExtractionBatch(
                project=project_name,
                source_records=batch_candidates,
                existing_learns=existing,
            )
        )

    return batches


async def run_extraction(
    config: DistillerConfig,
    state: DistillerState,
    skills_dir: Path,
    project_filter: str | None = None,
) -> RunResult:
    """Run a complete extraction: Phase 1 scan + Phase 2 agent extraction."""
    run_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now(timezone.utc).isoformat()
    vault_path = config.vault.vault_path

    log.info("extraction.start", run_id=run_id)

    # Phase 1: Scan candidates
    candidates = scan_candidates(
        vault_path=vault_path,
        ignore_dirs=config.vault.ignore_dirs,
        ignore_files=config.vault.ignore_files,
        source_types=config.extraction.source_types,
        threshold=config.extraction.candidate_threshold,
        distilled_files=state.get_distilled_md5s(),
        project_filter=project_filter,
    )

    result = RunResult(
        run_id=run_id,
        timestamp=timestamp,
        candidates_found=len(candidates),
    )

    if not candidates:
        log.info("extraction.no_candidates", run_id=run_id)
        state.add_run(result)
        state.save()
        return result

    # Phase 2: Build batches and invoke agent
    skill_text = _load_skill(skills_dir)
    if not skill_text:
        log.warning("extraction.no_skill", msg="No SKILL.md found — skipping agent")
        state.add_run(result)
        state.save()
        return result

    backend = _create_backend(config)
    use_mutation_log = isinstance(backend, (ClaudeBackend, OpenClawBackend))
    batches = _build_batches(config, candidates, vault_path)
    result.batches = len(batches)

    for batch in batches:
        project_desc = _get_project_description(vault_path, batch.project)

        prompt = build_extraction_prompt(
            skill_text=skill_text,
            vault_path=str(vault_path),
            project_name=batch.project,
            project_description=project_desc,
            existing_learns_formatted=format_existing_learns(batch.existing_learns),
            source_records_formatted=format_source_records(batch.source_records),
        )

        session_path = None
        if use_mutation_log:
            session_path = create_session_file()
            backend.env_overrides = {
                "ALFRED_VAULT_PATH": str(vault_path),
                "ALFRED_VAULT_SCOPE": "distiller",
                "ALFRED_VAULT_SESSION": session_path,
            }
        else:
            before = snapshot_vault(vault_path, config.vault.ignore_dirs)

        log.info(
            "extraction.agent_invoke",
            run_id=run_id,
            project=batch.project or "(ungrouped)",
            sources=len(batch.source_records),
        )

        # Invoke agent
        agent_result = await backend.process(
            prompt=prompt,
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
        append_to_audit_log(audit_path, "distiller", audit_mutations, detail=run_id)

        result.candidates_processed += len(batch.source_records)

        # Log created learn records
        source_paths = [sc.record.rel_path for sc in batch.source_records]
        for f in created:
            # Try to identify learn type from path
            learn_type = "unknown"
            for lt in config.extraction.learn_types:
                if f.startswith(f"{lt}/"):
                    learn_type = lt
                    break

            result.records_created[learn_type] = (
                result.records_created.get(learn_type, 0) + 1
            )

            state.add_log_entry(
                ExtractionLogEntry(
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    run_id=run_id,
                    action="created",
                    learn_type=learn_type,
                    learn_file=f,
                    source_files=source_paths,
                    detail=f"Extracted from {batch.project or 'ungrouped'} batch",
                )
            )

        # Update source file states
        for sc in batch.source_records:
            learn_paths = [
                f
                for f in created
                if any(f.startswith(f"{lt}/") for lt in config.extraction.learn_types)
            ]
            state.update_file(sc.record.rel_path, sc.md5, learn_paths)

        if not agent_result.success:
            log.error(
                "extraction.agent_failed",
                run_id=run_id,
                summary=agent_result.summary[:500],
            )

    log.info(
        "extraction.complete",
        run_id=run_id,
        candidates=len(candidates),
        processed=result.candidates_processed,
        records_created=sum(result.records_created.values()),
    )

    state.add_run(result)
    state.save()
    return result


async def run_watch(
    config: DistillerConfig,
    state: DistillerState,
    skills_dir: Path,
) -> None:
    """Daemon mode — extract on interval until interrupted."""
    interval = config.extraction.interval_seconds
    deep_interval_hours = config.extraction.deep_interval_hours

    last_deep = datetime.now(timezone.utc)

    log.info(
        "daemon.starting",
        interval=interval,
        deep_interval_hours=deep_interval_hours,
    )

    while True:
        now = datetime.now(timezone.utc)
        hours_since_deep = (now - last_deep).total_seconds() / 3600

        if hours_since_deep >= deep_interval_hours:
            log.info("daemon.deep_extraction")
            await run_extraction(config, state, skills_dir)
            last_deep = now
        else:
            # Light pass — just scan, no agent invocation
            log.info("daemon.light_scan")
            candidates = scan_candidates(
                vault_path=config.vault.vault_path,
                ignore_dirs=config.vault.ignore_dirs,
                ignore_files=config.vault.ignore_files,
                source_types=config.extraction.source_types,
                threshold=config.extraction.candidate_threshold,
                distilled_files=state.get_distilled_md5s(),
            )
            if candidates:
                log.info("daemon.pending_candidates", count=len(candidates))

        await asyncio.sleep(interval)
