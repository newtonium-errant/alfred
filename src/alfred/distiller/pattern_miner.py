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
from datetime import datetime, timezone
from pathlib import Path

import structlog

from .pattern_miner_state import (
    RECONCILABLE_STATUSES,
    STATUS_DISCARDED,
    STATUS_PENDING,
    STATUS_PROMOTED,
    STATUS_SPLIT_PENDING,
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


# ---------------------------------------------------------------------------
# Drafting prompt — externalized to a bundled .md file (2026-05-10)
# ---------------------------------------------------------------------------
#
# Originally an inline constant per the design memo's c1 discipline.
# Externalized after the Phase 4 first-live-run review found severe W1
# voice mismatch (9/10 proposals) — the prompt-tuner is hook-blocked
# from editing inline Python prompt constants (precedent: commit
# ``9904730`` 2026-05-09, voice-prompts externalization). Stage 1 ships
# the mechanical refactor only; content changes (refusal sentinel,
# split sentinel, voice alignment) land in stage 2 against the now-
# externalized .md file.
#
# Loader semantics mirror ``alfred.distiller.pipeline._load_stage_prompt``
# exactly: ``importlib.resources``-located parent dir, fresh read per
# call (so prompt-tuner edits land without daemon restart), warning +
# empty string on missing file (the empty string falls through to
# ``str.format`` which propagates the missing-prompt as a render-time
# diagnostic rather than a silent KeyError mid-call).
#
# Placeholders kept byte-identical with the prior inline constant:
# ``{labels}`` and ``{count}`` substituted at call time;
# ``{members_with_previews}`` carries the cluster's member titles +
# body previews assembled from vault paths.


def _load_draft_prompt_template() -> str:
    """Load the Phase 4 drafter prompt from the bundled skills dir.

    Path: ``src/alfred/_bundled/skills/vault-distiller/prompts/
    draft_canonical_proposal.md``. Mirrors the canonical distiller
    precedent (``alfred.distiller.pipeline._load_stage_prompt``) —
    same parent dir, same fresh-per-call semantics, same warning-
    plus-empty-string fallback on missing file.

    Fresh read per call (no module-level cache, no ``functools.cache``)
    so prompt-tuner edits to the .md file take effect on the NEXT
    miner invocation without needing a daemon restart. The cost is
    one filesystem stat + read per mining run, which is negligible
    next to the per-cluster LLM call.
    """
    from alfred._data import get_skills_dir

    prompt_path = (
        get_skills_dir() / "vault-distiller" / "prompts"
        / "draft_canonical_proposal.md"
    )
    if not prompt_path.exists():
        log.warning(
            "pattern_miner.prompt_not_found",
            path=str(prompt_path),
        )
        return ""
    return prompt_path.read_text(encoding="utf-8")


# Per-call HTTP timeout. Phase 4's drafter is a single per-cluster call
# against a local Ollama at qwen2.5:14b — typically <10s on hardware
# the surveyor labeler already runs on. 120s is generous enough to
# cover cold-start model load (Ollama unloads idle models) without
# masking a stuck endpoint indefinitely.
_DRAFTER_HTTP_TIMEOUT_SECONDS: float = 120.0

# Per-member body preview length in the drafter prompt. Larger gives the
# LLM more grounding text but multiplies prompt token cost and risks
# pushing past the local model's context window. 200 chars matches
# surveyor's labeler default (``LabelerConfig.body_preview_chars``).
_DRAFTER_BODY_PREVIEW_CHARS: int = 200

# Cap on members included in the drafter prompt. Beyond this the
# prompt risks exceeding qwen2.5:14b's effective context. Surveyor's
# labeler caps at 20 (``LabelerConfig.max_files_per_cluster_context``);
# we use the same default so behavior is consistent across the two
# LLM-touch points the same backend serves.
_DRAFTER_MAX_MEMBERS: int = 20


# Re-parse helpers for the drafter response. The TYPE / SLUG line is
# optional (operator can revise post-write); we extract them if present
# so the proposal frontmatter starts in the right canonical bucket.
_TYPE_LINE_RE = re.compile(r"^\s*TYPE\s*:\s*(architecture|principles)\s*$", re.IGNORECASE | re.MULTILINE)
_SLUG_LINE_RE = re.compile(r"^\s*SLUG\s*:\s*([a-z0-9][a-z0-9-]*)\s*$", re.IGNORECASE | re.MULTILINE)


# Three-outcome contract from the stage 2a drafter prompt
# (``draft_canonical_proposal.md``). The LLM picks ONE per cluster and
# emits the matching format. Sentinel string-constants for grep-ability;
# mirror the status-constant pattern in ``pattern_miner_state.py``.
OUTCOME_PROPOSAL: str = "proposal"  # happy path: claim + TYPE/SLUG trailer
OUTCOME_NO_CLAIM: str = "no_claim"  # refusal: cluster has no shared theme
OUTCOME_SPLIT: str = "split"        # split: cluster has 2+ distinct sub-themes

_VALID_OUTCOMES: frozenset[str] = frozenset({
    OUTCOME_PROPOSAL, OUTCOME_NO_CLAIM, OUTCOME_SPLIT,
})

# Sentinel detection — the LLM is instructed to emit ``NO-CLAIM`` or
# ``SPLIT`` on its own line as the FIRST line of the response. Match
# at start of stripped content rather than scanning the whole body so
# a happy-path paragraph that happens to mention "NO-CLAIM" inline
# (e.g. quoting the prompt) doesn't get misclassified as a refusal.
_NO_CLAIM_RE = re.compile(r"^\s*NO-CLAIM\s*$", re.MULTILINE)
_SPLIT_RE = re.compile(r"^\s*SPLIT\s*$", re.MULTILINE)
_REASON_RE = re.compile(r"^\s*REASON\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
_THEMES_HEADER_RE = re.compile(r"^\s*THEMES\s*:\s*$", re.MULTILINE | re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$", re.MULTILINE)


@dataclass
class DraftResult:
    """Outcome of a drafter LLM call. Three outcomes per the stage 2a
    prompt; ``outcome`` discriminates which fields are populated.

    ``outcome == OUTCOME_PROPOSAL`` (default — happy path):
        ``paragraph`` carries the claim. ``llm_type_suggestion`` and
        ``llm_slug_suggestion`` come from the optional TYPE/SLUG
        trailer; either may be empty if the LLM omitted them or the
        parser couldn't pick them up — the writer falls back to the
        heuristic-derived values via :func:`derive_proposed_slug` /
        :func:`derive_proposed_canonical_type`.

    ``outcome == OUTCOME_NO_CLAIM``:
        LLM refused — cluster has no shared theme. ``reason`` carries
        the one-line explanation from the ``REASON:`` line (may be
        empty if the LLM emitted ``NO-CLAIM`` without a REASON line;
        the token alone is the load-bearing signal). Other fields
        unused. Orchestrator skips the cluster — no file written, no
        state entry recorded.

    ``outcome == OUTCOME_SPLIT``:
        LLM identified 2+ distinct sub-themes. ``themes`` carries
        the bulleted list under ``THEMES:``. Other fields unused.
        Orchestrator writes a split-marker file at
        ``<proposed_dir>/<slug>-needs-split.md`` and records the
        entry with status ``split_pending``.

    ``error`` is non-empty when the HTTP / LLM call itself failed
    (network, non-2xx, malformed JSON, empty response). On error,
    ``outcome`` stays at the default ``OUTCOME_PROPOSAL`` so the
    orchestrator's existing placeholder-paragraph fallback runs;
    a degraded proposal is preferable to a silent skip.
    """

    paragraph: str = ""
    llm_type_suggestion: str = ""
    llm_slug_suggestion: str = ""
    error: str = ""
    outcome: str = OUTCOME_PROPOSAL
    reason: str = ""
    themes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Drafter — Ollama via httpx (sync, OpenAI-compatible chat-completions)
# ---------------------------------------------------------------------------


def _build_members_block(
    candidate: ProposalCandidate,
    vault_path: Path,
    *,
    body_preview_chars: int = _DRAFTER_BODY_PREVIEW_CHARS,
    max_members: int = _DRAFTER_MAX_MEMBERS,
) -> str:
    """Assemble the ``{members_with_previews}`` block for the drafter
    prompt. Reads each member file's frontmatter title + body preview;
    falls back to the basename if the file is unreadable.

    Imports ``frontmatter`` lazily so a slim install (no python-
    frontmatter) only fails if Phase 4 is actually invoked.
    """
    try:
        import frontmatter  # type: ignore
    except ImportError:
        log.warning(
            "pattern_miner.frontmatter_unavailable",
            note="install python-frontmatter for member previews",
        )
        # Degrade gracefully: list bare paths so the LLM at least sees
        # the member set even without title/body previews.
        return "\n".join(
            f"- {rel}" for rel in candidate.cluster.member_files[:max_members]
        )

    lines: list[str] = []
    for rel in candidate.cluster.member_files[:max_members]:
        abs_path = vault_path / rel
        if not abs_path.is_file():
            lines.append(f"- {rel} (file missing)")
            continue
        try:
            with abs_path.open("r", encoding="utf-8") as fh:
                post = frontmatter.load(fh)
        except (OSError, UnicodeDecodeError) as exc:
            lines.append(f"- {rel} (read error: {exc.__class__.__name__})")
            continue
        except Exception as exc:  # noqa: BLE001 — frontmatter raises broad
            lines.append(f"- {rel} (parse error: {exc.__class__.__name__})")
            continue
        fm = dict(post.metadata or {})
        title = fm.get("name") or fm.get("title") or Path(rel).stem
        body = (post.content or "")[:body_preview_chars].replace("\n", " ").strip()
        lines.append(f"- [{title}] {body}")
    return "\n".join(lines)


def call_drafter(
    candidate: ProposalCandidate,
    vault_path: Path,
    *,
    endpoint: str,
    model: str,
    api_key: str = "",
    timeout: float = _DRAFTER_HTTP_TIMEOUT_SECONDS,
    body_preview_chars: int = _DRAFTER_BODY_PREVIEW_CHARS,
    max_members: int = _DRAFTER_MAX_MEMBERS,
) -> DraftResult:
    """Call the OpenAI-compatible chat-completions endpoint to draft a
    one-paragraph claim plus an optional TYPE/SLUG trailer.

    Endpoint shape: ``POST {endpoint}/chat/completions`` (so an
    ``endpoint`` of ``http://172.22.0.1:11434/v1`` maps to
    ``/v1/chat/completions``, matching surveyor's labeler config). The
    ``api_key`` is sent as ``Authorization: Bearer <key>`` when present;
    Ollama accepts any value (or no header) so the default empty string
    works against local Ollama.

    Failure modes (all return a :class:`DraftResult` with empty
    ``paragraph`` and a populated ``error`` field):
    - Network failure (connection refused, DNS, timeout)
    - HTTP non-2xx status
    - Malformed JSON response
    - Empty content in the OpenAI shape

    Per the subprocess-failure logging discipline (CLAUDE.md / agent
    instructions): logs include both ``stderr``-equivalent details
    AND a ``stdout_tail`` of the raw response body so post-mortem
    analysis can distinguish "Ollama returned a 200 with weird body"
    from "Ollama refused the connection."
    """
    try:
        import httpx
    except ImportError as exc:
        log.warning(
            "pattern_miner.httpx_unavailable",
            error=str(exc),
        )
        return DraftResult(error=f"httpx unavailable: {exc}")

    members_block = _build_members_block(
        candidate, vault_path,
        body_preview_chars=body_preview_chars,
        max_members=max_members,
    )
    template = _load_draft_prompt_template()
    prompt = template.format(
        labels=", ".join(candidate.cluster.labels),
        count=len(candidate.cluster.member_files),
        members_with_previews=members_block,
    )

    url = f"{endpoint.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "temperature": 0.3,
    }

    raw_body: str = ""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json=payload, headers=headers)
        raw_body = response.text or ""
    except Exception as exc:  # noqa: BLE001 — broad to capture all httpx errors
        # Network-layer failure. Per the subprocess-failure-logging
        # discipline: emit both the error and a stdout_tail sentinel
        # (empty here because no response was received) so the empty-
        # diagnostic-output signature stays grep-able.
        summary = f"{exc.__class__.__name__}: {exc}"
        log.warning(
            "pattern_miner.llm_failed",
            cluster_id=candidate.cluster.cluster_id,
            endpoint=endpoint,
            model=model,
            error_type=exc.__class__.__name__,
            error=str(exc)[:500],
            stdout_tail="",  # explicit empty per the load-bearing sentinel
            summary=f"Network error: {summary[:200]}",
        )
        return DraftResult(error=summary)

    if response.status_code != 200:
        # Application-layer failure. ``raw_body`` is the response body;
        # log both stderr-equivalent (status+headers) AND stdout_tail
        # so the operator sees Ollama's own error message.
        body_tail = raw_body[-2000:] if raw_body else ""
        detail = (raw_body[:200] if raw_body else "(no body)")
        log.warning(
            "pattern_miner.llm_failed",
            cluster_id=candidate.cluster.cluster_id,
            endpoint=endpoint,
            model=model,
            status=response.status_code,
            stdout_tail=body_tail,
            summary=f"HTTP {response.status_code}: {detail}",
        )
        return DraftResult(error=f"HTTP {response.status_code}: {detail}")

    try:
        data = response.json()
    except json.JSONDecodeError as exc:
        body_tail = raw_body[-2000:] if raw_body else ""
        log.warning(
            "pattern_miner.llm_failed",
            cluster_id=candidate.cluster.cluster_id,
            endpoint=endpoint,
            model=model,
            error_type="JSONDecodeError",
            error=str(exc)[:500],
            stdout_tail=body_tail,
            summary=f"Bad JSON in response: {str(exc)[:200]}",
        )
        return DraftResult(error=f"JSON decode failed: {exc}")

    choices = data.get("choices") if isinstance(data, dict) else None
    if not isinstance(choices, list) or not choices:
        body_tail = raw_body[-2000:] if raw_body else ""
        log.warning(
            "pattern_miner.llm_failed",
            cluster_id=candidate.cluster.cluster_id,
            endpoint=endpoint,
            model=model,
            error_type="ShapeError",
            error="response.choices missing or empty",
            stdout_tail=body_tail,
            summary="OpenAI-shape response missing choices",
        )
        return DraftResult(error="response.choices missing or empty")

    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    if not content.strip():
        body_tail = raw_body[-2000:] if raw_body else ""
        # Empty-content is unusual but not a hard failure — still log
        # explicitly per the intentionally-left-blank discipline.
        log.info(
            "pattern_miner.llm_empty_response",
            cluster_id=candidate.cluster.cluster_id,
            stdout_tail=body_tail,
        )
        return DraftResult(error="LLM returned empty content")

    return _parse_drafter_response(content)


def _parse_drafter_response(content: str) -> DraftResult:
    """Parse the drafter's ``content`` into a populated DraftResult.

    Three-way branch on the stage 2a prompt's three outcomes. Order
    matters — sentinels are checked FIRST so a happy-path TYPE/SLUG
    trailer can never accidentally trigger NO-CLAIM/SPLIT detection
    AND vice versa.

    1. **NO-CLAIM**: literal token on its own line as the first non-
       whitespace content. Returns ``DraftResult(outcome=NO_CLAIM,
       reason=<text from REASON: line, or "" if absent>)``. A NO-CLAIM
       without a REASON line still counts — the token alone is the
       load-bearing skip signal; the reason is operator-helpful but
       not required.

    2. **SPLIT**: literal token on its own line as the first non-
       whitespace content. Returns ``DraftResult(outcome=SPLIT,
       themes=<bulleted list under THEMES:>)``. Bullets are matched
       greedily after ``THEMES:`` until the response ends.

    3. **Happy path** (default): paragraph + optional TYPE/SLUG
       trailer. Returns ``DraftResult(outcome=PROPOSAL, paragraph,
       llm_type_suggestion, llm_slug_suggestion)``.

    Sentinel matches anchor at start-of-content (``stripped`` view)
    so a happy-path paragraph that happens to mention "NO-CLAIM"
    inline (e.g. quoting the prompt) doesn't trigger a misclass.
    """
    stripped = content.strip()

    # Outcome B: NO-CLAIM. The prompt requires the token to appear on
    # its own line as the first line of the response.
    if stripped.startswith("NO-CLAIM"):
        no_claim_match = _NO_CLAIM_RE.match(stripped)
        if no_claim_match:
            reason_match = _REASON_RE.search(stripped)
            reason = reason_match.group(1).strip() if reason_match else ""
            return DraftResult(outcome=OUTCOME_NO_CLAIM, reason=reason)

    # Outcome C: SPLIT. The prompt requires the token to appear on its
    # own line as the first line, followed by THEMES: + bullets.
    if stripped.startswith("SPLIT"):
        split_match = _SPLIT_RE.match(stripped)
        if split_match:
            themes: list[str] = []
            themes_header = _THEMES_HEADER_RE.search(stripped)
            if themes_header:
                # Walk bullets after the THEMES: header. The prompt
                # asks for ``- <theme>`` shape; tolerate ``*`` too.
                tail = stripped[themes_header.end():]
                themes = [m.group(1).strip() for m in _BULLET_RE.finditer(tail)]
            return DraftResult(outcome=OUTCOME_SPLIT, themes=themes)

    # Outcome A: happy path. Existing TYPE/SLUG trailer parse.
    type_match = _TYPE_LINE_RE.search(content)
    slug_match = _SLUG_LINE_RE.search(content)
    type_sug = (type_match.group(1).lower() if type_match else "")
    slug_sug = (slug_match.group(1).lower() if slug_match else "")

    # Strip the matched trailer lines from the paragraph.
    paragraph = content
    if type_match:
        paragraph = paragraph.replace(type_match.group(0), "")
    if slug_match:
        paragraph = paragraph.replace(slug_match.group(0), "")
    # Collapse trailing blank lines.
    paragraph = paragraph.strip()
    return DraftResult(
        outcome=OUTCOME_PROPOSAL,
        paragraph=paragraph,
        llm_type_suggestion=type_sug,
        llm_slug_suggestion=slug_sug,
    )


# ---------------------------------------------------------------------------
# Markdown writer — atomic file write to inbox/proposed-canonical/
# ---------------------------------------------------------------------------


def render_proposal_markdown(
    candidate: ProposalCandidate,
    draft: DraftResult,
    *,
    proposed_at: str,
    proposed_path: str,
    proposed_canonical_type: str,
    proposed_slug: str,
    instance_config_basename: str = "config.yaml",
) -> str:
    """Render the proposal markdown body for an inbox file.

    The ``proposed_path`` field carried in frontmatter is informational
    — the operator can grep state for fingerprint or move the file by
    its filename. The "Suggested next step" hint includes a literal
    ``alfred vault move`` invocation pre-filled with the source +
    destination so the operator can copy-paste rather than type.

    ``instance_config_basename`` is the per-instance config filename
    (``config.kalle.yaml``, ``config.yaml``, etc.) so the suggested
    invocation is correct for the instance the miner is running under.
    Defaults to ``config.yaml`` for the "default" / Salem case.
    """
    title_text = " ".join(
        word[:1].upper() + word[1:] if word else word
        for word in proposed_slug.replace("-", " ").split(" ")
    ) or proposed_slug

    paragraph = draft.paragraph or (
        "_(Drafter LLM unavailable. Operator should write the unifying "
        "claim manually based on the source members below.)_"
    )

    src_destination = f"{proposed_canonical_type}/{proposed_slug}.md"

    fm_lines = [
        "---",
        "type: proposed-canonical",
        f'proposed_at: "{proposed_at}"',
        f'source_cluster_id: "{candidate.cluster.cluster_id}"',
        f"source_cluster_labels: {json.dumps(candidate.cluster.labels)}",
        f"source_member_count: {len(candidate.cluster.member_files)}",
        f'proposed_canonical_type: "{proposed_canonical_type}"',
        f'proposed_slug: "{proposed_slug}"',
        f'fingerprint: "{candidate.fingerprint}"',
        "status: proposed",
        "---",
        "",
        f"# {title_text}",
        "",
        "> Phase 4 pattern miner surfaced this cluster on "
        f"{proposed_at[:10]}. Andrew should read the source members "
        "below, decide whether to promote (move to "
        f"`{proposed_canonical_type}/<slug>.md`), refine, or discard.",
        "",
        "## Mined claim",
        "",
        paragraph,
        "",
        "## Source members",
        "",
    ]
    for rel in candidate.cluster.member_files:
        # Wikilink with the filename's basename (Obsidian-style).
        # Drop the ``.md`` extension since wikilinks omit it.
        stem = rel[:-3] if rel.endswith(".md") else rel
        fm_lines.append(f"- [[{stem}]]")
    fm_lines.append("")
    fm_lines.append("## Suggested next step")
    fm_lines.append("")
    fm_lines.append(
        f"Promote with: `alfred --config {instance_config_basename} "
        f'vault move "{proposed_path}" "{src_destination}"`'
    )
    fm_lines.append("")
    fm_lines.append("Or refine the body, then promote.")
    fm_lines.append("")
    fm_lines.append(
        "Or `rm` to discard (state will not re-propose unless cluster "
        "materially changes)."
    )
    fm_lines.append("")
    return "\n".join(fm_lines)


def render_split_marker_markdown(
    candidate: ProposalCandidate,
    draft: DraftResult,
    *,
    proposed_at: str,
    proposed_path: str,
    proposed_slug: str,
) -> str:
    """Render the SPLIT marker body for a multi-theme cluster.

    Mirrors ``render_proposal_markdown`` shape minus the "Mined claim"
    section. The marker file lives at
    ``<proposed_dir>/<slug>-needs-split.md`` (the ``-needs-split``
    suffix on the filename is the operator-visible signal in
    ``ls inbox/proposed-canonical/`` output — no need to open the file
    to see what kind it is).

    Frontmatter type is ``proposed-canonical-split`` (distinct from
    happy-path ``proposed-canonical``) so a future janitor pass or
    operator grep can filter by kind.

    Per ``feedback_intentionally_left_blank.md``: silent skip on a
    cluster the LLM identified as multi-theme would lose information.
    The marker file IS the operator-visible signal — operator reviews
    the themes, decides whether to fix labels at the surveyor layer
    or split into N hand-authored canonical records.
    """
    title_text = " ".join(
        word[:1].upper() + word[1:] if word else word
        for word in proposed_slug.replace("-", " ").split(" ")
    ) or proposed_slug

    fm_lines = [
        "---",
        "type: proposed-canonical-split",
        f'proposed_at: "{proposed_at}"',
        f'source_cluster_id: "{candidate.cluster.cluster_id}"',
        f"source_cluster_labels: {json.dumps(candidate.cluster.labels)}",
        f"source_member_count: {len(candidate.cluster.member_files)}",
        f'proposed_slug: "{proposed_slug}"',
        f'fingerprint: "{candidate.fingerprint}"',
        "status: split_pending",
        "---",
        "",
        f"# {title_text}",
        "",
        "> Phase 4 pattern miner flagged this cluster as multi-theme. "
        "The LLM identified 2+ distinct themes that would be better as "
        "separate canonical records.",
        "",
        "## Themes identified",
        "",
    ]
    if draft.themes:
        for theme in draft.themes:
            fm_lines.append(f"- {theme}")
    else:
        # Defensive: SPLIT outcome with no themes parsed (LLM emitted
        # the SPLIT token but the THEMES bullet list didn't parse).
        # Operator still gets the file + the cluster's source members
        # so they can investigate; record the parse failure inline so
        # the cause is visible without grepping logs.
        fm_lines.append(
            "_(No themes parsed from drafter response. Inspect the "
            "cluster's source members below; the LLM's THEMES list "
            "may have been malformed.)_"
        )
    fm_lines.append("")
    fm_lines.append("## Source members")
    fm_lines.append("")
    for rel in candidate.cluster.member_files:
        stem = rel[:-3] if rel.endswith(".md") else rel
        fm_lines.append(f"- [[{stem}]]")
    fm_lines.append("")
    fm_lines.append("## Suggested next step")
    fm_lines.append("")
    fm_lines.append(
        "Operator action: review the themes above. Either edit the "
        "cluster's labels at the surveyor layer to break the false-"
        "glue, or split into N separate proposed-canonical records "
        "by hand, or `rm` to discard."
    )
    fm_lines.append("")
    fm_lines.append(
        f"Marker file: `{proposed_path}` (state status `split_pending` "
        "until operator acts)."
    )
    fm_lines.append("")
    return "\n".join(fm_lines)


# ---------------------------------------------------------------------------
# Scaffolding-strip — transforms inbox proposal markdown into the body
# that lands at architecture/<slug>.md or principles/<slug>.md when the
# operator promotes via ``alfred distiller promote-proposal``.
# ---------------------------------------------------------------------------


# Match a YAML frontmatter block at the very start of the file.
# ``---`` on the first line, content, ``---`` on its own line. DOTALL
# so the body can contain newlines; non-greedy so we stop at the
# FIRST closing ``---``.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n?", re.DOTALL)

# Match the "Phase 4 pattern miner surfaced this cluster..." banner —
# always emitted by ``render_proposal_markdown`` immediately after the
# H1 title, starting with ``> Phase 4 pattern miner surfaced``.
# Matches the entire blockquote (one line is the standard shape).
_PROPOSAL_BANNER_RE = re.compile(
    r"^>\s*Phase 4 pattern miner surfaced.*?$\n?",
    re.MULTILINE,
)

# Match the operator-facing footer that starts at ``## Suggested next
# step`` and runs to end-of-file. The footer is scaffolding —
# ``alfred vault move ...`` hint, "Or refine the body", "Or ``rm`` to
# discard" — none of which belong in the canonical promoted record.
_FOOTER_RE = re.compile(
    r"^##\s+Suggested next step\b.*\Z",
    re.MULTILINE | re.DOTALL,
)

# Match empty fenced code blocks — ``` followed by zero or more
# whitespace-only lines then ```. The drafter sometimes emits these
# when the LLM produces an empty code-fence placeholder.
_EMPTY_FENCE_RE = re.compile(
    r"^```[a-zA-Z0-9_+-]*\s*\n(?:[ \t]*\n)*```\s*\n?",
    re.MULTILINE,
)


def strip_proposal_scaffolding(content: str) -> str:
    """Strip inbox-only scaffolding from a proposal markdown body.

    Inbox proposals carry four kinds of scaffolding the canonical
    record shouldn't keep:

      1. YAML frontmatter (``type: proposed-canonical``, ``proposed_at``,
         etc.). Canonical records under ``architecture/`` and
         ``principles/`` are plain markdown — no frontmatter.
      2. The "Phase 4 pattern miner surfaced this cluster..." banner
         line immediately after the H1 title.
      3. The ``## Suggested next step`` section + everything after
         (the ``alfred vault move`` hint, the refine/discard prompts).
      4. Empty fenced code blocks — drafter occasionally emits these
         as placeholders.

    Returns the stripped content. Caller is responsible for any
    additional transformations (e.g., prepending a canonical
    promotion banner via :func:`canonical_promotion_banner`).

    Defensive against malformed proposals: each strip is independent;
    a missing frontmatter or missing banner just leaves that part of
    the content unchanged. Trailing whitespace collapsed to a single
    newline at end-of-file for canonical-record hygiene.

    Per the subprocess-failure-logging discipline: callers (the CLI
    handler) log the strip result so an operator can grep for
    surprising shapes (e.g. "stripped but content went to zero").
    """
    out = content
    out = _FRONTMATTER_RE.sub("", out, count=1)
    out = _PROPOSAL_BANNER_RE.sub("", out, count=1)
    out = _FOOTER_RE.sub("", out, count=1)
    out = _EMPTY_FENCE_RE.sub("", out)
    # Collapse runs of >2 consecutive blank lines to exactly 2 (one
    # blank line between sections is canonical; multiple are
    # scaffolding artifacts from the stripped sections above).
    out = re.sub(r"\n{3,}", "\n\n", out)
    # Trim trailing whitespace; ensure single trailing newline.
    out = out.rstrip() + "\n"
    return out


# Match the first ATX-style H1 heading in the body (``# Title``).
# MULTILINE so ``^`` anchors at line starts; non-greedy on the title
# capture so a stray ``\n`` in the middle of the line doesn't extend
# the match. The negative lookahead on ``#`` rules out ``##`` headers
# (those are section headers, not titles).
_TITLE_RE = re.compile(r"^#(?!#)\s+(.+?)\s*$", re.MULTILINE)


def insert_promotion_banner_after_title(
    body: str,
    banner: str,
) -> str:
    """Insert the canonical promotion banner AFTER the first H1 heading.

    Matches the ``aftermath-lab/architecture/cli-logging.md`` convention
    (title on line 1, banner on line 3 after a blank line). The prior
    behavior — prepending banner above title — inverted the convention
    and produced an awkward "banner-then-title" shape that doesn't
    render cleanly in Obsidian or look right in plain markdown viewers.

    Algorithm:
    - Find the first ``# Heading`` line in ``body`` via ``_TITLE_RE``.
    - Normalize both arguments to bare-text (strip leading + trailing
      newlines from the banner; consume any newlines already between
      title and body). The helper is the single source of truth for
      the title / blank / banner / blank / rest spacing — caller's
      banner can carry whatever trailing-newline shape it likes and
      the output stays uniform.
    - Compose: ``<title-line>\\n\\n<bare-banner>\\n\\n<rest>``.
    - If no H1 heading is found (edge case — proposal got malformed,
      or strip ran on a body without a title), fall back to the
      legacy prepend-to-top behavior so the banner doesn't get lost.
      The fall-back is logged at info-level so a future operator
      grep can surface "promote produced a no-title canonical."

    Returns the body with the banner inserted (always — either after
    the title or prepended). Caller writes the result to disk.

    Per the 2026-05-11 amend: the banner's caller-provided trailing
    ``\\n\\n`` (from ``canonical_promotion_banner``) used to compose
    with the helper's own separator to produce TWO blank lines
    between title and banner. The fix strips banner-edge newlines
    inside the helper so the spacing contract is owned in one place.
    """
    title_match = _TITLE_RE.search(body)
    # Normalize the banner: strip leading + trailing newlines so the
    # helper owns the title/banner/body spacing contract. Caller can
    # pass banner shapes like "> ...\n\n" (current canonical_promotion_banner
    # output) OR "> ..." (bare) OR "\n> ...\n" — all collapse to the
    # same bare-text shape here.
    bare_banner = banner.strip("\n")

    if title_match is None:
        # No H1 heading. Fall back to prepend-to-top so the banner
        # still lands on disk. Log so the operator can see this in
        # post-mortem if a canonical record ends up looking weird.
        # Use the bare-banner shape + ``\n\n`` separator so the
        # prepend-fallback spacing matches the after-title shape
        # (banner / blank-line / body) for consistency.
        log.info(
            "pattern_miner.promotion_banner_no_title",
            note=(
                "no H1 heading found in proposal body; banner "
                "prepended above content instead of after title"
            ),
        )
        return bare_banner + "\n\n" + body

    # Split the body at the title-line boundary. The regex's
    # ``\s*$`` may have greedily consumed trailing whitespace
    # (including the newline closing the title line), so
    # ``title_match.end()`` can land AFTER the title's ``\n``. Take
    # ``title_match.group(0)`` for the canonical title-line text and
    # walk past any newlines in the body following the match end to
    # find where the rest-of-body content starts.
    title_line = title_match.group(0).rstrip()
    title_start = title_match.start()
    # Strip leading newlines from the after-match remainder so the
    # helper owns the blank-line spacing.
    rest = body[title_match.end():].lstrip("\n")

    # Compose:
    #   <prefix><title-line>\n        — title's own line ends here
    #   \n                             — blank separator
    #   <bare-banner>                  — banner content, no edge newlines
    #   \n\n                           — blank separator
    #   <rest>                         — body remainder, leading newlines stripped
    # ``prefix`` is whatever was before the title in the original body
    # (typically empty when the title is line 1; preserved for the
    # rare case where the body has leading content before the title).
    prefix = body[:title_start]
    return (
        prefix
        + title_line
        + "\n\n"
        + bare_banner
        + "\n\n"
        + rest
    )


def canonical_promotion_banner(
    *,
    promoted_at_iso: str,
    member_count: int,
    fingerprint: str,
) -> str:
    """Return a one-line banner to prepend to a promoted canonical
    record. Recording provenance keeps the audit trail visible at
    the canonical record itself, not only in vault_audit.log.

    Format::

        > Promoted from inbox/proposed-canonical on YYYY-MM-DD.
        > Sources: N records (fingerprint: <short-fp>).

    The fingerprint is truncated to 12 chars for readability; the
    full hash lives in state for grep-by-fingerprint workflows.
    """
    date_part = promoted_at_iso[:10] if promoted_at_iso else "unknown date"
    short_fp = fingerprint[:12] if fingerprint else "unknown"
    return (
        f"> Promoted from inbox/proposed-canonical on {date_part}. "
        f"Sources: {member_count} records (fingerprint: {short_fp}).\n\n"
    )


def _atomic_write(path: Path, content: str) -> None:
    """Atomic-ish write: tmp → rename. Creates parent dirs first.

    Same shape as ``radar_day._atomic_write`` so the on-disk
    semantics match: a half-written proposal can never be observed by
    the curator's inbox watcher.
    """
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _write_empty_state_marker(
    proposed_dir: Path,
    *,
    last_mined: str,
    candidate_count: int,
    survivor_count: int,
) -> None:
    """Touch ``.gitkeep`` with a comment recording the last mine timestamp.

    Per the universal "intentionally left blank" rule. When no clusters
    pass the gate, an operator running ``ls inbox/proposed-canonical/``
    can see the file's mtime + a one-liner inside it explaining the
    daemon ran but had nothing to surface — distinguishes idle-healthy
    from broken.

    Idempotent: rewrites the file every run. The .gitkeep convention
    keeps the directory present in git even when empty.
    """
    proposed_dir.mkdir(parents=True, exist_ok=True)
    keepfile = proposed_dir / ".gitkeep"
    body = (
        f"# Pattern miner — no candidates surfaced as of {last_mined}\n"
        f"#\n"
        f"# Candidates evaluated: {candidate_count}\n"
        f"# Candidates that passed the gate: {survivor_count}\n"
        f"#\n"
        f"# This is a placeholder so the directory stays in git. The miner\n"
        f"# rewrites this file every run; a stale timestamp here means the\n"
        f"# miner hasn't run recently. See\n"
        f"# project_kalle_phase4_pattern_miner.md for the gate rules.\n"
    )
    keepfile.write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# Reconcile sweep — walk existing proposals, mark promoted/discarded
# ---------------------------------------------------------------------------


def _load_canonical_path_index(
    vault_path: Path,
    canonical_match_dirs: list[str] | tuple[str, ...],
) -> dict[str, Path]:
    """Sibling to :func:`load_canonical_index` that returns
    ``{slug: absolute_path}`` instead of just the slug set.

    Reconcile needs the absolute path of the matching canonical
    artifact so it can populate ``promoted_to`` on the state entry.
    Gate logic only needs the set (does-it-exist check) so the
    public ``load_canonical_index`` stays slug-only; this one is
    private to reconcile.

    First-write-wins on slug collisions (rare but possible when two
    canonical dirs both have a file with the same stem). Walk order
    is determined by ``canonical_match_dirs`` order — typically
    ``architecture`` → ``principles`` → ``stack`` per the default,
    which is also priority order for promotion classification.
    """
    out: dict[str, Path] = {}
    for dirname in canonical_match_dirs:
        d = vault_path / dirname
        if not d.is_dir():
            continue
        for md_path in d.rglob("*.md"):
            if md_path.name == ".gitkeep":
                continue
            slug = slugify(md_path.stem)
            if slug and slug not in out:
                # First-wins. A later dir's same-slug file doesn't
                # overwrite; gate logic also treats slugs as a set
                # (existence-only), so this matches.
                out[slug] = md_path
    return out


def _find_canonical_by_fingerprint(
    vault_path: Path,
    canonical_match_dirs: list[str] | tuple[str, ...],
    short_fingerprint: str,
) -> list[Path]:
    """Grep canonical_match_dirs for the short-form fingerprint.

    The ``canonical_promotion_banner`` helper embeds ``fingerprint:
    <12-char>`` in the banner it prepends to promoted records. This
    helper greps for that signal so reconcile can detect promotions
    that bypassed the slug-match path (operator renamed the slug
    during promote — the case fix #2 is closing).

    Returns a list of absolute paths whose body contains the
    fingerprint substring. Empty list = no match. Length > 1 = the
    operator copy-pasted the banner across multiple files (rare but
    real); caller decides how to handle (today: warn, pick first).

    Per the subprocess-failure-logging discipline: unreadable files
    are logged + skipped + iteration continues. A single permission
    error on one file MUST NOT crash the whole reconcile sweep.
    """
    if not short_fingerprint:
        return []
    needle = f"fingerprint: {short_fingerprint}"
    matches: list[Path] = []
    for dirname in canonical_match_dirs:
        d = vault_path / dirname
        if not d.is_dir():
            continue
        for md_path in d.rglob("*.md"):
            if md_path.name == ".gitkeep":
                continue
            try:
                content = md_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                log.warning(
                    "pattern_miner.fingerprint_grep_read_failed",
                    path=str(md_path),
                    error_type=type(exc).__name__,
                    error=str(exc)[:200],
                )
                continue
            if needle in content:
                matches.append(md_path)
    return matches


def reconcile_state(
    state: PatternMinerState,
    vault_path: Path,
    canonical_match_dirs: list[str] | tuple[str, ...],
) -> dict[str, int]:
    """For each reconcilable entry, check whether the operator acted.

    Reconcilable statuses (per ``RECONCILABLE_STATUSES`` in
    ``pattern_miner_state.py``): ``pending`` (happy-path proposal
    awaiting operator promote/discard) AND ``split_pending``
    (split-marker awaiting operator review). Promoted / discarded /
    superseded are terminal; reconcile leaves them alone.

    Four outcomes per reconcilable entry, checked in order:

    1. ``proposed_path`` still exists → still pending (or
       split_pending), no change.
    2. ``proposed_path`` missing AND a file with ``proposed_slug``
       exists under any ``canonical_match_dirs`` → status =
       promoted, ``promoted_to`` set to the matched path,
       ``promoted_at`` set to now.
    3. ``proposed_path`` missing AND ``proposed_slug`` doesn't match
       AND the fingerprint short-form is found in some canonical
       file's body → status = promoted, ``promoted_to`` set to the
       found path (operator renamed the slug during promote; the
       banner still carries the fingerprint signal). [2026-05-11
       extension closing the slug-rename misclassification.]
    4. ``proposed_path`` missing AND no canonical match anywhere →
       status = discarded, ``discarded_at`` set to now.

    Per-action fields (``promoted_to`` / ``promoted_at`` /
    ``discarded_at``) are populated on the entry — reconcile now
    matches the CLI-promote contract for these fields. Reconcile-
    set ``promoted_to`` is vault-relative (matches what the CLI
    writes); ``discarded_reason`` stays empty (reconcile can't
    infer operator intent).

    Note: split_pending entries follow the same contract.

    Returns a tally dict: ``{"promoted": N, "discarded": M,
    "still_pending": K}``. Caller (mine_patterns) logs this. Per-
    entry transitions also emit individual
    ``pattern_miner.reconcile_transition`` log events with
    fingerprint + from-status + to-status + detection-rule fields
    so an operator can grep "what just got reclassified this run."

    Reconcile is opt-in by the caller. Top-level mine_patterns calls
    it before the new-mining pass so the state is fresh; CLI handlers
    can call it standalone for status reporting without re-mining.
    """
    # Build both indices once for the whole sweep. The slug index is
    # cheap (one stat per file). The path index is the same walk; we
    # do both rather than choosing because reconcile may need both.
    canonical_path_index = _load_canonical_path_index(
        vault_path, canonical_match_dirs,
    )
    # The set view drives the existing slug-match step.
    canonical_index = set(canonical_path_index.keys())

    promoted = 0
    discarded = 0
    still_pending = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for fp, entry in list(state.proposals.items()):
        if entry.status not in RECONCILABLE_STATUSES:
            continue
        from_status = entry.status

        # Step 1: proposed file still on disk → no transition.
        proposed_abs = vault_path / entry.proposed_path
        if proposed_abs.is_file():
            still_pending += 1
            continue

        # Step 2: slug-match against canonical_match_dirs.
        slug_match_path: Path | None = None
        if entry.proposed_slug and entry.proposed_slug in canonical_index:
            slug_match_path = canonical_path_index[entry.proposed_slug]

        # Step 3 (2026-05-11 extension): fingerprint-grep fallback.
        # Only runs when slug-match didn't fire — slug-match is more
        # deterministic and faster (set membership vs. file read per
        # candidate). When slug-match finds a hit, step 3 is skipped.
        fp_match_paths: list[Path] = []
        if slug_match_path is None and entry.fingerprint:
            short_fp = entry.fingerprint[:12]
            fp_match_paths = _find_canonical_by_fingerprint(
                vault_path, canonical_match_dirs, short_fp,
            )

        # Classify based on signals.
        if slug_match_path is not None:
            # Step 2 hit. Compute vault-relative path for promoted_to.
            try:
                target_rel = str(slug_match_path.relative_to(vault_path))
            except ValueError:
                target_rel = str(slug_match_path)
            state.mark_status(fp, STATUS_PROMOTED)
            entry.promoted_to = target_rel
            entry.promoted_at = now_iso
            promoted += 1
            log.info(
                "pattern_miner.reconcile_transition",
                fingerprint=fp[:12],
                from_status=from_status,
                to_status=STATUS_PROMOTED,
                detection_rule="slug_match",
                promoted_to=target_rel,
            )
        elif fp_match_paths:
            # Step 3 hit. Pick first match; warn if multiple.
            if len(fp_match_paths) > 1:
                # Operator copy-paste mistake — fingerprint banner
                # appears in multiple files. Pick the first one and
                # flag for review. The operator-actionable log carries
                # all matching paths so the operator can investigate.
                log.warning(
                    "pattern_miner.fingerprint_multiple_matches",
                    fingerprint=fp[:12],
                    matching_paths=[str(p) for p in fp_match_paths],
                    chosen_path=str(fp_match_paths[0]),
                    note=(
                        "fingerprint banner found in multiple canonical "
                        "files; first match wins. Operator should "
                        "review and dedup."
                    ),
                )
            chosen = fp_match_paths[0]
            try:
                target_rel = str(chosen.relative_to(vault_path))
            except ValueError:
                target_rel = str(chosen)
            state.mark_status(fp, STATUS_PROMOTED)
            entry.promoted_to = target_rel
            entry.promoted_at = now_iso
            promoted += 1
            log.info(
                "pattern_miner.reconcile_transition",
                fingerprint=fp[:12],
                from_status=from_status,
                to_status=STATUS_PROMOTED,
                detection_rule="fingerprint_grep",
                promoted_to=target_rel,
            )
        else:
            # Step 4 fallback: discarded.
            state.mark_status(fp, STATUS_DISCARDED)
            entry.discarded_at = now_iso
            discarded += 1
            log.info(
                "pattern_miner.reconcile_transition",
                fingerprint=fp[:12],
                from_status=from_status,
                to_status=STATUS_DISCARDED,
                detection_rule="no_match",
            )

    return {
        "promoted": promoted,
        "discarded": discarded,
        "still_pending": still_pending,
    }


# ---------------------------------------------------------------------------
# Top-level orchestration — used by the CLI handler
# ---------------------------------------------------------------------------


@dataclass
class MineResult:
    """Outcome summary returned by :func:`mine_patterns`.

    Used by the CLI for the one-screen ops summary AND by tests to
    assert on counts / paths without re-walking the vault. ``proposed``
    and ``skipped_dedup`` come from the new-mining pass; the
    ``reconcile_*`` fields come from the upfront reconcile sweep.
    """

    candidates_evaluated: int = 0
    survivors: int = 0
    proposed: list[ProposalCandidate] = field(default_factory=list)
    skipped_dedup: int = 0
    skipped_no_slug: int = 0
    skipped_slug_unresolvable: int = 0
    slug_collisions_resolved: int = 0
    drafter_failures: int = 0
    # Phase 4 stage 2b (2026-05-10) — drafter LLM outcome counters.
    # ``skipped_no_claim`` increments when the LLM emits the NO-CLAIM
    # sentinel: cluster cosine-coherent but no shared theme; no file
    # written, no state recorded (intentional — re-run re-evaluates if
    # cluster materially changes). ``flagged_split`` increments when
    # the LLM emits the SPLIT sentinel: cluster has 2+ distinct sub-
    # themes; a split-marker file IS written under ``inbox/proposed-
    # canonical/<slug>-needs-split.md`` AND a state entry IS recorded
    # with status ``split_pending`` (operator action needed; reconcile
    # sweep walks split_pending the same way it walks pending).
    skipped_no_claim: int = 0
    flagged_split: int = 0
    reconcile_promoted: int = 0
    reconcile_discarded: int = 0
    reconcile_still_pending: int = 0
    proposed_dir: Path | None = None
    dry_run: bool = False


def mine_patterns(
    vault_path: Path,
    surveyor_state_path: Path,
    state: PatternMinerState,
    *,
    proposed_dir: Path,
    canonical_match_dirs: list[str] | tuple[str, ...] = _DEFAULT_CANONICAL_MATCH_DIRS,
    label_denylist: frozenset[str] | set[str] | None = None,
    min_cluster_size: int = _DEFAULT_MIN_CLUSTER_SIZE,
    top_n: int | None = None,
    drafter_endpoint: str = "",
    drafter_model: str = "",
    drafter_api_key: str = "",
    instance_config_basename: str = "config.yaml",
    dry_run: bool = False,
    now_iso: str | None = None,
) -> MineResult:
    """End-to-end Phase 4 mine: reconcile → gate → draft → write.

    Args:
        vault_path: Vault root (e.g. ``/home/andrew/aftermath-lab``).
        surveyor_state_path: Path to ``surveyor_state.json``.
        state: Loaded :class:`PatternMinerState`. Caller is responsible
            for ``state.load()`` before passing in.
        proposed_dir: Where ``<slug>.md`` proposals land. Typically
            ``vault_path / "inbox" / "proposed-canonical"``.
        canonical_match_dirs: Dirs scanned for the no-canonical-match
            gate. Defaults to ``("architecture", "principles", "stack")``.
        label_denylist: Override the default denylist. Pass ``None`` to
            use the default; pass a set to use that set verbatim.
        min_cluster_size: Cluster-size threshold (default 3).
        top_n: Max NEW proposals to emit per run. ``None`` = unlimited.
        drafter_endpoint, drafter_model, drafter_api_key: LLM caller
            config. Empty endpoint → skip the drafter entirely (the
            proposal still gets written with a "drafter unavailable"
            placeholder paragraph). This makes dry-run + no-Ollama
            cases sane.
        instance_config_basename: Filename to embed in the proposal's
            "Suggested next step" CLI invocation.
        dry_run: When True, evaluate + log but DON'T write proposal
            files OR mutate state. Useful for "what would I propose
            if I ran live now?" inspection.
        now_iso: Override for tests. Defaults to current UTC ISO.

    Returns a :class:`MineResult` with counts + emitted candidates.
    Empty-state observability: when no candidates survive, emits
    ``pattern_miner.no_candidates`` log AND (live-only) writes the
    ``.gitkeep`` placeholder per the intentionally-left-blank rule.
    """
    if now_iso is None:
        now_iso = datetime.now(timezone.utc).isoformat()
    denylist = (
        label_denylist if label_denylist is not None
        else _DEFAULT_LABEL_DENYLIST
    )

    # 1. Reconcile pending proposals against current vault state.
    reconcile = reconcile_state(state, vault_path, canonical_match_dirs)

    # 2. Read the surveyor cluster output.
    clusters = read_surveyor_clusters(surveyor_state_path)

    # 3. Build the canonical-match index once.
    canonical_index = load_canonical_index(vault_path, canonical_match_dirs)

    result = MineResult(
        candidates_evaluated=len(clusters),
        reconcile_promoted=reconcile["promoted"],
        reconcile_discarded=reconcile["discarded"],
        reconcile_still_pending=reconcile["still_pending"],
        proposed_dir=proposed_dir,
        dry_run=dry_run,
    )

    # 4. Gate + dedup pass.
    survivors: list[ProposalCandidate] = []
    skipped_dedup = 0
    skipped_no_slug = 0
    for cluster in clusters:
        # Gate first (cheap), then dedup.
        if not gate_cluster(
            cluster, canonical_index, denylist,
            min_cluster_size=min_cluster_size,
        ):
            continue
        fp = fingerprint_cluster(cluster.member_files, cluster.labels)
        if state.has_entry_for_fingerprint(fp):
            skipped_dedup += 1
            continue
        slug = derive_proposed_slug(cluster.labels)
        if not slug:
            skipped_no_slug += 1
            continue
        candidate = ProposalCandidate(
            cluster=cluster,
            fingerprint=fp,
            proposed_slug=slug,
            proposed_canonical_type=derive_proposed_canonical_type(cluster.labels),
        )
        survivors.append(candidate)
        if top_n is not None and len(survivors) >= top_n:
            break

    result.survivors = len(survivors)
    result.skipped_dedup = skipped_dedup
    result.skipped_no_slug = skipped_no_slug

    # 5. Empty-state observability per the universal rule.
    if not survivors:
        log.info(
            "pattern_miner.no_candidates",
            evaluated=len(clusters),
            skipped_dedup=skipped_dedup,
            skipped_no_slug=skipped_no_slug,
            reconcile=reconcile,
            proposed_dir=str(proposed_dir),
            dry_run=dry_run,
        )
        if not dry_run:
            _write_empty_state_marker(
                proposed_dir,
                last_mined=now_iso,
                candidate_count=len(clusters),
                survivor_count=0,
            )
        return result

    # 6. For each survivor: drafter call + markdown write + state record.
    #
    # Pre-seed the within-run slug claim set with slugs already owned
    # by prior runs' state entries (any status). Without this, a fresh
    # mine could produce ``local-llm-hardware.md`` overwriting a prior
    # run's pending or already-promoted file of the same slug — silent
    # data loss. The same set is augmented per-iteration so two new
    # candidates competing for the same slug within ONE run also
    # uniquify cleanly. Per WARN-1 in the 2026-05-10 code-review pass.
    claimed_slugs: set[str] = set()
    for entry in state.proposals.values():
        if entry.proposed_slug:
            claimed_slugs.add(entry.proposed_slug)
    drafter_failures = 0
    skipped_slug_unresolvable = 0
    slug_collisions_resolved = 0
    skipped_no_claim = 0
    flagged_split = 0
    for candidate in survivors:
        # LLM draft. Empty endpoint = skip the drafter and use a
        # placeholder paragraph (operator will write the claim manually).
        if drafter_endpoint and drafter_model and not dry_run:
            draft = call_drafter(
                candidate, vault_path,
                endpoint=drafter_endpoint,
                model=drafter_model,
                api_key=drafter_api_key,
            )
            if draft.error:
                drafter_failures += 1
        else:
            draft = DraftResult()  # paragraph empty → placeholder used

        # Phase 4 stage 2b (2026-05-10): branch on draft.outcome before
        # the slug-resolution + write path. NO-CLAIM short-circuits
        # entirely (no file, no state); SPLIT routes through a marker
        # file with a distinct ``-needs-split`` suffix and records
        # status ``split_pending``. Happy-path is unchanged.
        if draft.outcome == OUTCOME_NO_CLAIM:
            # Cluster cosine-coherent but no shared theme. Per the
            # design memo + stage 2a prompt: skip entirely. Don't
            # claim the slug (no file written), don't record state
            # (re-run re-evaluates if cluster materially changes —
            # the LLM may judge differently if labels/members shift,
            # and a stale "no_claim" state entry would silently
            # block legitimate future proposals on the same shape).
            log.info(
                "pattern_miner.cluster_no_claim",
                cluster_id=candidate.cluster.cluster_id,
                fingerprint=candidate.fingerprint,
                labels=list(candidate.cluster.labels),
                reason=draft.reason,
            )
            skipped_no_claim += 1
            continue

        # Honor LLM TYPE/SLUG suggestions when present + valid; otherwise
        # fall back to the heuristic-derived ones we already computed.
        # SPLIT outcome doesn't carry TYPE/SLUG (the prompt forbids it
        # — "Do NOT also emit a paragraph or a TYPE/SLUG trailer") so
        # both branches stay on the heuristic-derived defaults.
        proposed_canonical_type = candidate.proposed_canonical_type
        if draft.llm_type_suggestion in ("architecture", "principles"):
            proposed_canonical_type = draft.llm_type_suggestion
        proposed_slug = candidate.proposed_slug
        if draft.llm_slug_suggestion:
            # Re-slugify the LLM's suggestion before assignment. The
            # _SLUG_LINE_RE regex permits trailing hyphens and the
            # current ``_parse_drafter_response`` only lower-cases — both
            # would let an ill-formed shape (e.g. ``local-llm-hardware-``)
            # through to the filesystem. ``slugify()`` is the single
            # source of truth for slug shape; any operator-facing path
            # that derives a slug must pass through it. Per WARN-2 in
            # the 2026-05-10 code-review pass.
            normalized = slugify(draft.llm_slug_suggestion)
            if normalized:
                proposed_slug = normalized

        # Resolve same-run + cross-run slug collisions by appending a
        # numeric suffix until the slug is unique against both prior-
        # run state claims AND within-run claims. Cap the search at 50
        # attempts so a pathological collision storm can't spin
        # indefinitely; if 50 isn't enough the cluster is dropped with
        # a load-bearing log so the operator can investigate. Per
        # WARN-1 in the 2026-05-10 code-review pass.
        #
        # Slug resolution applies to BOTH proposal and split outcomes —
        # the SPLIT marker filename is ``<slug>-needs-split.md`` so the
        # ``-needs-split`` suffix on the file naturally distinguishes
        # it from a happy-path ``<slug>.md`` proposal. The state-side
        # ``proposed_slug`` field stays bare so reconcile_state's
        # canonical-match check operates on the slug the operator
        # would actually use when promoting (architecture/<slug>.md).
        original_slug = proposed_slug
        if proposed_slug in claimed_slugs:
            uniquified: str | None = None
            for n in range(2, 52):  # 2..51 inclusive → 50 attempts
                candidate_slug = f"{original_slug}-{n}"
                if candidate_slug not in claimed_slugs:
                    uniquified = candidate_slug
                    break
            if uniquified is None:
                # Pathological — log + skip rather than silently
                # overwrite. The cluster will surface again on the next
                # mine pass once the operator clears one of the
                # colliding slugs.
                log.warning(
                    "pattern_miner.slug_unresolvable",
                    cluster_id=candidate.cluster.cluster_id,
                    fingerprint=candidate.fingerprint,
                    original_slug=original_slug,
                    attempts=50,
                )
                skipped_slug_unresolvable += 1
                continue
            log.info(
                "pattern_miner.slug_collision_resolved",
                cluster_id=candidate.cluster.cluster_id,
                fingerprint=candidate.fingerprint,
                original_slug=original_slug,
                resolved_slug=uniquified,
            )
            proposed_slug = uniquified
            slug_collisions_resolved += 1
        claimed_slugs.add(proposed_slug)

        # Compute file basename + path. SPLIT uses ``<slug>-needs-split.md``
        # so an operator running ``ls inbox/proposed-canonical/`` can
        # tell at a glance which clusters need split-review vs which
        # are happy-path proposals — no need to open the file.
        if draft.outcome == OUTCOME_SPLIT:
            file_basename = f"{proposed_slug}-needs-split.md"
        else:
            file_basename = f"{proposed_slug}.md"

        # Compute the final proposed_path now that we know the slug
        # AND the file basename. vault-relative for portability; the
        # writer resolves against ``proposed_dir`` (which is itself
        # vault_path-relative or absolute, caller's choice).
        try:
            proposed_dir_rel = proposed_dir.relative_to(vault_path)
            proposed_path_rel = str(proposed_dir_rel / file_basename)
        except ValueError:
            # ``proposed_dir`` not under vault — fall back to absolute.
            proposed_path_rel = str(proposed_dir / file_basename)

        # Render markdown — happy-path proposal vs SPLIT marker. The
        # SPLIT marker uses the dedicated render_split_marker_markdown
        # which has a distinct frontmatter type and an operator-action
        # "review themes" footer instead of the proposal's "promote /
        # refine / discard" hint.
        if draft.outcome == OUTCOME_SPLIT:
            markdown = render_split_marker_markdown(
                candidate, draft,
                proposed_at=now_iso,
                proposed_path=proposed_path_rel,
                proposed_slug=proposed_slug,
            )
        else:
            markdown = render_proposal_markdown(
                candidate, draft,
                proposed_at=now_iso,
                proposed_path=proposed_path_rel,
                proposed_canonical_type=proposed_canonical_type,
                proposed_slug=proposed_slug,
                instance_config_basename=instance_config_basename,
            )

        # Persist (live only). Dry-run leaves the survivors list
        # populated for the CLI to summarize without touching disk.
        if not dry_run:
            target = proposed_dir / file_basename
            _atomic_write(target, markdown)
            entry_status = (
                STATUS_SPLIT_PENDING
                if draft.outcome == OUTCOME_SPLIT
                else STATUS_PENDING
            )
            entry = ProposalEntry(
                fingerprint=candidate.fingerprint,
                cluster_id=candidate.cluster.cluster_id,
                labels=list(candidate.cluster.labels),
                member_count=len(candidate.cluster.member_files),
                proposed_at=now_iso,
                proposed_path=proposed_path_rel,
                proposed_slug=proposed_slug,
                proposed_canonical_type=proposed_canonical_type,
                status=entry_status,
            )
            state.record_proposal(entry)

        if draft.outcome == OUTCOME_SPLIT:
            log.info(
                "pattern_miner.cluster_multi_theme",
                cluster_id=candidate.cluster.cluster_id,
                fingerprint=candidate.fingerprint,
                themes=list(draft.themes),
            )
            flagged_split += 1
        else:
            result.proposed.append(candidate)

    result.drafter_failures = drafter_failures
    result.skipped_slug_unresolvable = skipped_slug_unresolvable
    result.slug_collisions_resolved = slug_collisions_resolved
    result.skipped_no_claim = skipped_no_claim
    result.flagged_split = flagged_split

    # 7. Save state ONCE at the end (atomic; one .tmp → rename per run).
    # Save when EITHER proposals OR split markers were recorded — both
    # mutate state. Without the flagged_split clause a SPLIT-only run
    # would write the marker file but lose the state entry on the next
    # daemon start, which would re-emit the same SPLIT cluster. The
    # reconcile sweep also mutates state (promoted/discarded
    # transitions); save when reconcile fired any transition too so
    # those don't get lost.
    if not dry_run and (
        result.proposed
        or flagged_split
        or reconcile["promoted"]
        or reconcile["discarded"]
    ):
        state.save()

    log.info(
        "pattern_miner.run_complete",
        evaluated=len(clusters),
        survivors=len(survivors),
        proposed=len(result.proposed),
        skipped_dedup=skipped_dedup,
        skipped_no_slug=skipped_no_slug,
        skipped_slug_unresolvable=skipped_slug_unresolvable,
        slug_collisions_resolved=slug_collisions_resolved,
        skipped_no_claim=skipped_no_claim,
        flagged_split=flagged_split,
        drafter_failures=drafter_failures,
        reconcile=reconcile,
        dry_run=dry_run,
    )

    return result


__all__ = [
    "ClusterRecord",
    "DraftResult",
    "MineResult",
    "OUTCOME_NO_CLAIM",
    "OUTCOME_PROPOSAL",
    "OUTCOME_SPLIT",
    "ProposalCandidate",
    "ProposalEntry",
    "PatternMinerState",
    "call_drafter",
    "canonical_promotion_banner",
    "insert_promotion_banner_after_title",
    "cluster_matches_canonical",
    "cluster_passes_label_quality",
    "derive_proposed_canonical_type",
    "derive_proposed_slug",
    "evaluate_cluster",
    "fingerprint_cluster",
    "gate_cluster",
    "label_segments",
    "load_canonical_index",
    "mine_patterns",
    "read_surveyor_clusters",
    "reconcile_state",
    "render_proposal_markdown",
    "render_split_marker_markdown",
    "slugify",
    "strip_proposal_scaffolding",
]
