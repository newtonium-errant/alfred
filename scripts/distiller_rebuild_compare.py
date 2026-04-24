#!/usr/bin/env python3
"""Shadow-vs-legacy comparison harness for the distiller rebuild (c7).

Week 1 shipped a shadow pipeline writing to ``data/shadow/distiller/<type>/``
in parallel with the legacy agentic distiller writing to ``vault/<type>/``.
Week 2's validation plan originally required the operator to hand-compare
records across both trees (~30-45 min). This script does the mechanical
diff so the operator's hand-rating is limited to the disagreements the
machine can't adjudicate.

See memory: project_distiller_rebuild.md
See session note: vault/session/Distiller rebuild Week 1 MVP 2026-04-24.md

Offline — no Anthropic API calls, no vault writes. Read-only.
No pytest dependency (feedback_pytest_wsl_hang.md).

Run:

    python scripts/distiller_rebuild_compare.py
    python scripts/distiller_rebuild_compare.py --type assumption,decision
    python scripts/distiller_rebuild_compare.py --format json --since 72
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
        help=f"Shadow root (default: {DEFAULT_SHADOW_ROOT})",
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=DEFAULT_VAULT_ROOT,
        help=f"Vault root (default: {DEFAULT_VAULT_ROOT})",
    )
    args = parser.parse_args(argv)

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

    since_cutoff = time.time() - (args.since * 3600)
    types: list[str] = args.type

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
