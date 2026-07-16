"""P4-5c FIX ROUND — pins for the 12 QA-confirmed findings (F1-F12).

Cohesive delta for the QA panel: every fix-round pin lives here (except F12, a stale-comment
fix in test_scribe_p45_fixround.py). Grouped by finding. The torch-free CI half is pinned
here (fake seam + mocked decode/torch/embedder); the real wespeaker forward pass stays the
on-box IT in test_scribe_diarize_engine.py.

  F1  [HIGH] degenerate (NaN/zero) embedding e1 attractor — both normalize seams RAISE now
  F2  single-cluster chunk conf must not be a vacuous 1.0 — cap at best_cosine
  F3/F5 the REAL pyannote-seam dim belt (was unbound)
  F4  pipeline stamp hop match_sink -> diarize_stats row (was unbound)
  F6  merge-then-floor composition (net from UNMERGED spans double-counts)
  F7/F9/F10 min_turn_s coupling — BOTH production call sites now bound
  F8  5b match_rate same-population numerator (role_counts_eligible)
  F11 shared-embedder inference lock (P4-4 carry-forward item 8, now live)
"""
from __future__ import annotations

import json
import math
import sys
import types

import pytest
import structlog

from alfred.scribe import diarize as diarize_mod
from alfred.scribe import embed_voice
from alfred.scribe import enrollment
from alfred.scribe import pipeline as pl
from alfred.scribe.config import load_from_unified
from alfred.scribe.enrollment import ResolvedEnrollment
from alfred.scribe.transcript import ROLE_CLINICIAN, ROLE_UNKNOWN, Segment, Transcript

_DIM = embed_voice.EMBED_DIM
_SALT = "DUMMY_SCRIBE_TEST_SALT"


# --- local helpers (self-contained; mirror test_scribe_diarize_engine.py) -----

def _config(*, provider="pyannote", enabled=True, pipeline_config="",
            enrollment_dir="", purity=0.80):
    return load_from_unified({"scribe": {
        "mode": "synthetic",
        "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "diarize": {
            "provider": provider, "enabled": enabled,
            "pipeline_config": pipeline_config, "enrollment_dir": enrollment_dir,
            "purity_threshold": purity,
        },
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
    }})


def _seg(i, text="x", *, start=None, end=None, speaker=None, cluster=None, conf=None):
    s = float(i) if start is None else start
    e = s + 1.0 if end is None else end
    return Segment(id=f"S{i}", start_s=s, end_s=e, text=text,
                   speaker=speaker, speaker_cluster=cluster, speaker_conf=conf)


def _tx(*segs, diarized=False, source_id="enc-fr"):
    return Transcript(source_id=source_id, mode="synthetic",
                      segments=list(segs), diarized=diarized)


def _resolved(centroids, *, dim=None):
    return ResolvedEnrollment(
        user="np_jamie", preset_id="pst-0000000000000-0000000000000000",
        centroid_version=1, centroids=centroids,
        embedding_dim=dim if dim is not None else _DIM,
    )


def _unit_vec(dim):
    """Deterministic pseudo-random UNIT vector of length ``dim`` (fixture centroid)."""
    import hashlib as _h
    import struct as _st
    out: list[float] = []
    seed = _h.sha256(b"p4-5c-fixround-vec").digest()
    i = 0
    while len(out) < dim:
        block = _h.sha256(seed + _st.pack(">I", i)).digest()
        for j in range(0, len(block), 4):
            if len(out) >= dim:
                break
            out.append(_st.unpack(">I", block[j:j + 4])[0] / 0xFFFFFFFF * 2.0 - 1.0)
        i += 1
    n = math.sqrt(sum(x * x for x in out))
    return [x / n for x in out]


class _SliceRec:
    """Torch-free mono-waveform stand-in (records sliced sample ranges)."""

    def __init__(self, total):
        self._total = total
        self.slices: list[tuple[int, int]] = []

    @property
    def shape(self):
        return (1, self._total)

    def __getitem__(self, key):
        _, tsl = key
        self.slices.append((tsl.start, tsl.stop))
        return ("SLICE", tsl.start, tsl.stop)


def _stub_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", types.ModuleType("torch"))


def _read_rows(tmp_path):
    path = tmp_path / "learning" / "attest_capture.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# ===========================================================================
# F1 [HIGH] — degenerate (NaN/zero) embedding e1 attractor: BOTH normalize seams
# RAISE fail-closed now (never canonicalize to the shared e1), and the extraction
# seams OMIT the cluster + log. e1 was a shared attractor: two independent silent
# failures both coerce to e1 and score cosine=1.0 (max-confidence mis-attribution).
# ===========================================================================

def test_unit_normalize_raises_on_zero_norm():
    with pytest.raises(embed_voice.DegenerateEmbeddingError):
        embed_voice._unit_normalize([0.0] * 8)


def test_unit_normalize_raises_on_nonfinite():
    with pytest.raises(embed_voice.DegenerateEmbeddingError):
        embed_voice._unit_normalize([float("nan"), 1.0, 2.0])
    with pytest.raises(embed_voice.DegenerateEmbeddingError):
        embed_voice._unit_normalize([float("inf"), 0.0, 0.0])


def test_unit_normalize_passes_valid_vector():
    out = embed_voice._unit_normalize([3.0, 4.0])
    assert out == pytest.approx([0.6, 0.8])


def test_enrollment_unit_normalize_raises_on_degenerate():
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.unit_normalize([0.0, 0.0, 0.0])
    with pytest.raises(enrollment.EnrollmentError):
        enrollment.unit_normalize([float("nan"), 1.0])


def test_degenerate_error_is_embed_error_subclass():
    # The enrollment window path skips a bad window via `except Exception`; a degenerate
    # window must be SKIPPED (not crash the batch), then all-degenerate → the existing
    # 'every window failed' EmbedError fires (enroll fails LOUD). This pins the hierarchy
    # that makes the existing handler catch it (the full torchaudio window loop is on-box).
    assert issubclass(embed_voice.DegenerateEmbeddingError, embed_voice.EmbedError)
    assert issubclass(embed_voice.DegenerateEmbeddingError, Exception)


def test_pyannote_extraction_omits_degenerate_cluster_and_logs(monkeypatch):
    # The degeneracy BELT on the REAL seam: a DegenerateEmbeddingError from embed_waveform
    # OMITS the cluster (never e1) + an explicit degeneracy log; chunk resolves all-unknown.
    sr = 16000
    wave = _SliceRec(total=sr * 10)
    monkeypatch.setattr(diarize_mod, "_decode_audio", lambda p: (wave, sr))
    _stub_torch(monkeypatch)

    def _degenerate(cfg, w, s):
        raise embed_voice.DegenerateEmbeddingError("zero norm")

    monkeypatch.setattr(embed_voice, "embed_waveform", _degenerate)
    cfg = _config(provider="pyannote", enabled=True, pipeline_config="/x.yaml")
    with structlog.testing.capture_logs() as caps:
        emb = diarize_mod._cluster_embeddings_for(
            cfg, "/enc/c.webm", [(0.0, 5.0, "S00")], expected_dim=_DIM, source_id="enc")
    assert emb == {}                                   # degenerate cluster OMITTED
    deg = [c for c in caps if c.get("event") == "scribe.diarize.extraction_degenerate"]
    assert len(deg) == 1 and deg[0]["clusters_omitted"] == 1
    empty = [c for c in caps if c.get("event") == "scribe.diarize.extraction_empty"]
    assert len(empty) == 1 and empty[0]["reason"] == "degenerate"


def test_pyannote_extraction_non_degenerate_raise_still_propagates(monkeypatch):
    # NOTE-2 preserved: ONLY DegenerateEmbeddingError is caught. An unexpected raise (torch
    # OOM class) still PROPAGATES so stage-before-commit leaves the chunk foldable un-attributed.
    sr = 16000
    wave = _SliceRec(total=sr * 10)
    monkeypatch.setattr(diarize_mod, "_decode_audio", lambda p: (wave, sr))
    _stub_torch(monkeypatch)

    def _oom(cfg, w, s):
        raise RuntimeError("torch OOM")

    monkeypatch.setattr(embed_voice, "embed_waveform", _oom)
    cfg = _config(provider="pyannote", enabled=True, pipeline_config="/x.yaml")
    with pytest.raises(RuntimeError):
        diarize_mod._cluster_embeddings_for(
            cfg, "/enc/c.webm", [(0.0, 5.0, "S00")], expected_dim=_DIM, source_id="enc")

def test_pyannote_extraction_plain_embed_error_propagates(monkeypatch):
    # NOTE-2 catch NARROWNESS: the pyannote seam catches ONLY DegenerateEmbeddingError. A
    # PLAIN EmbedError (e.g. the staged model vanished mid-sweep) must PROPAGATE — never be
    # swallowed as a degenerate omit, which would fold the chunk all-unknown WITH the extractor
    # stamp (a poisoned health-eligible row, the exact class the marker exists to prevent). The
    # RuntimeError pin above does NOT bind this: a `except DegenerateEmbeddingError -> except
    # EmbedError` broadening mutant still lets RuntimeError through (EmbedError is not a
    # RuntimeError), so it survives that pin. This drives a plain EmbedError, which the
    # broadening mutant WOULD swallow — so the mutant dies here.
    sr = 16000
    wave = _SliceRec(total=sr * 10)
    monkeypatch.setattr(diarize_mod, "_decode_audio", lambda p: (wave, sr))
    _stub_torch(monkeypatch)

    def _embed_error(cfg, w, s):
        raise embed_voice.EmbedError("staged model vanished mid-sweep")

    monkeypatch.setattr(embed_voice, "embed_waveform", _embed_error)
    cfg = _config(provider="pyannote", enabled=True, pipeline_config="/x.yaml")
    with structlog.testing.capture_logs() as caps:
        with pytest.raises(embed_voice.EmbedError):
            diarize_mod._cluster_embeddings_for(
                cfg, "/enc/c.webm", [(0.0, 5.0, "S00")], expected_dim=_DIM, source_id="enc")
    # a plain EmbedError is NOT counted/logged as a degenerate omit (it propagated).
    assert not [c for c in caps if c.get("event") == "scribe.diarize.extraction_degenerate"]


def test_fake_extraction_omits_degenerate_cluster(monkeypatch):
    # The degeneracy belt fires on the FAKE seam too (symmetry): a DegenerateEmbeddingError
    # from embed_windows omits the cluster + logs. (Fake never produces degenerate naturally
    # — hash-derived norm ~9 — so this drives it via a raise.)
    def _degenerate(cfg, payloads):
        raise embed_voice.DegenerateEmbeddingError("zero norm")

    monkeypatch.setattr(embed_voice, "embed_windows", _degenerate)
    cfg = _config(provider="fake")
    with structlog.testing.capture_logs() as caps:
        emb = diarize_mod._cluster_embeddings_for(
            cfg, None, [(0.0, 5.0, "S00")], expected_dim=_DIM, source_id="enc")
    assert emb == {}
    deg = [c for c in caps if c.get("event") == "scribe.diarize.extraction_degenerate"]
    assert len(deg) == 1 and deg[0]["clusters_omitted"] == 1


# ===========================================================================
# F2 — single-cluster chunks auto-clear separation; conf must reflect match quality.
# Fix: cap committed conf at best_cosine for a single-cluster matched chunk, so a
# borderline match DEMOTES via the existing speaker_attribution gate — never conf=1.0
# on possibly-under-clustered (mixed-speaker) speech. The frozen matcher is untouched.
# ===========================================================================

def test_single_cluster_conf_capped_at_best_cosine(monkeypatch):
    cfg = _config(provider="fake")
    cm = diarize_mod.ClusterMatch(
        roles={"S00": ROLE_CLINICIAN}, best_cosine=0.78, separation=1.78, matched=True)
    monkeypatch.setattr(diarize_mod, "match_cluster_roles", lambda *a, **k: cm)
    tx = _tx(_seg(1, start=0.0, end=5.0), diarized=False)
    sink: dict = {}
    diarize_mod._apply_diarization(
        cfg, tx, [(0.0, 5.0, "S00")], resolved=_resolved([_unit_vec(_DIM)]),
        audio_path=None, match_sink=sink)
    assert tx.segments[0].speaker == ROLE_CLINICIAN
    # purity is 1.0 (one cluster) but conf is CAPPED at best_cosine=0.78, NOT vacuous 1.0.
    assert tx.segments[0].speaker_conf == pytest.approx(0.78)
    assert sink["single_cluster"] is True


def test_capped_conf_below_purity_threshold_demotes_via_attribution():
    # The cap flows through the EXISTING speaker_attribution conf gate: conf < purity_threshold
    # -> the role resolves unknown (fires speaker_unverified flags + the note-level banner).
    from alfred.scribe.speaker_attribution import _resolve_role
    seg = _seg(1, start=0.0, end=5.0, speaker=ROLE_CLINICIAN, cluster="S00", conf=0.78)
    assert _resolve_role(seg, 0.80) == ROLE_UNKNOWN     # 0.78 < 0.80 -> demote (fail-closed)
    seg.speaker_conf = 0.85
    assert _resolve_role(seg, 0.80) == ROLE_CLINICIAN   # a strong match stands


def test_multi_cluster_conf_not_capped(monkeypatch):
    # A MULTI-cluster chunk has real separation; purity reflects genuine diarization quality,
    # so it is NOT capped — conf stays purity (1.0 here), the cap targets single-cluster only.
    cfg = _config(provider="fake")
    cm = diarize_mod.ClusterMatch(
        roles={"S00": ROLE_CLINICIAN, "S01": ROLE_UNKNOWN},
        best_cosine=0.78, separation=0.5, matched=True)
    monkeypatch.setattr(diarize_mod, "match_cluster_roles", lambda *a, **k: cm)
    tx = _tx(_seg(1, start=0.0, end=5.0), diarized=False)
    sink: dict = {}
    diarize_mod._apply_diarization(
        cfg, tx, [(0.0, 5.0, "S00")], resolved=_resolved([_unit_vec(_DIM)]),
        audio_path=None, match_sink=sink)
    assert tx.segments[0].speaker == ROLE_CLINICIAN
    assert tx.segments[0].speaker_conf == pytest.approx(1.0)   # NOT capped
    assert sink["single_cluster"] is False


# ===========================================================================
# F3/F5 — the REAL (pyannote) seam dim belt was unbound (only the fake copy pinned).
# A mutant deleting the line-759 guard survived the suite. Bind it via the mock seam.
# ===========================================================================

def test_pyannote_seam_dim_belt_omits_wrong_dim(monkeypatch):
    sr = 16000
    wave = _SliceRec(total=sr * 10)
    monkeypatch.setattr(diarize_mod, "_decode_audio", lambda p: (wave, sr))
    _stub_torch(monkeypatch)
    # embed_waveform returns a WRONG-dim vector (128 != expected 256) on the REAL seam.
    monkeypatch.setattr(embed_voice, "embed_waveform", lambda c, w, s: [0.1] * 128)
    cfg = _config(provider="pyannote", enabled=True, pipeline_config="/x.yaml")
    with structlog.testing.capture_logs() as caps:
        emb = diarize_mod._cluster_embeddings_for(
            cfg, "/enc/c.webm", [(0.0, 5.0, "S00")], expected_dim=_DIM, source_id="enc")
    assert emb == {}                                   # wrong-dim OMITTED on the real seam
    mism = [c for c in caps if c.get("event") == "scribe.diarize.extraction_engine_mismatch"]
    assert len(mism) == 1 and mism[0]["clusters_omitted"] == 1 and mism[0]["expected_dim"] == _DIM


# ===========================================================================
# F4 — the pipeline stamp HOP (match_sink -> diarize_stats row) was unbound. A mutant
# typoing pipeline.py:682 (extractor=m.get("extractor_version")) survived the suite.
# ===========================================================================

def test_pipeline_forwards_extractor_and_single_cluster_to_row(tmp_path):
    cfg = _config(provider="fake", enrollment_dir=str(tmp_path))
    tx = _tx(_seg(1, start=0.0, end=5.0, speaker=ROLE_CLINICIAN, cluster="S00", conf=0.9),
             diarized=True)
    pl._record_diarize_stats(
        cfg, encounter_id="enc-jamie", chunk_seq=1, chunk_tx=tx, resolved=None,
        engine_fingerprint={"embedding_model": "pyannote/wespeaker"},
        match={"best_cosine": 0.8, "separation": 0.5, "matched": True,
               "extractor": "p4-5c", "single_cluster": True},
    )
    rows = _read_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["extractor"] == "p4-5c"        # hop-2 binding (pipeline forwards m.get)
    assert rows[0]["single_cluster"] is True       # F2 field threaded through
    assert rows[0]["best_cosine"] == 0.8


# ===========================================================================
# F6 — merge-then-floor: the floor must be computed from MERGED (deduped) spans, not
# the raw sum. A mutant summing raw spans survived because no fixture had overlapping
# sub-floor turns whose UNMERGED sum crossed the floor.
# ===========================================================================

def test_pool_merge_before_floor_omits_overlapping_subfloor():
    # merged net = 0.7 (< 1.0 floor) -> OMITTED. A sum-before-merge mutant sees
    # 0.6 + 0.6 = 1.2 >= 1.0 and (wrongly) keeps it.
    turns = [(0.0, 0.6, "X"), (0.1, 0.7, "X")]
    pooled = diarize_mod._pool_turns_by_cluster(turns, min_speech_s=1.0)
    assert "X" not in pooled                            # 0.7 net < floor -> dropped


# ===========================================================================
# F7/F9/F10 — min_turn_s coupling: BOTH production consumers read the config field.
# Hardcoding either call site to 1.0 must die. (The prior coupling test hand-PASSED
# the field into the helpers; it never exercised the production reads.)
# ===========================================================================

def test_extraction_floor_reads_config_min_turn_s():
    # PRODUCTION read at diarize._cluster_embeddings_for (not the helper). min_turn_s=2.0 ->
    # a 1.5s cluster is below the CONFIG floor -> extraction empty. Hardcoded-1.0 mutant keeps it.
    cfg = _config(provider="fake")
    cfg.diarize.min_turn_s = 2.0
    emb = diarize_mod._cluster_embeddings_for(
        cfg, None, [(0.0, 1.5, "S00")], expected_dim=_DIM, source_id="enc")
    assert emb == {}                                    # would be {'S00': ...} at a hardcoded 1.0


def test_eligible_denominator_reads_config_min_turn_s(tmp_path):
    # PRODUCTION read at pipeline._record_diarize_stats -> _eligible_turns(config.min_turn_s).
    # min_turn_s=2.0 -> a 1.5s segment is ineligible. Hardcoded-1.0 mutant records eligible=1.
    cfg = _config(provider="fake", enrollment_dir=str(tmp_path))
    cfg.diarize.min_turn_s = 2.0
    tx = _tx(_seg(1, start=0.0, end=1.5, speaker=ROLE_UNKNOWN, cluster="S00", conf=0.5),
             diarized=True)
    pl._record_diarize_stats(
        cfg, encounter_id="enc", chunk_seq=1, chunk_tx=tx, resolved=None,
        engine_fingerprint={"embedding_model": "x"}, match={})
    rows = _read_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["eligible_turns"] == 0               # 1.5 < 2.0 config floor
    assert rows[0]["min_turn_s"] == 2.0
    # F8 THIRD min_turn_s read: _role_counts_eligible (pipeline.py) must ALSO read the config
    # floor, so its population equals eligible_turns. A hardcoded-1.0 mutant at that call site
    # would count the 1.5s segment (sum 1) while eligible_turns=0 — the invariant breaks.
    assert sum(rows[0]["role_counts_eligible"].values()) == rows[0]["eligible_turns"]  # == 0
    assert rows[0]["role_counts_eligible"]["unknown"] == 0   # the 1.5s unknown seg is sub-floor


# ===========================================================================
# F8 — 5b match_rate: the ratified formula mixed populations (unknown from ALL segments,
# eligible from >= min_turn_s -> can go negative). The new role_counts_eligible draws the
# numerator from the SAME eligible population, so match_rate is well-defined in [0, 1].
# ===========================================================================

def test_role_counts_eligible_is_same_population(tmp_path):
    cfg = _config(provider="fake", enrollment_dir=str(tmp_path))
    # 2 clinician eligible (1.5s) + 8 unknown sub-floor interjections (0.8s, ineligible).
    segs = [_seg(i, start=0.0, end=1.5, speaker=ROLE_CLINICIAN, cluster="S00", conf=0.9)
            for i in range(2)]
    segs += [_seg(100 + i, start=0.0, end=0.8, speaker=ROLE_UNKNOWN, cluster="S01", conf=0.5)
             for i in range(8)]
    tx = _tx(*segs, diarized=True)
    pl._record_diarize_stats(
        cfg, encounter_id="enc", chunk_seq=1, chunk_tx=tx, resolved=None,
        engine_fingerprint={"embedding_model": "x"}, match={})
    row = _read_rows(tmp_path)[0]
    # role_counts (ALL segments) counts the 8 short unknowns; role_counts_eligible does not.
    assert row["role_counts"]["unknown"] == 8
    assert row["role_counts_eligible"]["unknown"] == 0
    assert row["role_counts_eligible"]["clinician"] == 2
    assert row["eligible_turns"] == 2
    # OLD mixed-population metric: 1 - 8/2 = -3 (ill-defined). FIXED same-population metric:
    match_rate = 1 - row["role_counts_eligible"]["unknown"] / row["eligible_turns"]
    assert 0.0 <= match_rate <= 1.0 and match_rate == pytest.approx(1.0)


# ===========================================================================
# F11 — shared-embedder concurrency (P4-4 carry-forward item 8, now live via P4-5c):
# the forward pass runs under a module-level inference lock so a concurrent diarize
# extraction + enroll finalize never do simultaneous forwards on the one cached model.
# ===========================================================================

def test_embed_tensor_holds_inference_lock_during_forward(monkeypatch):
    fake_ta = types.ModuleType("torchaudio")
    fake_ta.functional = types.SimpleNamespace(resample=lambda w, a, b: w)
    monkeypatch.setitem(sys.modules, "torchaudio", fake_ta)
    monkeypatch.setattr(diarize_mod, "_to_mono", lambda w: w)
    seen: dict = {}

    class _Wave:
        def unsqueeze(self, d):
            return self

    class _Embedder:
        def __call__(self, x):
            seen["locked"] = embed_voice._EMBED_INFERENCE_LOCK.locked()
            return [3.0, 4.0]

    out = embed_voice._embed_tensor(_Embedder(), _Wave(), embed_voice._EMBED_SAMPLE_RATE)
    assert seen["locked"] is True                       # forward ran UNDER the lock
    assert out == pytest.approx([0.6, 0.8])             # normalized (3,4) -> (0.6,0.8)
    assert not embed_voice._EMBED_INFERENCE_LOCK.locked()  # released after
