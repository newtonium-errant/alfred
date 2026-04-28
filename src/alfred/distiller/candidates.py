"""Phase 1: Candidate identification, scoring, and grouping."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from alfred.vault.ops import is_ignored_path

from .parser import VaultRecord, parse_file, stripped_body_length, extract_wikilinks
from .utils import compute_body_hash, compute_md5, get_logger

log = get_logger(__name__)

# --- Keyword patterns ---

DECISION_KEYWORDS = re.compile(
    r"\b(decided|agreed|approved|chose|going with|settled on|confirmed|resolved to)\b",
    re.IGNORECASE,
)
ASSUMPTION_KEYWORDS = re.compile(
    r"\b(assumed|assuming|expecting|believed|should be|we think|presumably|take it that)\b",
    re.IGNORECASE,
)
CONSTRAINT_KEYWORDS = re.compile(
    r"\b(must|cannot|required|regulation|limit|deadline|restricted|prohibited|mandatory|compliance)\b",
    re.IGNORECASE,
)
CONTRADICTION_KEYWORDS = re.compile(
    r"\b(but|however|conflicting|disagrees|changed|contradicts|inconsistent|whereas|on the other hand)\b",
    re.IGNORECASE,
)


@dataclass
class CandidateSignal:
    body_length: int = 0
    has_outcome: bool = False
    has_context: bool = False
    decision_keywords: int = 0
    assumption_keywords: int = 0
    constraint_keywords: int = 0
    contradiction_keywords: int = 0
    link_density: int = 0
    already_distilled: bool = False


@dataclass
class ScoredCandidate:
    record: VaultRecord
    score: float
    signals: CandidateSignal
    md5: str
    body_hash: str = ""


@dataclass
class ExtractionBatch:
    project: str | None  # project name or None for ungrouped
    source_records: list[ScoredCandidate] = field(default_factory=list)
    existing_learns: list[VaultRecord] = field(default_factory=list)


def _count_matches(pattern: re.Pattern, text: str) -> int:
    return len(pattern.findall(text))


def score_candidate(record: VaultRecord) -> CandidateSignal:
    """Compute scoring signals for a single record."""
    body = record.body
    full_text = body + "\n" + "\n".join(
        f"{k}: {v}" for k, v in record.frontmatter.items() if isinstance(v, str)
    )

    signals = CandidateSignal(
        body_length=stripped_body_length(body),
        has_outcome="## Outcome" in body or "## outcome" in body,
        has_context="## Context" in body or "## context" in body,
        decision_keywords=_count_matches(DECISION_KEYWORDS, full_text),
        assumption_keywords=_count_matches(ASSUMPTION_KEYWORDS, full_text),
        constraint_keywords=_count_matches(CONSTRAINT_KEYWORDS, full_text),
        contradiction_keywords=_count_matches(CONTRADICTION_KEYWORDS, full_text),
        link_density=len(record.wikilinks),
    )
    return signals


def compute_score(signals: CandidateSignal) -> float:
    """Turn signals into a 0.0–1.0 candidate score."""
    score = min(signals.body_length / 500.0, 0.3)

    if signals.decision_keywords > 0:
        score += 0.15
    if signals.assumption_keywords > 0:
        score += 0.15
    if signals.constraint_keywords > 0:
        score += 0.15
    if signals.contradiction_keywords > 0:
        score += 0.15

    if signals.has_outcome:
        score += 0.1
    if signals.has_context:
        score += 0.1

    return min(score, 1.0)


def _get_project_link(record: VaultRecord) -> str | None:
    """Extract project name from a record's frontmatter."""
    proj = record.frontmatter.get("project", "")
    if isinstance(proj, list):
        proj = proj[0] if proj else ""
    if isinstance(proj, str) and proj:
        # Extract name from wikilink: "[[project/Eagle Farm]]" -> "Eagle Farm"
        links = extract_wikilinks(proj)
        if links:
            name = links[0]
            # Strip directory prefix
            if "/" in name:
                name = name.split("/", 1)[1]
            return name
    return None


def scan_candidates(
    vault_path: Path,
    ignore_dirs: list[str],
    ignore_files: list[str],
    source_types: list[str],
    threshold: float,
    distilled_files: dict[str, str] | None = None,
    project_filter: str | None = None,
    distilled_last_distilled: dict[str, str] | None = None,
) -> list[ScoredCandidate]:
    """Walk vault and identify candidate records for distillation.

    ``distilled_files`` maps rel_path → body_hash for already-distilled
    sources. The skip-distill gate compares the file's current body hash
    (frontmatter stripped) against the stored value: cosmetic frontmatter
    rewrites by janitor / surveyor don't shift body bytes, so they no
    longer cause the candidate to re-qualify on the next scan. A file with
    no stored body_hash (legacy state) is treated as unknown — re-extract
    once to populate the field, then hash-gate from then on.

    ``distilled_last_distilled`` is an optional sidecar dict mapping
    rel_path → last_distilled ISO timestamp. When present, the scanner
    emits a ``candidates.drift_skip`` log line every time the body-hash
    gate skips a file whose mtime has bumped since the last distillation.
    This is the explicit "intentionally left blank" signal we need to
    measure how often janitor/surveyor cosmetic writes would otherwise
    have triggered a wasteful re-extraction. Aggregating these counts
    over ~1 week tells us whether Option 3 (audit-log mutation-source
    gate, deferred per ``project_distiller_drift_mitigation.md``) is
    worth shipping. When the sidecar is absent the gate still works
    silently — same behavior as before, no observability.
    """
    ignore_d = set(ignore_dirs)
    ignore_f = set(ignore_files)
    distilled = distilled_files or {}
    last_distilled_map = distilled_last_distilled or {}
    candidates: list[ScoredCandidate] = []
    drift_skip_count = 0

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        rel_str = str(rel).replace("\\", "/")

        # Skip ignored dirs/files
        if is_ignored_path(rel, ignore_d):
            continue
        if rel.name in ignore_f:
            continue

        # Compute MD5 (still tracked for the legacy md5 field) and body hash
        # (the actual skip-distill gate).
        try:
            md5 = compute_md5(md_file)
            text = md_file.read_text(encoding="utf-8")
        except OSError:
            continue

        body_hash = compute_body_hash(text)

        # Skip if body unchanged since last distillation. Empty/missing
        # stored hash means legacy state — fall through to re-extract once.
        stored = distilled.get(rel_str)
        if stored and stored == body_hash:
            # Body bytes unchanged. If we have last_distilled bookkeeping
            # AND the file's mtime has bumped since then, log a drift_skip
            # — this is the cosmetic-frontmatter-rewrite signature we want
            # to count. Failing the stat lookup is non-fatal: just skip
            # silently as before.
            last_distilled_iso = last_distilled_map.get(rel_str)
            if last_distilled_iso:
                try:
                    file_mtime = md_file.stat().st_mtime
                    last_distilled_dt = _parse_iso_to_epoch(last_distilled_iso)
                except (OSError, ValueError, TypeError):
                    continue
                if last_distilled_dt is not None and file_mtime > last_distilled_dt:
                    drift_skip_count += 1
                    log.info(
                        "candidates.drift_skip",
                        path=rel_str,
                        last_distilled=last_distilled_iso,
                        file_mtime=file_mtime,
                        body_hash_unchanged=True,
                    )
            continue

        # Parse
        try:
            record = parse_file(vault_path, rel_str)
        except Exception:
            continue

        # Filter by source type
        if record.record_type not in source_types:
            continue

        # Filter by project if requested
        if project_filter:
            proj = _get_project_link(record)
            if proj != project_filter:
                continue

        # Score
        signals = score_candidate(record)
        score = compute_score(signals)

        if score >= threshold:
            candidates.append(ScoredCandidate(
                record=record,
                score=score,
                signals=signals,
                md5=md5,
                body_hash=body_hash,
            ))

    # Sort by score descending
    candidates.sort(key=lambda c: c.score, reverse=True)
    log.info(
        "candidates.scanned",
        total=len(candidates),
        drift_skips=drift_skip_count,
    )
    return candidates


def _parse_iso_to_epoch(iso_ts: str) -> float | None:
    """Parse an ISO-8601 timestamp into a POSIX epoch float.

    Tolerates the trailing-Z form ("...Z") that ``datetime.now(timezone.utc).isoformat()``
    does not produce but external tooling sometimes does. Returns None if
    parsing fails — caller treats that as "no comparison data, skip silently."
    """
    from datetime import datetime
    try:
        # Python 3.11+ fromisoformat handles offset suffixes like "+00:00".
        # Replace trailing "Z" with "+00:00" for older-format compatibility.
        normalized = iso_ts.replace("Z", "+00:00") if iso_ts.endswith("Z") else iso_ts
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return None


def group_by_project(
    candidates: list[ScoredCandidate],
) -> dict[str | None, list[ScoredCandidate]]:
    """Group candidates by their project link. None key = ungrouped."""
    groups: dict[str | None, list[ScoredCandidate]] = {}
    for c in candidates:
        proj = _get_project_link(c.record)
        groups.setdefault(proj, []).append(c)
    return groups


def _is_ungrouped(record: VaultRecord) -> bool:
    """True when a record has no ``project`` wikilink in its frontmatter.

    Used by :func:`collect_existing_learns` to scope the dedup context
    for ungrouped batches. A learn record is "ungrouped" iff
    :func:`_get_project_link` returns ``None`` — either the ``project``
    frontmatter key is absent/empty or it does not resolve to a vault
    wikilink.
    """
    return _get_project_link(record) is None


def collect_existing_learns(
    vault_path: Path,
    ignore_dirs: list[str],
    learn_types: list[str],
    project_name: str | None = None,
    ungrouped_only: bool = False,
) -> list[VaultRecord]:
    """Find existing learn records, scoped to the caller's batch.

    Scoping modes:

    - ``project_name="Eagle Farm"`` → only learns whose ``project``
      frontmatter resolves to "Eagle Farm" (project-scoped dedup
      context for a project-specific extraction batch).
    - ``ungrouped_only=True`` (c9, 2026-04-24) → only learns without
      a ``project`` wikilink. Used by the ungrouped extraction batch
      so its dedup context is peer-ungrouped learns only, not every
      learn record in the vault.
    - Default (both falsy) → every learn record in the vault.
      Used by Pass B meta-analysis and the consolidation sweep, which
      genuinely want vault-wide scope.

    The c9 change added ``ungrouped_only`` rather than overloading
    ``project_name=None`` because Pass B / consolidation rely on
    "all learns" as the default. The Plan-agent diagnosis 2026-04-24
    flagged the ungrouped extraction batch pre-c9 was passing
    ``project_name=None`` and getting 588 learns back — a firehose
    dedup context that told the LLM "everything is already captured."
    Callers opt into the narrower scope via the explicit flag.

    Precedence: ``project_name`` wins over ``ungrouped_only`` when
    both are set — a project-scoped batch has no reason to also
    restrict to ungrouped learns.
    """
    ignore_d = set(ignore_dirs)
    learns: list[VaultRecord] = []

    for md_file in vault_path.rglob("*.md"):
        rel = md_file.relative_to(vault_path)
        rel_str = str(rel).replace("\\", "/")

        if is_ignored_path(rel, ignore_d):
            continue

        try:
            record = parse_file(vault_path, rel_str)
        except Exception:
            continue

        if record.record_type not in learn_types:
            continue

        if project_name:
            # Project-scoped: only learns tied to this project.
            proj = _get_project_link(record)
            if proj != project_name:
                continue
        elif ungrouped_only:
            # Ungrouped-scoped: only learns without a project wikilink.
            if not _is_ungrouped(record):
                continue

        learns.append(record)

    return learns
