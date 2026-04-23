"""Backfill command — classify existing email-derived note records.

The c1 post-processor only fires on NEW inbox files. The ``vault/note/``
directory already holds hundreds of records from before c1 shipped
(Salem's live email pipeline ran for weeks before the classifier
existed). Those records have no ``priority`` / ``action_hint`` fields,
which means the Daily Sync calibration loop has nothing to surface to
the operator.

This module backfills the gap. Given a vault path and a classifier
config, it walks ``vault/note/*.md``, skips records that already have
``priority`` set, skips records that don't look email-derived, and
calls :func:`alfred.email_classifier.classify_record` on the remainder.

Choice: for backfill we pass the note's own body as the
``inbox_content`` argument. The classifier prompt was tuned on original
email shape (``**From:**`` + ``**Subject:**`` markers), but most notes
in the vault don't link back to their source inbox file — only 11 of
514 carry a ``relationships:`` back-reference. The curator's note body
is a distilled summary of the original email, so it carries the same
signal (sender, intent, content) in a different format. We accept the
small modality shift in exchange for simplicity + completeness. If the
LLM can't classify from the note body it writes the ``unclassified``
sentinel — same fallback path the post-processor uses.

Email-derived detection: a note is treated as email-derived when its
frontmatter ``subtype`` is ``reference`` OR its body contains email
shape markers (``**From:**``, ``**Subject:**``, an ``@``-bearing email
address, or common email-context words). This is intentionally
permissive — a false positive just means we classify a non-email note
(low harm), whereas a false negative means we miss a real email note
(which is the whole point of backfill).

Rate limiting: classification calls are sequential. The c1 classifier
uses a synchronous Anthropic SDK call; parallelising would complicate
error handling without meaningful speedup for the expected batch size
(~500 records, ~25 min total).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import frontmatter
import structlog

from .classifier import LLMCaller, classify_record
from .config import EmailClassifierConfig

log = structlog.get_logger(__name__)


# --- Constants --------------------------------------------------------------

# Email-shape markers that suggest a note was produced from an email.
# A match on any one of these flips the record into the backfill queue.
# We keep these permissive — the cost of a false positive (classifying a
# non-email note) is just one LLM call + one "unclassified" result; the
# cost of a false negative is a real email missing from the corpus.
_EMAIL_BODY_MARKERS = (
    re.compile(r"^\s*\*\*From:\*\*", re.MULTILINE),
    re.compile(r"^\s*\*\*Subject:\*\*", re.MULTILINE),
    re.compile(r"^\s*\*\*Account:\*\*", re.MULTILINE),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"\b(?:email|newsletter|sender|subject line|unsubscribe|inbox)\b", re.IGNORECASE),
)

# Progress log is emitted every N records processed (classified or skipped).
_PROGRESS_EVERY = 25


# --- Result shape -----------------------------------------------------------


@dataclass
class BackfillSummary:
    """Summary of a backfill run, returned to the CLI caller.

    ``classified`` counts records the classifier ran on (regardless of
    whether the LLM returned a valid tier or the sentinel). ``skipped_*``
    buckets are mutually exclusive. ``errors`` counts records where
    :func:`classify_record` raised an unexpected exception — the run
    continues, but we want the count visible in the summary.
    """

    candidates: int = 0
    classified: int = 0
    skipped_already_done: int = 0
    skipped_not_email: int = 0
    errors: int = 0
    elapsed_seconds: float = 0.0
    error_paths: list[str] = field(default_factory=list)


# --- Heuristics -------------------------------------------------------------


def is_email_derived_note(metadata: dict, body: str) -> bool:
    """Return True when the note record looks email-derived.

    Permissive by design — see module docstring. A note qualifies when
    its ``subtype`` is ``reference`` (the curator's default tag for
    email summaries) OR its body contains any of the email-shape
    markers. Description is included in the scan so short-body notes
    with descriptive summaries still match.
    """
    subtype = metadata.get("subtype")
    if isinstance(subtype, str) and subtype.strip().lower() == "reference":
        return True

    description = metadata.get("description") or ""
    if not isinstance(description, str):
        description = ""
    scan_text = description + "\n" + (body or "")
    for pattern in _EMAIL_BODY_MARKERS:
        if pattern.search(scan_text):
            return True
    return False


def has_priority(metadata: dict) -> bool:
    """Return True when the record's frontmatter already carries a priority.

    Treat any string-valued ``priority`` field as "already classified"
    — including the unclassified sentinel (c3 calibration will pick
    those up on its own cadence). Empty string or missing key = not yet
    classified.
    """
    value = metadata.get("priority")
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


# --- Backfill runner --------------------------------------------------------


def run_backfill(
    vault_path: Path,
    config: EmailClassifierConfig,
    *,
    dry_run: bool = False,
    limit: int | None = None,
    llm_caller: LLMCaller | None = None,
    now: Callable[[], float] = time.monotonic,
    progress_every: int = _PROGRESS_EVERY,
) -> BackfillSummary:
    """Walk ``vault/note/`` and classify email-derived notes without priority.

    Parameters mirror the CLI flags:

    - ``dry_run``: scan + count but make no LLM calls or frontmatter
      writes. Useful for pre-flight check on a large vault.
    - ``limit``: stop after N classifications (skipped records don't
      count toward the limit, so the cap applies to actual LLM work).
    - ``llm_caller``: pluggable for tests — defaults to the classifier's
      built-in Anthropic SDK path.
    - ``now``: injectable clock for deterministic test timing.
    - ``progress_every``: emit a progress log every N *processed*
      records (candidates + skipped). 0 disables progress logging.

    Returns a :class:`BackfillSummary` with counts. The caller (CLI) is
    responsible for rendering the summary to the operator.
    """
    note_dir = vault_path / "note"
    if not note_dir.is_dir():
        log.warning("email_classifier.backfill.no_note_dir", path=str(note_dir))
        return BackfillSummary()

    start = now()
    summary = BackfillSummary()

    note_files = sorted(note_dir.glob("*.md"))
    total = len(note_files)
    log.info(
        "email_classifier.backfill.start",
        total=total,
        dry_run=dry_run,
        limit=limit,
    )

    for idx, note_file in enumerate(note_files, start=1):
        rel_path = f"note/{note_file.name}"
        try:
            post = frontmatter.load(str(note_file))
        except Exception as exc:  # noqa: BLE001 — malformed frontmatter → skip
            log.warning(
                "email_classifier.backfill.parse_error",
                path=rel_path,
                error=str(exc),
            )
            summary.errors += 1
            summary.error_paths.append(rel_path)
            continue

        metadata = post.metadata or {}
        body = post.content or ""

        if has_priority(metadata):
            summary.skipped_already_done += 1
            _maybe_log_progress(idx, total, start, now, summary, progress_every)
            continue

        if not is_email_derived_note(metadata, body):
            summary.skipped_not_email += 1
            _maybe_log_progress(idx, total, start, now, summary, progress_every)
            continue

        summary.candidates += 1

        if dry_run:
            log.debug(
                "email_classifier.backfill.dry_run_candidate",
                path=rel_path,
            )
            _maybe_log_progress(idx, total, start, now, summary, progress_every)
            if limit is not None and summary.candidates >= limit:
                log.info("email_classifier.backfill.limit_reached", limit=limit)
                break
            continue

        # Real run — call the classifier. ``inbox_content`` is the
        # note's own body (see module docstring for rationale). The
        # classifier writes priority + action_hint via vault_edit.
        try:
            classify_record(
                vault_path=vault_path,
                note_rel_path=rel_path,
                inbox_content=body,
                config=config,
                llm_caller=llm_caller,
            )
            summary.classified += 1
        except Exception as exc:  # noqa: BLE001 — one bad record must not abort the batch
            log.warning(
                "email_classifier.backfill.classify_error",
                path=rel_path,
                error=str(exc),
            )
            summary.errors += 1
            summary.error_paths.append(rel_path)

        _maybe_log_progress(idx, total, start, now, summary, progress_every)

        if limit is not None and summary.classified >= limit:
            log.info("email_classifier.backfill.limit_reached", limit=limit)
            break

    summary.elapsed_seconds = now() - start
    log.info(
        "email_classifier.backfill.complete",
        classified=summary.classified,
        skipped_already_done=summary.skipped_already_done,
        skipped_not_email=summary.skipped_not_email,
        errors=summary.errors,
        elapsed_seconds=round(summary.elapsed_seconds, 2),
        dry_run=dry_run,
    )
    return summary


def _maybe_log_progress(
    processed: int,
    total: int,
    start: float,
    now_fn: Callable[[], float],
    summary: BackfillSummary,
    every: int,
) -> None:
    """Emit a progress log every ``every`` records.

    Estimated-remaining math is deliberately cheap: elapsed / processed
    × remaining. It's approximate (backfill skips a lot of records
    quickly, then runs the slow classifier on the rest), but accurate
    enough for an operator to decide whether to grab a coffee.
    """
    if every <= 0 or processed % every != 0:
        return
    elapsed = now_fn() - start
    if processed <= 0:
        return
    per_record = elapsed / processed
    remaining = max(0, total - processed)
    eta = per_record * remaining
    log.info(
        "email_classifier.backfill.progress",
        processed=processed,
        total=total,
        classified=summary.classified,
        skipped_already_done=summary.skipped_already_done,
        skipped_not_email=summary.skipped_not_email,
        errors=summary.errors,
        elapsed_seconds=round(elapsed, 2),
        estimated_remaining_seconds=round(eta, 2),
    )
