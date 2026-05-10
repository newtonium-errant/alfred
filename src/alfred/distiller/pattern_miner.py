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
    STATUS_DISCARDED,
    STATUS_PENDING,
    STATUS_PROMOTED,
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
# Drafting prompt — inline constant per the dispatch's c1 discipline
# ---------------------------------------------------------------------------
#
# Q5 prompt verbatim from the design memo. Externalization to a
# ``prompts/`` file is a deferred follow-up if prompt-tuner needs to
# iterate. For v1 the prompt lives here so the call site is the single
# source of truth.
#
# The two ``{labels}`` and ``{count}`` placeholders are substituted at
# call time. ``{members_with_previews}`` carries the cluster's member
# titles + body previews; the writer assembles this string from the
# cluster's vault paths.

DRAFT_PROMPT_TEMPLATE: str = """\
You are reading a cluster of related documents from a developer's knowledge vault.
The cluster has cohered semantically but has not yet been promoted to a canonical
named artifact (architecture/<theme>.md or principles/<rule>.md).

Your job: write a SINGLE-PARAGRAPH (3-5 sentences) summary of what unifies these
documents — the theme that nobody has named yet. Be specific and concrete. Cite
patterns, not generic categories.

Cluster labels (from surveyor): {labels}
Document count: {count}

Documents:
{members_with_previews}

Respond with ONLY the paragraph. No preamble, no "the unifying theme is", just
the claim.

Then on a new line, suggest whether this is better as `architecture/<slug>.md`
(structural design choice) or `principles/<slug>.md` (rule of practice). Format:
TYPE: architecture|principles
SLUG: <kebab-case-slug-no-extension>
"""


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


@dataclass
class DraftResult:
    """Outcome of a drafter LLM call. ``paragraph`` empty iff failed.

    ``llm_type_suggestion`` and ``llm_slug_suggestion`` come from the
    optional TYPE/SLUG trailer the prompt asks for; either may be
    empty if the LLM omitted them or the parser couldn't pick them
    up. The writer falls back to the heuristic-derived slug + type
    from :func:`derive_proposed_slug` / :func:`derive_proposed_canonical_type`
    when the LLM suggestions are missing.
    """

    paragraph: str = ""
    llm_type_suggestion: str = ""
    llm_slug_suggestion: str = ""
    error: str = ""


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
    prompt = DRAFT_PROMPT_TEMPLATE.format(
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

    paragraph, type_sug, slug_sug = _parse_drafter_response(content)
    return DraftResult(
        paragraph=paragraph,
        llm_type_suggestion=type_sug,
        llm_slug_suggestion=slug_sug,
    )


def _parse_drafter_response(content: str) -> tuple[str, str, str]:
    """Split the drafter's ``content`` into (paragraph, type, slug).

    The prompt asks for the paragraph on its own, then a TYPE/SLUG
    trailer. We extract the trailer with regex and treat everything
    NOT matched by the trailer as the paragraph. The trailer is
    optional; when it's absent we return ``("", "")`` for the
    suggestions and the writer falls back to the heuristic.

    Strips trailing whitespace + blank lines from the paragraph so
    the proposal markdown looks clean.
    """
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
    return paragraph, type_sug, slug_sug


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


def reconcile_state(
    state: PatternMinerState,
    vault_path: Path,
    canonical_match_dirs: list[str] | tuple[str, ...],
) -> dict[str, int]:
    """For each ``pending`` proposal, check whether the operator acted.

    Three outcomes per pending entry:
    1. ``proposed_path`` still exists → still pending, no change.
    2. ``proposed_path`` missing AND a file with ``proposed_slug``
       exists under any ``canonical_match_dirs`` → status = promoted.
    3. ``proposed_path`` missing AND no canonical match → status =
       discarded.

    Returns a tally dict: ``{"promoted": N, "discarded": M,
    "still_pending": K}`` for caller-side logging.

    Note: the canonical-match check uses :func:`slugify` on the file
    stem so e.g. an operator who renames during promotion still
    counts as "promoted" if the slug substantially matches.

    Reconcile is opt-in by the caller. Top-level mine_patterns calls
    it before the new-mining pass so the state is fresh; CLI handlers
    can call it standalone for status reporting without re-mining.
    """
    canonical_index = load_canonical_index(vault_path, canonical_match_dirs)
    promoted = 0
    discarded = 0
    still_pending = 0
    for fp, entry in list(state.proposals.items()):
        if entry.status != STATUS_PENDING:
            continue
        # Vault-relative proposed_path; resolve against vault_path.
        proposed_abs = vault_path / entry.proposed_path
        if proposed_abs.is_file():
            still_pending += 1
            continue
        # Operator removed the proposal file. Was it promoted (a
        # matching canonical artifact appeared) or discarded (deleted
        # outright)?
        if entry.proposed_slug and entry.proposed_slug in canonical_index:
            state.mark_status(fp, STATUS_PROMOTED)
            promoted += 1
        else:
            state.mark_status(fp, STATUS_DISCARDED)
            discarded += 1
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
    drafter_failures: int = 0
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
    drafter_failures = 0
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

        # Honor LLM TYPE/SLUG suggestions when present + valid; otherwise
        # fall back to the heuristic-derived ones we already computed.
        proposed_canonical_type = candidate.proposed_canonical_type
        if draft.llm_type_suggestion in ("architecture", "principles"):
            proposed_canonical_type = draft.llm_type_suggestion
        proposed_slug = candidate.proposed_slug
        if draft.llm_slug_suggestion:
            proposed_slug = draft.llm_slug_suggestion

        # Compute the final proposed_path now that we know the slug.
        # vault-relative for portability; the writer resolves against
        # ``proposed_dir`` (which is itself vault_path-relative or
        # absolute, caller's choice).
        try:
            proposed_dir_rel = proposed_dir.relative_to(vault_path)
            proposed_path_rel = str(proposed_dir_rel / f"{proposed_slug}.md")
        except ValueError:
            # ``proposed_dir`` not under vault — fall back to absolute.
            proposed_path_rel = str(proposed_dir / f"{proposed_slug}.md")

        # Render markdown (whether dry-run or live — we want the same
        # observable content for inspection).
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
            target = proposed_dir / f"{proposed_slug}.md"
            _atomic_write(target, markdown)
            entry = ProposalEntry(
                fingerprint=candidate.fingerprint,
                cluster_id=candidate.cluster.cluster_id,
                labels=list(candidate.cluster.labels),
                member_count=len(candidate.cluster.member_files),
                proposed_at=now_iso,
                proposed_path=proposed_path_rel,
                proposed_slug=proposed_slug,
                proposed_canonical_type=proposed_canonical_type,
            )
            state.record_proposal(entry)

        result.proposed.append(candidate)

    result.drafter_failures = drafter_failures

    # 7. Save state ONCE at the end (atomic; one .tmp → rename per run).
    if not dry_run and result.proposed:
        state.save()

    log.info(
        "pattern_miner.run_complete",
        evaluated=len(clusters),
        survivors=len(survivors),
        proposed=len(result.proposed),
        skipped_dedup=skipped_dedup,
        skipped_no_slug=skipped_no_slug,
        drafter_failures=drafter_failures,
        reconcile=reconcile,
        dry_run=dry_run,
    )

    return result


__all__ = [
    "ClusterRecord",
    "DraftResult",
    "MineResult",
    "ProposalCandidate",
    "ProposalEntry",
    "PatternMinerState",
    "call_drafter",
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
    "slugify",
]
