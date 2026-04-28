"""Pair correction notes with their referenced inferred blocks.

Closes the audit loop opened by ``alfred.vault.attribution``: when an
agent (Salem, Hypatia, KAL-LE, etc.) appends a correction note that
references a prior inferred block via ``<!-- SUPERSEDES: inf-XXX -->``,
the original wrong block stays in place (the debugging-trail
convention from ``feedback_correction_attribution_pattern.md``) but
nothing on the wrong block visually flags that it's been superseded.

This sweep walks the vault, finds every SUPERSEDES reference, and
back-annotates the referenced ``BEGIN_INFERRED`` block with a
matching ``<!-- SUPERSEDED: see <correction-id> -->`` marker on the
line immediately after the BEGIN_INFERRED.

## Discriminator: LLM-attributed vs user-attributed

Only LLM-attributed corrections get marked. User-attributed errors
(Andrew said something wrong originally) are corrected in-place per
the memo's rule and don't carry SUPERSEDES references — the wrong
content was overwritten, not preserved.

The discriminator reads the *body* of the correction note (the prose
between the SUPERSEDES marker and the next blank line / end of note).
Per the memo's worked examples and rule:

  * LLM-attributed: explicit phrases like ``mis-inference was``,
    ``recorded inaccurately``, ``mis-inferred``, ``recorded incorrectly``,
    ``error was <agent>'s`` (where ``<agent>`` is anything other than
    Andrew / the user).
  * User-attributed: ``error was andrew's``, ``error was the user's``,
    ``andrew gave wrong``, ``recorded accurately`` (the contradiction
    of an LLM error: Salem got it right, Andrew was wrong).

If neither pattern matches, the correction is **ambiguous** — we log
a warning and skip. The memo says "without explicit attribution,
future readers can't tell which case it was" — so without explicit
attribution we don't act either.

## Correction-id convention

Correction notes don't yet have a standardized ID format the way
``inf-XXX`` markers do. To keep the SUPERSEDED back-pointer
referenceable, this sweep derives a correction-id from the note's
own date + content hash:

    correction-YYYY-MM-DD-<6chars>

Where ``YYYY-MM-DD`` is parsed from the correction line itself
(``<!-- correction 2026-04-27: ... -->``) and ``<6chars>`` is the
first 6 hex chars of sha256 over the correction's full text. If the
correction line lacks a date, today's UTC date is used as a fallback.

The convention is documented here (not in a SKILL) because it's a
janitor-internal label — the SKILL spec (owned by prompt-tuner)
defines the *correction note shape*; the *back-pointer label* is
ours.

## Idempotence

A SUPERSEDED marker line for a given correction-id is detected by
substring match (``<!-- SUPERSEDED: see correction-...``) on the
line immediately after the BEGIN_INFERRED. If already present for
the same correction-id, the sweep skips. Re-running the sweep
produces no new writes.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter

import structlog

from alfred.vault.ops import is_ignored_path

from .config import JanitorConfig

log = structlog.get_logger(__name__)


# Match a SUPERSEDES reference inside an HTML comment.
# Captures the full ``inf-YYYYMMDD-agent-hash`` ID.
#
# The trailing ``[\w-]+`` accepts hyphens so hyphenated agent names
# (``kal-le``, ``stay-c``) round-trip correctly. Mirrors the canonical
# ``_BEGIN_RE`` shape in ``alfred.vault.attribution`` (which uses
# ``[\w-]+`` for the same reason). The two regexes drifted on day one
# — see the regression test for ``make_marker_id("kal-le", ...)`` and
# ``make_marker_id("stay-c", ...)`` round-tripping through this regex.
#
# Tolerates either end form (``-->`` may be on a separate line in the
# wild, but the brief's spec is single-line — we keep the regex
# single-line for now).
_SUPERSEDES_RE = re.compile(
    r"<!--\s*SUPERSEDES:\s*(?P<inf_id>inf-\d{8}-[\w-]+)\s*-->"
)

# Match the BEGIN_INFERRED opening for a specific marker_id. We rebuild
# this per lookup so the marker_id is correctly escaped.
_BEGIN_INFERRED_TEMPLATE = (
    r"<!--\s*BEGIN_INFERRED\s+marker_id=[\"']{marker_id}[\"']\s*-->"
)

# Detect an existing SUPERSEDED back-pointer. Substring match is enough
# for the idempotence check — we look at the line immediately after
# BEGIN_INFERRED and bail if it already mentions SUPERSEDED for any
# correction-id.
_SUPERSEDED_LINE_RE = re.compile(
    r"<!--\s*SUPERSEDED:\s*see\s+(?P<correction_id>correction-[\w-]+)\s*-->"
)

# Match a correction note opening line with optional date stamp.
# Examples:
#   ``<!-- correction 2026-04-27: ... -->``
#   ``<!-- correction-2026-04-27 -->``
#   ``<!-- correction: ... -->`` (no date)
#
# We use this to anchor "where does the correction note start?" so we
# can extract its body for attribution discrimination.
_CORRECTION_OPEN_RE = re.compile(
    r"<!--\s*correction[\s:-]+(?P<date>\d{4}-\d{2}-\d{2})?[^\n]*-->",
    re.IGNORECASE,
)


# Phrase patterns indicating the original error was the agent's
# (LLM-attributed). Lowercased substring match — the memo's worked
# examples use natural prose, so we accept any of these markers.
_LLM_ATTRIBUTED_PHRASES = (
    "mis-inference was",
    "misinference was",
    "mis-inferred",
    "misinferred",
    "recorded inaccurately",
    "recorded incorrectly",
    "wrong content was",  # e.g. "wrong content was Salem's"
    "crossed wires",
    "model got wrong",
    "model recorded",
    "instance recorded",
    "agent's mistake",
    "agent's error",
)

# Phrase patterns indicating the original error was the user's.
# Lowercased substring match. Includes named agent forms ("Salem
# recorded accurately") because the contrastive shape is the
# discriminator: when the prose says the agent recorded *accurately*,
# the implication is the user gave bad input.
_USER_ATTRIBUTED_PHRASES = (
    "error was andrew's",
    "error was the user's",
    "andrew's mistake",
    "user's mistake",
    "andrew gave wrong",
    "user gave wrong",
    "recorded accurately",
    "input was wrong",
    "andrew was wrong",
)


@dataclass
class SupersededMarkerCandidate:
    """One SUPERSEDES → BEGIN_INFERRED pair found during a sweep.

    ``record_path`` is vault-relative.
    ``supersedes_line`` is 1-indexed (matches what an editor shows).
    ``inf_id`` is the inferred-block ID being superseded.
    ``correction_id`` is the back-pointer label we'll embed.
    ``attribution`` is one of ``"agent"`` (LLM-attributed → mark),
    ``"user"`` (user-attributed → skip), or ``"unknown"`` (ambiguous
    → skip with warning). The orphaned-vs-located status is tracked
    separately on ``is_orphaned`` so the attribution domain stays
    closed at three values.
    ``is_orphaned`` is True when the agent-attributed correction's
    ``inf-XXX`` block can't be located in the same record (broken
    back-reference). Counted, logged, and surfaced in
    ``result.candidates`` for triage but no marker is written.
    """

    record_path: str
    supersedes_line: int
    inf_id: str
    correction_id: str
    attribution: str
    is_orphaned: bool = False


@dataclass
class SupersededMarkerResult:
    """Aggregated outcome of a sweep run.

    ``marked``: BEGIN_INFERRED blocks that got a fresh SUPERSEDED
    back-pointer this run.
    ``skipped_already_marked``: pairs that were already back-pointed
    for the same correction-id (idempotent re-run).
    ``skipped_user_attributed``: SUPERSEDES references where the
    correction note explicitly attributes the error to the user.
    Per the memo, those don't get markers — the wrong content was
    fixed in-place, not preserved.
    ``skipped_ambiguous``: SUPERSEDES references whose correction
    note didn't carry explicit attribution. We don't guess.
    ``orphaned``: SUPERSEDES references whose ``inf-XXX`` block
    couldn't be located in the same record (broken back-reference,
    likely a typo or a deleted block).
    ``errors``: per-record (path, error_message).
    ``elapsed_seconds``: wall-clock duration of the sweep.
    """

    marked: int = 0
    skipped_already_marked: int = 0
    skipped_user_attributed: int = 0
    skipped_ambiguous: int = 0
    orphaned: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)
    candidates: list[SupersededMarkerCandidate] = field(default_factory=list)
    elapsed_seconds: float = 0.0

    def summary_line(self) -> str:
        return (
            f"marked={self.marked} "
            f"skipped_already_marked={self.skipped_already_marked} "
            f"skipped_user_attributed={self.skipped_user_attributed} "
            f"skipped_ambiguous={self.skipped_ambiguous} "
            f"orphaned={self.orphaned} "
            f"errors={len(self.errors)} "
            f"elapsed={self.elapsed_seconds:.2f}s"
        )


def _classify_attribution(correction_body: str) -> str:
    """Classify a correction note as agent-/user-attributed.

    Returns ``"agent"`` (LLM-attributed → act), ``"user"`` (user-
    attributed → skip), or ``"unknown"`` (ambiguous → skip with
    warning).

    The decision rule:

    1. If a user-attributed phrase matches → ``"user"``. We give
       user-attribution priority because the memo's "recorded
       accurately" phrasing is contrastive — when present, it
       explicitly *denies* an LLM error, so we shouldn't second-
       guess it even if some unrelated phrase looks LLM-ish.
    2. Else if an agent-attributed phrase matches → ``"agent"``.
    3. Else → ``"unknown"``.
    """
    text = correction_body.lower()
    for phrase in _USER_ATTRIBUTED_PHRASES:
        if phrase in text:
            return "user"
    for phrase in _LLM_ATTRIBUTED_PHRASES:
        if phrase in text:
            return "agent"
    return "unknown"


def _extract_correction_block(
    lines: list[str], supersedes_line_idx: int
) -> tuple[str, str | None]:
    """Return (correction_body, correction_date) for the note containing
    the SUPERSEDES at ``supersedes_line_idx``.

    Walks backward from the SUPERSEDES line to find the nearest
    ``<!-- correction ... -->`` opening (or the start of file), then
    forward from the SUPERSEDES line until a blank line / next
    correction opening / EOF.

    Returns the joined block as ``correction_body`` and the parsed
    ``YYYY-MM-DD`` date string (or ``None`` if not present).

    If no correction-opening anchor is found above the SUPERSEDES,
    we fall back to "the SUPERSEDES line itself plus everything
    after it until a blank line" — the SUPERSEDES marker may BE the
    correction-note opener in some shapes.
    """
    n = len(lines)

    # Walk backward for a correction-opening anchor or a blank-line
    # boundary. We stop at the FIRST blank line we encounter going
    # back — a correction note is a contiguous prose paragraph.
    start_idx = supersedes_line_idx
    correction_date: str | None = None
    for i in range(supersedes_line_idx - 1, -1, -1):
        line = lines[i]
        if not line.strip():
            # Blank line: prior content isn't part of this correction.
            start_idx = i + 1
            break
        m = _CORRECTION_OPEN_RE.search(line)
        if m:
            start_idx = i
            correction_date = m.group("date")
            break
        start_idx = i
    else:
        # Reached top of file without a blank/anchor — the whole
        # body up to supersedes_line_idx is the candidate block.
        start_idx = 0

    # Walk forward until blank line or EOF. We DON'T stop at a
    # second correction-opening because in practice corrections are
    # paragraph-shaped — a blank line is the natural terminator.
    end_idx = supersedes_line_idx + 1
    for i in range(supersedes_line_idx + 1, n):
        if not lines[i].strip():
            end_idx = i
            break
        end_idx = i + 1

    body = "\n".join(lines[start_idx:end_idx])

    # Try to recover a date from the body if the open-anchor regex
    # didn't capture one (e.g. SUPERSEDES on a line that also has
    # a date later in it).
    if correction_date is None:
        date_m = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", body)
        if date_m:
            correction_date = date_m.group(1)

    return body, correction_date


def _build_correction_id(body: str, date: str | None) -> str:
    """Build a stable correction-id from a correction note's body+date.

    Shape: ``correction-YYYY-MM-DD-<6hex>``. Falls back to
    ``correction-undated-<6hex>`` if no date can be recovered. The
    hash is over the trimmed body so re-running the sweep on
    unchanged text yields the same ID — that's the basis for
    idempotence.
    """
    digest = hashlib.sha256(body.strip().encode("utf-8")).hexdigest()[:6]
    if date:
        return f"correction-{date}-{digest}"
    return f"correction-undated-{digest}"


def _find_begin_inferred_line(lines: list[str], inf_id: str) -> int | None:
    """Return the 0-indexed line of the BEGIN_INFERRED for ``inf_id``,
    or ``None`` if not present in this body."""
    pattern = re.compile(_BEGIN_INFERRED_TEMPLATE.format(marker_id=re.escape(inf_id)))
    for i, line in enumerate(lines):
        if pattern.search(line):
            return i
    return None


def _already_marked_for_correction(
    lines: list[str], begin_idx: int, correction_id: str
) -> bool:
    """Return True if the line right after ``begin_idx`` already carries
    a SUPERSEDED marker for the same correction-id.

    We scan ONLY the immediately-following line. If a different
    correction-id already supersedes the block, we'll happily add a
    second SUPERSEDED marker for our id below it — multiple
    corrections supersedng the same wrong block is a real (if
    rare) pattern.
    """
    if begin_idx + 1 >= len(lines):
        return False
    next_line = lines[begin_idx + 1]
    m = _SUPERSEDED_LINE_RE.search(next_line)
    if m is None:
        return False
    return m.group("correction_id") == correction_id


def _scan_record(
    rel_path: str, full_path: Path
) -> tuple[list[SupersededMarkerCandidate], int, int, int, str | None]:
    """Read one record and find SUPERSEDES candidates.

    Returns ``(candidates, already_marked, user_attributed,
    ambiguous, error)``. ``error`` is ``None`` on success.

    ``already_marked`` counts pairs that are already back-pointed
    for the same correction-id (idempotence skip).
    ``user_attributed`` and ``ambiguous`` are pre-buckets — they're
    counted at scan time so the sweep can return accurate stats
    even on dry-run (apply=False).
    """
    try:
        raw = full_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [], 0, 0, 0, f"read failed: {exc}"

    lines = raw.splitlines()
    candidates: list[SupersededMarkerCandidate] = []
    already_marked = 0
    user_attributed = 0
    ambiguous = 0

    for i, line in enumerate(lines):
        m = _SUPERSEDES_RE.search(line)
        if m is None:
            continue
        inf_id = m.group("inf_id")

        body, date = _extract_correction_block(lines, i)
        attribution = _classify_attribution(body)
        correction_id = _build_correction_id(body, date)

        candidate = SupersededMarkerCandidate(
            record_path=rel_path,
            supersedes_line=i + 1,
            inf_id=inf_id,
            correction_id=correction_id,
            attribution=attribution,
        )

        if attribution == "user":
            user_attributed += 1
            continue
        if attribution == "unknown":
            ambiguous += 1
            log.warning(
                "janitor.superseded.ambiguous_attribution",
                path=rel_path,
                line=i + 1,
                inf_id=inf_id,
                detail="correction note lacks explicit agent/user attribution",
            )
            continue

        # Agent-attributed. Verify the BEGIN_INFERRED block exists in
        # this record and isn't already marked for this correction.
        begin_idx = _find_begin_inferred_line(lines, inf_id)
        if begin_idx is None:
            # Orphaned → counted by caller after we return. Attribution
            # stays ``"agent"`` so downstream consumers can still see
            # *why* we'd have marked it had the block been findable.
            candidate.is_orphaned = True
            candidates.append(candidate)
            continue

        if _already_marked_for_correction(lines, begin_idx, correction_id):
            already_marked += 1
            continue

        candidates.append(candidate)

    return candidates, already_marked, user_attributed, ambiguous, None


def _apply_marker(
    full_path: Path,
    candidate: SupersededMarkerCandidate,
) -> bool:
    """Insert a SUPERSEDED line after the BEGIN_INFERRED for ``candidate``.

    Returns True on a successful write, False when the block can't
    be located on re-read (e.g. another sweep raced us). Re-reads
    the file fresh so multiple candidates targeting the same record
    each see the latest content — earlier inserts shift line
    numbers, so we never trust the scan-time line indices for the
    write.
    """
    try:
        raw = full_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False

    lines = raw.splitlines(keepends=True)
    # We need to find begin_idx fresh on the current content. Re-use
    # the same regex builder.
    pattern = re.compile(
        _BEGIN_INFERRED_TEMPLATE.format(marker_id=re.escape(candidate.inf_id))
    )
    begin_idx: int | None = None
    for i, line in enumerate(lines):
        if pattern.search(line):
            begin_idx = i
            break
    if begin_idx is None:
        return False

    # Idempotence re-check on fresh content.
    if begin_idx + 1 < len(lines):
        next_line = lines[begin_idx + 1]
        existing = _SUPERSEDED_LINE_RE.search(next_line)
        if existing and existing.group("correction_id") == candidate.correction_id:
            return False

    # Determine the line ending used by the file. ``splitlines(True)``
    # preserves the original ending of each line, so the BEGIN line's
    # trailing chars are our reference.
    begin_line = lines[begin_idx]
    if begin_line.endswith("\r\n"):
        eol = "\r\n"
    elif begin_line.endswith("\n"):
        eol = "\n"
    else:
        eol = "\n"

    new_line = f"<!-- SUPERSEDED: see {candidate.correction_id} -->{eol}"
    new_lines = lines[: begin_idx + 1] + [new_line] + lines[begin_idx + 1 :]

    try:
        full_path.write_text("".join(new_lines), encoding="utf-8")
    except OSError as exc:
        log.warning(
            "janitor.superseded.write_failed",
            path=str(full_path),
            error=str(exc)[:200],
        )
        return False
    return True


def run_superseded_marker_sweep(
    config: JanitorConfig,
    *,
    apply: bool = True,
) -> SupersededMarkerResult:
    """Walk the vault and add SUPERSEDED back-pointers.

    Runs as part of the hourly structural pass — pure Python, no
    LLM, deterministic.

    Pass ``apply=False`` for a dry-run that collects candidates
    without writing.
    """
    started = perf_counter()
    result = SupersededMarkerResult()

    vault_path = config.vault.vault_path
    ignore_dirs = set(config.vault.ignore_dirs)
    ignore_files = set(config.vault.ignore_files)

    if not vault_path.exists():
        log.warning("janitor.superseded.vault_missing", path=str(vault_path))
        result.elapsed_seconds = perf_counter() - started
        return result

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        if is_ignored_path(rel, ignore_dirs):
            continue
        if md_file.name in ignore_files:
            continue
        rel_str = str(rel).replace("\\", "/")

        candidates, already_marked, user_attr, ambiguous, err = _scan_record(
            rel_str, md_file
        )
        if err is not None:
            result.errors.append((rel_str, err))
            continue

        result.skipped_already_marked += already_marked
        result.skipped_user_attributed += user_attr
        result.skipped_ambiguous += ambiguous

        for candidate in candidates:
            if candidate.is_orphaned:
                result.orphaned += 1
                log.warning(
                    "janitor.superseded.orphaned_reference",
                    path=rel_str,
                    line=candidate.supersedes_line,
                    inf_id=candidate.inf_id,
                    detail="SUPERSEDES references inf-XXX that isn't in this record",
                )
                result.candidates.append(candidate)
                continue

            result.candidates.append(candidate)
            if not apply:
                continue

            if _apply_marker(md_file, candidate):
                result.marked += 1
                log.info(
                    "janitor.superseded.marked",
                    path=rel_str,
                    inf_id=candidate.inf_id,
                    correction_id=candidate.correction_id,
                )
            else:
                # Either the block disappeared between scan and write,
                # or someone else marked it for the same correction-id
                # in between. Both are benign — count under
                # already_marked so the stats stay clean.
                result.skipped_already_marked += 1

    result.elapsed_seconds = perf_counter() - started
    log.info(
        "janitor.superseded.sweep_complete",
        marked=result.marked,
        skipped_already_marked=result.skipped_already_marked,
        skipped_user_attributed=result.skipped_user_attributed,
        skipped_ambiguous=result.skipped_ambiguous,
        orphaned=result.orphaned,
        errors=len(result.errors),
        elapsed_seconds=result.elapsed_seconds,
    )
    return result


__all__ = [
    "SupersededMarkerCandidate",
    "SupersededMarkerResult",
    "run_superseded_marker_sweep",
]
