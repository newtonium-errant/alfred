#!/usr/bin/env python3
"""Distiller comparison harness — pairwise (shadow vs legacy) + N-way.

ORIGINAL USE CASE — pairwise (Week 1 distiller rebuild, c7):

    Week 1 shipped a shadow pipeline writing to
    ``data/shadow/distiller/<type>/`` in parallel with the legacy
    agentic distiller writing to ``vault/<type>/``. Week 2's
    validation plan originally required the operator to hand-compare
    records across both trees (~30-45 min). The pairwise mode does
    the mechanical diff so the operator's hand-rating is limited to
    the disagreements the machine can't adjudicate.

NEW USE CASE — N-way (Path C Phase 1 spike, 2026-05-06):

    The spike runs distiller against the same corpus 3 times
    (anthropic / ollama-32b / ollama-72b), each writing to its own
    output tree. The N-way mode (``--backend NAME=PATH`` repeated)
    produces a pivot table comparing all N backends per source
    record + the metrics specified in
    ``project_path_c_phase1_spike.md`` Step 5: count match per
    backend, per-type agreement %, confidence-bucket distribution,
    SequenceMatcher claim similarity, optional latency from stamp
    files.

    Semantic-cosine similarity via surveyor's nomic-embed-text
    embedder is DEFERRED for now — the harness reports
    SequenceMatcher claim similarity, which is sufficient for the
    spike's PASS/FAIL gate (≥0.7 on ≥75% of pairs). Operator can
    spot-check any sub-0.7 pair manually; if the SequenceMatcher
    floor turns out too noisy, a follow-up adds embeddings.

See memory: project_distiller_rebuild.md, project_path_c_phase1_spike.md
See session note: vault/session/Distiller rebuild Week 1 MVP 2026-04-24.md

Offline — no Anthropic API calls, no vault writes. Read-only.
No pytest dependency (feedback_pytest_wsl_hang.md).

Run (pairwise — existing shape):

    python scripts/distiller_rebuild_compare.py
    python scripts/distiller_rebuild_compare.py --type assumption,decision
    python scripts/distiller_rebuild_compare.py --format json --since 72

Run (N-way — Path C spike):

    python scripts/distiller_rebuild_compare.py \\
        --backend anthropic=/home/andrew/.alfred/spike/anthropic_2026-05-06 \\
        --backend ollama-32b=/home/andrew/.alfred/spike/ollama-32b_2026-05-06 \\
        --backend ollama-72b=/home/andrew/.alfred/spike/ollama-72b_2026-05-06 \\
        --type assumption,constraint,decision \\
        --format md
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import frontmatter  # noqa: E402

from alfred.vault.schema import LEARN_TYPES, TYPE_DIRECTORY  # noqa: E402

DEFAULT_SHADOW_ROOT = REPO_ROOT / "data" / "shadow" / "distiller"
DEFAULT_VAULT_ROOT = REPO_ROOT / "vault"

# Frontmatter fields we compare 1:1 for equality. The brief calls out
# these five. ``name`` and ``title`` are treated as aliases (legacy
# uses ``name``, shadow writer emits ``name`` as well — same field).
COMPARE_FIELDS: tuple[str, ...] = ("type", "status", "confidence", "name", "project")

# Fields that carry provenance (source) links. We union these across
# both records and treat them as a set. Legacy writer emits ``based_on``;
# the shadow writer emits ``source_links``. A few older legacy records
# also use ``source`` (string) or ``related``. Union keeps the set
# comparison stable across writer shape drift.
PROVENANCE_FIELDS: tuple[str, ...] = ("source_links", "based_on", "related", "source")

# Fuzzy-match title threshold — above this two titles are considered
# the "same record" when slug-match fails. 0.85 is empirically
# tight enough to catch "Thirty-Second Poll Interval..." vs
# "30-Second Poll Interval..." without false-merging distinct records.
TITLE_MATCH_THRESHOLD = 0.85

# Claim body similarity threshold — below this we flag a disagreement
# even if frontmatter fields agree. Set at 0.70 because the legacy
# writer often pads the claim with basis prose the shadow writer
# returns as a bare claim; we want to flag semantic divergence, not
# prose-length differences.
CLAIM_SIMILARITY_THRESHOLD = 0.70


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------


@dataclass
class Record:
    """One learn record loaded from disk, side-agnostic."""

    path: Path
    slug: str          # filename stem (case-insensitive comparison)
    title: str         # frontmatter `name` or derived from stem
    mtime: float
    meta: dict[str, Any]
    body: str
    record_type: str
    claim: str         # extracted from frontmatter OR body `## Claim` section


@dataclass
class PairDiff:
    """Result of comparing one shadow/legacy pair."""

    slug: str
    shadow_path: str
    legacy_path: str
    match_kind: str    # "slug" | "fuzzy-title"
    field_mismatches: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    provenance_only_shadow: list[str] = field(default_factory=list)
    provenance_only_legacy: list[str] = field(default_factory=list)
    claim_similarity: float = 1.0
    claim_below_threshold: bool = False

    @property
    def agreed(self) -> bool:
        return (
            not self.field_mismatches
            and not self.provenance_only_shadow
            and not self.provenance_only_legacy
            and not self.claim_below_threshold
        )


# ---------------------------------------------------------------------------
# Loading + normalization
# ---------------------------------------------------------------------------


_CLAIM_HEADING = re.compile(r"^##\s+Claim\s*$", re.IGNORECASE | re.MULTILINE)
_NEXT_HEADING = re.compile(r"^##\s+", re.MULTILINE)
# Wikilink strip: `[[type/Name]]` or `[[Name]]` → `Name`. We drop the
# type/ prefix because legacy and shadow writers disagree on whether
# the prefix belongs in the stored value (legacy wraps in full wikilink
# including type path; shadow stores either bare or type-prefixed
# strings). Normalizing to bare Name makes set comparison reliable.
_WIKILINK_STRIP = re.compile(r"^\[\[(?:[^/\]]+/)?([^\]]+)\]\]$")
# Bare-string prefix strip: "note/Shared Source" → "Shared Source".
# Mirrors the wikilink-strip so both forms collapse to the same normal
# form regardless of which writer wrote the field.
_TYPE_PREFIX_STRIP = re.compile(r"^[a-z_]+/(.+)$")


def _extract_claim(meta: dict[str, Any], body: str) -> str:
    """Pull the claim text — frontmatter first, then `## Claim` body section."""
    fm_claim = meta.get("claim")
    if isinstance(fm_claim, str) and fm_claim.strip():
        return fm_claim.strip()

    match = _CLAIM_HEADING.search(body)
    if not match:
        return ""
    start = match.end()
    tail = body[start:]
    next_match = _NEXT_HEADING.search(tail)
    end = next_match.start() if next_match else len(tail)
    return tail[:end].strip()


def _normalize_scalar(value: Any) -> Any:
    """Normalize a single scalar: strip wikilinks, strip type/ prefix."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    m = _WIKILINK_STRIP.match(stripped)
    if m:
        stripped = m.group(1).strip()
    m2 = _TYPE_PREFIX_STRIP.match(stripped)
    if m2:
        stripped = m2.group(1).strip()
    return stripped


def _normalize_value(value: Any) -> Any:
    """Strip wikilink wrappers + type/ prefixes; collapse list/scalar aliases.

    Single-element lists ``['Alfred']`` compare equal to scalar ``'Alfred'``
    because legacy and shadow writers disagree on whether ``project`` is
    list or scalar. Empty lists/strings normalize to None so equality
    doesn't trip on ``[] != None``.
    """
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, list):
        items = [_normalize_scalar(v) for v in value if v not in (None, "")]
        items = [i for i in items if i not in (None, "")]
        if not items:
            return None
        if len(items) == 1:
            return items[0]
        return sorted(items)
    return _normalize_scalar(value)


def _collect_provenance(meta: dict[str, Any]) -> set[str]:
    """Union provenance links across all known writer shapes.

    Normalized via ``_normalize_scalar`` so ``[[note/Foo]]``, ``note/Foo``,
    and ``Foo`` all collapse to the same element.
    """
    links: set[str] = set()
    for field_name in PROVENANCE_FIELDS:
        raw = meta.get(field_name)
        if raw is None or raw == "":
            continue
        items = raw if isinstance(raw, list) else [raw]
        for item in items:
            if item in (None, ""):
                continue
            norm = _normalize_scalar(item)
            if norm:
                links.add(str(norm))
    return links


def _load_record(path: Path) -> Record | None:
    """Parse one .md file into a Record. Returns None on parse failure."""
    try:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(
            f"[warn] skipping unparseable file {path}: {exc}",
            file=sys.stderr,
        )
        return None

    meta = dict(post.metadata or {})
    body = post.content or ""
    record_type = str(meta.get("type", "")).strip()
    title = str(meta.get("name") or meta.get("title") or path.stem).strip()
    claim = _extract_claim(meta, body)

    return Record(
        path=path,
        slug=path.stem,
        title=title,
        mtime=path.stat().st_mtime,
        meta=meta,
        body=body,
        record_type=record_type,
        claim=claim,
    )


def _walk_tree(root: Path, record_type: str, since_cutoff: float) -> list[Record]:
    """Load records of ``record_type`` under ``root``, mtime-filtered."""
    directory_name = TYPE_DIRECTORY.get(record_type, record_type)
    type_dir = root / directory_name
    if not type_dir.is_dir():
        return []

    records: list[Record] = []
    for path in sorted(type_dir.glob("*.md")):
        if not path.is_file():
            continue
        if path.stat().st_mtime < since_cutoff:
            continue
        rec = _load_record(path)
        if rec is None:
            continue
        # Defensive: if frontmatter type disagrees with directory, skip
        # rather than miscompare. Legacy vault occasionally has stray
        # records with mismatched frontmatter type.
        if rec.record_type and rec.record_type != record_type:
            continue
        records.append(rec)
    return records


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


def _match_records(
    shadow: list[Record],
    legacy: list[Record],
) -> tuple[list[tuple[Record, Record, str]], list[Record], list[Record]]:
    """Pair shadow/legacy records. Returns (pairs, orphan_shadow, orphan_legacy).

    Matching strategy:
      1. Exact slug match (case-insensitive) — most reliable.
      2. Fuzzy title match via SequenceMatcher above TITLE_MATCH_THRESHOLD.
    """
    pairs: list[tuple[Record, Record, str]] = []
    legacy_by_slug: dict[str, Record] = {r.slug.lower(): r for r in legacy}
    used_legacy: set[str] = set()
    unmatched_shadow: list[Record] = []

    # Pass 1: exact slug.
    for s_rec in shadow:
        key = s_rec.slug.lower()
        if key in legacy_by_slug and key not in used_legacy:
            pairs.append((s_rec, legacy_by_slug[key], "slug"))
            used_legacy.add(key)
        else:
            unmatched_shadow.append(s_rec)

    # Pass 2: fuzzy title for the leftovers.
    remaining_legacy = [r for r in legacy if r.slug.lower() not in used_legacy]
    still_unmatched_shadow: list[Record] = []
    for s_rec in unmatched_shadow:
        best_ratio = 0.0
        best: Record | None = None
        for l_rec in remaining_legacy:
            if l_rec.slug.lower() in used_legacy:
                continue
            ratio = difflib.SequenceMatcher(
                None, s_rec.title.lower(), l_rec.title.lower()
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = l_rec
        if best is not None and best_ratio >= TITLE_MATCH_THRESHOLD:
            pairs.append((s_rec, best, "fuzzy-title"))
            used_legacy.add(best.slug.lower())
        else:
            still_unmatched_shadow.append(s_rec)

    orphan_legacy = [r for r in legacy if r.slug.lower() not in used_legacy]
    return pairs, still_unmatched_shadow, orphan_legacy


# ---------------------------------------------------------------------------
# Diffing
# ---------------------------------------------------------------------------


def _diff_pair(shadow: Record, legacy: Record, match_kind: str) -> PairDiff:
    diff = PairDiff(
        slug=shadow.slug,
        shadow_path=str(shadow.path),
        legacy_path=str(legacy.path),
        match_kind=match_kind,
    )

    for fname in COMPARE_FIELDS:
        s_val = _normalize_value(shadow.meta.get(fname))
        l_val = _normalize_value(legacy.meta.get(fname))
        if s_val != l_val:
            diff.field_mismatches[fname] = (s_val, l_val)

    s_prov = _collect_provenance(shadow.meta)
    l_prov = _collect_provenance(legacy.meta)
    diff.provenance_only_shadow = sorted(s_prov - l_prov)
    diff.provenance_only_legacy = sorted(l_prov - s_prov)

    if shadow.claim and legacy.claim:
        ratio = difflib.SequenceMatcher(
            None, shadow.claim.lower(), legacy.claim.lower()
        ).ratio()
        diff.claim_similarity = round(ratio, 3)
        diff.claim_below_threshold = ratio < CLAIM_SIMILARITY_THRESHOLD
    elif shadow.claim or legacy.claim:
        # One side has a claim, the other doesn't — definitely a mismatch.
        diff.claim_similarity = 0.0
        diff.claim_below_threshold = True
    # If both sides empty, leave similarity at 1.0 and don't flag.

    return diff


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------


def _format_json(
    pairs: list[PairDiff],
    orphan_shadow: list[Record],
    orphan_legacy: list[Record],
) -> str:
    payload = {
        "pairs": [
            {
                "slug": d.slug,
                "shadow_path": d.shadow_path,
                "legacy_path": d.legacy_path,
                "match_kind": d.match_kind,
                "agreed": d.agreed,
                "field_mismatches": {
                    k: {"shadow": v[0], "legacy": v[1]}
                    for k, v in d.field_mismatches.items()
                },
                "provenance_only_shadow": d.provenance_only_shadow,
                "provenance_only_legacy": d.provenance_only_legacy,
                "claim_similarity": d.claim_similarity,
                "claim_below_threshold": d.claim_below_threshold,
            }
            for d in pairs
        ],
        "orphans_in_shadow": [
            {"slug": r.slug, "path": str(r.path), "title": r.title}
            for r in orphan_shadow
        ],
        "orphans_in_legacy": [
            {"slug": r.slug, "path": str(r.path), "title": r.title}
            for r in orphan_legacy
        ],
    }
    return json.dumps(payload, indent=2, default=str)


def _format_md(
    pairs: list[PairDiff],
    orphan_shadow: list[Record],
    orphan_legacy: list[Record],
    types: list[str],
    since_hours: int,
) -> str:
    agreed = [d for d in pairs if d.agreed]
    disagreed = [d for d in pairs if not d.agreed]
    lines: list[str] = []

    lines.append(f"# Distiller shadow-vs-legacy comparison")
    lines.append("")
    lines.append(f"- Types: {', '.join(types)}")
    lines.append(f"- Window: last {since_hours}h")
    lines.append(
        f"- Pairs: {len(pairs)} "
        f"(agreed={len(agreed)}, disagreed={len(disagreed)})"
    )
    lines.append(
        f"- Orphans: shadow={len(orphan_shadow)} legacy={len(orphan_legacy)}"
    )
    lines.append("")

    lines.append(f"## AGREED ({len(agreed)})")
    lines.append("")
    if agreed:
        lines.append(
            "Records where frontmatter fields, provenance links, and claim "
            "body all matched. No operator review needed."
        )
    else:
        lines.append("_No fully-agreed pairs in this window._")
    lines.append("")

    lines.append(f"## DISAGREEMENTS — OPERATOR REVIEW ({len(disagreed)})")
    lines.append("")
    if not disagreed:
        lines.append("_No disagreements in this window._")
    else:
        for d in disagreed:
            lines.append(f"### {d.slug}")
            lines.append(f"- match: `{d.match_kind}`")
            lines.append(f"- shadow: `{d.shadow_path}`")
            lines.append(f"- legacy: `{d.legacy_path}`")
            if d.field_mismatches:
                lines.append("- field mismatches:")
                for fname, (s_val, l_val) in d.field_mismatches.items():
                    lines.append(
                        f"  - `{fname}`: shadow={s_val!r}  legacy={l_val!r}"
                    )
            if d.provenance_only_shadow:
                lines.append(
                    f"- provenance only in shadow: {d.provenance_only_shadow}"
                )
            if d.provenance_only_legacy:
                lines.append(
                    f"- provenance only in legacy: {d.provenance_only_legacy}"
                )
            if d.claim_below_threshold:
                lines.append(
                    f"- claim similarity: {d.claim_similarity} "
                    f"(below {CLAIM_SIMILARITY_THRESHOLD})"
                )
            lines.append("")

    lines.append(f"## ORPHANS IN SHADOW ({len(orphan_shadow)})")
    lines.append("")
    lines.append(
        "_v2 extracted these but legacy did not. Candidates for new "
        "insight — or v2 over-extracting._"
    )
    lines.append("")
    if orphan_shadow:
        for r in orphan_shadow:
            lines.append(f"- `{r.slug}` — {r.title}")
    else:
        lines.append("_None._")
    lines.append("")

    lines.append(f"## ORPHANS IN LEGACY ({len(orphan_legacy)})")
    lines.append("")
    lines.append(
        "_Legacy extracted these but v2 did not. Candidates for v2 missing "
        "content — or legacy hallucinating._"
    )
    lines.append("")
    if orphan_legacy:
        for r in orphan_legacy:
            lines.append(f"- `{r.slug}` — {r.title}")
    else:
        lines.append("_None._")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# N-way mode (Path C Phase 1 spike — 2026-05-06)
# ---------------------------------------------------------------------------
#
# The pairwise (shadow vs legacy) machinery above is preserved as-is.
# The N-way path below adds a parallel surface that takes
# ``[BackendRun, ...]`` and produces ``[NwayDiff, ...]``. Records are
# matched by slug across ALL backends — same matching shape as the
# pairwise path but generalized.


# ±20% drift threshold per the spike spec. Per-record count drift
# beyond this fires a "drift" flag in the pivot output.
COUNT_DRIFT_THRESHOLD = 0.20

# Confidence buckets. Records have a ``confidence`` frontmatter field
# with one of these values; the calibration baseline is per-bucket
# percentage across all extracted records (legacy 26%/33%/40% per
# the spike spec).
CONFIDENCE_BUCKETS: tuple[str, ...] = ("high", "medium", "low")


@dataclass
class BackendRun:
    """One backend's distillation output tree.

    The spike runs three of these (anthropic / ollama-32b /
    ollama-72b) against the same source corpus; the harness diffs
    them N-way. ``records`` is keyed by slug (lowercase) so the
    cross-backend matching can find the same record across all N.
    """

    name: str            # operator-supplied label ("anthropic" / "ollama-72b" / etc.)
    root: Path           # output tree root
    records: dict[str, Record]  # slug-lowercase → Record

    @property
    def total_records(self) -> int:
        return len(self.records)

    @property
    def latency_seconds(self) -> float | None:
        """Read total wall time from a ``.latency_seconds`` stamp file
        in the backend's output tree. Spike harness writes this on
        each run; absence is non-fatal (returns ``None``)."""
        stamp = self.root / ".latency_seconds"
        if not stamp.is_file():
            return None
        try:
            text = stamp.read_text(encoding="utf-8").strip()
            return float(text)
        except (OSError, ValueError):
            return None


@dataclass
class NwayDiff:
    """One source record's N-way comparison row.

    ``per_backend`` maps backend name → Record (or None when the
    backend missed this slug entirely). The metrics summarize the
    cross-backend agreement for this one record:

      * ``type_agreement`` — fraction of present backends that
        emitted the same type. 1.0 = unanimous, 0.5 = split.
      * ``confidence_distribution`` — per-backend confidence bucket
        for this record (used by the aggregate calibration table).
      * ``claim_similarity_pairs`` — pairwise SequenceMatcher ratio
        between every (backend_a, backend_b) pair where both have
        a claim. Sub-0.7 entries flag for operator review per the
        spike PASS/FAIL gate.
    """

    slug: str
    per_backend: dict[str, Record | None] = field(default_factory=dict)
    type_agreement: float = 1.0
    confidence_distribution: dict[str, str | None] = field(default_factory=dict)
    claim_similarity_pairs: dict[tuple[str, str], float] = field(default_factory=dict)


def _walk_tree_nway(
    root: Path, record_type: str, since_cutoff: float,
) -> dict[str, Record]:
    """Same as ``_walk_tree`` but returns slug-keyed dict for N-way matching.

    Spike output trees mirror the vault layout (``<root>/<type>/<slug>.md``),
    same as the pairwise path's ``shadow_root`` and ``vault_root``.
    """
    records = _walk_tree(root, record_type, since_cutoff)
    return {r.slug.lower(): r for r in records}


def _diff_nway_one_record(
    slug: str, runs: list[BackendRun],
) -> NwayDiff:
    """Build one NwayDiff row from the matching record (or absence)
    in each backend run."""
    diff = NwayDiff(slug=slug)

    # Resolve per-backend record (None when the backend missed it).
    for run in runs:
        diff.per_backend[run.name] = run.records.get(slug)

    # Type agreement — only consider backends that produced a record.
    present = [(name, rec) for name, rec in diff.per_backend.items() if rec]
    if present:
        types = [rec.record_type for _, rec in present]
        most_common_count = max(types.count(t) for t in set(types))
        diff.type_agreement = round(most_common_count / len(present), 3)
    # else: no backend produced this record at all — leaves type_agreement
    # at the dataclass default 1.0 (vacuously true; pivot row will show
    # the absence directly).

    # Per-backend confidence bucket. ``None`` when backend missed the
    # record OR when the record has no confidence field. Both cases
    # render as a blank cell in the pivot.
    for name, rec in diff.per_backend.items():
        if rec is None:
            diff.confidence_distribution[name] = None
            continue
        conf = rec.meta.get("confidence")
        if isinstance(conf, str) and conf.lower() in CONFIDENCE_BUCKETS:
            diff.confidence_distribution[name] = conf.lower()
        else:
            diff.confidence_distribution[name] = None

    # Pairwise claim-similarity for every present pair. Skip pairs
    # where either side has an empty claim — same defensive shape as
    # the pairwise diff.
    present_names = [name for name, rec in diff.per_backend.items() if rec]
    for i in range(len(present_names)):
        for j in range(i + 1, len(present_names)):
            name_a, name_b = present_names[i], present_names[j]
            rec_a = diff.per_backend[name_a]
            rec_b = diff.per_backend[name_b]
            if rec_a is None or rec_b is None:
                continue
            if not rec_a.claim or not rec_b.claim:
                continue
            ratio = difflib.SequenceMatcher(
                None, rec_a.claim.lower(), rec_b.claim.lower(),
            ).ratio()
            diff.claim_similarity_pairs[(name_a, name_b)] = round(ratio, 3)

    return diff


def diff_nway(
    runs: list[BackendRun],
) -> list[NwayDiff]:
    """Build the N-way diff table — one row per UNION-of-slugs across
    all backends.

    Per ``feedback_intentionally_left_blank.md``: an empty backend run
    (zero records produced) still appears in the per-row pivot as
    None values, so the operator can distinguish "backend ran and
    extracted nothing for this source" from "backend wasn't even
    invoked." The harness can't tell the two apart from disk alone;
    operator-supplied stamp files (``.latency_seconds`` etc.) carry
    the run-fired signal at the BackendRun level.
    """
    union_slugs: set[str] = set()
    for run in runs:
        union_slugs.update(run.records.keys())

    return [_diff_nway_one_record(slug, runs) for slug in sorted(union_slugs)]


def _aggregate_count_match(
    runs: list[BackendRun],
) -> dict[str, dict[str, Any]]:
    """Per-backend total + pairwise drift vs the FIRST backend (the
    spike's anthropic baseline by convention).

    Returns shape:
      {
        "<backend>": {
          "total": int,
          "drift_vs_baseline": float,    # signed, fraction
          "drift_flagged": bool,         # |drift| > COUNT_DRIFT_THRESHOLD
        },
        ...
      }
    """
    if not runs:
        return {}
    baseline = runs[0]
    baseline_total = baseline.total_records or 1  # avoid div by zero
    out: dict[str, dict[str, Any]] = {}
    for run in runs:
        drift = (run.total_records - baseline.total_records) / baseline_total
        out[run.name] = {
            "total": run.total_records,
            "drift_vs_baseline": round(drift, 3),
            "drift_flagged": abs(drift) > COUNT_DRIFT_THRESHOLD,
        }
    return out


def _aggregate_type_match(
    diffs: list[NwayDiff], runs: list[BackendRun],
) -> dict[str, Any]:
    """Per-record type-agreement summary.

    Returns:
      {
        "unanimous": <count of rows where ALL present backends agreed>,
        "majority": <count where >50% but <100% agreed>,
        "split": <count where ≤50% agreed>,
        "by_record_type": {<type>: {"unanimous": n, ...}},
      }

    "Present" means the backend produced a record for that slug; an
    absent backend doesn't break unanimity for the present set.
    """
    summary = {
        "unanimous": 0,
        "majority": 0,
        "split": 0,
        "by_record_type": {},
    }
    for diff in diffs:
        present = [r for r in diff.per_backend.values() if r is not None]
        if not present:
            continue
        if diff.type_agreement >= 0.999:  # account for float rounding
            bucket = "unanimous"
        elif diff.type_agreement > 0.5:
            bucket = "majority"
        else:
            bucket = "split"
        summary[bucket] += 1
        # Per-type rollup keyed by majority-vote type for this row.
        types = [r.record_type for r in present]
        majority_type = max(set(types), key=types.count)
        per_type = summary["by_record_type"].setdefault(majority_type, {
            "unanimous": 0, "majority": 0, "split": 0,
        })
        per_type[bucket] += 1
    return summary


def _aggregate_confidence_calibration(
    diffs: list[NwayDiff], runs: list[BackendRun],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Per-backend confidence distribution.

    Returns shape:
      {
        "<backend>": {
          "high":   {"count": n, "pct": 0.30},
          "medium": {"count": n, "pct": 0.40},
          "low":    {"count": n, "pct": 0.30},
        },
        ...
      }

    Spike's calibration baseline: legacy 26% / 33% / 40%. Per the
    PASS gate: each ollama backend should be within ±10% of that
    baseline per bucket. Operator interprets the table directly;
    the harness doesn't gate on the threshold.
    """
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for run in runs:
        counts = {b: 0 for b in CONFIDENCE_BUCKETS}
        total = 0
        for diff in diffs:
            bucket = diff.confidence_distribution.get(run.name)
            if bucket is None:
                continue
            counts[bucket] += 1
            total += 1
        out[run.name] = {}
        for bucket in CONFIDENCE_BUCKETS:
            pct = counts[bucket] / total if total else 0.0
            out[run.name][bucket] = {
                "count": counts[bucket],
                "pct": round(pct, 3),
            }
    return out


def _aggregate_claim_similarity(
    diffs: list[NwayDiff], runs: list[BackendRun],
) -> dict[str, dict[str, float | int]]:
    """Pairwise claim-similarity rollup.

    Returns shape:
      {
        "<backend_a>__<backend_b>": {
          "n_pairs": int,
          "mean":    float,
          "median":  float,
          "below_threshold": int,    # count of pairs with ratio < 0.7
        },
        ...
      }

    Spike PASS gate: ≥75% of pairs ≥0.7 vs anthropic baseline.
    """
    out: dict[str, dict[str, float | int]] = {}
    for i in range(len(runs)):
        for j in range(i + 1, len(runs)):
            name_a, name_b = runs[i].name, runs[j].name
            key = f"{name_a}__{name_b}"
            ratios: list[float] = []
            for diff in diffs:
                ratio = (
                    diff.claim_similarity_pairs.get((name_a, name_b))
                    or diff.claim_similarity_pairs.get((name_b, name_a))
                )
                if ratio is not None:
                    ratios.append(ratio)
            if not ratios:
                out[key] = {
                    "n_pairs": 0,
                    "mean": 0.0,
                    "median": 0.0,
                    "below_threshold": 0,
                }
                continue
            ratios.sort()
            mid = len(ratios) // 2
            median = (
                ratios[mid] if len(ratios) % 2 == 1
                else (ratios[mid - 1] + ratios[mid]) / 2
            )
            out[key] = {
                "n_pairs": len(ratios),
                "mean": round(sum(ratios) / len(ratios), 3),
                "median": round(median, 3),
                "below_threshold": sum(1 for r in ratios if r < CLAIM_SIMILARITY_THRESHOLD),
            }
    return out


def _aggregate_latency(runs: list[BackendRun]) -> dict[str, float | None]:
    """Per-backend total wall time. ``None`` when the backend's
    output tree doesn't carry a stamp file."""
    return {run.name: run.latency_seconds for run in runs}


def format_md_pivot(
    diffs: list[NwayDiff], runs: list[BackendRun], types: list[str],
) -> str:
    """Render the N-way comparison as a Markdown report.

    Layout:
      1. Header (backends, types, total rows)
      2. Aggregate metric tables (count match, type match,
         confidence calibration, claim similarity, latency)
      3. Per-record pivot — one row per slug, one column per
         backend, showing type + confidence + presence
      4. Disagreements — per-record sections for split-type rows
         and sub-0.7 claim-similarity pairs
    """
    backend_names = [run.name for run in runs]
    lines: list[str] = []

    lines.append("# Distiller N-way comparison (Path C Phase 1 spike)")
    lines.append("")
    lines.append(f"- Backends: {', '.join(backend_names)}")
    lines.append(f"- Types: {', '.join(types)}")
    lines.append(f"- Total rows (union-of-slugs): {len(diffs)}")
    lines.append("")

    # --- Count match ---
    lines.append("## Count match")
    lines.append("")
    counts = _aggregate_count_match(runs)
    lines.append("| Backend | Records | Drift vs baseline | Flagged? |")
    lines.append("|---|---|---|---|")
    for name in backend_names:
        c = counts[name]
        flag = "FLAG" if c["drift_flagged"] else "ok"
        lines.append(
            f"| {name} | {c['total']} | "
            f"{c['drift_vs_baseline']:+.1%} | {flag} |"
        )
    lines.append("")
    lines.append(
        f"_Drift threshold: ±{COUNT_DRIFT_THRESHOLD:.0%} (spike spec). "
        f"Baseline is the FIRST backend listed (anthropic by convention)._"
    )
    lines.append("")

    # --- Type match ---
    lines.append("## Type match")
    lines.append("")
    type_summary = _aggregate_type_match(diffs, runs)
    total_with_records = (
        type_summary["unanimous"]
        + type_summary["majority"]
        + type_summary["split"]
    )
    lines.append("| Bucket | Count | % |")
    lines.append("|---|---|---|")
    for bucket in ("unanimous", "majority", "split"):
        n = type_summary[bucket]
        pct = n / total_with_records if total_with_records else 0.0
        lines.append(f"| {bucket} | {n} | {pct:.1%} |")
    lines.append("")
    if type_summary["by_record_type"]:
        lines.append("Per-type breakdown (majority-vote type per row):")
        lines.append("")
        lines.append("| Type | Unanimous | Majority | Split |")
        lines.append("|---|---|---|---|")
        for rec_type, stats in sorted(type_summary["by_record_type"].items()):
            lines.append(
                f"| {rec_type} | {stats['unanimous']} | "
                f"{stats['majority']} | {stats['split']} |"
            )
        lines.append("")

    # --- Confidence calibration ---
    lines.append("## Confidence calibration")
    lines.append("")
    calib = _aggregate_confidence_calibration(diffs, runs)
    header = "| Backend | " + " | ".join(
        f"{b} (count / %)" for b in CONFIDENCE_BUCKETS
    ) + " |"
    sep = "|---|" + "---|" * len(CONFIDENCE_BUCKETS)
    lines.append(header)
    lines.append(sep)
    for name in backend_names:
        cells = [name]
        for bucket in CONFIDENCE_BUCKETS:
            c = calib[name][bucket]
            cells.append(f"{c['count']} / {c['pct']:.1%}")
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    lines.append(
        "_Legacy distiller calibration baseline: 26% high / 33% medium / "
        "40% low. PASS gate: each ollama backend within ±10% per bucket._"
    )
    lines.append("")

    # --- Claim similarity ---
    lines.append("## Claim similarity (pairwise)")
    lines.append("")
    sim = _aggregate_claim_similarity(diffs, runs)
    if sim:
        lines.append("| Pair | n pairs | mean | median | below 0.7 |")
        lines.append("|---|---|---|---|---|")
        for key, stats in sim.items():
            n = stats["n_pairs"]
            below = stats["below_threshold"]
            below_pct = below / n if n else 0.0
            lines.append(
                f"| {key.replace('__', ' vs ')} | {n} | "
                f"{stats['mean']:.3f} | {stats['median']:.3f} | "
                f"{below} ({below_pct:.1%}) |"
            )
        lines.append("")
        lines.append(
            f"_SequenceMatcher claim ratio. Sub-{CLAIM_SIMILARITY_THRESHOLD} "
            f"pairs flag for operator spot-check. PASS gate: ≥75% of pairs "
            f"≥0.7 vs anthropic baseline. Embedding-cosine similarity is "
            f"deferred — operator may add a follow-up if SequenceMatcher "
            f"floor is too noisy._"
        )
        lines.append("")

    # --- Latency ---
    lines.append("## Latency")
    lines.append("")
    latency = _aggregate_latency(runs)
    lines.append("| Backend | Wall seconds |")
    lines.append("|---|---|")
    for name in backend_names:
        secs = latency[name]
        cell = f"{secs:.1f}" if secs is not None else "_(no stamp file)_"
        lines.append(f"| {name} | {cell} |")
    lines.append("")
    lines.append(
        "_Stamp file: ``<backend_root>/.latency_seconds`` — written by "
        "the spike harness on each run. Absence is non-fatal (the "
        "comparison still works without it; operator's wall-clock notes "
        "fill the gap)._"
    )
    lines.append("")

    # --- Per-record pivot ---
    lines.append("## Per-record pivot")
    lines.append("")
    if not diffs:
        lines.append("_No records in the union of all backends — nothing to pivot._")
        lines.append("")
    else:
        header = "| Slug | " + " | ".join(
            f"{n} (type / conf)" for n in backend_names
        ) + " | type-agree |"
        sep = "|---|" + "---|" * (len(backend_names) + 1)
        lines.append(header)
        lines.append(sep)
        for diff in diffs:
            cells = [diff.slug]
            for name in backend_names:
                rec = diff.per_backend.get(name)
                if rec is None:
                    cells.append("_(missed)_")
                else:
                    conf = diff.confidence_distribution.get(name) or "—"
                    cells.append(f"{rec.record_type} / {conf}")
            cells.append(f"{diff.type_agreement:.2f}")
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    # --- Disagreements (split type + low similarity) ---
    split_rows = [d for d in diffs if d.type_agreement < 0.999]
    low_sim_rows = [
        d for d in diffs
        if any(
            r < CLAIM_SIMILARITY_THRESHOLD
            for r in d.claim_similarity_pairs.values()
        )
    ]
    lines.append(
        f"## Disagreements — operator review "
        f"(type splits: {len(split_rows)}; sub-0.7 similarity: "
        f"{len(low_sim_rows)})"
    )
    lines.append("")
    if not split_rows and not low_sim_rows:
        # Per feedback_intentionally_left_blank.md.
        lines.append("_No disagreements to surface — every present-in-multiple-")
        lines.append("backends record agrees on type AND clears the 0.7 ")
        lines.append("similarity floor._")
        lines.append("")
    else:
        if split_rows:
            lines.append("### Type splits")
            lines.append("")
            for d in split_rows:
                lines.append(f"- `{d.slug}` (agreement {d.type_agreement:.2f})")
                for name in backend_names:
                    rec = d.per_backend.get(name)
                    if rec is None:
                        lines.append(f"  - {name}: _(missed)_")
                    else:
                        lines.append(
                            f"  - {name}: type=`{rec.record_type}` "
                            f"conf=`{d.confidence_distribution.get(name) or '—'}`"
                        )
            lines.append("")
        if low_sim_rows:
            lines.append("### Sub-0.7 claim similarity")
            lines.append("")
            for d in low_sim_rows:
                low_pairs = [
                    (k, v) for k, v in d.claim_similarity_pairs.items()
                    if v < CLAIM_SIMILARITY_THRESHOLD
                ]
                lines.append(f"- `{d.slug}`")
                for (a, b), ratio in low_pairs:
                    lines.append(f"  - {a} vs {b}: {ratio:.3f}")
            lines.append("")

    return "\n".join(lines)


def format_json_pivot(
    diffs: list[NwayDiff], runs: list[BackendRun],
) -> str:
    """JSON representation of the N-way diff. Same metric shape as
    the markdown pivot, machine-readable for CI / scripting."""
    payload = {
        "backends": [run.name for run in runs],
        "count_match": _aggregate_count_match(runs),
        "type_match": _aggregate_type_match(diffs, runs),
        "confidence_calibration": _aggregate_confidence_calibration(
            diffs, runs,
        ),
        "claim_similarity": _aggregate_claim_similarity(diffs, runs),
        "latency": _aggregate_latency(runs),
        "rows": [
            {
                "slug": d.slug,
                "per_backend": {
                    name: (
                        None if rec is None
                        else {
                            "type": rec.record_type,
                            "title": rec.title,
                            "path": str(rec.path),
                            "confidence": d.confidence_distribution.get(name),
                        }
                    )
                    for name, rec in d.per_backend.items()
                },
                "type_agreement": d.type_agreement,
                # tuple keys aren't JSON — encode as "a__b".
                "claim_similarity_pairs": {
                    f"{a}__{b}": v
                    for (a, b), v in d.claim_similarity_pairs.items()
                },
            }
            for d in diffs
        ],
    }
    return json.dumps(payload, indent=2, default=str)


def _parse_backend_arg(arg: str) -> tuple[str, Path]:
    """Parse one ``--backend NAME=PATH`` value into (name, path).

    Validation:
      * Must contain exactly one ``=``.
      * NAME must be non-empty.
      * PATH must be non-empty (existence is checked at run time, not
        parse time, so the harness can validate all backends together).
    """
    if "=" not in arg:
        raise argparse.ArgumentTypeError(
            f"--backend expects NAME=PATH; got {arg!r} (no '=')"
        )
    name, _, path_str = arg.partition("=")
    name = name.strip()
    path_str = path_str.strip()
    if not name:
        raise argparse.ArgumentTypeError(
            f"--backend NAME must be non-empty; got {arg!r}"
        )
    if not path_str:
        raise argparse.ArgumentTypeError(
            f"--backend PATH must be non-empty; got {arg!r}"
        )
    return name, Path(path_str)


def _run_nway_mode(
    backend_specs: list[tuple[str, Path]],
    types: list[str],
    since_cutoff: float,
    output_format: str,
) -> int:
    """Drive the N-way comparison from parsed CLI args."""
    # Build BackendRun objects. Validation: each path must be a
    # readable directory; missing → fail loudly so the operator sees
    # which backend tree is broken (NO silent skip — the spike's
    # whole point is the cross-backend comparison).
    runs: list[BackendRun] = []
    for name, root in backend_specs:
        if not root.is_dir():
            print(
                f"[error] --backend {name}={root} — not a directory",
                file=sys.stderr,
            )
            return 1
        # Build slug-keyed dict across all requested types.
        records: dict[str, Record] = {}
        per_type_counts: dict[str, int] = {}
        for t in types:
            type_records = _walk_tree(root, t, since_cutoff)
            for rec in type_records:
                records[rec.slug.lower()] = rec
            per_type_counts[t] = len(type_records)
        runs.append(BackendRun(name=name, root=root, records=records))
        print(
            f"[info] backend={name}: "
            f"total_records={len(records)} "
            f"per_type={per_type_counts}",
            file=sys.stderr,
        )

    diffs = diff_nway(runs)

    if output_format == "json":
        print(format_json_pivot(diffs, runs))
    else:
        print(format_md_pivot(diffs, runs, types))

    # Aggregate summary on stderr (mirrors pairwise mode's tail line).
    counts = _aggregate_count_match(runs)
    flagged = sum(1 for c in counts.values() if c["drift_flagged"])
    type_summary = _aggregate_type_match(diffs, runs)
    print(
        f"BACKENDS={len(runs)}  ROWS={len(diffs)}  "
        f"DRIFT_FLAGGED={flagged}  "
        f"UNANIMOUS={type_summary['unanimous']}  "
        f"MAJORITY={type_summary['majority']}  "
        f"SPLIT={type_summary['split']}",
        file=sys.stderr,
    )
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_types(arg: str) -> list[str]:
    types = [t.strip() for t in arg.split(",") if t.strip()]
    bad = [t for t in types if t not in LEARN_TYPES]
    if bad:
        raise argparse.ArgumentTypeError(
            f"unknown learn type(s): {bad}. "
            f"Valid: {sorted(LEARN_TYPES)}"
        )
    return types


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Mechanically diff shadow-pipeline output against legacy "
            "distiller output so the Week 2 operator review only has "
            "to hand-rate the disagreements."
        ),
    )
    parser.add_argument(
        "--type",
        type=_parse_types,
        default=["assumption"],
        help="Comma-separated learn types to compare (default: assumption)",
    )
    parser.add_argument(
        "--since",
        type=int,
        default=48,
        help="Only compare records with mtime within the last N hours "
             "(default: 48)",
    )
    parser.add_argument(
        "--format",
        choices=("md", "json"),
        default="md",
        help="Output format (default: md)",
    )
    parser.add_argument(
        "--shadow-root",
        type=Path,
        default=DEFAULT_SHADOW_ROOT,
        help=(
            f"[pairwise mode] Shadow root (default: {DEFAULT_SHADOW_ROOT}). "
            f"Ignored when --backend is used."
        ),
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
        help=(
            f"[pairwise mode] Vault root (default: {DEFAULT_VAULT_ROOT}). "
            f"Ignored when --backend is used."
        ),
    )
    parser.add_argument(
        "--backend",
        type=_parse_backend_arg,
        action="append",
        default=None,
        metavar="NAME=PATH",
        help=(
            "[N-way mode] Repeatable. NAME is the operator-supplied "
            "label (e.g. 'anthropic' / 'ollama-32b' / 'ollama-72b'); "
            "PATH is the backend's distillation output tree root. "
            "Pass --backend multiple times for the N-way pivot. "
            "When this flag is present, --shadow-root / --vault-root "
            "are ignored. Example: --backend anthropic=/path/to/anth "
            "--backend ollama-72b=/path/to/72b"
        ),
    )
    args = parser.parse_args(argv)

    since_cutoff = time.time() - (args.since * 3600)
    types: list[str] = args.type

    # N-way mode short-circuit. When the operator passes one or more
    # --backend args, route to the N-way pivot path and skip the
    # pairwise machinery entirely. Single --backend works too — the
    # output collapses to a one-column "stats only" table per the
    # spike spec's solo-run case.
    if args.backend:
        return _run_nway_mode(
            backend_specs=args.backend,
            types=types,
            since_cutoff=since_cutoff,
            output_format=args.format,
        )

    shadow_root: Path = args.shadow_root
    vault_root: Path = args.vault_root

    if not shadow_root.is_dir() or not any(shadow_root.iterdir()):
        print(
            f"[info] shadow root {shadow_root} is empty or missing — "
            f"nothing to compare. This is the expected state before the "
            f"Week 2 flag flip. Exiting 0.",
            file=sys.stderr,
        )
        return 0

    # since_cutoff + types are set above (shared with N-way path).
    all_pairs: list[PairDiff] = []
    all_orphan_shadow: list[Record] = []
    all_orphan_legacy: list[Record] = []

    for t in types:
        shadow_recs = _walk_tree(shadow_root, t, since_cutoff)
        legacy_recs = _walk_tree(vault_root, t, since_cutoff)
        pairs, orph_s, orph_l = _match_records(shadow_recs, legacy_recs)

        for s_rec, l_rec, match_kind in pairs:
            diff = _diff_pair(s_rec, l_rec, match_kind)
            all_pairs.append(diff)
        all_orphan_shadow.extend(orph_s)
        all_orphan_legacy.extend(orph_l)

        print(
            f"[info] type={t}: shadow={len(shadow_recs)} "
            f"legacy={len(legacy_recs)} pairs={len(pairs)} "
            f"orphan_shadow={len(orph_s)} orphan_legacy={len(orph_l)}",
            file=sys.stderr,
        )

    if args.format == "json":
        print(_format_json(all_pairs, all_orphan_shadow, all_orphan_legacy))
    else:
        print(
            _format_md(
                all_pairs,
                all_orphan_shadow,
                all_orphan_legacy,
                types,
                args.since,
            )
        )

    agreed = sum(1 for d in all_pairs if d.agreed)
    disagreed = len(all_pairs) - agreed
    print(
        f"AGREED={agreed}  DISAGREE={disagreed}  "
        f"ORPHAN_SHADOW={len(all_orphan_shadow)}  "
        f"ORPHAN_LEGACY={len(all_orphan_legacy)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
