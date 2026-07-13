"""Local multi-speaker diarization for the sovereign scribe (scribe P4-1).

The DIARIZER-WRITER half of P4: it resolves each transcript segment's
``speaker`` to a canonical ROLE ({clinician, patient, other, unknown}) and
latches ``Transcript.diarized``. The attribution-READER half (the mis-attribution
safety net) is P4-2 — NO consumer reads ``speaker`` yet this phase (the plumbing
+ the fake seam + the frozen data shapes ship first, fully CI-covered, before the
heavy engine).

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
purity (< 1.0); at the P4-2 attribution layer a sub-purity ``speaker_conf`` demotes
the role to ``unknown`` (``speaker_unverified``). So "split-on-straddle" is realized
as "straddle → low purity → unknown", which is fail-closed (un-attributed ≫
mis-attributed) and COMPOSES with the already-shipped safety net — no new mutation
surface, no boundary/text change. (The overlap core is word-CAPABLE — it aggregates
over a list of intervals per segment — so P4-5 can thread STT word timings for finer
purity without a redesign; P4-4 feeds it the single segment span.)

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
triggers a hub GET. ``HF_HUB_OFFLINE=1`` (set in the engine path) + the sovereign
requests-guard are the fail-closed BACKSTOPS. The sovereign boundary
(``_check_diarize_local``) independently refuses a non-local provider at load.
"""

from __future__ import annotations

import importlib.util
import math
import os
from pathlib import Path

import structlog

from alfred.scribe.config import ScribeConfig
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
    """
    provider = (config.diarize.provider or "").strip().lower()
    if (
        provider in _REAL_ENGINE_PROVIDERS
        and config.diarize.enabled
        and not _pyannote_available()
    ):
        raise MissingDiarizeDependency(
            f"scribe diarize provider {provider!r} (enabled) needs pyannote.audio, "
            f"which is not installed. Install the [scribe-diarize] extra into the "
            f"STAY-C venv (torch from the CPU wheel index). The 'off'/'fake' "
            f"providers — and a disabled pyannote (enabled:false) — need no dependency."
        )


def assign_speakers(
    config: ScribeConfig, audio_path: str | Path, chunk_tx: Transcript,
) -> Transcript:
    """Resolve per-segment speaker roles on ``chunk_tx`` — the pipeline entry.

    Dispatches on ``config.diarize.provider`` (all barrier-a-sibling-allowlisted).
    ``off`` returns the chunk untouched (no diarization); ``fake`` reads the
    sidecar; ``pyannote`` is P4-4. On success the transcript's ``diarized`` gate
    is latched. The pipeline wraps this call so any exception degrades to
    ``speaker=None`` and STILL folds (fail-open-for-availability).
    """
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
        return _pyannote_diarize(config, audio_path, chunk_tx)
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


def _overlap(a0: float, a1: float, b0: float, b1: float) -> float:
    """Overlap (seconds) of ``[a0, a1]`` with ``[b0, b1]``, clamped at 0."""
    return max(0.0, min(a1, b1) - max(a0, b0))


def _dominant_cluster_over_intervals(
    intervals: list[tuple[float, float]], turns: list[Turn],
) -> tuple[str | None, float]:
    """MAX-OVERLAP alignment of one SEGMENT (given as a list of timed intervals)
    onto the diarization ``turns``. Returns ``(dominant_cluster, purity)``.

    ``intervals`` is the segment's constituent spans — the single ``[start, end]``
    span in P4-4, or per-WORD spans when P4-5 threads STT word timings (the reason
    this is interval-list-shaped, not a single span: word-level slots in with no
    redesign). Overlap is summed per cluster across all of the segment's intervals;
    the DOMINANT cluster is the argmax (ties broken by sorted cluster label →
    deterministic). PURITY = dominant / total-labeled-overlap ∈ [0, 1] — the
    dominant cluster's SHARE of the segment's overlapped speech (silence / unlabeled
    gaps are NOT in the denominator, so trailing silence does not dilute a clean
    segment; a genuine speaker STRADDLE does). A segment overlapping NO turn (or a
    zero/negative-duration interval) → ``(None, 0.0)`` — never a division by zero,
    never a NaN."""
    by_cluster: dict[str, float] = {}
    for i0, i1 in intervals:
        if not (math.isfinite(i0) and math.isfinite(i1)) or i1 <= i0:
            continue  # zero/negative/non-finite interval contributes nothing (no NaN)
        for t0, t1, cluster in turns:
            ov = _overlap(i0, i1, t0, t1)
            if ov > 0.0:
                by_cluster[cluster] = by_cluster.get(cluster, 0.0) + ov
    if not by_cluster:
        return None, 0.0  # no labeled speech overlaps this segment → unknown, purity 0
    total = sum(by_cluster.values())
    # Deterministic argmax: iterate clusters in sorted-label order so ties resolve
    # to the lexicographically smallest label (never dict-insertion-order-dependent).
    dominant = max(sorted(by_cluster), key=by_cluster.__getitem__)
    purity = by_cluster[dominant] / total if total > 0.0 else 0.0
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
    role matching here beyond the fail-safe."""
    return ROLE_UNKNOWN


def _apply_diarization(
    config: ScribeConfig, chunk_tx: Transcript, turns: list[Turn],
) -> Transcript:
    """Align ``turns`` onto ``chunk_tx``'s segments and commit speaker/cluster/conf.

    ATOMIC (NOTE-2): STAGE every segment's ``(cluster, conf, role)`` BEFORE COMMITTING
    any. The real engine can raise mid-iteration (torch OOM, a malformed turn); a
    half-labeled chunk must NOT fold. Turns are produced fully (in
    ``_run_pyannote_pipeline``) BEFORE this runs, and staging completes before the
    first mutation — so a raise leaves the chunk UNTOUCHED (speaker=None,
    diarized=False) and the pipeline folds it un-attributed. NOTE-3: touches ONLY
    ``speaker`` / ``speaker_cluster`` / ``speaker_conf`` — never text / id / bounds.
    Pure + torch-free (the pyannote-specific work is upstream), so CI covers it fully."""
    # STAGE — compute all assignments first (this is where a raise would happen).
    staged: list[tuple[str | None, float, str]] = []
    for seg in chunk_tx.segments:
        cluster, purity = _dominant_cluster_over_intervals(
            [(seg.start_s, seg.end_s)], turns,
        )
        staged.append((cluster, _guard_conf(purity), _cluster_to_role(cluster, config)))
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
        turns=len(turns),
        clusters=len({t[2] for t in turns}),
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


def _run_pyannote_pipeline(config: ScribeConfig, audio_path: str | Path) -> list[Turn]:
    """Load the OFFLINE pyannote pipeline + diarize ``audio_path`` → turns.

    Lazy-imports pyannote (torch heavy — never imported in CI). Loads from the
    MATERIALIZED, repo-id-free ``diarize.pipeline_config`` (absolute local paths) —
    the primary offline mechanism, because ``from_pretrained`` does not reliably
    honor ``local_files_only``. ``HF_HUB_OFFLINE=1`` is set here as a BELT, and the
    sovereign requests-guard is the fail-closed BACKSTOP. Fail-LOUD (``DiarizeError``)
    when the materialized config is unset/missing — a real engine with no offline
    config must never boot (and must never risk a hub GET)."""
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
    # BELT: force HF offline in the engine path (the materialized local-path config
    # is the PRIMARY mechanism; this + the requests-guard are the backstops).
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        from pyannote.audio import Pipeline
    except ImportError as e:  # pragma: no cover — guarded by ensure_diarize_backend_available
        raise MissingDiarizeDependency(
            "pyannote.audio is not installed — install the [scribe-diarize] extra "
            "into the STAY-C venv."
        ) from e
    pipeline = Pipeline.from_pretrained(str(cfg_path))
    if pipeline is None:
        # from_pretrained returns None on a load failure (e.g. a bad/incomplete
        # materialized config) instead of raising — fail-loud rather than crash
        # later on a None call.
        raise DiarizeError(
            f"pyannote Pipeline.from_pretrained({cfg_path}) returned None — the "
            f"materialized pipeline config is malformed or its local model paths are "
            f"missing. Re-run scripts.stage_diarize_models."
        )
    diarization = pipeline(str(audio_path))
    return _turns_from_annotation(diarization)


def _pyannote_diarize(
    config: ScribeConfig, audio_path: str | Path, chunk_tx: Transcript,
) -> Transcript:
    """The real-engine path: run pyannote (heavy, on-box) → align + commit (pure).

    Split so the ONLY torch-touching call is ``_run_pyannote_pipeline``; the
    alignment/commit (``_apply_diarization``) is pure + fully CI-covered. Turns are
    materialized BEFORE ``_apply_diarization`` (NOTE-2 atomicity — a mid-iteration
    engine raise happens here, before any segment is touched)."""
    turns = _run_pyannote_pipeline(config, audio_path)
    return _apply_diarization(config, chunk_tx, turns)
