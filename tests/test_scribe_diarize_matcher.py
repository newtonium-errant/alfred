"""P4-5 K=2 clinician-anchor matcher — contract tests (fake embed seam only).

Binds the FROZEN CONTRACT for :func:`match_cluster_roles`: anchor the single
best-matching cluster to ``clinician`` IFF it clears BOTH tau (strong match) AND the
separation delta (unambiguous vs the next cluster); every other cluster → ``unknown``
(we hold ONLY the clinician centroid — a non-clinician cluster is never asserted
patient). Anchored by EMBEDDING, never the per-chunk-arbitrary label. The real
per-cluster embedding extraction from pyannote is the on-box placeholder; the matcher
is exercised here against constructed unit vectors + the embed_voice FAKE seam.
"""

from __future__ import annotations

import math

import pytest

from alfred.scribe import embed_voice
from alfred.scribe import enrollment as en
from alfred.scribe.config import ScribeConfig, ScribeDiarizeConfig
from alfred.scribe.diarize import match_cluster_roles
from alfred.scribe.transcript import ROLE_CLINICIAN, ROLE_UNKNOWN

_TAU = 0.75
_DELTA = 0.15
_DIM = embed_voice.EMBED_DIM


def _vec(cos: float) -> list[float]:
    """A ``_DIM`` unit vector whose cosine to the reference centroid ``_e1`` is ``cos``.
    Built as ``[cos, sqrt(1-cos^2), 0, 0, ...]`` — unit-norm, so cosine(_vec(x), e1)==x."""
    v = [0.0] * _DIM
    v[0] = cos
    v[1] = math.sqrt(max(0.0, 1.0 - cos * cos))
    return v


def _e1() -> list[float]:
    v = [0.0] * _DIM
    v[0] = 1.0
    return v


_CENTROIDS = [_e1()]      # the enrolled clinician centroid (a single unit vector)


def _match(embeddings, *, tau=_TAU, delta=_DELTA, centroids=None):
    return match_cluster_roles(embeddings, centroids or _CENTROIDS, tau=tau, delta=delta)


# --- the happy path: one clinician cluster, one other -----------------------

def test_best_cluster_clears_both_gates_is_clinician():
    m = _match({"SPEAKER_00": _vec(0.90), "SPEAKER_01": _vec(0.10)})
    assert m.matched is True
    assert m.roles == {"SPEAKER_00": ROLE_CLINICIAN, "SPEAKER_01": ROLE_UNKNOWN}
    assert round(m.best_cosine, 3) == 0.90 and round(m.separation, 3) == 0.80


def test_non_clinician_cluster_is_unknown_never_patient():
    # The complement cluster is UNKNOWN (we hold only the clinician centroid) — never
    # inferred patient. This is the load-bearing fail-closed direction.
    m = _match({"A": _vec(0.95), "B": _vec(0.05)})
    assert m.roles["B"] == ROLE_UNKNOWN
    assert ROLE_CLINICIAN not in [m.roles["B"]]


# --- tau boundary ------------------------------------------------------------

def test_below_tau_no_match_all_unknown():
    # best 0.70 < tau 0.75 → NO clinician, even with a clean separation.
    m = _match({"A": _vec(0.70), "B": _vec(0.10)})
    assert m.matched is False
    assert all(r == ROLE_UNKNOWN for r in m.roles.values())


def test_above_tau_matches():
    m = _match({"A": _vec(0.80), "B": _vec(0.10)})
    assert m.matched is True and m.roles["A"] == ROLE_CLINICIAN


def test_tau_boundary_is_inclusive():
    # cosine(v, v) == 1.0 exactly; with tau set to 1.0 the ``>= tau`` boundary must
    # ACCEPT (inclusive), pinning the boundary direction without float-edge flakiness.
    c = _vec(0.5)
    m = match_cluster_roles({"A": list(c)}, [c], tau=1.0, delta=_DELTA)
    assert m.matched is True and m.roles["A"] == ROLE_CLINICIAN
    # a strictly-smaller cosine at tau=1.0 → rejected.
    m2 = match_cluster_roles({"A": _vec(0.999)}, _CENTROIDS, tau=1.0, delta=_DELTA)
    assert m2.matched is False


# --- separation / delta (the ambiguity guard) --------------------------------

def test_separation_below_delta_is_ambiguous_fail_closed():
    # both clusters clear tau, but within delta of each other → ambiguous near-tie →
    # NO clinician (can't tell which voice is the clinician) → all unknown.
    m = _match({"A": _vec(0.90), "B": _vec(0.80)})     # sep 0.10 < delta 0.15
    assert m.matched is False
    assert all(r == ROLE_UNKNOWN for r in m.roles.values())


def test_separation_at_delta_matches():
    m = _match({"A": _vec(0.90), "B": _vec(0.70)})     # sep 0.20 >= delta 0.15
    assert m.matched is True and m.roles["A"] == ROLE_CLINICIAN


def test_delta_boundary_is_inclusive():
    # The contract says `best - second >= delta`. Pin the DIRECTION float-exactly (an
    # implementation using strict `>` would fail-closed at exactly the calibrated margin,
    # silently dropping clinician matches that sit on the ratified boundary).
    # scores 1.0 and 0.0 → separation is EXACTLY 1.0; with delta=1.0 the boundary must ACCEPT.
    c = _e1()
    m = match_cluster_roles({"A": list(c), "B": _vec(0.0)}, [c], tau=0.75, delta=1.0)
    assert m.separation == pytest.approx(1.0)
    assert m.matched is True and m.roles["A"] == ROLE_CLINICIAN
    # a hair MORE than the separation → rejected (proves the comparison is live).
    m2 = match_cluster_roles({"A": list(c), "B": _vec(0.0)}, [c], tau=0.75, delta=1.0001)
    assert m2.matched is False


def test_exact_tie_is_ambiguous_all_unknown():
    # two clusters with IDENTICAL score → separation 0 < delta → fail-closed; the
    # roles are deterministic (all unknown) regardless of label order.
    m = _match({"SPEAKER_01": _vec(0.90), "SPEAKER_00": _vec(0.90)})
    assert m.matched is False
    assert m.roles == {"SPEAKER_00": ROLE_UNKNOWN, "SPEAKER_01": ROLE_UNKNOWN}


# --- K counts: single cluster, empty, no-centroid, dim mismatch -------------

def test_single_cluster_matches_when_over_tau():
    # one voice, clears tau, no competitor → clinician (separation vacuously satisfied).
    m = _match({"ONLY": _vec(0.88)})
    assert m.matched is True and m.roles == {"ONLY": ROLE_CLINICIAN}


def test_single_cluster_below_tau_is_unknown():
    m = _match({"ONLY": _vec(0.40)})
    assert m.matched is False and m.roles == {"ONLY": ROLE_UNKNOWN}


def test_no_clusters_empty_roles():
    m = _match({})
    assert m.roles == {} and m.matched is False


def test_no_centroids_all_unknown():
    m = match_cluster_roles({"A": _vec(0.99)}, [], tau=_TAU, delta=_DELTA)
    assert m.matched is False and m.roles == {"A": ROLE_UNKNOWN}


def test_dim_mismatch_embedding_fails_safe_to_unknown():
    # a malformed embedding (wrong length) → cosine 0.0 (fail-safe) → below tau → unknown,
    # never a spurious match.
    m = _match({"A": [1.0, 0.0, 0.0]})     # 3-dim vs 256-dim centroid
    assert m.roles["A"] == ROLE_UNKNOWN and m.matched is False


# --- the actual embed_voice FAKE seam (end-to-end vector realism) ------------

def _fake_cfg() -> ScribeConfig:
    return ScribeConfig(diarize=ScribeDiarizeConfig(provider="fake"))


def test_fake_embed_seam_self_match_is_clinician():
    # Enroll a centroid from one window via the REAL fake embed provider; the SAME
    # window's embedding matches it (cosine 1.0) → clinician, while a different window
    # (an independent pseudo-random unit vector) is well-separated → unknown.
    cfg = _fake_cfg()
    clinician_window = b"clinician-voice-window-bytes"
    patient_window = b"a-different-voice-entirely!!"
    centroid = en.spherical_mean_centroid(
        embed_voice.embed_windows(cfg, [clinician_window]))
    clin_emb = embed_voice.embed_windows(cfg, [clinician_window])[0]
    pat_emb = embed_voice.embed_windows(cfg, [patient_window])[0]
    m = match_cluster_roles(
        {"SPEAKER_00": clin_emb, "SPEAKER_01": pat_emb}, [centroid],
        tau=_TAU, delta=_DELTA,
    )
    assert m.roles["SPEAKER_00"] == ROLE_CLINICIAN     # self-match cosine ~1.0
    assert m.roles["SPEAKER_01"] == ROLE_UNKNOWN       # independent vector, low cosine
    assert m.matched is True
