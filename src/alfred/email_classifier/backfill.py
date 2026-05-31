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

    ``skipped_already_done`` counts records that had ``priority`` set
    AND were therefore skipped by the default-behavior gate. Always
    zero when ``reclassify=True`` is passed to :func:`run_backfill`
    (reclassify mode bypasses the gate, so no record is ever "skipped
    as already done"). Operator log review of a reclassify-mode run
    should see this field at 0 and the classified count include the
    previously-classified records that got re-evaluated.

    ``reclassified_verdict_changes`` (2026-05-31) counts records where
    a reclassify-mode run produced a DIFFERENT priority than the one
    already on disk. Zero on default runs (reclassify=False), and zero
    on reclassify runs where the new few-shot confirms every old
    verdict. Operator-actionable signal: "how many records actually
    moved tier under the corrected classifier?" If this stays at 0
    on a large reclassify run, the corrected few-shot didn't change
    the model's mind on anything — investigate whether the corpus
    fix landed in the prompt.
    """

    candidates: int = 0
    classified: int = 0
    skipped_already_done: int = 0
    skipped_not_email: int = 0
    errors: int = 0
    elapsed_seconds: float = 0.0
    error_paths: list[str] = field(default_factory=list)
    reclassified_verdict_changes: int = 0


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
    reclassify: bool = False,
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
    - ``reclassify`` (2026-05-31): when True, process records EVEN
      when ``priority`` is already set; overwrite priority +
      action_hint + reasoning with the new classification. Default
      False preserves the original "fill in the missing field" use
      case. Composes with ``dry_run`` (counts re-evaluations without
      writing) and ``limit`` (caps LLM calls, not skips). Operator
      use case: a post-corpus-fix retroactive re-evaluation so the
      improved few-shot rewrites earlier verdicts. Verdict CHANGES
      (vs no-op re-confirmations) are counted on
      ``summary.reclassified_verdict_changes`` and logged at info-
      level via ``email_classifier.backfill.reclassified`` with
      ``old_priority`` + ``new_priority`` fields for operator grep.

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
        reclassify=reclassify,
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

        # In default mode, skip records that already carry a priority.
        # In reclassify mode, bypass this gate but capture the old
        # priority so the verdict-change comparison + log can show the
        # before/after pair.
        old_priority: str | None = None
        if has_priority(metadata):
            if not reclassify:
                summary.skipped_already_done += 1
                _maybe_log_progress(idx, total, start, now, summary, progress_every)
                continue
            # reclassify mode: capture pre-reclassify verdict for the
            # change-detection log below. Coerce to a str so non-str
            # frontmatter (defensive — shouldn't happen on real vault
            # records but has_priority's docstring tolerates non-str
            # truthy values) renders cleanly in the log.
            old_val = metadata.get("priority")
            old_priority = str(old_val) if old_val is not None else None

        if not is_email_derived_note(metadata, body):
            summary.skipped_not_email += 1
            _maybe_log_progress(idx, total, start, now, summary, progress_every)
            continue

        summary.candidates += 1

        if dry_run:
            log.debug(
                "email_classifier.backfill.dry_run_candidate",
                path=rel_path,
                # Surface old_priority on the dry-run log so a
                # reclassify dry-run shows which records WOULD be
                # re-evaluated, distinct from first-time candidates.
                old_priority=old_priority,
            )
            _maybe_log_progress(idx, total, start, now, summary, progress_every)
            if limit is not None and summary.candidates >= limit:
                log.info("email_classifier.backfill.limit_reached", limit=limit)
                break
            continue

        # Real run — call the classifier. ``inbox_content`` is the
        # note's own body (see module docstring for rationale). The
        # classifier writes priority + action_hint via vault_edit.
        # In reclassify mode we use the returned ClassificationResult
        # to compare against the captured old_priority and emit the
        # verdict-change log when they differ.
        try:
            result = classify_record(
                vault_path=vault_path,
                note_rel_path=rel_path,
                inbox_content=body,
                config=config,
                llm_caller=llm_caller,
            )
            summary.classified += 1
            # Verdict-change accounting only meaningful in reclassify
            # mode (default mode's old_priority is always None because
            # records with priority were skipped before this point).
            if reclassify and old_priority is not None:
                if result.priority != old_priority:
                    summary.reclassified_verdict_changes += 1
                    log.info(
                        "email_classifier.backfill.reclassified",
                        path=rel_path,
                        old_priority=old_priority,
                        new_priority=result.priority,
                    )
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
        reclassify=reclassify,
        reclassified_verdict_changes=summary.reclassified_verdict_changes,
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
