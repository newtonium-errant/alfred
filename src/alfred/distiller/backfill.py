"""One-time backfill scan over a source directory of session/note files.

Built for KAL-LE distiller-radar Phase 1 (2026-04-29). KAL-LE distiller
walks an external directory of dev session notes (e.g. Salem's
``/home/andrew/alfred/vault/session/``), extracts learn records from
each file's ``## Alfred Learnings`` section, and writes those learnings
into KAL-LE's vault (``aftermath-lab/learn/<type>/``).

This is NOT a file migration: the source directory is read-only.
Learn records land in the configured vault path; the source files
are never touched.

Eligibility: a source ``.md`` file is eligible when it contains an
``## Alfred Learnings`` section. Files without the section are skipped
silently (they're auto-generated talker conversation records or
non-dev notes — not the convention's intended surface).

State: processed source paths land in ``BackfillState`` keyed by the
source-directory path (so multiple backfills against different roots
don't shadow each other). A subsequent run on the same root with the
same files completed-set is a no-op.

Anti-scope:
  - Does NOT modify source files.
  - Does NOT auto-trigger from any daemon — this is operator-invoked
    once per source root. No watcher, no schedule.
  - Does NOT migrate files between vaults.

Operationally: run via the ``alfred distiller backfill`` CLI:

  alfred --config config.kalle.yaml distiller backfill \\
      --source /home/andrew/alfred/vault/session/

Add ``--dry-run`` to preview eligibility + extraction counts without
writing learn records or updating state.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from .candidates import compute_score, score_candidate
from .config import DistillerConfig
from .extractor import extract as v2_extract
from .parser import VaultRecord, extract_alfred_learnings_section, extract_wikilinks
from .utils import get_logger
from .writer import write_learn_record

log = get_logger(__name__)


# --- State -----------------------------------------------------------------
#
# Backfill state lives in its own JSON file (sibling of the daemon's
# ``distiller_state.json``). The daemon's state is keyed by vault-relative
# rel_path; backfill is keyed by the absolute source path because the
# source directory is OUTSIDE the configured vault. Mixing the two would
# require either a shared schema (clutter) or path-prefix gymnastics
# (fragile). Separate file is the cheapest option.

@dataclass
class BackfillRecord:
    """Per-source-root backfill bookkeeping.

    ``processed_paths`` are absolute source paths (string form) that have
    been extracted into the vault. ``backfill_complete`` is set to True
    after a successful pass over the root has touched every eligible
    file. A second run with the flag set is a no-op (operator can clear
    by deleting the state file).
    """
    backfill_complete: bool = False
    processed_paths: list[str] = field(default_factory=list)
    last_run_at: str = ""
    eligible_count: int = 0
    extracted_count: int = 0
    error_count: int = 0


@dataclass
class BackfillState:
    """JSON state for backfill — keyed by source-root absolute path."""
    roots: dict[str, BackfillRecord] = field(default_factory=dict)


def _backfill_state_path(config: DistillerConfig) -> Path:
    """Sibling of the daemon's state file: ``<state_dir>/distiller_backfill_state.json``."""
    state_path = Path(config.state.path)
    return state_path.parent / "distiller_backfill_state.json"


def load_backfill_state(config: DistillerConfig) -> BackfillState:
    """Load backfill state from disk, returning empty state if absent."""
    path = _backfill_state_path(config)
    if not path.exists():
        return BackfillState()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        log.warning("backfill.state_load_failed", path=str(path))
        return BackfillState()
    roots_raw = raw.get("roots", {}) or {}
    roots: dict[str, BackfillRecord] = {}
    known_fields = set(BackfillRecord.__dataclass_fields__.keys())
    for root, rec in roots_raw.items():
        if not isinstance(rec, dict):
            continue
        # Forward-compat: filter unknown keys (state-load schema-tolerance contract).
        filtered = {k: v for k, v in rec.items() if k in known_fields}
        roots[root] = BackfillRecord(**filtered)
    return BackfillState(roots=roots)


def save_backfill_state(config: DistillerConfig, state: BackfillState) -> None:
    """Atomic save of backfill state."""
    path = _backfill_state_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "roots": {
            root: asdict(rec) for root, rec in state.roots.items()
        }
    }
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


# --- Source-file walk ------------------------------------------------------


def _parse_source_file(source_path: Path) -> VaultRecord | None:
    """Parse an absolute source file into a VaultRecord.

    Source-side parsing differs from ``parser.parse_file`` (which expects
    a vault-relative path). We use the absolute path as ``rel_path`` so
    log lines and source_links can identify the origin file.
    """
    try:
        raw_text = source_path.read_text(encoding="utf-8")
    except OSError:
        return None
    post = frontmatter.loads(raw_text)
    fm = dict(post.metadata)
    body = post.content
    record_type = fm.get("type", "session")
    wikilinks = extract_wikilinks(raw_text)
    return VaultRecord(
        rel_path=str(source_path),
        frontmatter=fm,
        body=body,
        record_type=record_type,
        wikilinks=wikilinks,
    )


@dataclass
class EligibilityReport:
    scanned: int = 0
    eligible: list[Path] = field(default_factory=list)
    ineligible_count: int = 0
    already_processed: int = 0


def scan_eligible_files(
    source_dir: Path,
    state: BackfillState,
) -> EligibilityReport:
    """Walk the source directory and identify files with ``## Alfred Learnings``.

    Already-processed files (in state) count as eligible-but-skipped so
    repeat runs are no-ops without surprising the operator.
    """
    report = EligibilityReport()
    root_key = str(source_dir.resolve())
    rec = state.roots.get(root_key, BackfillRecord())
    processed = set(rec.processed_paths)

    if not source_dir.is_dir():
        log.warning("backfill.source_missing", path=str(source_dir))
        return report

    for md_file in sorted(source_dir.rglob("*.md")):
        report.scanned += 1
        try:
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            report.ineligible_count += 1
            continue
        # Cheap eligibility check — section detector on raw body. We use
        # the same regex the production code uses so eligibility matches
        # what the extractor actually surfaces.
        if extract_alfred_learnings_section(text) is None:
            report.ineligible_count += 1
            continue
        if str(md_file.resolve()) in processed:
            report.already_processed += 1
            continue
        report.eligible.append(md_file)

    return report


# --- Main backfill driver --------------------------------------------------


@dataclass
class BackfillResult:
    run_id: str
    timestamp: str
    source_dir: str
    scanned: int = 0
    eligible: int = 0
    already_processed: int = 0
    extracted: int = 0
    errors: int = 0
    learnings_by_type: dict[str, int] = field(default_factory=dict)
    dry_run: bool = False


async def run_backfill(
    source_dir: Path,
    config: DistillerConfig,
    dry_run: bool = False,
) -> BackfillResult:
    """Walk ``source_dir``, extract learnings, write to vault.

    Behavior:
      - Walks ``source_dir`` recursively for ``*.md`` files.
      - Skips files lacking ``## Alfred Learnings`` (read-only check).
      - For each eligible new file: parses, scores, calls v2 extractor,
        writes each learning to the configured vault path via
        ``write_learn_record`` (live mode — vault-side scope gate).
      - Records the source path in backfill state on success so a
        repeat run is a no-op.
      - On extractor or write error: logs structured warning, increments
        error counter, continues to next file.

    ``dry_run=True`` does the walk + eligibility check + log preview
    but does NOT call the extractor or write any records or state.
    """
    run_id = str(uuid.uuid4())[:8]
    timestamp = datetime.now(timezone.utc).isoformat()
    source_root_abs = source_dir.resolve()
    root_key = str(source_root_abs)

    state = load_backfill_state(config)
    rec = state.roots.get(root_key, BackfillRecord())

    log.info(
        "backfill.start",
        run_id=run_id,
        source=root_key,
        dry_run=dry_run,
        already_complete=rec.backfill_complete,
    )

    report = scan_eligible_files(source_root_abs, state)
    result = BackfillResult(
        run_id=run_id,
        timestamp=timestamp,
        source_dir=root_key,
        scanned=report.scanned,
        eligible=len(report.eligible),
        already_processed=report.already_processed,
        dry_run=dry_run,
    )

    if dry_run:
        log.info(
            "backfill.dry_run_summary",
            run_id=run_id,
            scanned=result.scanned,
            eligible=result.eligible,
            already_processed=result.already_processed,
        )
        return result

    if not report.eligible:
        log.info(
            "backfill.no_new_eligible",
            run_id=run_id,
            scanned=report.scanned,
            already_processed=report.already_processed,
        )
        # Mark complete only when there's nothing left to do AND we've
        # actually walked at least one source file. Empty source roots
        # don't earn the complete flag.
        if report.scanned > 0 and not rec.backfill_complete:
            rec.backfill_complete = True
            rec.last_run_at = timestamp
            state.roots[root_key] = rec
            save_backfill_state(config, state)
        return result

    vault_path = config.vault.vault_path

    for source_file in report.eligible:
        record = _parse_source_file(source_file)
        if record is None:
            result.errors += 1
            continue

        signals = score_candidate(record)
        # Score is informational only here — the operator-invoked
        # backfill bypasses the daemon's threshold gate. Eligibility
        # is "has flagged learnings", not "score above threshold".
        _ = compute_score(signals)

        try:
            extraction = await v2_extract(
                source_body=record.body,
                source_frontmatter=record.frontmatter,
                existing_learn_titles=[],
                signals=signals,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001 — isolate per-source LLM/SDK errors
            log.warning(
                "backfill.extract_error",
                run_id=run_id,
                source=str(source_file),
                error=str(exc)[:500],
            )
            result.errors += 1
            continue

        if not extraction.learnings:
            log.info(
                "backfill.no_learnings",
                run_id=run_id,
                source=str(source_file),
            )
            # Still mark processed — we asked, the model said nothing.
            # A second pass would re-spend the same LLM cost for no gain.
            rec.processed_paths.append(str(source_file.resolve()))
            continue

        written = 0
        for spec in extraction.learnings:
            try:
                write_learn_record(spec=spec, body_draft="", vault_path=vault_path)
            except Exception as exc:  # noqa: BLE001 — per-record write isolation
                log.warning(
                    "backfill.write_error",
                    run_id=run_id,
                    source=str(source_file),
                    title=spec.title,
                    error=str(exc)[:500],
                )
                result.errors += 1
                continue
            written += 1
            result.learnings_by_type[spec.type] = (
                result.learnings_by_type.get(spec.type, 0) + 1
            )

        if written > 0:
            result.extracted += written

        # Whether 0 or N records actually wrote, we mark this source
        # as processed so a re-run doesn't re-extract. Fail-and-retry
        # is operator-driven (delete the state file).
        rec.processed_paths.append(str(source_file.resolve()))

    # Update state
    rec.last_run_at = timestamp
    rec.eligible_count = result.eligible
    rec.extracted_count = result.extracted
    rec.error_count = result.errors
    if result.errors == 0 and len(report.eligible) > 0:
        # Successful pass — set complete if we got through every eligible.
        # Subsequent re-runs will short-circuit on the no_new_eligible path.
        rec.backfill_complete = True
    state.roots[root_key] = rec
    save_backfill_state(config, state)

    log.info(
        "backfill.complete",
        run_id=run_id,
        scanned=result.scanned,
        eligible=result.eligible,
        extracted=result.extracted,
        errors=result.errors,
    )
    return result


def cmd_backfill(
    config: DistillerConfig,
    source_dir: Path,
    dry_run: bool = False,
) -> None:
    """CLI entry point for ``alfred distiller backfill``."""
    if not source_dir.exists():
        print(f"Source directory does not exist: {source_dir}")
        return
    if not source_dir.is_dir():
        print(f"Source path is not a directory: {source_dir}")
        return

    result = asyncio.run(run_backfill(source_dir, config, dry_run=dry_run))

    print()
    if dry_run:
        print(f"=== Backfill DRY-RUN — {result.timestamp} ===")
    else:
        print(f"=== Backfill {result.run_id} — {result.timestamp} ===")
    print(f"Source: {result.source_dir}")
    print(f"Scanned: {result.scanned} .md files")
    print(f"Eligible (had ## Alfred Learnings): {result.eligible}")
    print(f"Already processed (skipped): {result.already_processed}")
    if dry_run:
        print(f"\nWould extract from {result.eligible} files. Run without --dry-run to proceed.")
        return
    print(f"Extracted: {result.extracted} learn records")
    print(f"Errors: {result.errors}")
    if result.learnings_by_type:
        print(f"\nLearn records by type:")
        for lt, count in sorted(result.learnings_by_type.items()):
            print(f"  {lt}: {count}")
    if result.errors > 0:
        print(f"\n{result.errors} errors occurred — see logs for details.")
