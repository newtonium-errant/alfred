"""Tests for the embedder's L2-normalization contract.

The daemon's entity-link gate computes cosine similarity via
``np.dot(reg_vec, e_vec)`` ASSUMING the stored vectors are unit
length. The threshold (default 0.85) is documented as cosine.

Pre-fix bug: the single-chunk fast path in ``_get_embedding``
returned the raw API embedding from ``_embed_single`` without
normalizing — Ollama returns vectors with magnitude ~16 — so the
daemon's dot product on two single-chunk records produced values
like 277, the threshold gate became meaningless, and operator-
visible logs (``surveyor.entity_link_blocked_no_text_anchor
similarity=277.2776 threshold=0.85``) were alarming and
operator-confusing.

Post-fix contract: every vector returned by ``_get_embedding`` must
have ``||v|| ≈ 1.0`` so cosine similarity == dot product. Multi-
chunk path was already compliant; single-chunk path now matches.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import numpy as np
import pytest

from alfred.surveyor.embedder import Embedder


def _stub_embedder() -> Embedder:
    """Minimal Embedder instance without Milvus/HTTP setup.

    Mirrors the fixture used in test_embedder_batching.py — uses
    ``Embedder.__new__`` to skip __init__ entirely so we don't need
    a Milvus client.
    """
    e = Embedder.__new__(Embedder)
    e.api_key = None  # local Ollama path (matches KAL-LE / Salem prod)
    e.model = "nomic-embed-text"
    e.embedding_dims = 768
    e.embed_url = "http://localhost:11434/api/embeddings"
    e._http = None
    e._throttle_seconds = 0.0
    return e


@pytest.mark.asyncio
async def test_get_embedding_single_chunk_normalizes_to_unit_length(
    monkeypatch,
) -> None:
    """Single-chunk fast path must L2-normalize the API-returned vector.

    Pre-fix: raw vector returned, magnitude ~16 in production.
    Post-fix: ``||v|| ≈ 1.0`` regardless of API return magnitude.
    """
    e = _stub_embedder()

    # Mock _embed_single to return a non-normalized vector. The
    # actual Ollama response shape; magnitude here is sqrt(50) ≈ 7.07
    # so we have a definite signal that normalization happened (any
    # value !≈ 1.0 in the magnitude assertion would mean no
    # normalization).
    raw_vec = [3.0, 4.0, 5.0]  # magnitude = sqrt(9+16+25) = sqrt(50) ≈ 7.07
    e._embed_single = AsyncMock(return_value=raw_vec)

    result = await e._get_embedding("short text — single chunk")
    assert result is not None
    arr = np.asarray(result, dtype=np.float32)
    magnitude = float(np.linalg.norm(arr))
    assert abs(magnitude - 1.0) < 1e-5, (
        f"Single-chunk path returned non-unit vector (||v||={magnitude}); "
        "L2 normalization missing. Daemon's cosine-similarity gate would "
        "produce out-of-range values like 277."
    )

    # Direction preserved — the unit vector is the original / magnitude.
    expected = np.asarray(raw_vec, dtype=np.float32) / np.linalg.norm(raw_vec)
    np.testing.assert_allclose(arr, expected, rtol=1e-5)


@pytest.mark.asyncio
async def test_get_embedding_single_chunk_returns_none_on_api_failure(
    monkeypatch,
) -> None:
    """API failure → None propagates without numpy crash on the
    normalization step. Defensive: the new normalization branch must
    handle the None return from _embed_single before calling
    np.linalg.norm."""
    e = _stub_embedder()
    e._embed_single = AsyncMock(return_value=None)
    result = await e._get_embedding("short text")
    assert result is None


@pytest.mark.asyncio
async def test_get_embedding_single_chunk_zero_vector_passes_through(
    monkeypatch,
) -> None:
    """Edge: zero-magnitude vector (norm == 0) — division would NaN.
    The fix's ``if norm > 0.0`` guard preserves the zero vector
    rather than producing NaNs."""
    e = _stub_embedder()
    e._embed_single = AsyncMock(return_value=[0.0, 0.0, 0.0])
    result = await e._get_embedding("text")
    assert result is not None
    arr = np.asarray(result, dtype=np.float32)
    # Zero vector preserved; not NaN.
    assert not np.any(np.isnan(arr))
    assert float(np.linalg.norm(arr)) == 0.0


@pytest.mark.asyncio
async def test_get_embedding_already_normalized_vector_stays_unit(
    monkeypatch,
) -> None:
    """If the API returns an already-unit vector, normalization must
    NOT distort it (modulo float-roundoff). Confirms the fix is
    idempotent for providers that already L2-normalize on their side."""
    e = _stub_embedder()
    # Already unit: 1/sqrt(2), 1/sqrt(2), 0 → magnitude 1.0
    inv_sqrt2 = 1.0 / (2 ** 0.5)
    raw_vec = [inv_sqrt2, inv_sqrt2, 0.0]
    e._embed_single = AsyncMock(return_value=raw_vec)
    result = await e._get_embedding("text")
    arr = np.asarray(result, dtype=np.float32)
    np.testing.assert_allclose(
        arr, np.asarray(raw_vec, dtype=np.float32), rtol=1e-5,
    )
    assert abs(float(np.linalg.norm(arr)) - 1.0) < 1e-5


@pytest.mark.asyncio
async def test_two_normalized_vectors_dot_product_in_cosine_range(
    monkeypatch,
) -> None:
    """End-to-end contract: two vectors returned from _get_embedding,
    their dot product must lie in [-1, 1] (the cosine range that the
    daemon's threshold=0.85 gate assumes).

    This is the regression-pin for the operator-confusing 277 value
    the K3 cleanup ticket flagged. Pre-fix: dot products well above
    1.0 because raw API vectors were not unit length. Post-fix: dot
    products in [-1, 1] because both inputs are unit length.
    """
    e = _stub_embedder()
    # Two distinct raw vectors, both with non-unit magnitudes.
    e._embed_single = AsyncMock(side_effect=[
        [3.0, 4.0, 0.0],  # magnitude 5
        [0.0, 12.0, 5.0],  # magnitude 13
    ])
    v1 = await e._get_embedding("text one")
    v2 = await e._get_embedding("text two")
    a1 = np.asarray(v1, dtype=np.float32)
    a2 = np.asarray(v2, dtype=np.float32)
    dot = float(np.dot(a1, a2))
    assert -1.0 - 1e-5 <= dot <= 1.0 + 1e-5, (
        f"Dot product {dot} outside cosine range [-1, 1] — "
        "vectors not unit-length. Daemon's threshold gate would "
        "produce out-of-range similarity values."
    )
