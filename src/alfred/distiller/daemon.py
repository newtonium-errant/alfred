"""Extraction orchestrator — two-phase scan + agent extraction pipeline.

For OpenClaw backends, uses a multi-stage pipeline (pipeline.py):
  Pass A: EXTRACT (LLM per-source) → DEDUP (Python) → CREATE (LLM per-learning)
  Pass B: Cross-learning meta-analysis (contradictions, syntheses across records)

For other backends, falls back to the legacy single-LLM-call approach.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from alfred.common.heartbeat import Heartbeat
from alfred.common.schedule import compute_next_fire
from alfred.vault.mutation_log import append_to_audit_log, cleanup_session_file, create_session_file, read_mutations
from alfred.vault.ops import is_ignored_path

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
from .extractor import extract as v2_extract
from .parser import parse_file
from .pipeline import run_meta_analysis, run_pipeline
from .state import DistillerState, ExtractionLogEntry, RunResult
from .utils import compute_md5, get_logger
from .writer import write_learn_record

log = get_logger(__name__)

# Module-level idle-tick heartbeat — see ``alfred.common.heartbeat`` for
# the rationale ("intentionally left blank" pattern). Counter is bumped
# in :func:`run_extraction` for each learn record created (both the
# pipeline path and the legacy single-call path). The heartbeat task is
# spawned in :func:`run_watch` only when ``config.idle_tick.enabled`` is
# True.
heartbeat: Heartbeat = Heartbeat(daemon_name="distiller", log=log)


def _use_pipeline(config: DistillerConfig) -> bool:
    """Check if the multi-stage pipeline should be used.

    The pipeline (per-source extraction → dedup → create → meta-analysis)
    is supported for Claude and OpenClaw backends. Zo uses the legacy
    single-call path because it has its own filesystem access.
    """
    return config.agent.backend in ("claude", "openclaw")


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
        if is_ignored_path(rel, ignore):
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

        # Collect existing learns for dedup. When ``project_name`` is
        # None (ungrouped batch) we explicitly restrict to ungrouped
        # learns — otherwise the extractor sees every learn record in
        # the vault as dedup context (~588 titles in production, c9
        # Plan-agent diagnosis 2026-04-24) and spuriously concludes
        # "everything is already captured." Project-scoped batches
        # still dedup against their project's learns.
        existing = collect_existing_learns(
            vault_path,
            config.vault.ignore_dirs,
            config.extraction.learn_types,
            project_name=project_name,
            ungrouped_only=(project_name is None),
        )

        batches.append(
            ExtractionBatch(
                project=project_name,
                source_records=batch_candidates,
                existing_learns=existing,
            )
        )

    return batches


async def _run_v2_shadow(
    batch: ExtractionBatch,
    config: DistillerConfig,
    run_id: str,
) -> None:
    """Distiller rebuild (Week 1): run the non-agentic v2 path in parallel.

    This is the operator-diffable shadow pipeline. It:
      - Calls the Pydantic-gated extractor on every source in the batch.
      - Filters the extractor's ``LearningCandidate`` output to types in
        ``config.extraction.v2_types`` (default: ``assumption`` only, to
        keep blast radius tight during Week 2 measurement — v2_types
        filters OUTPUT (learning) types, not source record types).
      - Writes each kept ``LearningCandidate`` to the shadow root via
        the deterministic writer (``writer.write_learn_record``).
      - Does NOT update state, does NOT touch mutation log, does NOT
        write to the live vault. The legacy path owns all that; v2
        is bookkeeping-free so it can run alongside without interfering.

    Exceptions are caught per-source so one bad source doesn't poison
    the whole batch. They propagate as structured-log warnings; the
    daemon's top-level ``try`` still catches anything that escapes.
    """
    allowed_output_types = set(config.extraction.v2_types or [])
    if not allowed_output_types:
        # No types allow-listed → v2 effectively off even if flag is on.
        return
    shadow_root = Path(config.extraction.shadow_root)

    existing_titles: list[tuple[str, str]] = []
    for learn_record in batch.existing_learns:
        title = str(
            learn_record.frontmatter.get("name")
            or learn_record.frontmatter.get("title")
            or ""
        )
        if title:
            existing_titles.append((title, learn_record.record_type))

    if not batch.source_records:
        return

    log.info(
        "distiller.v2.batch_start",
        run_id=run_id,
        project=batch.project or "(ungrouped)",
        sources=len(batch.source_records),
        allowed_output_types=sorted(allowed_output_types),
        shadow_root=str(shadow_root),
    )

    for sc in batch.source_records:
        try:
            result = await v2_extract(
                source_body=sc.record.body,
                source_frontmatter=sc.record.frontmatter,
                existing_learn_titles=existing_titles,
                signals=sc.signals,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 — isolate per-source LLM/network errors
            log.warning(
                "distiller.v2.extract_error",
                run_id=run_id,
                source=sc.record.rel_path,
                error=str(exc)[:500],
            )
            continue

        # v2_types filters OUTPUT — keep only learnings of allow-listed
        # types before writing to shadow. An extractor producing 3
        # learnings [assumption, decision, synthesis] with v2_types=
        # ["assumption"] writes just the assumption; the others are
        # dropped (not waste — extractor already paid the LLM cost, and
        # widening v2_types later costs only re-writes, not re-extracts).
        kept = [s for s in result.learnings if s.type in allowed_output_types]
        skipped = len(result.learnings) - len(kept)

        written = 0
        for spec in kept:
            try:
                write_learn_record(
                    spec=spec,
                    body_draft="",
                    shadow_root=shadow_root,
                )
                written += 1
            except Exception as exc:  # noqa: BLE001 — isolate per-record write errors
                log.warning(
                    "distiller.v2.write_error",
                    run_id=run_id,
                    source=sc.record.rel_path,
                    title=spec.title,
                    error=str(exc)[:500],
                )

        log.info(
            "distiller.v2.extract_complete",
            run_id=run_id,
            source=sc.record.rel_path,
            learnings=len(result.learnings),
            written=written,
            skipped_wrong_type=skipped,
            shadow=True,
        )


def recompute_source_md5s(
    batch_source_records: list[ScoredCandidate],
    vault_path: Path,
    state: DistillerState,
) -> None:
    """Re-read source file MD5s after pipeline writes and update state.

    The pipeline may write distiller_signals/distiller_learnings back into
    source file frontmatter (see our merge-semantics path in pipeline.py),
    changing the file's MD5 on disk. Without this refresh the stored MD5
    would be stale, causing the file to re-qualify as a candidate on the
    next scan (infinite-loop re-processing).

    Ports upstream a3a44a4 with the f45d05d follow-up that drops the
    learn_records argument so existing entries aren't duplicated — our
    update_file() already preserves learn_records_created when the arg
    is omitted.
    """
    for sc in batch_source_records:
        full_path = vault_path / sc.record.rel_path
        if not full_path.exists():
            continue
        try:
            fresh_md5 = compute_md5(full_path)
        except OSError:
            continue
        if fresh_md5 != sc.md5:
            state.update_file(sc.record.rel_path, fresh_md5)
            log.debug(
                "extraction.md5_refreshed",
                path=sc.record.rel_path,
                old_md5=sc.md5[:8],
                new_md5=fresh_md5[:8],
            )


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
        distilled_files=state.get_distilled_body_hashes(),
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

    # Phase 2: Build batches and extract
    batches = _build_batches(config, candidates, vault_path)
    result.batches = len(batches)

    # --- Distiller rebuild (Week 1): v2 shadow pipeline -----------------
    # When ``extraction.use_deterministic_v2`` is True, run the non-agentic
    # extractor + deterministic writer in parallel with the legacy path.
    # v2 writes to ``extraction.shadow_root`` (default ``data/shadow/distiller``);
    # the live vault is untouched. Legacy continues as normal below.
    # Week 2 plan: flip the flag for assumption-type sources only, diff
    # shadow output against the legacy vault, decide rollout.
    if config.extraction.use_deterministic_v2:
        for batch in batches:
            await _run_v2_shadow(batch, config, run_id)

    if _use_pipeline(config):
        # Multi-stage pipeline for OpenClaw backend
        session_path = create_session_file()
        any_created = False

        for batch in batches:
            log.info(
                "extraction.pipeline_invoke",
                run_id=run_id,
                project=batch.project or "(ungrouped)",
                sources=len(batch.source_records),
            )

            pipeline_result = await run_pipeline(
                batch=batch,
                config=config,
                session_path=session_path,
            )

            result.candidates_processed += pipeline_result.candidates_processed

            # Merge records_created counts
            for lt, count in pipeline_result.records_created.items():
                result.records_created[lt] = (
                    result.records_created.get(lt, 0) + count
                )
                any_created = True

            # Update source file states
            mutations = read_mutations(session_path)
            created = mutations["files_created"]
            source_paths = [sc.record.rel_path for sc in batch.source_records]

            for f in created:
                learn_type = "unknown"
                for lt in config.extraction.learn_types:
                    if f.startswith(f"{lt}/"):
                        learn_type = lt
                        break

                state.add_log_entry(
                    ExtractionLogEntry(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        run_id=run_id,
                        action="created",
                        learn_type=learn_type,
                        learn_file=f,
                        source_files=source_paths,
                        detail=f"Pipeline: {batch.project or 'ungrouped'} batch",
                    )
                )
                # Idle-tick counter — one learn record created = one event.
                heartbeat.record_event()

            for sc in batch.source_records:
                learn_paths = [
                    f
                    for f in created
                    if any(f.startswith(f"{lt}/") for lt in config.extraction.learn_types)
                ]
                state.update_file(
                    sc.record.rel_path, sc.md5, learn_paths,
                    body_hash=sc.body_hash,
                )

            # Refresh MD5s after pipeline may have written distiller_signals /
            # distiller_learnings back to source files (prevents re-qualify loop).
            recompute_source_md5s(batch.source_records, vault_path, state)

            if not pipeline_result.success:
                log.error(
                    "extraction.pipeline_failed",
                    run_id=run_id,
                    summary=pipeline_result.summary[:500],
                )

        # Pass B: Cross-learning meta-analysis (runs once after all batches)
        if any_created:
            log.info("extraction.passb_start", run_id=run_id)
            meta_created = await run_meta_analysis(config, session_path)
            if meta_created > 0:
                result.records_created["meta"] = meta_created

        # Final audit and cleanup
        mutations = read_mutations(session_path)
        audit_mutations = {
            "files_created": mutations["files_created"],
            "files_modified": mutations["files_modified"],
            "files_deleted": mutations["files_deleted"],
        }
        audit_path = str(Path(config.state.path).parent / "vault_audit.log")
        append_to_audit_log(audit_path, "distiller", audit_mutations, detail=run_id)
        cleanup_session_file(session_path)

    else:
        # Legacy path for Claude and Zo backends
        skill_text = _load_skill(skills_dir)
        if not skill_text:
            log.warning("extraction.no_skill", msg="No SKILL.md found — skipping agent")
            state.add_run(result)
            state.save()
            return result

        backend = _create_backend(config)
        use_mutation_log = isinstance(backend, (ClaudeBackend, OpenClawBackend))

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

            agent_result = await backend.process(
                prompt=prompt,
                vault_path=str(vault_path),
            )

            if use_mutation_log and session_path:
                mutations = read_mutations(session_path)
                created = mutations["files_created"]
                modified = mutations["files_modified"]
                deleted = mutations["files_deleted"]
                cleanup_session_file(session_path)
            else:
                after = snapshot_vault(vault_path, config.vault.ignore_dirs)
                created, modified, deleted = diff_vault(before, after)

            audit_mutations = {"files_created": created, "files_modified": modified, "files_deleted": deleted}
            audit_path = str(Path(config.state.path).parent / "vault_audit.log")
            append_to_audit_log(audit_path, "distiller", audit_mutations, detail=run_id)

            result.candidates_processed += len(batch.source_records)

            source_paths = [sc.record.rel_path for sc in batch.source_records]
            for f in created:
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
                # Idle-tick counter — one learn record created = one event.
                heartbeat.record_event()

            for sc in batch.source_records:
                learn_paths = [
                    f
                    for f in created
                    if any(f.startswith(f"{lt}/") for lt in config.extraction.learn_types)
                ]
                state.update_file(
                    sc.record.rel_path, sc.md5, learn_paths,
                    body_hash=sc.body_hash,
                )

            # Refresh MD5s after legacy agent path may have written back to source files.
            recompute_source_md5s(batch.source_records, vault_path, state)

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
    deep_schedule = config.extraction.deep_extraction_schedule
    consolidation_schedule = config.extraction.consolidation_schedule

    # Persist last deep extraction time across restarts. Without this,
    # every daemon restart reset last_deep to epoch and re-triggered a
    # full deep extraction on boot. On first boot we seed last_deep =
    # now so the first fire lands at the next scheduled window
    # (03:30 Halifax by default) rather than immediately.
    now_utc_init = datetime.now(timezone.utc)
    if state.last_deep_extraction:
        try:
            last_deep = datetime.fromisoformat(state.last_deep_extraction)
        except (ValueError, TypeError):
            last_deep = now_utc_init
    else:
        last_deep = now_utc_init
    # Consolidation is weekly (Sundays 04:00 Halifax by default). Seed
    # ``last_consolidation = now`` on first boot so we don't fire
    # immediately — wait for the next Sunday at 04:00. This is a
    # behavior tightening from the pre-c5 rolling interval (which
    # fired consolidation on first boot then waited 168h); the new
    # semantics match Andrew's intent ("heavy passes land overnight").
    # Not persisted across restarts — an in-process restart within the
    # same window won't re-fire because last_consolidation is set by
    # the first fire, but a full daemon restart after a fire will get
    # a fresh seed. Acceptable for a weekly pass.
    last_consolidation = now_utc_init

    log.info(
        "daemon.starting",
        interval=interval,
        deep_extraction_time=deep_schedule.time,
        deep_extraction_tz=deep_schedule.timezone,
        deep_extraction_day_of_week=deep_schedule.day_of_week,
        consolidation_time=consolidation_schedule.time,
        consolidation_tz=consolidation_schedule.timezone,
        consolidation_day_of_week=consolidation_schedule.day_of_week,
    )

    # Idle-tick heartbeat task — emits ``distiller.idle_tick`` every
    # ``config.idle_tick.interval_seconds``. Default 60s, on by default.
    # See ``alfred.common.heartbeat`` for the "intentionally left blank"
    # rationale. Spawned only when enabled — disabled path is silent.
    heartbeat_shutdown = asyncio.Event()
    if config.idle_tick.enabled:
        asyncio.create_task(
            heartbeat.run(
                interval_seconds=config.idle_tick.interval_seconds,
                shutdown_event=heartbeat_shutdown,
            ),
            name="distiller-heartbeat",
        )
        log.info(
            "daemon.heartbeat_started",
            interval_seconds=config.idle_tick.interval_seconds,
        )

    while True:
        now = datetime.now(timezone.utc)

        # Clock-aligned deep extraction gate. ``compute_next_fire``
        # returns the next scheduled fire strictly after ``last_deep``,
        # so a restart inside the same window won't re-fire.
        next_deep_fire = compute_next_fire(deep_schedule, last_deep)
        deep_due = now >= next_deep_fire

        try:
            if deep_due:
                log.info("daemon.deep_extraction")
                await run_extraction(config, state, skills_dir)
                last_deep = now
                state.last_deep_extraction = now.isoformat()
                state.save()
                log.info(
                    "daemon.deep_extraction_next",
                    next_fire=compute_next_fire(
                        deep_schedule, last_deep,
                    ).isoformat(),
                )
            else:
                # Light pass — just scan, no agent invocation
                log.info("daemon.light_scan")
                candidates = scan_candidates(
                    vault_path=config.vault.vault_path,
                    ignore_dirs=config.vault.ignore_dirs,
                    ignore_files=config.vault.ignore_files,
                    source_types=config.extraction.source_types,
                    threshold=config.extraction.candidate_threshold,
                    distilled_files=state.get_distilled_body_hashes(),
                )
                if candidates:
                    log.info("daemon.pending_candidates", count=len(candidates))

            # Consolidation sweep — clock-aligned weekly (Sundays 04:00
            # Halifax by default). Same pattern as deep extraction:
            # fire when now >= compute_next_fire(schedule, last).
            next_consolidation_fire = compute_next_fire(
                consolidation_schedule, last_consolidation,
            )
            if now >= next_consolidation_fire:
                log.info("daemon.consolidation_start")
                from alfred.vault.mutation_log import (
                    append_to_audit_log,
                    cleanup_session_file,
                    create_session_file,
                    read_mutations,
                )
                session_path = create_session_file()
                try:
                    from .pipeline import run_consolidation
                    modified = await run_consolidation(config, skills_dir, session_path)
                    log.info("daemon.consolidation_complete", modified=modified)
                    mutations = read_mutations(session_path)
                    total = sum(len(v) for v in mutations.values())
                    if total > 0:
                        audit_path = str(Path(config.state.path).parent / "vault_audit.log")
                        append_to_audit_log(audit_path, "distiller", mutations, detail="consolidation")
                except Exception:
                    log.exception("daemon.consolidation_error")
                finally:
                    cleanup_session_file(session_path)
                last_consolidation = now
                log.info(
                    "daemon.consolidation_next",
                    next_fire=compute_next_fire(
                        consolidation_schedule, last_consolidation,
                    ).isoformat(),
                )
        except Exception:
            log.exception("daemon.extraction_error")

        await asyncio.sleep(interval)
