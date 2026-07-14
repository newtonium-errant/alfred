"""Local multi-speaker diarization for the sovereign scribe (scribe P4, real engine
in P4-4).

The DIARIZER-WRITER half of P4: it resolves each transcript segment's ``speaker``
to a canonical ROLE ({clinician, patient, other, unknown}) and latches
``Transcript.diarized``. The attribution-READER half — the mis-attribution safety
net ``speaker_attribution.py`` — shipped in P4-2 and DOES consume ``speaker`` /
``speaker_conf`` now (the P4-2 flags + banner). P4-1 shipped the plumbing + the fake
seam + the frozen shapes; P4-4 (here) replaces the pyannote stub with the real
engine.

Providers (dispatch on ``config.diarize.provider``) — ALL on the sovereign
barrier-a-sibling allowlist, so no cloud diarization is reachable:

  * ``off`` — the fail-closed default. NO diarization: the chunk is returned
    unchanged (``speaker`` stays ``None``, ``diarized`` stays ``False``). The
    note-gen path is byte-identical to P3.
  * ``fake`` — a DETERMINISTIC CI backend that re-reads the fake-STT ``.txt``
    sidecar, parses an optional leading role tag per line, and writes the
    resolved role onto each segment. NO heavy dep; gives the P4 plumbing
    unconditional coverage.
  * ``pyannote`` — the REAL on-box engine (P4-4). Runs pyannote's
    speaker-diarization pipeline, aligns its speaker turns onto the STT segments,
    and writes ``speaker`` / ``speaker_cluster`` / ``speaker_conf``. The dependency
    is the ``[scribe-diarize]`` extra (STAY-C venv only — pulls torch), so it is
    lazy-imported and never loaded in torch-free CI. Gated by ``diarize.enabled``
    (NOTE-1): ``provider=pyannote, enabled=false`` is INERT (returned untouched,
    like ``off``) so the engine can be disabled without a provider edit.

ALIGNMENT + THE SPLIT-ON-STRADDLE RECONCILIATION (P4-4). pyannote emits speaker
TURNS on a fine timeline; the STT layer emits coarser SEGMENTS that are the STABLE
``[S#]`` grounding anchors. We map turns → segments by MAX-OVERLAP: each segment's
``speaker_cluster`` is the cluster covering the most of its labeled speech, and
``speaker_conf`` is that cluster's PURITY (its share of the segment's total
overlapped speech ∈ [0, 1]). The frozen design line says "split-on-straddle", but
P4-1 NOTE-3 forbids the engine touching text OR segment boundaries, and splitting a
segment would break the ``[S#]`` id invariant (`append_chunk` monotonic ids, the
grounding cite graph, the note-gen per-line ``S#`` contract). RECONCILIATION: we do
NOT physically split. A segment that STRADDLES a speaker change gets a REDUCED
purity (< 1.0).

⚠ HONEST RESIDUAL (C1) — this fail-closes ONLY when the straddle drives purity
BELOW ``purity_threshold``. A SUPRA-threshold dominant cluster (e.g. an 85%/15%
straddle at threshold 0.80) is accepted as that speaker and ABSORBS the minority
words — they ride the majority speaker's role with no flag. That is a genuine
GRANULARITY LOSS vs a true per-word split: the segment is the attribution unit, so a
minority speaker's words inside a majority segment are mis-attributed silently. The
true fix is per-WORD attribution (P4-5 threads STT word timings into the same
word-CAPABLE overlap core — it already aggregates over an interval-list per segment,
so no redesign; P4-4 feeds it the single segment span). Until then this layer
narrows, not closes, intra-segment mixing; the human ATTEST remains the primary
control and the note is NEVER "attribution verified". Below-threshold straddles DO
fail-closed (→ ``unknown`` / ``speaker_unverified`` at P4-2), composing with the
shipped safety net — no new mutation surface, no boundary/text change.

CROSS-CHUNK CLUSTER-LABEL INSTABILITY (C3) — pyannote mints ARBITRARY per-chunk
labels (``SPEAKER_00``, ``SPEAKER_01``, ...): chunk 3's ``SPEAKER_00`` is NOT chunk
1's. ``speaker_cluster`` is therefore chunk-local and MEANINGLESS across chunks.
Harmless in P4-4 (every cluster → ``unknown`` regardless), but the P4-5 role registry
CANNOT key on the label string — it must re-anchor each chunk by EMBEDDING match
against the stable clinician enrollment. See the ``_cluster_to_role`` seam.

CARRY-FORWARDS honored here (from the P4-1/P4-2 SHIPPED blocks):
  * NOTE-1 — ``enabled`` is now the real-engine kill-switch (dispatch + the
    startup dep-check both gate on it), not defined-but-unwired.
  * NOTE-2 — the real engine can raise mid-iteration, so assignment is ATOMIC:
    ``_apply_diarization`` STAGES every segment's (cluster, conf, role) BEFORE it
    COMMITS any, and turns are produced fully before apply — a half-labeled chunk
    never folds (a raise leaves the chunk untouched → the pipeline folds it
    un-attributed).
  * NOTE-3 — the engine writes ONLY ``speaker`` / ``speaker_cluster`` /
    ``speaker_conf``; never ``text`` / ``id`` / ``start_s`` / ``end_s``.
  * P4-2 — the engine MUST never emit NaN/±inf conf: ``_guard_conf`` clamps to
    [0, 1] and maps non-finite → 0.0 AT THE SOURCE (the attribution layer also
    demotes non-finite, defense-in-depth). Roles fail-CLOSED to ``unknown`` at
    resolution — with no P4-5 enrollment, EVERY cluster resolves ``unknown``.

FAIL-SAFE-for-safety / FAIL-OPEN-for-availability: a diarize failure degrades to
``speaker=None`` + a loud log and STILL folds the text (un-attributed ≫
mis-attributed). Unlike an STT decode failure it does NOT hold the encounter —
the pipeline wraps ``assign_speakers`` accordingly.

LOCAL-BY-CONSTRUCTION: no ``api_key`` / ``base_url``; the real engine loads OFFLINE
from a MATERIALIZED, repo-id-free pipeline config (``diarize.pipeline_config``,
written by ``scripts.stage_diarize_models``) because pyannote's ``from_pretrained``
does not reliably honor ``local_files_only`` — a repo-id-bearing config still
triggers a hub GET. The OFFLINE layers, honestly (A3):
  1. load from the MATERIALIZED repo-id-free config [PRIMARY];
  2. ``_validate_materialized_config_local`` — pre-import assert every model ref is
     an existing LOCAL path (fail-loud; closes the mispointed-config hole) [compensating];
  3. the SovereignHttpGuard requests/httpx wrap [backstop];
  4. the systemd unit's PRE-IMPORT ``Environment=HF_HUB_OFFLINE=1`` [the only working
     env belt].
⚠ A RUNTIME ``os.environ['HF_HUB_OFFLINE']='1'`` set is INERT and was DELETED:
huggingface_hub freezes the constant at IMPORT, and STT imports hub before diarize —
so an in-process set never takes effect (stt.py carries the on-box proof). The
sovereign boundary (``_check_diarize_local``) independently refuses a non-local
provider at load.
"""

from __future__ import annotations

import importlib.util
import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from alfred.scribe.config import ScribeConfig
from alfred.scribe.enrollment import ResolvedEnrollment, cosine
from alfred.scribe.transcript import (
    ROLE_CLINICIAN,
    ROLE_OTHER,
    ROLE_PATIENT,
    ROLE_UNKNOWN,
    Transcript,
    normalize_role,
)

log = structlog.get_logger(__name__)

# The diarize dispatch set. MUST equal the sovereign barrier-a-sibling allowlist
# (SOVEREIGN_DIARIZE_ALLOWLIST) — pinned in tests. A provider the boundary
# permits is dispatchable here, and nothing else is.
SCRIBE_DIARIZE_PROVIDERS: frozenset[str] = frozenset({"off", "fake", "pyannote"})
# Providers that need a heavy dependency (the [scribe-diarize] extra).
_REAL_ENGINE_PROVIDERS: frozenset[str] = frozenset({"pyannote"})

# The fake-sidecar role-tag syntax → the role WORD ``normalize_role`` folds. An
# unrecognized bracket token (or no tag) is treated as untagged → ``unknown``.
_FAKE_ROLE_TAGS: dict[str, str] = {
    "[CLIN]": "clinician",
    "[PT]": "patient",
    "[OTHER]": "other",
}


class DiarizeError(Exception):
    """Diarization failed — unknown provider, unreadable input, engine failure."""


class MissingDiarizeDependency(Exception):
    """The ``pyannote`` engine is configured but pyannote.audio isn't installed.

    The scribe daemon maps this to exit 78 (missing deps, no-restart) — mirrors
    :class:`~alfred.scribe.stt.MissingSTTDependency`. The ``off`` / ``fake``
    providers never raise this (the daemon boots torch-free).
    """


def _pyannote_available() -> bool:
    """True iff pyannote.audio is importable (the ``[scribe-diarize]`` extra).

    ``find_spec`` on a dotted name imports the parent package, which raises
    ``ModuleNotFoundError`` when pyannote itself is absent — caught here so the
    probe returns a clean ``False`` (never propagates)."""
    try:
        return importlib.util.find_spec("pyannote.audio") is not None
    except ImportError:
        return False


def ensure_diarize_backend_available(config: ScribeConfig) -> None:
    """Fail-loud if the configured real-engine provider's dep is missing.

    Called at daemon startup (beside ``stt.ensure_backend_available``). No-op for
    ``off`` / ``fake`` — the daemon boots torch-free. Raises
    :class:`MissingDiarizeDependency` for ``pyannote`` when pyannote.audio isn't
    installed → the runner exits 78.

    NOTE-1: also gated on ``diarize.enabled`` — ``provider=pyannote, enabled=false``
    is INERT (``assign_speakers`` returns the chunk untouched), so it must ALSO boot
    torch-free (an operator disabling the engine shouldn't be forced to keep torch
    installed). The dep is required only when the real engine will actually run.

    A4 BOOT GATE: an ENABLED pyannote engine ALSO requires a MATERIALIZED offline
    ``pipeline_config`` that EXISTS. Without it every chunk would degrade to
    un-diarized with only a per-chunk log (a silent-drift boot), so fail LOUD here
    (``MissingDiarizeDependency`` → exit 78, no-restart — same clean give-up as a
    missing dep; a config edit + restart is the fix). Consistent with the
    encounter_salt fail-loud-on-missing precedent.
    """
    provider = (config.diarize.provider or "").strip().lower()
    if provider not in _REAL_ENGINE_PROVIDERS or not config.diarize.enabled:
        return  # off / fake / disabled pyannote → boots torch-free, no gate
    if not _pyannote_available():
        raise MissingDiarizeDependency(
            f"scribe diarize provider {provider!r} (enabled) needs pyannote.audio, "
            f"which is not installed. Install the [scribe-diarize] extra into the "
            f"STAY-C venv (torch from the CPU wheel index). The 'off'/'fake' "
            f"providers — and a disabled pyannote (enabled:false) — need no dependency."
        )
    pipeline_config = (config.diarize.pipeline_config or "").strip()
    if not pipeline_config or not Path(pipeline_config).is_file():
        raise MissingDiarizeDependency(
            f"scribe diarize provider 'pyannote' (enabled) requires a materialized "
            f"pipeline_config that EXISTS on disk; got "
            f"{pipeline_config or '(unset)'!r}. Run `python -m "
            f"alfred.scripts.stage_diarize_models` on-box to download the models + "
            f"materialize the repo-id-free config, then set "
            f"scribe.diarize.pipeline_config. Refusing to boot (exit 78) rather than "
            f"degrade every encounter to un-diarized."
        )


def assign_speakers(
    config: ScribeConfig, audio_path: str | Path, chunk_tx: Transcript,
    *, resolved: ResolvedEnrollment | None = None,
) -> Transcript:
    """Resolve per-segment speaker roles on ``chunk_tx`` — the pipeline entry.

    Dispatches on ``config.diarize.provider`` (all barrier-a-sibling-allowlisted).
    ``off`` returns the chunk untouched (no diarization); ``fake`` reads the
    sidecar; ``pyannote`` is P4-4. On success the transcript's ``diarized`` gate
    is latched. The pipeline wraps this call so any exception degrades to
    ``speaker=None`` and STILL folds (fail-open-for-availability).

    P4-5 — ``resolved`` (the encounter's bound + resolved preset, or ``None`` when no
    preset is bound / a typed refusal fell open) is the ANCHOR for the pyannote path's
    K=2 clinician matcher. ``None`` ⇒ the all-``unknown`` P4-4 end-state (fail-open).
    The ``off`` / ``fake`` seams ignore it — ``fake`` assigns roles from its sidecar
    tags (the CI role seam), ``off`` does nothing."""
    provider = (config.diarize.provider or "").strip().lower()
    if provider == "off":
        return chunk_tx  # no diarization — speaker stays None, diarized stays False
    if provider == "fake":
        return _fake_diarize(chunk_tx, audio_path)
    if provider == "pyannote":
        # NOTE-1 — ``enabled`` is the real-engine kill-switch. provider=pyannote +
        # enabled:false is INERT: return the chunk untouched (speaker=None,
        # diarized=False), byte-identical to ``off``, so the engine can be disabled
        # without a provider edit. (``off``/``fake`` are NOT gated on ``enabled``:
        # ``off`` is already inert, and ``fake`` is the CI seam that must run
        # without an operator flipping ``enabled``.)
        if not config.diarize.enabled:
            log.info(
                "scribe.diarize.disabled",
                provider="pyannote",
                source_id=chunk_tx.source_id,
                detail="provider=pyannote but diarize.enabled=false — diarization "
                       "INERT (chunk returned un-attributed, same as provider=off)",
            )
            return chunk_tx
        return _pyannote_diarize(config, audio_path, chunk_tx, resolved=resolved)
    # Defense in depth: the barrier-a sibling already refuses a non-local provider
    # at load; the dispatch fails closed too rather than silently no-op.
    raise DiarizeError(
        f"scribe diarize provider {provider or '(unset)'!r} is not a local "
        f"backend ({', '.join(sorted(SCRIBE_DIARIZE_PROVIDERS))})."
    )


def _split_role_tag(line: str) -> tuple[str | None, str]:
    """Split an optional leading fake role tag from a sidecar line.

    Recognized (case-insensitive): ``[CLIN]`` / ``[PT]`` / ``[OTHER]`` → the role
    WORD (clinician/patient/other), with the tag stripped from the text. Anything
    else — no tag, or an unrecognized bracket token — returns ``(None, line)``;
    the untagged case folds to ``unknown`` via ``normalize_role`` and the text is
    left verbatim.
    """
    stripped = line.lstrip()
    upper = stripped.upper()
    for tag, role in _FAKE_ROLE_TAGS.items():
        if upper.startswith(tag):
            return role, stripped[len(tag):].strip()
    return None, line


def _fake_diarize(chunk_tx: Transcript, audio_path: str | Path) -> Transcript:
    """Deterministic CI backend — re-reads the fake-STT ``.txt`` sidecar, parses a
    role tag per line, writes the resolved role onto each segment.

    The sidecar location mirrors ``stt._fake_transcribe`` (the ``audio_path``
    itself when it is a ``.txt``, else a sibling ``<stem>.txt``). Sidecar lines
    align 1:1 with the STT segments (both are the same non-empty-line sequence).
    A segment with no corresponding line — or an untagged line — resolves to
    ``unknown`` (fail-closed via ``normalize_role``). Latches ``diarized``.
    """
    p = Path(audio_path)
    sidecar = p if p.suffix == ".txt" else p.with_suffix(".txt")
    if not sidecar.is_file():
        raise DiarizeError(
            f"fake diarize backend needs the same text sidecar at {sidecar} as "
            f"the fake STT backend (one role-tagged line per segment). Synthetic "
            f"input only."
        )
    lines = [
        ln.strip()
        for ln in sidecar.read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    roles: list[str] = []
    for i, seg in enumerate(chunk_tx.segments):
        if i < len(lines):
            tag, text = _split_role_tag(lines[i])
        else:
            tag, text = None, seg.text  # no line → unknown, text unchanged (fail-closed)
        seg.speaker = normalize_role(tag)
        seg.text = text
        roles.append(seg.speaker)
    chunk_tx.diarized = True
    log.info(
        "scribe.diarize.assigned",
        provider="fake",
        source_id=chunk_tx.source_id,
        segments=len(chunk_tx.segments),
        clinician=roles.count(ROLE_CLINICIAN),
        patient=roles.count(ROLE_PATIENT),
        other=roles.count(ROLE_OTHER),
        unknown=roles.count(ROLE_UNKNOWN),
    )
    return chunk_tx


# --- P4-4 real pyannote engine ----------------------------------------------
# A diarization TURN: ``(start_s, end_s, cluster_label)`` — the fine-timeline
# output of pyannote, normalized off its Annotation. ``cluster_label`` is a RAW
# pyannote cluster id (e.g. ``SPEAKER_00``), NEVER a canonical role (that is
# ``_cluster_to_role``'s job; ``normalize_role`` folds a stray raw label to
# ``unknown`` downstream if it ever leaked).
Turn = tuple[float, float, str]


# B3 — labeled-coverage floor. A segment whose LABELED speech covers less than this
# fraction of its OWN duration is mostly diarizer-silence: the "dominant" cluster
# speaks only a sliver, so a high purity there is meaningless (the denominator
# conflates diarizer-silence with missed speech). Below the floor we fail-SAFE — conf
# is degraded to 0.0 (→ ``unknown`` at P4-2). CALIBRATE-tunable on-box (the same
# --calibrate pass that tunes purity/match thresholds); ship conservative-low.
_MIN_LABELED_COVERAGE = 0.3


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Overlap (seconds) of ``[a0, a1]`` with ``[b0, b1]``, clamped at 0."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def _partition_valid_turns(turns: list[Turn]) -> tuple[list[Turn], int]:
    """Split ``turns`` into (valid, n_invalid). A turn is INVALID (B1) when a bound
    is non-finite (NaN/±inf) or ``end <= start`` — such a turn would otherwise skew
    the overlap math (a NaN bound can claim an entire segment). Counted so the caller
    can log the drop (observability), never silently swallowed."""
    valid: list[Turn] = []
    n_invalid = 0
    for t0, t1, cluster in turns:
        if math.isfinite(t0) and math.isfinite(t1) and t1 > t0:
            valid.append((t0, t1, cluster))
        else:
            n_invalid += 1
    return valid, n_invalid


def _merge_intervals(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Union a cluster's (start, end) spans into NON-OVERLAPPING intervals (B2), so
    duplicate / overlapping turns of the SAME cluster are counted ONCE (else the same
    speech-time double-counts and can inflate purity above threshold). Inputs are
    pre-validated finite + ``end > start``."""
    if not spans:
        return []
    ordered = sorted(spans)
    merged = [ordered[0]]
    for s0, s1 in ordered[1:]:
        m0, m1 = merged[-1]
        if s0 <= m1:                      # overlap / touch → extend the run
            merged[-1] = (m0, max(m1, s1))
        else:
            merged.append((s0, s1))
    return merged


def _dominant_cluster_over_intervals(
    intervals: list[tuple[float, float]], turns: list[Turn],
) -> tuple[str | None, float]:
    """MAX-OVERLAP alignment of one SEGMENT (given as a list of timed intervals)
    onto the diarization ``turns``. Returns ``(dominant_cluster, purity)``.

    ``intervals`` is the segment's constituent spans — the single ``[start, end]``
    span in P4-4, or per-WORD spans when P4-5 threads STT word timings (the reason
    this is interval-list-shaped, not a single span: word-level slots in with no
    redesign). Per cluster, its turns are MERGED (B2 — same-cluster overlaps counted
    once) then overlapped against the segment's spans; the DOMINANT cluster is the
    argmax (ties → sorted cluster label, deterministic). PURITY = dominant /
    total-LABELED-overlap ∈ [0, 1] — the dominant cluster's SHARE of the segment's
    overlapped speech (silence / unlabeled gaps are NOT in the denominator, so
    trailing silence does not dilute a clean segment; a genuine STRADDLE does).

    Fail-SAFE guards, all → conf-degrading direction:
      * INVALID turns (non-finite / end<=start) are dropped (B1) — defensive belt;
        the caller pre-filters + logs the count.
      * a zero / non-finite segment interval contributes nothing (no NaN).
      * a segment overlapping NO valid turn → ``(None, 0.0)``.
      * LABELED-COVERAGE below :data:`_MIN_LABELED_COVERAGE` → ``(dominant, 0.0)`` —
        a mostly-silence segment never claims high purity (B3)."""
    seg_ivs = [
        (i0, i1) for i0, i1 in intervals
        if math.isfinite(i0) and math.isfinite(i1) and i1 > i0
    ]
    if not seg_ivs:
        return None, 0.0  # zero / non-finite segment → unknown, purity 0 (no NaN)
    seg_duration = sum(i1 - i0 for i0, i1 in seg_ivs)

    valid_turns, _ = _partition_valid_turns(turns)  # B1 belt (caller already filtered)
    by_cluster_spans: dict[str, list[tuple[float, float]]] = {}
    for t0, t1, cluster in valid_turns:
        by_cluster_spans.setdefault(cluster, []).append((t0, t1))

    by_cluster: dict[str, float] = {}
    for cluster, spans in by_cluster_spans.items():
        tot = 0.0
        for m0, m1 in _merge_intervals(spans):        # B2 — merge before aggregating
            for i0, i1 in seg_ivs:
                tot += _overlap(i0, i1, m0, m1)
        if tot > 0.0:
            by_cluster[cluster] = tot
    if not by_cluster:
        return None, 0.0  # no labeled speech overlaps this segment → unknown, purity 0

    labeled = sum(by_cluster.values())
    # Deterministic argmax: sorted-label order so ties resolve to the lexicographically
    # smallest label (never dict-insertion-order-dependent).
    dominant = max(sorted(by_cluster), key=by_cluster.__getitem__)
    purity = by_cluster[dominant] / labeled if labeled > 0.0 else 0.0

    coverage = labeled / seg_duration if seg_duration > 0.0 else 0.0
    if coverage < _MIN_LABELED_COVERAGE:
        return dominant, 0.0  # B3 — mostly silence: fail-safe (→ unknown at P4-2)
    return dominant, purity


def _guard_conf(purity: float) -> float:
    """Clamp a purity to a valid ``speaker_conf`` ∈ [0, 1]; map non-finite
    (NaN/±inf) → 0.0. The engine must NEVER emit NaN/±inf conf (P4-2 carry-forward)
    — this is the AT-THE-SOURCE guard (the attribution layer demotes non-finite too,
    defense-in-depth). Low (0.0) is the fail-closed direction: it demotes to
    ``unknown`` at P4-2."""
    if purity is None or not math.isfinite(purity):
        return 0.0
    return min(1.0, max(0.0, purity))


def _cluster_to_role(cluster: str | None, config: ScribeConfig) -> str:
    """Resolve a raw diarization cluster to a canonical ROLE — fail-CLOSED.

    P4-4 END-STATE: clinician enrollment (P4-5) is NOT built, so there is no basis
    to claim a KNOWN role for any cluster → EVERY cluster resolves ``unknown``
    (un-attributed ≫ mis-attributed; the P4-2 flags + banner then fire). This is the
    HARD REQUIREMENT that the engine fail-close weak/ambiguous matches to ``unknown``
    AT RESOLUTION — with no enrollment, ALL matches are "weak". The multi-preset
    cluster→role registry lands in P4-5 and extends THIS single seam; do not build
    role matching here beyond the fail-safe.

    ⚠ P4-5 SEAM NOTE (C3): the ``cluster`` string is a CHUNK-LOCAL pyannote label
    (``SPEAKER_00`` in chunk 3 ≠ chunk 1's) — it is NOT stable across chunks, so P4-5
    MUST NOT key roles on the label. It re-anchors each chunk by EMBEDDING match
    against the stable clinician enrollment, which means P4-5 will need the per-cluster
    EMBEDDING (or the audio to re-derive it) threaded into this seam — NOT built here
    (this signature stays cluster+config until then).

    P4-5 STATUS: this stays the NO-ENROLLMENT fallback (``resolved is None``) — still
    all-``unknown``. The enrollment-anchored resolution is :func:`match_cluster_roles`
    (the K=2 clinician-anchor matcher), wired into :func:`_apply_diarization` when a
    preset resolved for the encounter."""
    return ROLE_UNKNOWN


# --- P4-5 K=2 clinician-anchor matcher (embedding-anchored, fail-closed) ------

@dataclass
class ClusterMatch:
    """Result of the K=2 clinician-anchor match over ONE chunk's clusters.

    ``roles`` maps each raw pyannote cluster label → its resolved role
    (``clinician`` for the single matched cluster, ``unknown`` for every other).
    ``best_cosine`` / ``separation`` are the match telemetry the pipeline records into
    the ``diarize_stats`` capture sink; ``matched`` is True iff a cluster cleared BOTH
    gates (tau + separation) and was anchored to ``clinician``."""

    roles: dict[str, str]
    best_cosine: float
    separation: float
    matched: bool


def match_cluster_roles(
    cluster_embeddings: dict[str, list[float]],
    centroids: list[list[float]],
    *, tau: float, delta: float,
) -> ClusterMatch:
    """K=2 clinician-anchor matcher (P4-5). Resolve a chunk's raw pyannote clusters to
    roles by EMBEDDING match against the ACTIVE preset's centroids — NEVER by the
    per-chunk-arbitrary cluster LABEL (C3: chunk 3's ``SPEAKER_00`` ≠ chunk 1's).

    Each cluster's score = MAX cosine over the preset's OWN centroid list (the clinician
    hypothesis — anchored to that centroid ONLY, never across profiles/users). The
    single highest-scoring cluster is resolved ``clinician`` IFF:
      * ``best >= tau`` (a strong, unambiguous match — fail-closed-HIGH), AND
      * ``best − second >= delta`` (SEPARATION from the next-best cluster — two clusters
        that BOTH look like the clinician are an ambiguous near-tie → fail-closed).
    EVERY other cluster → ``unknown``. We hold ONLY the clinician centroid, so a
    non-clinician cluster has NOTHING to positively match against and can never be
    asserted ``patient`` (un-attributed ≫ mis-attributed; the P4-2 layer + banner
    render the unknowns; patient attribution is a 5b-and-beyond question, never a
    this-isn't-the-clinician-so-it-must-be-the-patient INFERENCE).

    Deterministic: clusters are scored in sorted-label order so an exact-tie argmax
    resolves to the lexicographically smallest label (never dict-insertion-dependent).
    ``cosine`` fails-safe to 0.0 on a dim mismatch / non-finite, so a malformed
    embedding degrades to ``unknown`` rather than a spurious match."""
    if not cluster_embeddings or not centroids:
        # No clusters, or no enrolled centroid → nothing to anchor → all unknown.
        return ClusterMatch(
            roles={c: ROLE_UNKNOWN for c in cluster_embeddings},
            best_cosine=0.0, separation=0.0, matched=False,
        )
    scores: dict[str, float] = {}
    for label in sorted(cluster_embeddings):          # sorted → deterministic tie-break
        emb = cluster_embeddings[label]
        scores[label] = max((cosine(emb, c) for c in centroids), default=-1.0)
    winner = max(scores, key=scores.__getitem__)      # first max in sorted order
    best = scores[winner]
    others = [s for lbl, s in scores.items() if lbl != winner]
    second = max(others) if others else -1.0          # no competitor → unambiguous
    separation = best - second
    matched = best >= tau and separation >= delta
    roles = {lbl: ROLE_UNKNOWN for lbl in scores}
    if matched:
        roles[winner] = ROLE_CLINICIAN
    return ClusterMatch(roles=roles, best_cosine=best, separation=separation, matched=matched)


def _cluster_embeddings_for(
    config: ScribeConfig, audio_path: str | Path, turns: list[Turn],
) -> dict[str, list[float]]:
    """Per-cluster speaker EMBEDDING for the K=2 match — the ON-BOX PLACEHOLDER seam.

    Extracting a wespeaker embedding per pyannote cluster from the chunk audio (slice
    the cluster's turns → embed the pooled speech) is a P4-4 dependency: it needs the
    real torch embedder + cluster→audio slicing, which land on-box, not in torch-free
    CI. Until then this returns ``{}`` → :func:`match_cluster_roles` resolves ALL
    clusters ``unknown`` — IDENTICAL to the P4-4 all-unknown end-state, so wiring the
    matcher in changes NO on-box behavior yet, but the matcher + the ``resolved``
    threading + the capture seam are all in place for the extraction to slot into.
    The matcher itself is CI-covered against the ``embed_voice`` FAKE seam directly."""
    return {}


def _apply_diarization(
    config: ScribeConfig, chunk_tx: Transcript, turns: list[Turn],
    *, resolved: ResolvedEnrollment | None = None, audio_path: str | Path | None = None,
) -> Transcript:
    """Align ``turns`` onto ``chunk_tx``'s segments and commit speaker/cluster/conf.

    ATOMIC (NOTE-2): STAGE every segment's ``(cluster, conf, role)`` BEFORE COMMITTING
    any. The real engine can raise mid-iteration (torch OOM, a malformed turn); a
    half-labeled chunk must NOT fold. Turns are produced fully (in
    ``_run_pyannote_pipeline``) BEFORE this runs, and staging completes before the
    first mutation — so a raise leaves the chunk UNTOUCHED (speaker=None,
    diarized=False) and the pipeline folds it un-attributed. NOTE-3: touches ONLY
    ``speaker`` / ``speaker_cluster`` / ``speaker_conf`` — never text / id / bounds.
    Pure + torch-free (the pyannote-specific work is upstream), so CI covers it fully.

    P4-5 ROLE RESOLUTION: with ``resolved`` (a preset bound + resolved for the
    encounter), the K=2 clinician-anchor matcher (:func:`match_cluster_roles`) resolves
    the chunk's clusters to roles by EMBEDDING — the matched cluster → ``clinician``,
    every other → ``unknown``. Without ``resolved`` (no enrollment / a typed refusal
    upstream), EVERY cluster → ``unknown`` via :func:`_cluster_to_role` (the P4-4
    end-state). The per-cluster embedding extraction is the on-box placeholder
    (:func:`_cluster_embeddings_for`), so on-box this is still all-``unknown`` until the
    real extraction lands — the matcher is exercised in CI against the FAKE embed seam."""
    # B1 — drop invalid turns (non-finite / end<=start) ONCE, loudly, before the
    # per-segment loop (a NaN-bounds turn would otherwise skew every segment).
    valid_turns, n_invalid = _partition_valid_turns(turns)
    if n_invalid:
        log.warning(
            "scribe.diarize.invalid_turns_dropped",
            source_id=chunk_tx.source_id,
            dropped=n_invalid,
            total=len(turns),
            detail="pyannote turns with non-finite / end<=start bounds dropped "
                   "before alignment (a bad-bounds turn can skew overlap)",
        )
    # P4-5 — resolve the chunk's clusters to roles ONCE (the matcher needs ALL clusters
    # to compute best-vs-second separation). With enrollment → K=2 anchor; without →
    # the all-unknown fallback (``cluster_roles=None`` routes to ``_cluster_to_role``).
    cluster_roles: dict[str, str] | None = None
    match: ClusterMatch | None = None
    if resolved is not None:
        embeddings = _cluster_embeddings_for(config, audio_path, valid_turns)
        match = match_cluster_roles(
            embeddings, resolved.centroids,
            tau=config.diarize.match_threshold, delta=config.diarize.separation_margin,
        )
        cluster_roles = match.roles
    # STAGE — compute all assignments first (this is where a raise would happen).
    staged: list[tuple[str | None, float, str]] = []
    for seg in chunk_tx.segments:
        cluster, purity = _dominant_cluster_over_intervals(
            [(seg.start_s, seg.end_s)], valid_turns,
        )
        if cluster_roles is not None:
            role = cluster_roles.get(cluster, ROLE_UNKNOWN)   # unmatched cluster → unknown
        else:
            role = _cluster_to_role(cluster, config)          # no enrollment → unknown
        staged.append((cluster, _guard_conf(purity), role))
    # COMMIT — pure assignment, cannot raise. Only speaker/cluster/conf (NOTE-3).
    roles: list[str] = []
    for seg, (cluster, conf, role) in zip(chunk_tx.segments, staged):
        seg.speaker = role
        seg.speaker_cluster = cluster
        seg.speaker_conf = conf
        roles.append(role)
    chunk_tx.diarized = True
    log.info(
        "scribe.diarize.assigned",
        provider="pyannote",
        source_id=chunk_tx.source_id,
        segments=len(chunk_tx.segments),
        turns=len(valid_turns),
        clusters=len({t[2] for t in valid_turns}),
        enrolled=resolved is not None,                        # P4-5 — was a preset anchoring this?
        matched=(match.matched if match is not None else False),
        best_cosine=(round(match.best_cosine, 4) if match is not None else None),
        separation=(round(match.separation, 4) if match is not None else None),
        clinician=roles.count(ROLE_CLINICIAN),
        patient=roles.count(ROLE_PATIENT),
        other=roles.count(ROLE_OTHER),
        unknown=roles.count(ROLE_UNKNOWN),
    )
    return chunk_tx


def _turns_from_annotation(diarization) -> list[Turn]:
    """Normalize a pyannote ``Annotation`` to ``[(start_s, end_s, cluster), ...]``.

    Kept tiny + separate so the heavy engine boundary is a single seam: everything
    downstream is pure and torch-free."""
    turns: list[Turn] = []
    for segment, _track, label in diarization.itertracks(yield_label=True):
        turns.append((float(segment.start), float(segment.end), str(label)))
    return turns


# A2 — pipeline cache. pyannote model load is seconds; loading PER CHUNK (every ~30s
# sweep) is wasteful. Cache a lazy singleton keyed by the resolved pipeline_config
# path. THREAD-SAFETY (A1 moved diarize onto worker threads): a lock guards the
# expensive first-load; subsequent hits are lock-free atomic dict reads (GIL).
# Invalidated ONLY on path change — a materialized-config EDIT requires a process
# restart anyway (the systemd unit's pre-import HF_HUB_OFFLINE etc. are import-frozen),
# so process restart is the config-change boundary.
_PIPELINE_CACHE: dict[str, Any] = {}
_PIPELINE_CACHE_LOCK = threading.Lock()


def _validate_materialized_config_local(cfg_path: Path) -> None:
    """Compensating OFFLINE layer (A3): BEFORE importing pyannote, assert the
    materialized pipeline config's model references are EXISTING LOCAL PATHS — never
    repo ids. Closes the mispointed-config scenario (operator points pipeline_config
    at the snapshot's ORIGINAL config.yaml, whose ``pyannote/...`` repo-id fields
    would trigger a hub GET at from_pretrained). Fail-loud ``DiarizeError`` otherwise.

    Offline layering, honestly (the runtime ``os.environ['HF_HUB_OFFLINE']='1'`` set
    was DELETED — it is INERT: huggingface_hub freezes the constant at import, and STT
    imports hub before diarize; see stt.py's on-box proof). The REAL layers: (1) load
    from the materialized repo-id-free config [PRIMARY]; (2) THIS pre-import local-path
    validation [compensating]; (3) the SovereignHttpGuard requests/httpx wrap
    [backstop]; (4) the systemd unit's PRE-IMPORT ``Environment=HF_HUB_OFFLINE=1`` [the
    only working env belt]."""
    import yaml
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise DiarizeError(
            f"materialized pipeline config {cfg_path} is unreadable / not valid YAML: "
            f"{e}. Re-run scripts.stage_diarize_models."
        ) from e
    pipeline = cfg.get("pipeline")
    params = pipeline.get("params") if isinstance(pipeline, dict) else None
    if not isinstance(params, dict):
        raise DiarizeError(
            f"materialized pipeline config {cfg_path} has no 'pipeline.params' — it is "
            f"not a materialized pyannote config. Re-run scripts.stage_diarize_models."
        )
    for field in ("segmentation", "embedding"):
        val = params.get(field)
        if not isinstance(val, str) or not Path(val).exists():
            raise DiarizeError(
                f"materialized pipeline config {cfg_path}: pipeline.params.{field} = "
                f"{val!r} is not an EXISTING local path — it looks like a repo id or a "
                f"missing file. Loading it would risk a HuggingFace hub GET "
                f"(from_pretrained does not honor local_files_only). Point "
                f"pipeline_config at the MATERIALIZED config (not a snapshot's original "
                f"config.yaml) — re-run scripts.stage_diarize_models."
            )


def _load_pipeline_cached(cfg_path: Path):
    """Lazy, thread-safe, per-config-path singleton pyannote Pipeline (A2).

    Lazy-imports pyannote (torch heavy — never imported in CI). Double-checked
    locking: the fast path is a lock-free atomic dict read; only the first load per
    path takes the lock. Fail-loud on a None load (bad/incomplete materialized
    config)."""
    key = str(cfg_path)
    cached = _PIPELINE_CACHE.get(key)
    if cached is not None:
        return cached
    with _PIPELINE_CACHE_LOCK:
        cached = _PIPELINE_CACHE.get(key)   # re-check under the lock
        if cached is not None:
            return cached
        try:
            from pyannote.audio import Pipeline
        except ImportError as e:  # pragma: no cover — guarded by ensure_diarize_backend_available
            raise MissingDiarizeDependency(
                "pyannote.audio is not installed — install the [scribe-diarize] extra "
                "into the STAY-C venv."
            ) from e
        pipeline = Pipeline.from_pretrained(str(cfg_path))
        if pipeline is None:
            # from_pretrained returns None on a load failure instead of raising —
            # fail-loud rather than crash later on a None call. NOT cached.
            raise DiarizeError(
                f"pyannote Pipeline.from_pretrained({cfg_path}) returned None — the "
                f"materialized pipeline config is malformed or its local model paths "
                f"are missing. Re-run scripts.stage_diarize_models."
            )
        _PIPELINE_CACHE[key] = pipeline
        return pipeline


def _run_pyannote_pipeline(config: ScribeConfig, audio_path: str | Path) -> list[Turn]:
    """Load the OFFLINE pyannote pipeline (cached) + diarize ``audio_path`` → turns.

    Loads from the MATERIALIZED, repo-id-free ``diarize.pipeline_config`` (absolute
    local paths) — the PRIMARY offline mechanism, because ``from_pretrained`` does not
    reliably honor ``local_files_only``. The config's local-path model refs are
    VALIDATED pre-import (:func:`_validate_materialized_config_local`, the compensating
    offline layer); the pipeline itself is CACHED per path (:func:`_load_pipeline_cached`).
    Fail-LOUD (``DiarizeError``) when the config is unset/missing — a real engine with
    no offline config must never run (and must never risk a hub GET)."""
    pipeline_config = (config.diarize.pipeline_config or "").strip()
    if not pipeline_config:
        raise DiarizeError(
            "scribe diarize provider 'pyannote' requires a MATERIALIZED, repo-id-free "
            "pipeline config (scribe.diarize.pipeline_config) — run "
            "`python -m alfred.scripts.stage_diarize_models` on-box to download the "
            "models offline and materialize it. from_pretrained does not honor "
            "local_files_only, so the materialized local-path config is the offline "
            "load mechanism (fail-loud rather than risk a hub egress)."
        )
    cfg_path = Path(pipeline_config)
    if not cfg_path.is_file():
        raise DiarizeError(
            f"scribe diarize pipeline_config {cfg_path} does not exist — stage the "
            f"models + materialize it on-box (scripts.stage_diarize_models) before "
            f"enabling the pyannote engine."
        )
    _validate_materialized_config_local(cfg_path)   # pre-import offline validation (A3)
    pipeline = _load_pipeline_cached(cfg_path)       # cached load (A2)
    diarization = pipeline(str(audio_path))
    return _turns_from_annotation(diarization)


def _pyannote_diarize(
    config: ScribeConfig, audio_path: str | Path, chunk_tx: Transcript,
    *, resolved: ResolvedEnrollment | None = None,
) -> Transcript:
    """The real-engine path: run pyannote (heavy, on-box) → align + commit (pure).

    Split so the ONLY torch-touching call is ``_run_pyannote_pipeline``; the
    alignment/commit (``_apply_diarization``) is pure + fully CI-covered. Turns are
    materialized BEFORE ``_apply_diarization`` (NOTE-2 atomicity — a mid-iteration
    engine raise happens here, before any segment is touched). ``resolved`` (P4-5)
    threads the encounter's bound preset into the K=2 clinician-anchor matcher."""
    turns = _run_pyannote_pipeline(config, audio_path)
    return _apply_diarization(config, chunk_tx, turns, resolved=resolved, audio_path=audio_path)
