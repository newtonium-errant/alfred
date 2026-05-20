"""Cluster→MOC suggestion mechanism (Phase 5 Sub-arc D1, 2026-05-19).

Per ``project_hypatia_zettelkasten_redesign.md`` Phase 5 + the
ratified D1 design (2026-05-19): when surveyor labels a cluster,
propose adding cluster members to relevant MOC ``# Contents``.
Operator-pull only — D1 NEVER writes to the vault. The accept path
(D2) edits each member record's ``mocs:`` frontmatter via canonical
``vault_edit``, which triggers the existing Phase 4 Sub-arc A
member-append hook. One write surface, one audit trail.

This module is pure: ``propose_moc_suggestions`` consumes labeled
cluster output + the in-memory record map + an existing-MOCs index
and returns a list of structured ``MocSuggestion`` records. I/O
(reading the MOC index, persisting to the queue) lives in
``daemon.py`` + ``moc_suggestion_queue.py``.

Mapping signals (ratified Q2):

  1. **member_overlap** (primary) — fraction of cluster members
     whose ``mocs:`` frontmatter list already cites the candidate
     MOC. Threshold defaults to 0.4 (40%). The operator already
     validated each ``mocs:`` reference; we just generalize the
     membership across the rest of the cluster.

  2. **fuzzy_label** (tiebreaker) — Jaccard token-overlap between
     the cluster's tag-set and the MOC's filename-stem tokens.
     Consulted only when member-overlap returns no candidates,
     so the threshold (0.5) is for genuinely novel topical
     matches, not refinements.

  3. **propose_new** — when both signals return zero, emit a
     suggestion with ``target_moc_rel_path=None`` +
     ``proposed_new_moc_name`` derived from the cluster's most
     distinctive label tag.

Inventory-MOC filter (ratified Q7): ``MOC/_*.md`` paths NEVER
appear as suggestion targets or proposed-new names. Filtered at
all three sites (target enumeration in this module, propose-new
name derivation here too, and apply-path defense in D2).

ID derivation: ``ms-YYYYMMDD-<sha256(sorted_members + target)[:8]>``.
Idempotent across sweeps with stable membership; the dedup key is
SORTED member paths + target (NOT cluster_id, which HDBSCAN
renumbers non-deterministically per sweep). Same lesson as
``daemon.py:membership_unchanged_skip`` gate.

Per-sweep + per-target caps live on ``MocSuggestionConfig`` and
are consumed by the daemon-side queue writer, not enforced here.
This module returns ALL candidate suggestions for a single
cluster; the queue layer applies cross-cluster caps.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import frontmatter
import structlog

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constants — inventory-MOC filter + filename-tokenization regex.
# ---------------------------------------------------------------------------

#: Underscore-prefix marker for inventory MOCs (Phase 4 Sub-arc B).
#: Inventory MOCs are predicate-driven and system-maintained; the
#: suggestion surface MUST NOT propose them as targets or as
#: proposed-new names. The check runs on the stem (post-strip-of
#: ``MOC/``) so any of these shapes is excluded:
#:   * ``MOC/_Open Questions.md``       (target enumeration)
#:   * ``_Open Questions``              (proposed-new name derivation)
INVENTORY_MOC_STEM_PREFIX: str = "_"

#: Tokenization regex — splits on whitespace, hyphens, underscores,
#: slashes, and obvious punctuation. Used for both cluster-tag
#: tokenization AND MOC filename-stem tokenization so the Jaccard
#: overlap compares apples to apples. Lowercased downstream.
_TOKEN_SPLIT_RE: re.Pattern[str] = re.compile(r"[\s\-_/.,:;()\[\]]+")

#: Stop-tokens dropped from the Jaccard set before overlap. ``moc`` is
#: the dominant noise word — every MOC filename ends with ``MOC`` (per
#: the ``Topic MOC.md`` filename convention from the locked plan), so
#: leaving it in would inflate the overlap score for every pair. The
#: rest are generic filler that appears in cluster tags + MOC names
#: with no discriminative value.
_TOKEN_STOPWORDS: frozenset[str] = frozenset({
    "moc", "the", "a", "an", "and", "or", "of", "on", "in", "to",
    "for", "by", "at", "is", "are", "was", "were", "be", "with",
})


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MocSuggestion:
    """A single cluster→MOC suggestion record.

    Persisted as one JSONL line by ``moc_suggestion_queue.py``.
    ``id`` is the dedup key — same (members, target) hash yields the
    same id across sweeps regardless of HDBSCAN cluster-id renumber.

    All datetime fields are ISO-8601 UTC strings (``...Z`` suffix
    omitted in favour of ``+00:00`` per ``datetime.isoformat()``).
    """

    id: str
    cluster_id_at_proposal: int
    cluster_tags: list[str]
    cluster_member_paths: list[str]  #: sorted; the dedup-key component
    target_moc_rel_path: str | None  #: None when mapping_signal=propose_new
    proposed_new_moc_name: str | None  #: set when target is None
    mapping_signal: str  #: "member_overlap" | "fuzzy_label" | "propose_new"
    mapping_score: float
    candidate_members_to_add: list[str]  #: members NOT already in target MOC
    reasoning: str
    created: str
    status: str = "pending"  #: pending | accepted | applied | rejected | archived
    decided_at: str | None = None
    applied_at: str | None = None
    last_apply_error: str | None = None  #: set when applied→failed re-flip occurs

    def to_dict(self) -> dict:
        """Serialize to a JSON-safe dict for queue persistence."""
        return asdict(self)


@dataclass
class ExistingMoc:
    """Lightweight handle for an MOC discovered on disk.

    ``mocs_index`` keys MOCs by their rel_path; this struct carries
    the post-loaded fields we need for matching without re-reading
    the file. Members are the wikilinks already present in
    ``# Contents`` (extracted by the index builder, NOT this
    module). When the index builder fails to extract for some
    reason, ``contents_members`` stays empty — match still works
    via member-overlap on the cluster side (members cite the MOC
    via their own ``mocs:`` frontmatter, which is the canonical
    membership signal, not the MOC's body Contents).
    """

    rel_path: str  #: e.g. "MOC/Stoicism MOC.md"
    stem: str  #: filename stem without extension; e.g. "Stoicism MOC"
    name: str  #: frontmatter ``name`` (display title)
    contents_members: frozenset[str]  #: rel_paths from the MOC's # Contents

    @property
    def is_inventory_moc(self) -> bool:
        """``True`` when filename stem starts with ``_`` (Sub-arc B)."""
        return self.stem.startswith(INVENTORY_MOC_STEM_PREFIX)


# ---------------------------------------------------------------------------
# MOC index builder — scans the vault's MOC/ directory.
# ---------------------------------------------------------------------------


_WIKILINK_BULLET_RE: re.Pattern[str] = re.compile(
    r"^[ \t]*-[ \t]+\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]",
    re.MULTILINE,
)
_CONTENTS_HEADING_RE: re.Pattern[str] = re.compile(
    r"^#\s+Contents\s*$", re.MULTILINE,
)
_NEXT_HEADING_RE: re.Pattern[str] = re.compile(r"^#{1,2}\s+", re.MULTILINE)


def build_existing_mocs_index(
    vault_path: Path,
) -> tuple[dict[str, ExistingMoc], bool]:
    """Scan ``<vault>/MOC/*.md`` and return a rel_path → ExistingMoc
    map of every NON-INVENTORY MOC on disk, paired with a boolean
    that reports whether the ``MOC/`` directory exists.

    Returns ``(index, moc_dir_exists)`` so the caller can decide
    whether to emit a lifecycle-gated "no MOC directory" log without
    this function having to know about the daemon's latch state.
    Per ``feedback_intentionally_left_blank.md`` + the Sub-arc D1
    fixup (2026-05-19 code-reviewer note): the suggester is pure
    logic — observability emission lives in the daemon, where the
    per-instance lifecycle state lives.

    When ``moc_dir_exists`` is False, ``index`` is empty by
    definition. When it's True, ``index`` may still be empty (the
    directory exists but holds no non-inventory MOCs).

    Inventory MOCs (``MOC/_*.md``) are filtered out here — they
    can't appear as candidate targets anyway, so excluding at the
    index-builder layer keeps downstream caller code simple.

    Body ``# Contents`` is parsed line-by-line for ``- [[type/Name]]``
    bullets; the wikilink targets become the MOC's
    ``contents_members``. The parsing is best-effort — a malformed
    bullet drops out of the set silently rather than failing the
    whole MOC. Per-bullet failure isolation matches the per-record
    failure isolation in :func:`alfred.telegram.inventory_views.collect_records`.

    Failure-isolated: any exception parsing a single MOC file logs
    a warning and skips that MOC; never raises to the caller.
    """
    moc_dir = vault_path / "MOC"
    if not moc_dir.is_dir():
        # No directory → caller logs the lifecycle-gated message.
        # We DO NOT emit ``surveyor.moc_suggestion.no_moc_dir`` here;
        # per-sweep emission was the pre-fixup behaviour and caused
        # log spam every tick on vaults that have not yet created
        # any MOCs (Hypatia's current state).
        return ({}, False)

    index: dict[str, ExistingMoc] = {}
    for md_file in sorted(moc_dir.glob("*.md")):
        stem = md_file.stem
        if stem.startswith(INVENTORY_MOC_STEM_PREFIX):
            continue  # Sub-arc B inventory MOC — never a candidate target
        try:
            post = frontmatter.load(str(md_file))
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "surveyor.moc_suggestion.moc_parse_failed",
                moc_path=str(md_file),
                error=str(exc)[:200],
            )
            continue
        fm = dict(post.metadata or {})
        name_raw = fm.get("name") or stem
        name = str(name_raw)
        body = post.content or ""
        contents_members = _extract_contents_member_paths(body)
        rel_path = f"MOC/{md_file.name}"
        index[rel_path] = ExistingMoc(
            rel_path=rel_path,
            stem=stem,
            name=name,
            contents_members=frozenset(contents_members),
        )
    return (index, True)


def _extract_contents_member_paths(body: str) -> list[str]:
    """Return the wikilink targets (without brackets, pipe-alias
    stripped) from the ``# Contents`` section. Bullets outside the
    section are ignored. Idempotent on bodies missing the section
    (returns empty list).
    """
    heading_match = _CONTENTS_HEADING_RE.search(body)
    if heading_match is None:
        return []
    section_start = heading_match.end()
    next_heading = _NEXT_HEADING_RE.search(body, pos=section_start)
    section_end = next_heading.start() if next_heading else len(body)
    section_body = body[section_start:section_end]
    targets: list[str] = []
    for m in _WIKILINK_BULLET_RE.finditer(section_body):
        target = m.group(1).strip()
        if target.endswith(".md"):
            target = target[:-3]
        if target:
            targets.append(target)
    return targets


# ---------------------------------------------------------------------------
# Suggestion proposal — pure logic, no I/O.
# ---------------------------------------------------------------------------


def propose_moc_suggestions(
    *,
    cluster_id: int,
    member_paths: list[str],
    cluster_tags: list[str],
    records: dict,
    existing_mocs: dict[str, ExistingMoc],
    member_overlap_threshold: float,
    fuzzy_label_jaccard_threshold: float,
    min_cluster_size: int,
    now: datetime | None = None,
) -> list[MocSuggestion]:
    """Compute suggestion candidates for one labeled cluster.

    Returns an empty list when the cluster is too small, when no
    candidate MOCs are eligible, or when every candidate would
    propose-add nothing (every cluster member already cites the
    target MOC).

    Per the ratified design, this is the single source of truth for
    "where could this cluster go." The daemon-side wrapper consults
    per-sweep + per-target caps from config; this function returns
    EVERY eligible candidate.

    ``records`` is the surveyor's in-memory record map (path →
    VaultRecord), used for ``mocs:`` frontmatter lookup on each
    cluster member. Members without records or with malformed
    ``mocs:`` are treated as "no current MOC membership" — the
    overlap calculation degrades gracefully (the member counts
    against the denominator but not the numerator).

    Inventory MOCs are filtered out of ``existing_mocs`` by
    :func:`build_existing_mocs_index`, but the propose-new name
    derivation here applies the underscore-strip defense again as
    belt-and-suspenders (a future contributor wiring this function
    directly with a hand-built index could otherwise skip the
    filter).

    ``now`` is injectable for deterministic testing; defaults to
    UTC now when None.

    **Trigger-type filter (2026-05-19 fix for live-queue bug class
    ``ms-20260519-d50d35e2``).** Surveyor clusters every embedded record
    regardless of type, but Phase 4 Sub-arc A's MOC-member append hook
    only fires for the four trigger types
    (``zettel`` / ``source`` / ``question`` / ``research-pointer``,
    canonical in ``vault/zettel_hooks._MOC_TRIGGER_TYPES``). A cluster
    of session/ records would silently orphan its members on accept:
    the bot's apply path writes ``mocs:`` frontmatter, but
    ``dispatch_moc_appends`` short-circuits on non-trigger types and
    the MOC's ``# Contents`` never gains the bullets. The filter
    excludes non-trigger members from ``cluster_member_paths`` AND
    ``candidate_members_to_add`` so proposals only include records the
    accept path will actually mirror to the MOC body. Cross-tool
    import (surveyor → vault) is lazy-imported below to avoid
    module-load cycles; the runtime cost is negligible (one set lookup
    per member, cached at function-entry).
    """
    # Trigger-type filter — applied BEFORE size check + sort + scoring
    # so every downstream calculation operates on the eligible-member
    # subset. Lazy import inside the function to avoid surveyor↔vault
    # module-load cycle risk; import resolves once-per-call and is
    # cheap (Python interns the module after the first import).
    from alfred.vault.zettel_hooks import _MOC_TRIGGER_TYPES
    eligible_member_paths = [
        p for p in member_paths
        if _is_trigger_eligible(records.get(p), _MOC_TRIGGER_TYPES)
    ]

    if len(eligible_member_paths) < min_cluster_size:
        return []

    sorted_members = sorted(eligible_member_paths)
    member_mocs: dict[str, set[str]] = {
        p: _extract_member_mocs(records.get(p))
        for p in sorted_members
    }

    # --------- Signal 1: member_overlap ---------
    overlap_candidates = _score_member_overlap(
        sorted_members, member_mocs, existing_mocs,
    )
    eligible_overlap = [
        (moc, score, members_citing)
        for moc, score, members_citing in overlap_candidates
        if score >= member_overlap_threshold
    ]

    suggestions: list[MocSuggestion] = []
    if eligible_overlap:
        for moc, score, members_citing in eligible_overlap:
            candidates_to_add = [
                p for p in sorted_members
                if moc.rel_path not in member_mocs[p]
            ]
            if not candidates_to_add:
                # Every member already cites this MOC — nothing to
                # add. Skip; don't queue a no-op suggestion.
                continue
            suggestions.append(_build_suggestion(
                cluster_id=cluster_id,
                cluster_tags=list(cluster_tags),
                sorted_members=sorted_members,
                target=moc,
                mapping_signal="member_overlap",
                mapping_score=score,
                candidates_to_add=candidates_to_add,
                reasoning=(
                    f"{len(members_citing)}/{len(sorted_members)} cluster "
                    f"members already cite {moc.rel_path}; "
                    f"{len(candidates_to_add)} candidate(s) to add"
                ),
                now=now,
            ))
        if suggestions:
            return suggestions

    # --------- Signal 2: fuzzy_label ---------
    fuzzy_candidates = _score_fuzzy_label(
        cluster_tags, existing_mocs,
    )
    eligible_fuzzy = [
        (moc, score) for moc, score in fuzzy_candidates
        if score >= fuzzy_label_jaccard_threshold
    ]
    if eligible_fuzzy:
        for moc, score in eligible_fuzzy:
            candidates_to_add = [
                p for p in sorted_members
                if moc.rel_path not in member_mocs[p]
            ]
            if not candidates_to_add:
                continue
            suggestions.append(_build_suggestion(
                cluster_id=cluster_id,
                cluster_tags=list(cluster_tags),
                sorted_members=sorted_members,
                target=moc,
                mapping_signal="fuzzy_label",
                mapping_score=score,
                candidates_to_add=candidates_to_add,
                reasoning=(
                    f"cluster tags {cluster_tags!r} share Jaccard "
                    f"{score:.2f} with MOC stem tokens "
                    f"({moc.stem!r}); proposing all "
                    f"{len(candidates_to_add)} member(s)"
                ),
                now=now,
            ))
        if suggestions:
            return suggestions

    # --------- Signal 3: propose_new ---------
    proposed_name = _derive_proposed_new_moc_name(cluster_tags)
    if proposed_name is None:
        # No usable tag → no propose-new candidate. Surfaced via the
        # daemon's no_candidates log (see daemon.py wrapper).
        return []
    proposed_rel = f"MOC/{proposed_name}.md"
    suggestions.append(_build_suggestion(
        cluster_id=cluster_id,
        cluster_tags=list(cluster_tags),
        sorted_members=sorted_members,
        target=None,
        proposed_new_moc_rel_path=proposed_rel,
        proposed_new_moc_name=proposed_name,
        mapping_signal="propose_new",
        mapping_score=0.0,
        candidates_to_add=list(sorted_members),
        reasoning=(
            f"no existing MOC matched cluster tags {cluster_tags!r}; "
            f"proposing new MOC {proposed_rel!r} with all "
            f"{len(sorted_members)} member(s)"
        ),
        now=now,
    ))
    return suggestions


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


def _is_trigger_eligible(record, trigger_types: frozenset[str]) -> bool:
    """``True`` iff the member record's type is in the MOC-trigger set.

    Records without a parseable type (parser failure, missing ``type``
    frontmatter) are excluded — propose-time can't know whether the
    accept path would mirror them to MOC ``# Contents``, and a
    conservative exclude is correct (no silent orphans). Same defense
    shape as the rest of the suggester's None-record handling
    (degrade-to-exclude rather than crash).

    ``trigger_types`` is passed explicitly so the call site can use
    the lazy-imported ``_MOC_TRIGGER_TYPES`` without this helper needing
    its own import of ``vault/zettel_hooks``. Decouples the helper
    from the import-order concern.
    """
    if record is None:
        return False
    record_type = getattr(record, "record_type", None)
    if not record_type:
        return False
    return record_type in trigger_types


def _extract_member_mocs(record) -> set[str]:
    """Pull a member record's ``mocs:`` frontmatter list and return
    the set of normalized rel_paths.

    Tolerates the same operator-typo shapes accepted by Phase 4
    Sub-arc A: bare strings, single string, list-of-strings, wikilinks,
    pipe-aliased wikilinks. Returns empty set when ``mocs:`` is missing
    or malformed.

    ``record`` is a ``VaultRecord`` from ``surveyor.parser`` (or
    None if the path didn't parse). ``frontmatter`` is a dict on the
    record; we read ``mocs`` by key.
    """
    if record is None:
        return set()
    fm = getattr(record, "frontmatter", None) or {}
    raw = fm.get("mocs")
    if raw is None:
        return set()
    if isinstance(raw, str):
        items: Iterable = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        return set()
    out: set[str] = set()
    for entry in items:
        if not entry:
            continue
        text = str(entry).strip()
        if not text:
            continue
        if text.startswith("[[") and text.endswith("]]"):
            text = text[2:-2]
        if "|" in text:
            text = text.split("|", 1)[0]
        text = text.strip()
        if not text:
            continue
        # Normalize to ``MOC/<Stem>.md`` shape for comparison against
        # ExistingMoc.rel_path.
        if not text.lower().endswith(".md"):
            text = text + ".md"
        if not text.startswith("MOC/"):
            # Operator may write ``[[Stoicism MOC]]`` without the
            # ``MOC/`` directory prefix. Coerce to canonical shape.
            stem = text[:-3] if text.lower().endswith(".md") else text
            text = f"MOC/{stem}.md"
        out.add(text)
    return out


def _score_member_overlap(
    sorted_members: list[str],
    member_mocs: dict[str, set[str]],
    existing_mocs: dict[str, ExistingMoc],
) -> list[tuple[ExistingMoc, float, list[str]]]:
    """For each existing (non-inventory) MOC, compute the fraction of
    cluster members whose ``mocs:`` already cites it.

    Returns a list of (MOC, score, members_citing) sorted by score
    descending. Score is ``len(members_citing) / len(sorted_members)``.
    Caller applies the threshold gate.
    """
    n = len(sorted_members)
    if n == 0:
        return []
    out: list[tuple[ExistingMoc, float, list[str]]] = []
    for moc in existing_mocs.values():
        # Defensive: even though the index builder filters inventory
        # MOCs out, re-check here in case a future caller passes a
        # hand-built index.
        if moc.is_inventory_moc:
            continue
        citing = [p for p in sorted_members if moc.rel_path in member_mocs[p]]
        if not citing:
            continue
        score = len(citing) / n
        out.append((moc, score, citing))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def _score_fuzzy_label(
    cluster_tags: list[str],
    existing_mocs: dict[str, ExistingMoc],
) -> list[tuple[ExistingMoc, float]]:
    """Jaccard token-overlap between cluster_tags and MOC filename
    stem tokens. Returns (MOC, score) descending."""
    cluster_tokens = _tokenize(cluster_tags)
    if not cluster_tokens:
        return []
    out: list[tuple[ExistingMoc, float]] = []
    for moc in existing_mocs.values():
        if moc.is_inventory_moc:
            continue
        moc_tokens = _tokenize([moc.stem])
        if not moc_tokens:
            continue
        intersection = cluster_tokens & moc_tokens
        union = cluster_tokens | moc_tokens
        if not union:
            continue
        score = len(intersection) / len(union)
        if score > 0.0:
            out.append((moc, score))
    out.sort(key=lambda t: t[1], reverse=True)
    return out


def _tokenize(values: Iterable[str]) -> frozenset[str]:
    """Lowercase + split + stopword-filter."""
    tokens: set[str] = set()
    for v in values:
        if not v:
            continue
        for piece in _TOKEN_SPLIT_RE.split(str(v).lower()):
            piece = piece.strip()
            if piece and piece not in _TOKEN_STOPWORDS:
                tokens.add(piece)
    return frozenset(tokens)


def _derive_proposed_new_moc_name(cluster_tags: list[str]) -> str | None:
    """Build a candidate new-MOC filename stem from cluster tags.

    Picks the first non-empty, non-stopword tag, title-cases it, and
    appends ``" MOC"`` per the ``Topic MOC.md`` convention (locked
    plan). Strips leading underscore so the inventory-MOC namespace
    can't be polluted via this path. Returns None when no usable tag
    exists.
    """
    for tag in cluster_tags:
        if not tag:
            continue
        cleaned = str(tag).strip()
        # Strip leading underscores so inventory-MOC namespace can't
        # be polluted via this path.
        cleaned = cleaned.lstrip("_")
        if not cleaned:
            continue
        # Replace separators with spaces for title-casing.
        cleaned = re.sub(r"[\-_/]+", " ", cleaned).strip()
        if not cleaned:
            continue
        title = " ".join(word.capitalize() for word in cleaned.split())
        if not title:
            continue
        return f"{title} MOC"
    return None


def _build_suggestion(
    *,
    cluster_id: int,
    cluster_tags: list[str],
    sorted_members: list[str],
    target: ExistingMoc | None,
    mapping_signal: str,
    mapping_score: float,
    candidates_to_add: list[str],
    reasoning: str,
    now: datetime | None,
    proposed_new_moc_rel_path: str | None = None,
    proposed_new_moc_name: str | None = None,
) -> MocSuggestion:
    """Assemble a MocSuggestion with the canonical ID derivation.

    ID = ``ms-YYYYMMDD-<sha256(sorted_members + target)[:8]>``. Stable
    across sweeps with stable membership + target; HDBSCAN cluster-id
    renumber does NOT affect the ID (cluster_id is captured for
    forensics but not in the hash). Same lesson as
    ``daemon.py:membership_unchanged_skip``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    target_rel = target.rel_path if target else (proposed_new_moc_rel_path or "")
    hash_input = "|".join(sorted_members) + "||" + target_rel
    hash_hex = hashlib.sha256(hash_input.encode("utf-8")).hexdigest()[:8]
    date_part = now.strftime("%Y%m%d")
    suggestion_id = f"ms-{date_part}-{hash_hex}"
    return MocSuggestion(
        id=suggestion_id,
        cluster_id_at_proposal=cluster_id,
        cluster_tags=list(cluster_tags),
        cluster_member_paths=list(sorted_members),
        target_moc_rel_path=(target.rel_path if target else None),
        proposed_new_moc_name=proposed_new_moc_name,
        mapping_signal=mapping_signal,
        mapping_score=float(mapping_score),
        candidate_members_to_add=list(candidates_to_add),
        reasoning=reasoning,
        created=now.isoformat(),
        status="pending",
    )


__all__ = [
    "MocSuggestion",
    "ExistingMoc",
    "INVENTORY_MOC_STEM_PREFIX",
    "build_existing_mocs_index",
    "propose_moc_suggestions",
]
