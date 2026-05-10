"""Subcommand implementations for the distiller CLI."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .backfill import cmd_backfill as _cmd_backfill_inner
from .candidates import scan_candidates, group_by_project
from .config import DistillerConfig
from .daemon import run_extraction, run_watch
from .state import DistillerState
from .utils import get_logger

log = get_logger(__name__)


def _init_state(config: DistillerConfig) -> DistillerState:
    state = DistillerState(config.state.path, config.state.max_run_history)
    state.load()
    return state


def cmd_scan(config: DistillerConfig, skills_dir: Path, project: str | None = None) -> None:
    """Phase 1 only: identify candidates, print report. No agent invocation."""
    state = _init_state(config)

    candidates = scan_candidates(
        vault_path=config.vault.vault_path,
        ignore_dirs=config.vault.ignore_dirs,
        ignore_files=config.vault.ignore_files,
        source_types=config.extraction.source_types,
        threshold=config.extraction.candidate_threshold,
        distilled_files=state.get_distilled_body_hashes(),
        distilled_last_distilled=state.get_distilled_last_distilled(),
        project_filter=project,
    )

    if not candidates:
        print("No candidates found.")
        return

    groups = group_by_project(candidates)

    print(f"\n=== Distiller Scan — {len(candidates)} candidates ===\n")

    for proj_name, group in sorted(groups.items(), key=lambda x: x[0] or ""):
        header = proj_name or "(ungrouped)"
        print(f"## {header} ({len(group)} records)")
        print(f"{'File':<60} {'Type':<15} {'Score':<8} {'Signals'}")
        print("-" * 110)

        for sc in group:
            signals_parts = []
            s = sc.signals
            if s.decision_keywords:
                signals_parts.append(f"dec:{s.decision_keywords}")
            if s.assumption_keywords:
                signals_parts.append(f"asm:{s.assumption_keywords}")
            if s.constraint_keywords:
                signals_parts.append(f"con:{s.constraint_keywords}")
            if s.contradiction_keywords:
                signals_parts.append(f"ctr:{s.contradiction_keywords}")
            if s.has_outcome:
                signals_parts.append("outcome")
            if s.has_context:
                signals_parts.append("context")

            signals_str = ", ".join(signals_parts) if signals_parts else "-"

            print(
                f"{sc.record.rel_path:<60} "
                f"{sc.record.record_type:<15} "
                f"{sc.score:<8.2f} "
                f"{signals_str}"
            )
        print()


def cmd_run(config: DistillerConfig, skills_dir: Path, project: str | None = None) -> None:
    """Full pipeline: scan + invoke agent to extract learnings."""
    state = _init_state(config)
    result = asyncio.run(run_extraction(config, state, skills_dir, project_filter=project))

    print(f"\n=== Extraction Run {result.run_id} — {result.timestamp} ===")
    print(f"Candidates found: {result.candidates_found}")
    print(f"Candidates processed: {result.candidates_processed}")
    print(f"Batches: {result.batches}")

    if result.records_created:
        print(f"\nRecords created:")
        for learn_type, count in sorted(result.records_created.items()):
            print(f"  {learn_type}: {count}")
        print(f"  Total: {sum(result.records_created.values())}")
    else:
        print("\nNo records created.")


def cmd_watch(config: DistillerConfig, skills_dir: Path) -> None:
    """Daemon mode — extract on interval."""
    state = _init_state(config)
    try:
        asyncio.run(run_watch(config, state, skills_dir))
    except KeyboardInterrupt:
        log.info("daemon.interrupted")
        print("\nStopped.")


def cmd_status(config: DistillerConfig) -> None:
    """Show last run, extraction counts, state summary."""
    state = _init_state(config)

    total_files = len(state.files)
    total_learns = sum(
        len(fs.learn_records_created) for fs in state.files.values()
    )

    # Count by learn type
    learn_counts: dict[str, int] = {}
    for fs in state.files.values():
        for lf in fs.learn_records_created:
            lt = lf.split("/")[0] if "/" in lf else "unknown"
            learn_counts[lt] = learn_counts.get(lt, 0) + 1

    print(f"=== Distiller Status ===")
    print(f"Tracked source files: {total_files}")
    print(f"Total learn records created: {total_learns}")
    print(f"Total runs recorded: {len(state.runs)}")
    print(f"Extraction log entries: {len(state.extraction_log)}")

    if learn_counts:
        print(f"\nLearn records by type:")
        for lt, count in sorted(learn_counts.items()):
            print(f"  {lt}: {count}")

    # Last run
    if state.runs:
        last = max(state.runs.values(), key=lambda r: r.timestamp)
        print(f"\nLast run: {last.run_id} at {last.timestamp}")
        print(f"  Candidates found: {last.candidates_found}")
        print(f"  Candidates processed: {last.candidates_processed}")
        if last.records_created:
            for lt, count in sorted(last.records_created.items()):
                print(f"  Created {lt}: {count}")

    # Recent extraction log
    if state.extraction_log:
        recent = state.extraction_log[-5:]
        print(f"\nRecent extractions:")
        for entry in recent:
            print(
                f"  [{entry.timestamp}] {entry.action} {entry.learn_type} "
                f"{entry.learn_file} — {entry.detail}"
            )


def cmd_consolidate(config: DistillerConfig, skills_dir: Path) -> None:
    """Run consolidation sweep: merge duplicates, upgrade assumptions, resolve contradictions."""
    from alfred.vault.mutation_log import (
        cleanup_session_file,
        create_session_file,
        read_mutations,
    )
    from .pipeline import run_consolidation

    session_path = create_session_file()
    try:
        modified = asyncio.run(run_consolidation(config, skills_dir, session_path))
        mutations = read_mutations(session_path)
    finally:
        cleanup_session_file(session_path)

    print(f"\n=== Consolidation Complete ===")
    print(f"Records modified: {len(mutations.get('files_modified', []))}")
    print(f"Records created: {len(mutations.get('files_created', []))}")
    print(f"Records deleted: {len(mutations.get('files_deleted', []))}")


def cmd_backfill(
    config: DistillerConfig,
    source: str,
    dry_run: bool = False,
) -> None:
    """One-time backfill: extract learn records from an external source dir.

    KAL-LE distiller-radar Phase 1. Walks ``source`` (typically Salem's
    ``vault/session/``) for ``*.md`` files containing
    ``## Alfred Learnings``, runs the v2 extractor on each, writes
    learn records to the configured vault path. Source files are NOT
    modified. Already-processed source paths are tracked in
    ``distiller_backfill_state.json`` so subsequent runs are no-ops.
    """
    source_path = Path(source).expanduser().resolve()
    _cmd_backfill_inner(config, source_path, dry_run=dry_run)


def cmd_rank_week(
    config: DistillerConfig,
    *,
    top_n: int = 12,
    window_days: int = 7,
    dry_run: bool = False,
) -> None:
    """Print the synthesis ranker's top-N for the configured vault.

    KAL-LE distiller-radar Phase 2 inspection tool. Reads
    ``synthesis/`` + ``decision/`` + ``contradiction/`` under
    ``config.vault.path`` and prints each ranked record with the per-
    term breakdown so the operator can tune the score formula. The
    command is read-only by design; ``--dry-run`` is accepted for
    symmetry with ``backfill`` but doesn't change behavior.
    """
    from .synthesis_ranker import rank_synthesis_records

    vault_path = config.vault.vault_path
    if not vault_path.is_dir():
        print(f"Vault path does not exist: {vault_path}")
        return

    results = rank_synthesis_records(
        vault_path, window_days=window_days, top_n=top_n,
    )

    print(f"=== Synthesis Ranker — vault={vault_path} ===")
    print(
        f"window_days={window_days}  top_n={top_n}  "
        f"dry_run={dry_run}  results={len(results)}"
    )
    if not results:
        print("\nNo records ranked.")
        return

    print(
        f"\n{'Rank':<5} {'Score':<8} {'Type':<14} {'Src':<5} "
        f"{'Ent':<5} {'Age(d)':<8} {'Path'}"
    )
    print("-" * 110)
    for i, r in enumerate(results, start=1):
        age = "-" if r.age_days is None else f"{r.age_days:.2f}"
        print(
            f"{i:<5} {r.score:<8.2f} {r.record_type:<14} "
            f"{r.source_count:<5} {r.entity_count:<5} {age:<8} {r.path.name}"
        )

    print("\n=== Score breakdowns ===")
    for i, r in enumerate(results, start=1):
        b = r.breakdown
        print(
            f"  #{i} {r.path.stem[:80]}\n"
            f"      cross_source={b.cross_source:.2f}  "
            f"entity_diversity={b.entity_diversity:.2f}  "
            f"recency={b.recency:.2f}  type_weight={b.type_weight:.2f}"
        )


def cmd_rank_day(
    config: DistillerConfig,
    *,
    top_n: int = 5,
    min_score: float | None = None,
    digests_dir: str | None = None,
    state_dir: str | None = None,
    dry_run: bool = False,
) -> None:
    """Phase 3a — daily radar wrapper around the synthesis ranker.

    Rebuilds the day's top-N synthesis/decision/contradiction items
    (1-day window), dedups against the rolling surfaced-log, writes
    ``<digests_dir>/daily/YYYY-MM-DD.md``, and appends each surfaced
    item to ``<state_dir>/radar_surfaced.jsonl``.

    Path resolution:
      - ``digests_dir``: explicit CLI flag wins; else falls back to
        ``vault_path/digests`` (KAL-LE convention).
      - ``state_dir``: explicit CLI flag wins; else uses the parent of
        ``config.state.path`` (so it sits next to
        ``distiller_state.json``).

    The empty-state behavior is per
    ``feedback_intentionally_left_blank.md``: a no-radar-items day
    still emits a file with an explicit "no radar items today" line.
    """
    from .radar_day import run_daily_radar

    vault_path = config.vault.vault_path
    if not vault_path.is_dir():
        print(f"Vault path does not exist: {vault_path}")
        return

    if digests_dir:
        digests_path = Path(digests_dir).expanduser().resolve()
    else:
        digests_path = (vault_path / "digests").resolve()

    if state_dir:
        state_path = Path(state_dir).expanduser().resolve()
    else:
        state_path = Path(config.state.path).expanduser().resolve().parent

    result = run_daily_radar(
        vault_path,
        digests_path,
        state_path,
        top_n=top_n,
        min_score=min_score,
        dry_run=dry_run,
    )

    print(f"=== Daily Radar — {result.date} ===")
    print(
        f"vault={vault_path}  digests={digests_path}  "
        f"state={state_path}  dry_run={dry_run}"
    )
    print(
        f"items={len(result.items)}  ranker_count={result.ranker_count}  "
        f"deduped={max(0, result.ranker_count - len(result.items))}"
    )
    if result.output_path is not None:
        verb = "would write" if dry_run else "wrote"
        print(f"{verb}: {result.output_path}")

    if not result.items:
        # Explicit empty-state ack — mirrors the rendered file. The
        # distinction "ran, nothing to surface" vs "didn't run" is the
        # whole point of feedback_intentionally_left_blank.md.
        print("\nno radar items today (corpus checked: synthesis/, "
              "decision/, contradiction/)")
        return

    print(
        f"\n{'Rank':<5} {'Score':<8} {'Type':<14} {'Src':<5} "
        f"{'Ent':<5} {'Age(d)':<8} {'Path'}"
    )
    print("-" * 110)
    for i, r in enumerate(result.items, start=1):
        age = "-" if r.age_days is None else f"{r.age_days:.2f}"
        print(
            f"{i:<5} {r.score:<8.2f} {r.record_type:<14} "
            f"{r.source_count:<5} {r.entity_count:<5} {age:<8} {r.path.name}"
        )


def cmd_mine_patterns(
    config: DistillerConfig,
    *,
    config_path: str | Path | None = None,
    dry_run: bool = False,
    min_cluster_size: int | None = None,
    top: int | None = None,
) -> None:
    """KAL-LE distiller-radar Phase 4 — embedding-pattern miner.

    Reads the surveyor pipeline's labeled-cluster output, gates each
    cluster against the four-part rule (labeled / substantive / no
    canonical match / label-quality), and surfaces survivors as inbox
    proposals for new ``architecture/`` or ``principles/`` records.

    Behavior is gated by ``distiller.pattern_miner.enabled`` — when
    the block is absent or ``enabled: false``, this handler prints a
    clear "not enabled in this config" message and returns. KAL-LE
    opts in via its own config; Salem omits the block.

    Reads from the surveyor state JSON at the configured path (NOT
    Milvus directly — see the design memo for the lock-contention
    rationale).

    Args:
        config: Loaded distiller config.
        config_path: The CLI's --config flag value. Used to derive the
            instance basename embedded in proposal "Suggested next
            step" CLI invocations so the operator can copy-paste a
            correct ``alfred --config <name> vault move ...`` line.
        dry_run: Evaluate + log + render counts but write neither
            proposal files nor state mutations. Useful as an
            inspection pass before a live run.
        min_cluster_size: Optional override for the gate's size
            threshold; when None, uses the config's value (default 3).
        top: Optional cap on new proposals per run. Useful for an
            initial bulk-mine when the queue is empty and the gate
            yields many candidates at once.
    """
    pm = config.pattern_miner
    if pm is None or not pm.enabled:
        print("Pattern miner not enabled in this config.")
        print("To enable, add `distiller.pattern_miner.enabled: true`.")
        return

    vault_path = config.vault.vault_path
    if not vault_path.is_dir():
        print(f"Vault path does not exist: {vault_path}")
        return

    surveyor_state_path = Path(pm.surveyor_state_path).expanduser()
    if not surveyor_state_path.exists():
        # Per the universal "intentionally left blank" rule — explicit
        # empty-state ack so the operator can distinguish "miner ran,
        # no surveyor data yet" from "miner is broken."
        print(
            f"Surveyor state file not found at {surveyor_state_path}.\n"
            f"Run the surveyor daemon first; it produces this file.\n"
        )
        return

    state_path = Path(pm.state.path).expanduser()
    proposed_dir_raw = pm.proposed_dir
    proposed_dir = (
        Path(proposed_dir_raw).expanduser()
        if Path(proposed_dir_raw).is_absolute()
        else (vault_path / proposed_dir_raw)
    )

    # Derive the instance basename for the suggested-next-step line.
    if config_path:
        instance_basename = Path(str(config_path)).name
    else:
        instance_basename = "config.yaml"

    # Resolve overrides + defaults from the config block.
    effective_min = (
        int(min_cluster_size)
        if min_cluster_size is not None
        else int(pm.min_cluster_size)
    )
    effective_top = top  # None → unlimited

    # Operator-extended denylist (default + config) per the design memo.
    from .pattern_miner import _DEFAULT_LABEL_DENYLIST  # type: ignore
    denylist = frozenset(set(_DEFAULT_LABEL_DENYLIST) | set(pm.label_denylist or []))

    # Drafter LLM endpoint. Empty endpoint OR empty model → skip the
    # drafter, write proposals with placeholder paragraphs. This is
    # the safe-degraded path the design memo calls out for env where
    # Ollama is down or not configured.
    drafter_endpoint = pm.openrouter.base_url or ""
    drafter_model = pm.openrouter.model or ""
    drafter_api_key = pm.openrouter.api_key or ""

    # Load state.
    from .pattern_miner_state import PatternMinerState
    state = PatternMinerState(state_path)
    state.load()

    from .pattern_miner import mine_patterns
    result = mine_patterns(
        vault_path=vault_path,
        surveyor_state_path=surveyor_state_path,
        state=state,
        proposed_dir=proposed_dir,
        canonical_match_dirs=tuple(pm.canonical_match_dirs),
        label_denylist=denylist,
        min_cluster_size=effective_min,
        top_n=effective_top,
        drafter_endpoint=drafter_endpoint,
        drafter_model=drafter_model,
        drafter_api_key=drafter_api_key,
        instance_config_basename=instance_basename,
        dry_run=dry_run,
    )

    print(f"=== Pattern Miner — vault={vault_path} ===")
    print(
        f"surveyor_state={surveyor_state_path}  proposed_dir={proposed_dir}\n"
        f"min_cluster_size={effective_min}  top={effective_top}  "
        f"dry_run={dry_run}"
    )
    print(
        f"\nReconcile sweep: promoted={result.reconcile_promoted}  "
        f"discarded={result.reconcile_discarded}  "
        f"still_pending={result.reconcile_still_pending}"
    )
    print(
        f"\nNew mining: clusters_evaluated={result.candidates_evaluated}  "
        f"survivors={result.survivors}  "
        f"proposed={len(result.proposed)}\n"
        f"  skipped_dedup={result.skipped_dedup}  "
        f"skipped_no_slug={result.skipped_no_slug}  "
        f"skipped_slug_unresolvable={result.skipped_slug_unresolvable}  "
        f"slug_collisions_resolved={result.slug_collisions_resolved}  "
        f"drafter_failures={result.drafter_failures}"
    )

    if not result.proposed:
        # Per the universal "intentionally left blank" rule — explicit
        # empty-result ack. mine_patterns has already written the
        # .gitkeep marker (live mode); just signal here.
        print("\nno new patterns surfaced this run.")
        if not dry_run:
            print(f"(placeholder marker written: {proposed_dir}/.gitkeep)")
        return

    print(f"\n{'#':<3} {'Type':<14} {'Members':<8} {'Slug':<40} {'Labels'}")
    print("-" * 110)
    for i, c in enumerate(result.proposed, start=1):
        labels = ", ".join(c.cluster.labels[:3])
        slug_display = c.proposed_slug[:38] + ("…" if len(c.proposed_slug) > 38 else "")
        print(
            f"{i:<3} {c.proposed_canonical_type:<14} "
            f"{len(c.cluster.member_files):<8} {slug_display:<40} {labels}"
        )

    verb = "would write" if dry_run else "wrote"
    print(f"\n{verb} {len(result.proposed)} proposal(s) under: {proposed_dir}")


def cmd_history(config: DistillerConfig, limit: int = 10) -> None:
    """Show past extraction runs."""
    state = _init_state(config)

    if not state.runs:
        print("No run history.")
        return

    sorted_runs = sorted(
        state.runs.values(), key=lambda r: r.timestamp, reverse=True
    )
    shown = sorted_runs[:limit]

    print(f"=== Run History (last {len(shown)}) ===\n")
    print(
        f"{'ID':<10} {'Timestamp':<28} {'Candidates':<12} {'Processed':<12} {'Created':<10}"
    )
    print("-" * 75)
    for run in shown:
        total_created = sum(run.records_created.values())
        created_parts = ", ".join(
            f"{lt}:{c}" for lt, c in sorted(run.records_created.items())
        )
        created_str = created_parts if created_parts else "0"
        print(
            f"{run.run_id:<10} {run.timestamp:<28} "
            f"{run.candidates_found:<12} {run.candidates_processed:<12} "
            f"{created_str}"
        )
