"""Local voice-embedding provider seam for the sovereign scribe (scribe P4-5a).

The speaker-embedding half of P4-5 enrollment: turns enrollment audio windows into
fixed-dim, UNIT-NORMALIZED embedding vectors. Same provider-seam shape as
``diarize.py`` (``off`` / ``fake`` / ``pyannote``) so the ENTIRE enrollment +
registry + cosine-match surface is CI-testable against the FAKE provider BEFORE the
real pyannote embedding engine (a P4-4 dependency) lands — nothing in P4-5a waits on
torch except real verdicts + calibration.

Providers (dispatch on ``config.diarize.provider`` — the embedder shares the diarize
ENGINE IDENTITY so a preset's engine fingerprint matches the runtime diarizer; an
engine upgrade invalidates presets, operator ruling 2):
  * ``off`` — inert; enrollment dormant (the config default).
  * ``fake`` — DETERMINISTIC: hash(window bytes) → seeded → :data:`EMBED_DIM`
    unit-normalized vector. No torch. The same bytes ALWAYS embed to the same
    vector, so centroid digests, load-contract validation, classification, and
    cosine matching are all reproducible in CI.
  * ``pyannote`` — the REAL on-box wespeaker embedding. Decodes each enrollment window
    (a webm/mp4/wav container) IN MEMORY via torchaudio (the diarize decode-fix path,
    reusing :func:`diarize._to_mono`), resamples to the wespeaker rate, and runs pyannote's
    cached speaker-embedding model (the SAME model the diarizer loads) → a 256-dim
    unit-normalized vector. Lazy torch (never imported in CI / the fake path). A per-window
    embed failure degrades (skip + log); an all-window failure raises → ``engine_error``.

ENGINE FINGERPRINT: a preset records the engine that produced its centroid so an
engine upgrade INVALIDATES it (embeddings-only custody). :func:`engine_fingerprint`
stamps from the RESOLVED runtime engine state — ``fake`` → a deterministic fake stamp
(dim + a fixed fake model id); ``pyannote`` → the model id + the staged checkpoint's
CONTENT digest (the resolved revision — not the raw, often-unset config revision, so an
unpinned revision still invalidates) + the installed ``pyannote.audio``/``torch`` version.
Deterministic + stable across restarts for the same staged model; changes on a
model/revision/engine-version change. Torch-free (a file hash + a metadata read).
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import math
import struct
import threading
from pathlib import Path
from typing import Any

import structlog

from alfred.scribe.config import ScribeConfig

log = structlog.get_logger(__name__)

# The wespeaker-voxceleb model runs at 16 kHz; the enrollment containers (webm/opus,
# mp4/AAC) are typically 48 kHz, so we resample to this before embedding. Confirmed
# on-box (the embedder does NOT resample a raw waveform the way the diarize pipeline
# does); flagged in the ship note as an on-box-validated constant.
_EMBED_SAMPLE_RATE = 16000

# Lazy, per-checkpoint-path singleton speaker-embedding model (mirrors diarize's
# _load_pipeline_cached): the heavy torch load happens once per staged model.
_EMBEDDER_CACHE: dict[str, Any] = {}
_EMBEDDER_CACHE_LOCK = threading.Lock()
# Serializes concurrent FORWARD PASSES on the shared cached embedder. P4-4 carry-forward
# checklist item 8 ("cached-pipeline concurrency is safe ONLY while the sweep serializes
# diarize — re-verify when enrollment-embed runs concurrently") goes LIVE with P4-5c: the
# diarize sweep now runs extraction (embed_waveform) on a worker thread while enroll finalize
# runs embed_windows on the SAME cached model from its own thread. The cache lock covers LOAD
# only; this covers INFERENCE. RTF impact negligible (diarize sweeps already serialize;
# enrollment is rare) — item 8 CLOSED.
_EMBED_INFERENCE_LOCK = threading.Lock()

# The embedding dimensionality (wespeaker-voxceleb-resnet34-LM is 256; the fake
# seam matches it so a fake-enrolled preset validates dim-consistently). The real
# dim is confirmed on-box (P4-4 dependency); if it differs, this constant + the
# fake seam move together.
EMBED_DIM = 256

# The embed provider set — MUST mirror the diarize provider set (the embedder rides
# the same engine identity). Pinned in tests.
SCRIBE_EMBED_PROVIDERS: frozenset[str] = frozenset({"off", "fake", "pyannote"})
_REAL_ENGINE_PROVIDERS: frozenset[str] = frozenset({"pyannote"})

# The fake engine's deterministic stamp (a stable fake model id + a version). A
# preset enrolled under the fake seam records THIS; changing it here would (by
# design) invalidate every fake preset — the CI analog of a real engine upgrade.
_FAKE_ENGINE = {
    "embedding_model": "fake-embed-v1",
    "embedding_revision": "fake",
    "engine_version": "fake-1",
}


class EmbedError(Exception):
    """Voice embedding failed — unknown provider, unusable input, engine failure."""


class DegenerateEmbeddingError(EmbedError):
    """A raw model embedding is DEGENERATE — zero L2 norm or a non-finite (NaN/±inf)
    component — so it cannot be unit-normalized without collapsing to the fixed canonical
    e1 vector. e1 is a SHARED ATTRACTOR: two independent degenerate embeddings (a muted-mic
    enrollment window, a near-silence extraction cluster) both coerce to e1 and score
    cosine=1.0 — a maximum-confidence WRONG attribution. Fail-closed instead: the extraction
    seam OMITS the cluster, the enrollment window path SKIPS the window (both + a log), and
    NEITHER canonicalizes. Raised by :func:`_unit_normalize`, the single normalization point."""


class MissingEmbedDependency(Exception):
    """The ``pyannote`` embedder is configured but the dependency isn't installed.

    Mirrors :class:`~alfred.scribe.diarize.MissingDiarizeDependency`; the
    ``off`` / ``fake`` providers never raise it (the enrollment face runs
    torch-free in CI + when diarize is disabled)."""


def _unit_normalize(vec: list[float]) -> list[float]:
    """Return ``vec`` scaled to unit L2 norm. A zero / non-finite vector is DEGENERATE and
    RAISES :class:`DegenerateEmbeddingError` rather than canonicalizing to e1 — canonicalizing
    is a shared attractor that can collide two silent failures at cosine=1.0 (max-confidence
    mis-attribution). The callers own the fail-closed degrade (extraction omits the cluster;
    enrollment skips the window). The deterministic fake embedder fills 256 hash-derived
    floats (norm ~9), so it never trips this — the fake path is unaffected."""
    norm = math.sqrt(sum(x * x for x in vec))
    if not math.isfinite(norm) or norm <= 0.0:
        raise DegenerateEmbeddingError(
            "raw embedding has zero / non-finite L2 norm — refusing to canonicalize to e1 "
            "(a shared attractor that collides at cosine=1.0); fail-closed."
        )
    return [x / norm for x in vec]


def _fake_embed(window: bytes) -> list[float]:
    """Deterministic unit-normalized :data:`EMBED_DIM` vector from ``window`` bytes.

    Reproducible by construction: SHA-256 of the bytes seeds a counter hash stream
    that fills the vector, then unit-normalize. Same bytes → same vector, so every
    downstream digest / match is CI-stable. NOT a real voiceprint — a fixture."""
    out: list[float] = []
    seed = hashlib.sha256(window).digest()
    counter = 0
    while len(out) < EMBED_DIM:
        block = hashlib.sha256(seed + struct.pack(">I", counter)).digest()
        # 8 float32-ish values per 32-byte block (4 bytes each → [-1, 1)).
        for i in range(0, len(block), 4):
            if len(out) >= EMBED_DIM:
                break
            u = struct.unpack(">I", block[i:i + 4])[0]
            out.append((u / 0xFFFFFFFF) * 2.0 - 1.0)
        counter += 1
    return _unit_normalize(out)


def embed_windows(config: ScribeConfig, windows: list[bytes]) -> list[list[float]]:
    """Embed each enrollment audio ``window`` → a unit-normalized vector.

    Dispatches on ``config.diarize.provider``. ``off`` is inert (raises — the caller
    gates enrollment on a non-off provider); ``fake`` is the deterministic CI seam;
    ``pyannote`` is the real engine (P4-4 placeholder). The per-window vectors are
    combined into a preset centroid by the enrollment store (trimmed spherical
    mean) — this layer only embeds."""
    provider = (config.diarize.provider or "").strip().lower()
    if provider == "fake":
        return [_fake_embed(w) for w in windows]
    if provider == "pyannote":
        return _pyannote_embed_windows(config, windows)
    if provider == "off":
        raise EmbedError(
            "voice embedding requested with diarize.provider='off' — enrollment is "
            "dormant. Set a real (pyannote) or fake provider to enroll."
        )
    # Defense in depth: the sovereign boundary already refuses a non-local diarize
    # provider at load; this dispatch fails closed too.
    raise EmbedError(
        f"scribe embed provider {provider or '(unset)'!r} is not a local backend "
        f"({', '.join(sorted(SCRIBE_EMBED_PROVIDERS))})."
    )


# --- the real on-box wespeaker embedder (P4-4 dependency; lazy torch) --------------


def _resolve_embedding_model_path(config: ScribeConfig) -> Path:
    """The staged wespeaker checkpoint path — the SAME model the diarizer loads.

    Read from the materialized, repo-id-free ``diarize.pipeline_config``'s
    ``pipeline.params.embedding`` (an absolute LOCAL path written by
    ``scripts.stage_diarize_models``). Torch-free (a YAML read + a path check), so the
    engine fingerprint resolves without loading torch. Fail-loud when unset/missing — a
    real engine with no staged model must never run (and the daemon boot gate already
    refuses this state, so a raise here can only surface a genuine misconfig)."""
    import yaml  # pyyaml is a base dep (torch-free)

    pipeline_config = (config.diarize.pipeline_config or "").strip()
    if not pipeline_config:
        raise EmbedError(
            "pyannote voice embedding requires a materialized diarize.pipeline_config "
            "(run scripts.stage_diarize_models on-box)."
        )
    cfg_path = Path(pipeline_config)
    if not cfg_path.is_file():
        raise EmbedError(f"diarize.pipeline_config {cfg_path} does not exist.")
    try:
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as e:
        raise EmbedError(f"pipeline config {cfg_path} unreadable: {e}") from e
    pipeline = cfg.get("pipeline")
    params = pipeline.get("params") if isinstance(pipeline, dict) else None
    emb = params.get("embedding") if isinstance(params, dict) else None
    if not isinstance(emb, str) or not Path(emb).exists():
        raise EmbedError(
            f"pipeline config {cfg_path}: pipeline.params.embedding = {emb!r} is not an "
            f"existing local path — re-run scripts.stage_diarize_models."
        )
    return Path(emb)


def _load_embedder_cached(model_path: Path):
    """Lazy, thread-safe, per-path singleton speaker-embedding model.

    Lazy-imports pyannote (torch heavy — NEVER imported in CI). Double-checked locking:
    the fast path is a lock-free dict read; only the first load per path takes the lock.
    Uses ``PretrainedSpeakerEmbedding`` (pyannote's speaker-verification embedding API)
    on the local wespeaker checkpoint — the same model the diarize pipeline loads.

    ⚠ ON-BOX-VALIDATED API SEAM: the exact load path (``PretrainedSpeakerEmbedding`` vs
    ``Model.from_pretrained`` vs ``Inference``) is confirmed on-box; flagged in the ship
    note. If wespeaker needs a different loader, THIS is the one function to change."""
    key = str(model_path)
    cached = _EMBEDDER_CACHE.get(key)
    if cached is not None:
        return cached
    with _EMBEDDER_CACHE_LOCK:
        cached = _EMBEDDER_CACHE.get(key)  # re-check under the lock
        if cached is not None:
            return cached
        try:
            from pyannote.audio.pipelines.speaker_verification import (
                PretrainedSpeakerEmbedding,
            )
        except ImportError as e:  # pragma: no cover — guarded by the daemon boot gate
            raise MissingEmbedDependency(
                "pyannote.audio is not installed — install the [scribe-diarize] extra "
                "into the STAY-C venv."
            ) from e
        embedder = PretrainedSpeakerEmbedding(str(model_path))
        _EMBEDDER_CACHE[key] = embedder
        return embedder


def _embed_tensor(embedder: Any, waveform: Any, sample_rate: int) -> list[float]:
    """Shared tensor→vector embedding core: mono-downmix (:func:`diarize._to_mono`),
    resample to the wespeaker rate, run the cached embedder, unit-normalize → a 256-dim
    vector. Used by BOTH the enrollment window path (:func:`_pyannote_embed_windows`, after
    its per-window CONTAINER decode) and the P4-5c per-cluster extraction path
    (:func:`embed_waveform`, on an already-decoded chunk slice — so extraction never
    re-encodes). torch/torchaudio are lazy-imported by the caller's seam; the resample import
    stays inside it. Raises on a decode/model failure — the callers own the degrade policy
    (per-window skip for enrollment; per-cluster omit / fail-open for extraction)."""
    import torchaudio

    from alfred.scribe.diarize import _to_mono  # the decode-fix downmix (reuse)

    waveform = _to_mono(waveform)                              # (C, T) -> (1, T)
    if sample_rate != _EMBED_SAMPLE_RATE:
        waveform = torchaudio.functional.resample(waveform, sample_rate, _EMBED_SAMPLE_RATE)
    # PretrainedSpeakerEmbedding wants (batch, channel, samples).
    with _EMBED_INFERENCE_LOCK:                              # P4-4 item 8 — serialize forwards
        emb = embedder(waveform.unsqueeze(0))                # -> (1, dim)
    return _unit_normalize([float(x) for x in _as_row(emb)])


def embed_waveform(config: ScribeConfig, waveform: Any, sample_rate: int) -> list[float]:
    """Embed an ALREADY-DECODED mono waveform slice → a 256-dim unit vector, via the SAME
    staged wespeaker model as enrollment (the P4-5c per-cluster extraction seam).

    The extraction (:func:`diarize._cluster_embeddings_for`) decodes the chunk ONCE (the P4-4
    ``diarize._decode_audio`` path), slices + pools a cluster's speech, and hands the pooled
    tensor here — so this does NOT re-decode; it runs the torch-free config gate FIRST (a
    missing staged model fails as a clean :class:`EmbedError`, never an ImportError), loads
    the cached embedder, and runs the shared :func:`_embed_tensor` core. Real engine ONLY —
    the fake extraction path embeds byte payloads via :func:`embed_windows`, never here."""
    provider = (config.diarize.provider or "").strip().lower()
    if provider != "pyannote":
        raise EmbedError(
            f"embed_waveform is the real-engine (pyannote) extraction seam; got provider "
            f"{provider or '(unset)'!r}. The fake path embeds byte windows via embed_windows."
        )
    model_path = _resolve_embedding_model_path(config)   # torch-free config gate FIRST
    embedder = _load_embedder_cached(model_path)
    return _embed_tensor(embedder, waveform, sample_rate)


def _pyannote_embed_windows(
    config: ScribeConfig, windows: list[bytes]
) -> list[list[float]]:
    """Embed each enrollment window (a webm/mp4/wav CONTAINER blob) → a 256-dim
    unit-normalized vector, matching the fake seam's exact output contract.

    Decodes IN MEMORY (RAM custody — no temp file) via torchaudio's ffmpeg backend, the
    same decoder as the diarize decode fix, reusing :func:`diarize._to_mono` for the
    mono downmix; resamples to the wespeaker rate; runs the cached embedder. torch /
    torchaudio / pyannote are lazy-imported HERE so CI + the fake path stay torch-free.

    DEGRADE, DON'T CRASH: a single window too short / undecodable to embed is SKIPPED
    (logged), not fatal — only if EVERY window fails do we raise :class:`EmbedError`,
    which the finalize worker maps to the ``engine_error`` verdict (bytes already cleared
    in its ``finally``; no raw audio leaks)."""
    # (1) config gate FIRST — torch-free, so a missing staged model fails as a clean
    # EmbedError, never an ImportError, and CI never reaches the torch import here.
    model_path = _resolve_embedding_model_path(config)
    # (2) heavy deps, lazy (never imported in CI / the fake path). The mono downmix +
    # resample live in the shared _embed_tensor core (which lazy-imports them too).
    try:
        import io

        import torchaudio
    except ImportError as e:  # pragma: no cover — the daemon boot gate guards this on-box
        raise MissingEmbedDependency(
            "torchaudio is not installed — install the [scribe-diarize] extra into the "
            "STAY-C venv."
        ) from e
    embedder = _load_embedder_cached(model_path)
    out: list[list[float]] = []
    for i, w in enumerate(windows):
        try:
            waveform, sr = torchaudio.load(io.BytesIO(w))          # container -> (C, T)
            vec = _embed_tensor(embedder, waveform, sr)            # -> unit-norm 256
        except Exception as e:  # noqa: BLE001 — a bad window degrades, never crashes the batch
            log.warning(
                "scribe.embed.window_skipped",
                window_index=i,
                error_class=type(e).__name__,      # class only — NO PHI, NO raw bytes
                detail="window too short / undecodable to embed — skipped (degrade)",
            )
            continue
        out.append(vec)
    if not out:
        # EVERY window failed → an engine/decode problem, not a per-window blip.
        raise EmbedError(
            "no enrollment window could be embedded (all too short / undecodable)."
        )
    return out


def _as_row(emb: Any) -> list[float]:
    """Coerce a ``(1, dim)`` embedder output (numpy array / torch tensor) → a flat
    ``list[float]`` of the single row. Tolerates a ``(dim,)`` result too."""
    row = emb[0] if getattr(emb, "ndim", 1) == 2 else emb
    return [float(x) for x in row]


def _checkpoint_digest(model_path: Path) -> str:
    """A stable content digest of the staged embedding checkpoint — the RESOLVED
    revision (not the raw, often-unset config ``embedding_revision``). Same staged file
    → same digest across restarts; a re-download of a different checkpoint → a different
    digest → presets correctly invalidate. Torch-free. A directory (the fallback
    materialized path) digests a sorted (name, size) manifest of its files."""
    p = Path(model_path)
    h = hashlib.sha256()
    if p.is_dir():
        for f in sorted(p.rglob("*")):
            if f.is_file():
                h.update(f.name.encode("utf-8"))
                h.update(str(f.stat().st_size).encode("utf-8"))
        return "dir:" + h.hexdigest()
    with p.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def _engine_version() -> str:
    """The RESOLVED engine version — the actually-installed ``pyannote.audio`` + ``torch``
    dist versions, read from package METADATA (``importlib.metadata`` does NOT import the
    package, so this stays torch-free). Changes on an engine upgrade → presets invalidate.
    In torch-free CI the packages are absent → a ``?`` marker (the pyannote fingerprint is
    only resolved on-box, where the engine is present)."""
    parts = []
    for pkg in ("pyannote.audio", "torch"):
        try:
            parts.append(f"{pkg}=={importlib.metadata.version(pkg)}")
        except importlib.metadata.PackageNotFoundError:
            parts.append(f"{pkg}==?")
    return ";".join(parts)


def _pyannote_fingerprint(config: ScribeConfig) -> dict[str, Any]:
    """The RESOLVED runtime embedding-engine stamp (replaces the P4-4 placeholder).

    Deterministic + STABLE across restarts for the same staged model + install (so a
    restart never spuriously invalidates presets); CHANGES on a model / revision /
    engine-version change (so an upgrade correctly invalidates). Stamped from the staged
    engine STATE — the checkpoint content digest + the installed engine version — NOT the
    raw config revision (which is often unset and would never invalidate).

    DEGRADES (never raises) when NO model is staged: ``provider=pyannote + enabled:false``
    boots WITHOUT a staged model (the boot gate only requires it when the engine will run),
    yet the pipeline still stamps provenance on such (un-diarized) encounters — so this
    accessor must not crash a read/provenance path. The degraded stamp is STABLE and
    (correctly) differs from a resolved one, and no preset can ever carry it (enrollment
    fail-louds without a model), so it drives no real match. ``embed_windows`` stays
    fail-loud — you cannot EMBED without the model, but you can stamp 'engine not resolved'."""
    model = config.diarize.embedding_model or ""
    version = _engine_version()
    try:
        model_path = _resolve_embedding_model_path(config)
    except EmbedError:
        return {"embedding_model": model, "embedding_revision": "", "engine_version": version}
    return {
        "embedding_model": model,
        "embedding_revision": _checkpoint_digest(model_path),
        "engine_version": version,
    }


def engine_fingerprint(config: ScribeConfig) -> dict[str, Any]:
    """The RESOLVED runtime embedding-engine stamp for a preset's ``engine`` block.

    ``fake`` → the deterministic fake stamp (so fake presets validate + match in
    CI). ``pyannote`` → the resolved model id / revision / engine version — a P4-4
    PLACEHOLDER (the offline-materialized engine is introspected on-box; until then
    it mirrors the config's ``embedding_model`` / ``embedding_revision`` so an
    unpinned revision still records SOMETHING invalidating). ``off`` → the fake
    stamp is never used (enrollment is dormant), but return a stable stamp so
    fingerprint comparisons never crash on a dormant config."""
    provider = (config.diarize.provider or "").strip().lower()
    if provider == "pyannote":
        return _pyannote_fingerprint(config)
    return dict(_FAKE_ENGINE)
