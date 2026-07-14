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
  * ``pyannote`` — the real on-box wespeaker embedding (P4-4 dependency); lazy,
    placeholder here (raises until the on-box engine ships).

ENGINE FINGERPRINT: a preset records the engine that produced its centroid so an
engine upgrade INVALIDATES it (embeddings-only custody). :func:`engine_fingerprint`
stamps from the RESOLVED runtime engine state — ``fake`` → a deterministic fake
stamp (dim + a fixed fake model id); ``pyannote`` → the resolved model id / revision
/ engine version (a P4-4 placeholder until the offline-materialized engine can be
introspected on-box). Stamped from the RESOLVED engine, NOT raw config, so an
unpinned revision still invalidates (parity with the diarize ``engine`` stamp).
"""

from __future__ import annotations

import hashlib
import math
import struct
from typing import Any

import structlog

from alfred.scribe.config import ScribeConfig

log = structlog.get_logger(__name__)

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


class MissingEmbedDependency(Exception):
    """The ``pyannote`` embedder is configured but the dependency isn't installed.

    Mirrors :class:`~alfred.scribe.diarize.MissingDiarizeDependency`; the
    ``off`` / ``fake`` providers never raise it (the enrollment face runs
    torch-free in CI + when diarize is disabled)."""


def _unit_normalize(vec: list[float]) -> list[float]:
    """Return ``vec`` scaled to unit L2 norm. A zero / non-finite vector maps to a
    fixed canonical unit vector (e1) rather than dividing by zero — fail-safe, and
    the enrollment load-contract's unit-norm check still passes."""
    norm = math.sqrt(sum(x * x for x in vec))
    if not math.isfinite(norm) or norm <= 0.0:
        canon = [0.0] * len(vec)
        if canon:
            canon[0] = 1.0
        return canon
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
        raise NotImplementedError(
            "pyannote voice embedding is a P4-4 dependency — the real wespeaker "
            "embedder loads from the offline-materialized engine on-box. P4-5a ships "
            "the fake seam; enrollment against the real engine lands with on-box "
            "verification."
        )
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
        # Placeholder until on-box: stamp from config (resolved-engine introspection
        # is a P4-4 dependency). ``engine_version`` is a marker the real accessor
        # replaces with the materialized engine's version.
        return {
            "embedding_model": config.diarize.embedding_model or "",
            "embedding_revision": config.diarize.embedding_revision or "",
            "engine_version": "pyannote-unresolved",  # replaced by the on-box accessor
        }
    return dict(_FAKE_ENGINE)
