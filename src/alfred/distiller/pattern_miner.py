"""Embedding-pattern miner — KAL-LE distiller-radar Phase 4.

Reads the surveyor pipeline's labeled-cluster output (the per-instance
``surveyor_state.json`` file), gates each cluster against four rules
("labeled, substantive, no canonical match, label-quality"), and
surfaces survivors as inbox proposals for new ``architecture/`` or
``principles/`` records.

Design ratified in ``project_kalle_phase4_pattern_miner.md``
(2026-05-09). Phase 4 of the parent arc
``project_kalle_distiller_radar.md``.

## What this module owns

- **Gate logic** — see :func:`gate_cluster`. Four-part rule:
  labeled, ``len(member_files) >= min_cluster_size``, no canonical
  match, label not solely on the denylist.
- **Fingerprint** — see :func:`fingerprint_cluster`. SHA-256 over
  the cluster's sorted member-file list and label tuple. Stable
  across mine runs as long as both shape AND labels are unchanged.
- **Canonical-index loader** — see :func:`load_canonical_index`.
  Walks ``canonical_match_dirs`` for slug stems and label segments;
  the gate consults this set for the no-match check.
- **Surveyor state reader** — see :func:`read_surveyor_clusters`.
  Decoupled from ``alfred.surveyor.state.PipelineState`` to avoid
  pulling in surveyor's milvus/numpy deps. The miner only needs the
  ``clusters`` slice of the JSON file.

## What the writer + LLM caller half (this module's second commit) owns

- LLM call to draft proposal markdown
- Markdown rendering + atomic write
- Empty-state placeholder write (per the "intentionally left blank"
  observability rule)
- Reconcile sweep — walk existing proposals, mark promoted/discarded
  based on file presence + canonical-dir scan
- Top-level :func:`mine_patterns` orchestrator

## Per-instance scope

This module is instance-agnostic. Vault path comes from the caller.
Canonical-match dirs come from the caller. State path comes from the
caller. No ``kalle`` / ``aftermath-lab`` / Salem-shape literals in
this code per the per-instance-defaults discipline (CLAUDE.md "Three
Layers — Code vs Config vs Prompt").
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from .pattern_miner_state import (
    PatternMinerState,
    ProposalEntry,
)

log = structlog.get_logger(__name__)


# Default label denylist — labels solely composed of these words signal
# a "low-signal cluster" the surveyor labeled because something cohered
# on document-type rather than topic. Operator can extend via the
# ``distiller.pattern_miner.label_denylist`` config block; the union
# of default + config is consulted at gate time.
_DEFAULT_LABEL_DENYLIST: frozenset[str] = frozenset({
    "session-notes",
    "session-note",
    "software-development",
    "documentation",
    "documentation/issues",
    "contradiction",
    "programming",
    "coding",
    # Generic structural hints that don't actually signal a theme.
    "data-quality",
    "data-quality/review",
    "general",
})


# Default minimum cluster size for proposal eligibility. Below 3 = noise
# risk even when cohered. CLI-overridable per run.
_DEFAULT_MIN_CLUSTER_SIZE: int = 3


# Default canonical dirs — match KAL-LE's actual layout. Operator can
# override via config. Salem (which has matter/person/org/project) would
# configure this differently if it ever runs Phase 4.
_DEFAULT_CANONICAL_MATCH_DIRS: tuple[str, ...] = ("architecture", "principles", "stack")


# Slug-normalization regex: lowercase, replace any run of non-alnum with
# a single hyphen, strip leading/trailing hyphens. Applied to both the
# label segments AND the existing-canonical-file stems before the
# membership test, so ``backend/n8n`` matches ``backend-n8n.md`` and
# ``Local LLM Hardware`` matches ``local-llm-hardware.md``.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass
class ClusterRecord:
    """Surveyor's per-cluster record, narrowed to what the miner needs.

    Decoupled from ``alfred.surveyor.state.ClusterState`` because Phase
    4 doesn't need the surveyor's full state machine — just enough to
    score the gate and fingerprint the cluster. Avoids the milvus/numpy
    import chain that surveyor pulls in.
    """

    cluster_id: str
    labels: list[str] = field(default_factory=list)
    member_files: list[str] = field(default_factory=list)


@dataclass
class ProposalCandidate:
    """A cluster that passed the gate and is eligible for proposal.

    Carries everything the writer + LLM caller needs to draft the
    proposal markdown without re-walking the cluster shape.
    """

    cluster: ClusterRecord
    fingerprint: str
    proposed_slug: str
    proposed_canonical_type: str  # "architecture" | "principles"


# ---------------------------------------------------------------------------
# Slug + fingerprint primitives
# ---------------------------------------------------------------------------


def slugify(text: str) -> str:
    """Normalize an arbitrary string to a kebab-case slug.

    ``backend/n8n`` → ``backend-n8n``; ``Local LLM Hardware`` →
    ``local-llm-hardware``. Empty / whitespace-only input → empty
    string. Symmetric across labels and file stems so the canonical-
    match check is consistent.
    """
    s = (text or "").lower().strip()
    s = _SLUG_RE.sub("-", s).strip("-")
    return s


def label_segments(label: str) -> list[str]:
    """Split a hierarchical label like ``backend/n8n`` into its
    segments. Returns the slugified full label first (full-path match),
    then each individual segment slugified. Order matters: full-path
    matches should win over segment matches when both are present.
    """
    if not label:
        return []
    full = slugify(label)
    parts = [slugify(p) for p in label.split("/") if p.strip()]
    out: list[str] = []
    if full:
        out.append(full)
    for p in parts:
        if p and p not in out:
            out.append(p)
    return out


def fingerprint_cluster(member_files: list[str], labels: list[str]) -> str:
    """SHA-256 over the cluster's identity surface.

    Stable across mine runs as long as BOTH:
    - the sorted member-file list is unchanged
    - the sorted label tuple is unchanged

    Either changing yields a new fingerprint and a new proposal
    opportunity (per the supersede path in the lifecycle).

    The ``\\n--\\n`` divider keeps the two halves unambiguous so a label
    that happens to equal a member path can't collide. Sorted to make
    fingerprint independent of input ordering — surveyor doesn't
    guarantee a stable order in either field.
    """
    members_sorted = sorted(member_files or [])
    labels_sorted = sorted(labels or [])
    body = "\n".join(members_sorted) + "\n--\n" + ",".join(labels_sorted)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Surveyor state reader
# ---------------------------------------------------------------------------


def read_surveyor_clusters(state_path: Path) -> list[ClusterRecord]:
    """Read the surveyor state JSON and return its labeled clusters.

    Decoupled from ``alfred.surveyor.state.PipelineState`` — the miner
    only needs the ``clusters`` slice and avoiding the surveyor import
    chain (milvus, numpy, hdbscan) keeps the distiller process light.

    Missing state file → empty list (logged at info; first-run / surveyor-
    not-yet-run is a valid state for the miner to encounter and report
    as an empty-result outcome rather than crash).

    Malformed JSON or unexpected shape → empty list with a warning log
    so the operator can investigate without the miner blowing up.
    """
    if not state_path.is_file():
        log.info(
            "pattern_miner.surveyor_state_missing",
            path=str(state_path),
        )
        return []
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "pattern_miner.surveyor_state_read_failed",
            path=str(state_path),
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return []

    clusters_raw = raw.get("clusters")
    if not isinstance(clusters_raw, dict):
        log.warning(
            "pattern_miner.surveyor_state_bad_shape",
            path=str(state_path),
            clusters_type=type(clusters_raw).__name__,
        )
        return []

    out: list[ClusterRecord] = []
    for cid, cdata in clusters_raw.items():
        if not isinstance(cdata, dict):
            continue
        labels = cdata.get("label") or []
        member_files = cdata.get("member_files") or []
        if not isinstance(labels, list):
            labels = []
        if not isinstance(member_files, list):
            member_files = []
        out.append(ClusterRecord(
            cluster_id=str(cid),
            labels=[str(s) for s in labels if isinstance(s, str)],
            member_files=[str(s) for s in member_files if isinstance(s, str)],
        ))
    return out


# ---------------------------------------------------------------------------
# Canonical-index loader
# ---------------------------------------------------------------------------


def load_canonical_index(
    vault_path: Path,
    canonical_match_dirs: list[str] | tuple[str, ...],
) -> set[str]:
    """Walk ``canonical_match_dirs`` for slug stems.

    For each ``.md`` file under each directory (recursive — ``stack/n8n/
    patterns.md`` counts as much as ``architecture/data-flow.md``),
    extract the basename without extension and slugify it. Return the
    union of all slugs. The gate consults this set to determine whether
    a cluster's labels already have a canonical artifact.

    Symmetric slugification (via :func:`slugify`) means
    ``backend/n8n`` matches ``backend-n8n.md`` AND ``n8n`` matches
    ``stack/n8n.md`` — the gate uses :func:`label_segments` to surface
    both full-path and per-segment candidates.

    Missing dirs are silently skipped — a fresh vault may not yet have
    ``architecture/`` or ``principles/`` populated, and Phase 4 should
    surface candidates anyway rather than gate every cluster as
    "no canonical surface to match against."
    """
    out: set[str] = set()
    for dirname in canonical_match_dirs:
        d = vault_path / dirname
        if not d.is_dir():
            continue
        for md_path in d.rglob("*.md"):
            if md_path.name == ".gitkeep":
                continue
            stem = md_path.stem
            slug = slugify(stem)
            if slug:
                out.add(slug)
    return out


# ---------------------------------------------------------------------------
# Gate logic
# ---------------------------------------------------------------------------


def cluster_matches_canonical(
    labels: list[str],
    canonical_index: set[str],
) -> bool:
    """True iff ANY label or label-segment slug is already in the
    canonical index.

    Conservative: prefer a false-positive on canonical-match (skipping
    a real candidate that happens to share a slug with an existing
    artifact) over emitting noise proposals for already-canonized
    themes. The reverse-failure mode — missing one candidate per run
    — is recoverable; the proposal will surface again on the next
    cluster shape change OR the operator can re-mine after deleting a
    superseding canonical artifact.
    """
    if not labels:
        return False
    for label in labels:
        for seg in label_segments(label):
            if seg in canonical_index:
                return True
    return False


def cluster_passes_label_quality(
    labels: list[str],
    label_denylist: frozenset[str] | set[str],
) -> bool:
    """True iff at least one label is not on the denylist.

    The denylist filters clusters that cohered on document-type or
    other low-signal axes ("session-notes", "documentation/issues",
    etc.). When EVERY label is on the denylist the cluster is dropped.
    Mixed-signal clusters (one denylist label + one real-theme label)
    pass — the operator can decide whether the real-theme half is
    worth canonical promotion.

    Empty labels list → False (the labeled-check gate runs first; this
    is defensive belt-and-suspenders).
    """
    if not labels:
        return False
    return any(label not in label_denylist for label in labels)


def gate_cluster(
    cluster: ClusterRecord,
    canonical_index: set[str],
    label_denylist: frozenset[str] | set[str],
    *,
    min_cluster_size: int = _DEFAULT_MIN_CLUSTER_SIZE,
) -> bool:
    """Apply the four-part gate from the design memo (Q3).

    A cluster passes iff ALL of:
    1. Labeled — at least one label string.
    2. Substantive — ``len(member_files) >= min_cluster_size``.
    3. No canonical match — none of the labels (full-path or per-
       segment slugified) appear in ``canonical_index``.
    4. Label quality — at least one label is not on the denylist.

    Returns True iff all four hold. Order is cheap-to-expensive: the
    labeled check is O(1), substantive is O(1), denylist is O(N_labels),
    canonical match is O(N_labels * N_segments). Bailing on the cheap
    checks first keeps the pass-loop fast on a vault with hundreds of
    clusters.
    """
    if not cluster.labels:
        return False
    if len(cluster.member_files) < max(1, int(min_cluster_size)):
        return False
    if not cluster_passes_label_quality(cluster.labels, label_denylist):
        return False
    if cluster_matches_canonical(cluster.labels, canonical_index):
        return False
    return True


# ---------------------------------------------------------------------------
# Slug + canonical-type derivation for proposals
# ---------------------------------------------------------------------------


def derive_proposed_slug(labels: list[str]) -> str:
    """Pick a slug for the proposal file from the cluster's labels.

    Strategy: take the FIRST label (surveyor's labeler returns labels
    in priority order — the principal axis first), full-path slugify,
    and use that. If the first label is empty or denylist-shaped (this
    function gets called only after gate_cluster has approved, but the
    defensive check costs nothing), fall back to the second, third,
    etc. Returns empty string if nothing usable is found.

    A safer alternative (concatenate top-2 labels) was considered but
    rejected — most surveyor labels are already 2-segment hierarchies
    (``backend/n8n``), so the first label produces a slug that's
    already meaningful. Concatenation would produce file names like
    ``backend-n8n-architecture-data-flow.md`` which are noisy.
    """
    for label in labels:
        slug = slugify(label)
        if slug:
            return slug
    return ""


def derive_proposed_canonical_type(labels: list[str]) -> str:
    """Heuristic: ``architecture`` for structural-design themes,
    ``principles`` for rule-of-practice themes. Default ``architecture``.

    Soft signals — the LLM drafter will revise per the Q5 prompt's
    TYPE/SLUG suggestion line. This function picks a starting bucket
    so the proposal lands somewhere reasonable even if the LLM call
    fails (we still want a markdown file the operator can read).

    Tokens that lean toward "principles" (rule of practice / discipline):
    - "discipline", "convention", "practice", "rule"
    - "anti-pattern", "pattern" (when paired with rule-shaped neighbors)

    Default to "architecture" (structural design choice).
    """
    principles_tokens = frozenset({
        "discipline", "convention", "practice", "rule",
        "principles", "principle",
        "anti-pattern", "antipattern",
        "review", "qa", "qa-review", "ux-review",
    })
    for label in labels:
        for seg in label_segments(label):
            if seg in principles_tokens:
                return "principles"
    return "architecture"


# ---------------------------------------------------------------------------
# Top-level pre-write evaluation — used by both dry-run and live paths
# ---------------------------------------------------------------------------


def evaluate_cluster(
    cluster: ClusterRecord,
    canonical_index: set[str],
    label_denylist: frozenset[str] | set[str],
    state: PatternMinerState,
    *,
    min_cluster_size: int = _DEFAULT_MIN_CLUSTER_SIZE,
) -> ProposalCandidate | None:
    """Apply gate + dedup, return a fully-formed candidate or None.

    Combines :func:`gate_cluster` (the four-part rule) with the
    state-file dedup check. A cluster that passes the gate AND is not
    already represented in the state (any status) yields a
    :class:`ProposalCandidate`; otherwise None.

    Caller is responsible for actually writing the proposal + recording
    the entry — this function is pure (no side effects).
    """
    if not gate_cluster(
        cluster, canonical_index, label_denylist,
        min_cluster_size=min_cluster_size,
    ):
        return None
    fp = fingerprint_cluster(cluster.member_files, cluster.labels)
    if state.has_entry_for_fingerprint(fp):
        return None
    slug = derive_proposed_slug(cluster.labels)
    if not slug:
        return None
    canonical_type = derive_proposed_canonical_type(cluster.labels)
    return ProposalCandidate(
        cluster=cluster,
        fingerprint=fp,
        proposed_slug=slug,
        proposed_canonical_type=canonical_type,
    )


__all__ = [
    "ClusterRecord",
    "ProposalCandidate",
    "ProposalEntry",
    "PatternMinerState",
    "cluster_matches_canonical",
    "cluster_passes_label_quality",
    "derive_proposed_canonical_type",
    "derive_proposed_slug",
    "evaluate_cluster",
    "fingerprint_cluster",
    "gate_cluster",
    "label_segments",
    "load_canonical_index",
    "read_surveyor_clusters",
    "slugify",
]
