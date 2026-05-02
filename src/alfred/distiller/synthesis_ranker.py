"""Weekly synthesis ranker — KAL-LE distiller-radar Phase 2 MVP.

Reads distilled learn records (synthesis / decision / contradiction)
out of an aftermath-lab-style vault and ranks them by a four-term
mechanical formula. The top N are slotted into the digest's section 4
("Cross-arc patterns"), replacing the legacy ``LLM synthesis layer not
yet implemented`` placeholder.

Why mechanical (no second LLM pass): the Phase 1 backfill already
produced 98 synthesis records over an 8-day window. Confidence as a
ranking signal proved unreliable (V2 produced 78% high), but
cross-source citation density and entity diversity are rich enough on
their own to surface the genuinely high-leverage records. Phase 2.5
adds an inspector-LLM pass only if a week of observation shows
mechanical ranking is inadequate.

Path A simplification (per the Phase 2 memo): ``entity_links`` is read
straight off the frontmatter as raw distinct counts. No surveyor entity
normalization. Lower fidelity (~10-20% drift versus a normalized count)
but ships in a half-day and avoids the surveyor-on-KAL-LE prereq.

Score formula::

    score = cross_source_density * w_cross_source
          + entity_diversity     * w_entity_diversity
          + recency_score        * w_recency
          + type_weight          * w_type

- ``cross_source_density``  = ``len(source_links)`` (raw count of
  cited source records)
- ``entity_diversity``      = distinct ``entity_links`` count
- ``recency_score``         = exponential decay with a 7-day half-life
  on the ``created`` timestamp; records older than the configured
  ``window_days`` get 0
- ``type_weight``           = synthesis=3, contradiction=2, decision=1
  (synthesis explicitly carries the highest leverage)

All four weights are configurable via the
``distiller.synthesis_ranker.weights`` config block (operator override).
Defaults match the Phase 2 spec.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import structlog

log = structlog.get_logger(__name__)

# Per-record subdirectories the ranker reads from. Each is a learn type
# the Phase 1 distiller writes deterministically; together they cover
# the cross-arc-pattern surface we care about for section 4.
_RANKED_TYPE_DIRS: tuple[str, ...] = ("synthesis", "decision", "contradiction")

# Default weights. See the formula in the module docstring.
_DEFAULT_WEIGHTS: dict[str, float] = {
    "cross_source": 3.0,
    "entity_diversity": 2.0,
    "recency": 1.0,
    "type": 1.0,
}

# Per-type leverage. Synthesis = highest (it's a generalization across
# multiple records by definition), contradiction = mid (gap-finder),
# decision = low (single-arc choice). Multiplied by ``weights["type"]``
# so operators can flatten the type axis without touching this table.
_TYPE_WEIGHTS: dict[str, float] = {
    "synthesis": 3.0,
    "contradiction": 2.0,
    "decision": 1.0,
}

# Wikilink shape inside a YAML list — ``[[type/Record Name]]`` or
# ``[[type/Record Name|Display]]``. The display alias is irrelevant to
# de-dup so we drop everything after a pipe before normalizing.
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")


@dataclass
class ScoreBreakdown:
    """Per-term contribution for a single record, exposed for tuning."""

    cross_source: float = 0.0
    entity_diversity: float = 0.0
    recency: float = 0.0
    type_weight: float = 0.0

    def total(self) -> float:
        return (
            self.cross_source
            + self.entity_diversity
            + self.recency
            + self.type_weight
        )


@dataclass
class RankedRecord:
    """One ranked record. ``path`` is absolute; ``frontmatter`` is the
    parsed YAML head; ``body`` is the post-frontmatter markdown.
    """

    path: Path
    record_type: str
    score: float
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    breakdown: ScoreBreakdown = field(default_factory=ScoreBreakdown)
    source_count: int = 0
    entity_count: int = 0
    age_days: float | None = None


def _parse_created(value: Any) -> datetime | None:
    """Permissive ISO-date / ISO-datetime parse → tz-aware UTC.

    Distiller writers emit ``created`` as either a bare ``YYYY-MM-DD``
    string or a full ISO-8601 timestamp with timezone. Both shapes
    return tz-aware datetimes; everything else returns None and the
    record's recency score collapses to 0.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if len(s) == 10 and s.count("-") == 2:
            s = s + "T00:00:00+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return None


def _distinct_link_count(links: Any) -> int:
    """Count distinct wikilinks in a frontmatter list field.

    Accepts either a list of wikilink strings (``["[[project/Alfred]]",
    ...]``) or the rare bare-string-list shape (``["project/Alfred"]``).
    Whitespace and the optional display alias (``|alias``) are stripped
    before de-dup so ``[[project/Alfred]]`` and ``[[project/Alfred|x]]``
    count once. Empty / non-list inputs return 0.
    """
    if not isinstance(links, list):
        return 0
    seen: set[str] = set()
    for raw in links:
        if not isinstance(raw, str):
            continue
        match = _WIKILINK_RE.search(raw)
        target = match.group(1).strip() if match else raw.strip()
        # Strip alias tail if the link arrived without bracket wrapping.
        if "|" in target:
            target = target.split("|", 1)[0].strip()
        if target:
            seen.add(target)
    return len(seen)


def _recency_score(
    created: datetime | None,
    *,
    now: datetime,
    window_days: int,
) -> tuple[float, float | None]:
    """Exponential decay with a 7-day half-life.

    Returns ``(score, age_days)``. Records outside ``window_days`` get
    score=0 (still scored on the other terms — they just don't earn
    recency weight). The half-life is fixed at 7 days regardless of
    ``window_days``; the window is the cliff, the half-life is the slope.
    """
    if created is None:
        return 0.0, None
    age_seconds = (now - created).total_seconds()
    age_days = age_seconds / 86400.0
    if age_days < 0:
        # Record dated in the future — clamp to 0 days, full credit.
        age_days = 0.0
    if age_days > window_days:
        return 0.0, age_days
    # 7-day half-life: score = 0.5 ** (age / 7)
    score = math.pow(0.5, age_days / 7.0)
    return score, age_days


def summary_from_record(fm: dict[str, Any], body: str) -> str:
    """Pick a one-line summary for the digest bullet.

    Preference order:
        1. ``claim`` frontmatter field (the canonical distiller summary)
        2. ``summary`` frontmatter field (some learn types use this)
        3. First non-empty paragraph of the body, with marker comments
           and embed shortcodes stripped

    Returns an empty string if nothing usable is found. The digest
    renderer collapses whitespace and adds a fallback message; this
    function does the extraction, not the formatting.
    """
    claim = fm.get("claim")
    if isinstance(claim, str) and claim.strip():
        return claim.strip()
    summary = fm.get("summary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    if not body:
        return ""
    cleaned: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if cleaned:
                # First paragraph closed.
                break
            continue
        if stripped.startswith(("<!--", "![[")):
            continue
        if stripped.startswith("#"):
            # Skip headings — they're navigation, not content.
            continue
        cleaned.append(stripped)
    return " ".join(cleaned).strip()


def _load_record(path: Path) -> tuple[dict[str, Any], str] | None:
    """Parse one ``.md`` file. Returns ``(frontmatter, body)`` or None.

    Per-file errors (unreadable, non-YAML head, etc.) are logged and
    skipped — one corrupt record must not crash the digest.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            post = frontmatter.load(fh)
    except (OSError, UnicodeDecodeError) as exc:
        log.info("synthesis_ranker.read_failed", path=str(path), error=str(exc))
        return None
    except Exception as exc:  # noqa: BLE001 — frontmatter raises broad
        log.info("synthesis_ranker.parse_failed", path=str(path), error=str(exc))
        return None
    fm = dict(post.metadata or {})
    body = post.content or ""
    return fm, body


def _iter_record_paths(vault_path: Path) -> list[tuple[str, Path]]:
    """Yield ``(record_type, path)`` for every ranked-type ``.md`` file.

    Only the three configured directories are scanned. Subdirectories
    inside e.g. ``synthesis/`` are walked so an instance that bucketizes
    by date or topic still gets picked up.
    """
    out: list[tuple[str, Path]] = []
    for record_type in _RANKED_TYPE_DIRS:
        type_dir = vault_path / record_type
        if not type_dir.is_dir():
            continue
        for md_path in type_dir.rglob("*.md"):
            if md_path.name == ".gitkeep":
                continue
            out.append((record_type, md_path))
    return out


def _normalize_weights(weights: dict[str, Any] | None) -> dict[str, float]:
    """Merge operator overrides over the default weight table.

    Unknown keys are accepted-and-ignored for forward-compat. Non-numeric
    values fall back to the default for that key with a log warning so a
    typo in config doesn't silently zero out a term.
    """
    out: dict[str, float] = dict(_DEFAULT_WEIGHTS)
    if not isinstance(weights, dict):
        return out
    for key, value in weights.items():
        if key not in out:
            continue
        try:
            out[key] = float(value)
        except (TypeError, ValueError):
            log.info(
                "synthesis_ranker.weight_override_invalid",
                key=key, value=str(value), default=out[key],
            )
    return out


def rank_synthesis_records(
    vault_path: Path,
    *,
    window_days: int = 7,
    top_n: int = 12,
    weights: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> list[RankedRecord]:
    """Rank every synthesis/decision/contradiction record in the vault.

    Args:
        vault_path: Vault root (e.g. ``/home/andrew/aftermath-lab``). The
            ranker reads from ``vault_path/{synthesis,decision,
            contradiction}/`` recursively.
        window_days: Recency cliff. Records older than this get 0 on the
            recency term but stay scored on the other three.
        top_n: How many records to return. Tied scores are ordered by
            (newer first, then path).
        weights: Operator override. ``{cross_source, entity_diversity,
            recency, type}``; missing keys fall back to defaults.
        now: Override for testing (deterministic recency). Defaults to
            ``datetime.now(timezone.utc)``.

    Returns:
        A list of ``RankedRecord`` of length ``min(top_n, total)``.
        Empty input → empty list (not an error).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    # Defensive: ``candidates[:-5]`` returns all-but-last-5 instead of
    # erroring, so a negative ``top_n`` would silently mangle output.
    top_n = max(0, top_n)
    w = _normalize_weights(weights)

    candidates: list[RankedRecord] = []
    for record_type, path in _iter_record_paths(vault_path):
        loaded = _load_record(path)
        if loaded is None:
            continue
        fm, body = loaded
        # Type from frontmatter is authoritative when present; falls back
        # to the directory name otherwise. Records whose frontmatter type
        # disagrees with the directory keep the directory's bucket weight
        # but inherit the frontmatter's type label in the output.
        fm_type = fm.get("type") if isinstance(fm.get("type"), str) else record_type
        source_count = len(fm.get("source_links") or []) if isinstance(
            fm.get("source_links"), list,
        ) else 0
        entity_count = _distinct_link_count(fm.get("entity_links"))
        recency, age_days = _recency_score(
            _parse_created(fm.get("created")),
            now=now, window_days=window_days,
        )
        type_score = _TYPE_WEIGHTS.get(record_type, 1.0)

        breakdown = ScoreBreakdown(
            cross_source=source_count * w["cross_source"],
            entity_diversity=entity_count * w["entity_diversity"],
            recency=recency * w["recency"],
            type_weight=type_score * w["type"],
        )
        ranked = RankedRecord(
            path=path,
            record_type=fm_type or record_type,
            score=breakdown.total(),
            frontmatter=fm,
            body=body,
            breakdown=breakdown,
            source_count=source_count,
            entity_count=entity_count,
            age_days=age_days,
        )
        candidates.append(ranked)

    # Stable sort: score desc, then created desc (None last), then path.
    def _sort_key(r: RankedRecord) -> tuple[float, float, str]:
        # Negate score for descending; recency descends naturally because
        # newer records have larger recency values, but to break true ties
        # we want path ordering for determinism — stable sort + path tail.
        created = _parse_created(r.frontmatter.get("created"))
        created_ts = created.timestamp() if created else 0.0
        return (-r.score, -created_ts, str(r.path))

    candidates.sort(key=_sort_key)
    return candidates[:top_n]


__all__ = [
    "RankedRecord",
    "ScoreBreakdown",
    "rank_synthesis_records",
    "summary_from_record",
]
