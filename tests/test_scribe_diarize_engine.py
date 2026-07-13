"""P4-4 — real pyannote engine: alignment/purity/role + staging + carry-forwards.

UNCONDITIONAL CI — no torch, no pyannote, no ``importorskip``. The engine is split
so the ONLY torch-touching call (``_run_pyannote_pipeline``) is a thin seam; the
alignment (max-overlap + purity), the guards, the atomic apply, the cluster→role
fail-safe, and the staging materialize/token helpers are all PURE + covered here.
The real end-to-end engine is skip-gated on-box (ALFRED_SCRIBE_DIARIZE_IT).

Covers: max-overlap alignment incl. STRADDLE (purity < threshold), empty-turn,
no-overlap, zero/non-finite-duration (NO NaN), multi-interval (word-level ready),
tie determinism; NaN/±inf conf guard; cluster→role empty-enrollment fail-safe;
_apply_diarization full assign + NOTE-3 (text/id/bounds untouched) + NOTE-2
atomicity (mid-iteration raise → nothing committed); the pyannote dispatch
(NOTE-1 enabled gate, fail-loud on missing config, injected-turns end-to-end,
engine-raise leaves chunk untouched); materialize transform + fail-loud; token
resolution + NEVER-logged; and the daemon-restart disk round-trip (diarized +
speaker_conf survive ledger reload → fold → P4-2 speaker pass composes).
"""

from __future__ import annotations

import os

import pytest

import alfred.scribe.diarize as diarize_mod
import alfred.scripts.stage_diarize_models as stage_mod
from alfred.scribe.config import load_from_unified
from alfred.scribe.diarize import (
    DiarizeError,
    assign_speakers,
    ensure_diarize_backend_available,
)
from alfred.scribe.ledger import ledger_path, load_ledger, save_ledger
from alfred.scribe.notegen import Claim, StructuredNote
from alfred.scribe.speaker_attribution import (
    SPEAKER_UNVERIFIED_REASON,
    check_speaker_attribution,
)
from alfred.scribe.transcript import ROLE_UNKNOWN, Segment, Transcript
from alfred.scripts.stage_diarize_models import (
    _pick_local_model_path,
    materialize_pipeline_config,
    resolve_token,
)

_SALT = "DUMMY_SCRIBE_TEST_SALT"


def _config(*, provider="pyannote", enabled=True, pipeline_config="",
            enrollment_path="", purity=0.80):
    return load_from_unified({"scribe": {
        "mode": "synthetic",
        "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "diarize": {
            "provider": provider, "enabled": enabled,
            "pipeline_config": pipeline_config, "enrollment_path": enrollment_path,
            "purity_threshold": purity,
        },
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
    }})


def _seg(i, text="x", *, start=None, end=None, speaker=None, cluster=None, conf=None):
    s = float(i) if start is None else start
    e = s + 1.0 if end is None else end
    return Segment(id=f"S{i}", start_s=s, end_s=e, text=text,
                   speaker=speaker, speaker_cluster=cluster, speaker_conf=conf)


def _tx(*segs, diarized=False, source_id="enc-eng"):
    return Transcript(source_id=source_id, mode="synthetic",
                      segments=list(segs), diarized=diarized)


# ---------------------------------------------------------------------------
# Pure alignment — max-overlap dominant cluster + purity
# ---------------------------------------------------------------------------

def test_alignment_dominant_cluster_clean_segment():
    # Segment fully inside one cluster's turns → that cluster, purity 1.0.
    turns = [(0.0, 5.0, "SPEAKER_00")]
    cl, pur = diarize_mod._dominant_cluster_over_intervals([(0.0, 5.0)], turns)
    assert cl == "SPEAKER_00" and pur == 1.0


def test_alignment_straddle_reduces_purity_below_threshold():
    # THE reconciliation pin: a segment straddling a speaker change → dominant
    # cluster + REDUCED purity (0.6 < the 0.80 threshold) → will demote to unknown
    # at P4-2. No physical split (NOTE-3); straddle == low purity.
    turns = [(0.0, 6.0, "SPEAKER_00"), (6.0, 10.0, "SPEAKER_01")]
    cl, pur = diarize_mod._dominant_cluster_over_intervals([(0.0, 10.0)], turns)
    assert cl == "SPEAKER_00" and abs(pur - 0.6) < 1e-9


def test_alignment_silence_not_in_denominator():
    # Trailing silence (segment extends past all turns) does NOT dilute purity —
    # the denominator is LABELED overlap, not segment duration.
    turns = [(0.0, 6.0, "SPEAKER_00")]
    cl, pur = diarize_mod._dominant_cluster_over_intervals([(0.0, 10.0)], turns)
    assert cl == "SPEAKER_00" and pur == 1.0


def test_alignment_no_overlap_is_none_zero():
    turns = [(20.0, 25.0, "SPEAKER_00")]
    assert diarize_mod._dominant_cluster_over_intervals([(0.0, 5.0)], turns) == (None, 0.0)


def test_alignment_empty_turns_is_none_zero():
    assert diarize_mod._dominant_cluster_over_intervals([(0.0, 5.0)], []) == (None, 0.0)


@pytest.mark.parametrize("interval", [(5.0, 5.0), (5.0, 4.0)])
def test_alignment_zero_or_negative_duration_no_nan(interval):
    turns = [(0.0, 10.0, "SPEAKER_00")]
    cl, pur = diarize_mod._dominant_cluster_over_intervals([interval], turns)
    assert cl is None and pur == 0.0   # never a division-by-zero / NaN


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_alignment_non_finite_interval_bound_no_nan(bad):
    turns = [(0.0, 10.0, "SPEAKER_00")]
    cl, pur = diarize_mod._dominant_cluster_over_intervals([(0.0, bad)], turns)
    assert cl is None and pur == 0.0


def test_alignment_word_level_multi_interval():
    # WORD-LEVEL ready: a segment given as multiple word spans aggregates overlap
    # across them (the P4-5 threading path). Two clinician words + one patient word.
    turns = [(0.0, 5.0, "SPEAKER_00"), (5.0, 10.0, "SPEAKER_01")]
    words = [(0.5, 1.0), (2.0, 2.5), (7.0, 8.0)]   # 1.0s SPEAKER_00, 1.0s SPEAKER_01
    cl, pur = diarize_mod._dominant_cluster_over_intervals(words, turns)
    # tie on overlap (1.0 vs 1.0) → deterministic lexicographic winner SPEAKER_00
    assert cl == "SPEAKER_00" and abs(pur - 0.5) < 1e-9


def test_alignment_tie_is_deterministic_lexicographic():
    turns = [(0.0, 5.0, "SPEAKER_01"), (5.0, 10.0, "SPEAKER_00")]  # equal 5s each
    cl, _ = diarize_mod._dominant_cluster_over_intervals([(0.0, 10.0)], turns)
    assert cl == "SPEAKER_00"   # lexicographically smallest label wins the tie


# ---------------------------------------------------------------------------
# _guard_conf — never NaN/±inf, clamp to [0,1] (P4-2 carry-forward, at source)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_guard_conf_non_finite_to_zero(bad):
    assert diarize_mod._guard_conf(bad) == 0.0


@pytest.mark.parametrize("val,exp", [(-0.3, 0.0), (0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (1.7, 1.0)])
def test_guard_conf_clamps(val, exp):
    assert diarize_mod._guard_conf(val) == exp


# ---------------------------------------------------------------------------
# cluster → role — P4-4 fail-safe: no enrollment ⇒ unknown
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("enrollment", ["", "/some/enrollment.npy"])
def test_cluster_to_role_is_unknown_in_p44(enrollment):
    # P4-4 end-state: no P4-5 matcher exists, so EVERY cluster resolves unknown
    # (fail-closed), whether or not an enrollment_path is set.
    cfg = _config(enrollment_path=enrollment)
    assert diarize_mod._cluster_to_role("SPEAKER_00", cfg) == ROLE_UNKNOWN
    assert diarize_mod._cluster_to_role(None, cfg) == ROLE_UNKNOWN


# ---------------------------------------------------------------------------
# _apply_diarization — full assign + NOTE-3 (untouched fields) + NOTE-2 atomicity
# ---------------------------------------------------------------------------

def test_apply_diarization_assigns_cluster_conf_role_and_latches():
    tx = _tx(_seg(1, "BP 120 over 80", start=0.0, end=10.0), diarized=False)
    turns = [(0.0, 6.0, "SPEAKER_00"), (6.0, 10.0, "SPEAKER_01")]  # straddle
    out = diarize_mod._apply_diarization(_config(), tx, turns)
    assert out is tx and tx.diarized is True
    seg = tx.segments[0]
    assert seg.speaker_cluster == "SPEAKER_00"
    assert abs(seg.speaker_conf - 0.6) < 1e-9         # straddle purity
    assert seg.speaker == ROLE_UNKNOWN                # P4-4 no enrollment


def test_apply_diarization_note3_touches_only_speaker_fields():
    tx = _tx(
        _seg(1, "clinician words", start=0.0, end=5.0),
        _seg(2, "patient words", start=5.0, end=10.0),
        diarized=False,
    )
    before = [(s.id, s.text, s.start_s, s.end_s) for s in tx.segments]
    diarize_mod._apply_diarization(_config(), tx, [(0.0, 10.0, "SPEAKER_00")])
    after = [(s.id, s.text, s.start_s, s.end_s) for s in tx.segments]
    assert after == before   # NOTE-3: id / text / start_s / end_s NEVER mutated


def test_apply_diarization_atomic_on_midway_raise(monkeypatch):
    # NOTE-2: the real engine can raise mid-iteration. Assignment STAGES all
    # segments before COMMITTING any, so a raise during staging leaves the chunk
    # UNTOUCHED (nothing committed, diarized not latched). If commit were
    # interleaved, segment 1 would be mutated — this pin fails then.
    tx = _tx(_seg(1), _seg(2), _seg(3), diarized=False)
    calls = {"n": 0}

    def _raising(intervals, turns):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("engine blew up mid-iteration")
        return ("SPEAKER_00", 1.0)

    monkeypatch.setattr(diarize_mod, "_dominant_cluster_over_intervals", _raising)
    with pytest.raises(RuntimeError):
        diarize_mod._apply_diarization(_config(), tx, [(0.0, 10.0, "SPEAKER_00")])
    assert all(
        s.speaker is None and s.speaker_cluster is None and s.speaker_conf is None
        for s in tx.segments
    )
    assert tx.diarized is False


# ---------------------------------------------------------------------------
# assign_speakers pyannote dispatch — NOTE-1 gate, fail-loud, injected turns
# ---------------------------------------------------------------------------

def test_pyannote_disabled_is_inert():
    tx = _tx(_seg(1), diarized=False)
    out = assign_speakers(_config(enabled=False), "/audio.wav", tx)
    assert out is tx and out.diarized is False and tx.segments[0].speaker is None


def test_pyannote_enabled_missing_config_fails_loud():
    tx = _tx(_seg(1), diarized=False)
    with pytest.raises(DiarizeError):
        assign_speakers(_config(enabled=True, pipeline_config=""), "/audio.wav", tx)


def test_pyannote_enabled_config_path_missing_fails_loud(tmp_path):
    tx = _tx(_seg(1), diarized=False)
    missing = tmp_path / "nope.yaml"
    with pytest.raises(DiarizeError):
        assign_speakers(_config(enabled=True, pipeline_config=str(missing)), "/a.wav", tx)


def test_pyannote_enabled_aligns_via_injected_turns(monkeypatch):
    # End-to-end provider glue WITHOUT torch: inject the turns _run_pyannote_pipeline
    # would return; the alignment + atomic commit run for real.
    tx = _tx(_seg(1, "a", start=0.0, end=10.0), diarized=False)
    monkeypatch.setattr(
        diarize_mod, "_run_pyannote_pipeline",
        lambda config, audio: [(0.0, 8.0, "SPEAKER_00"), (8.0, 10.0, "SPEAKER_01")],
    )
    out = assign_speakers(_config(enabled=True, pipeline_config="/x.yaml"), "/a.wav", tx)
    assert out.diarized is True
    assert tx.segments[0].speaker_cluster == "SPEAKER_00"
    assert abs(tx.segments[0].speaker_conf - 0.8) < 1e-9
    assert tx.segments[0].speaker == ROLE_UNKNOWN


def test_pyannote_engine_raise_leaves_chunk_untouched(monkeypatch):
    # NOTE-2 at the provider boundary: turns are produced BEFORE apply, so an engine
    # raise leaves the chunk untouched (the pipeline then folds it un-attributed).
    tx = _tx(_seg(1), _seg(2), diarized=False)

    def _boom(config, audio):
        raise RuntimeError("torch OOM mid-diarize")

    monkeypatch.setattr(diarize_mod, "_run_pyannote_pipeline", _boom)
    with pytest.raises(RuntimeError):
        assign_speakers(_config(enabled=True, pipeline_config="/x.yaml"), "/a.wav", tx)
    assert all(s.speaker is None for s in tx.segments) and tx.diarized is False


# ---------------------------------------------------------------------------
# staging — materialize transform (pure) + fail-loud on version skew
# ---------------------------------------------------------------------------

def _pcfg():
    return {
        "version": "3.1.0",
        "pipeline": {
            "name": "pyannote.audio.pipelines.SpeakerDiarization",
            "params": {
                "segmentation": "pyannote/segmentation-3.0",
                "embedding": "pyannote/wespeaker-voxceleb-resnet34-LM",
                "embedding_batch_size": 32,
            },
        },
        "params": {"clustering": {"threshold": 0.7045}},
    }


def test_materialize_substitutes_repo_ids_with_local_paths():
    m = materialize_pipeline_config(_pcfg(), segmentation_path="/l/seg.bin", embedding_path="/l/emb")
    assert m["pipeline"]["params"]["segmentation"] == "/l/seg.bin"
    assert m["pipeline"]["params"]["embedding"] == "/l/emb"
    # every OTHER key preserved byte-for-byte
    assert m["pipeline"]["params"]["embedding_batch_size"] == 32
    assert m["params"]["clustering"]["threshold"] == 0.7045
    assert m["version"] == "3.1.0"


def test_materialize_does_not_mutate_input():
    src = _pcfg()
    materialize_pipeline_config(src, segmentation_path="/l/seg", embedding_path="/l/emb")
    assert src["pipeline"]["params"]["segmentation"] == "pyannote/segmentation-3.0"
    assert src["pipeline"]["params"]["embedding"] == "pyannote/wespeaker-voxceleb-resnet34-LM"


@pytest.mark.parametrize("bad", [
    "not-a-dict",
    {},                                              # no pipeline
    {"pipeline": {}},                                # no params
    {"pipeline": {"params": {"segmentation": "x"}}},  # missing embedding
    {"pipeline": {"params": {"embedding": "x"}}},     # missing segmentation
])
def test_materialize_fails_loud_on_version_skew(bad):
    with pytest.raises(ValueError):
        materialize_pipeline_config(bad, segmentation_path="/s", embedding_path="/e")


def test_pick_local_model_path_prefers_checkpoint(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    assert _pick_local_model_path(snap) == str(snap)                 # no checkpoint → dir
    (snap / "pytorch_model.bin").write_bytes(b"x")
    assert _pick_local_model_path(snap) == str(snap / "pytorch_model.bin")


# ---------------------------------------------------------------------------
# staging — token resolution + NEVER logged (NOTE-4-style binding)
# ---------------------------------------------------------------------------

def test_resolve_token_from_env():
    assert resolve_token(token_file=None, env={"HF_TOKEN": "DUMMY_TOK_ENV"}) == "DUMMY_TOK_ENV"


def test_resolve_token_from_file(tmp_path):
    f = tmp_path / "tok"
    f.write_text("DUMMY_TOK_FILE\n", encoding="utf-8")
    assert resolve_token(token_file=f, env={}) == "DUMMY_TOK_FILE"


def test_resolve_token_empty_file_raises(tmp_path):
    f = tmp_path / "tok"
    f.write_text("   \n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        resolve_token(token_file=f, env={})


def test_resolve_token_neither_raises():
    with pytest.raises(RuntimeError):
        resolve_token(token_file=None, env={})


def test_main_dry_run_never_logs_token(tmp_path, capsys, monkeypatch):
    # The token value must NEVER appear in stdout/stderr (it is read-and-dropped).
    monkeypatch.setenv("HF_TOKEN", "SUPERSECRETTOKENVALUE")
    rc = stage_mod.main(["--hf-home", str(tmp_path / "hf"), "--dry-run"])
    assert rc == 0
    out = capsys.readouterr()
    assert "SUPERSECRETTOKENVALUE" not in out.out
    assert "SUPERSECRETTOKENVALUE" not in out.err


# ---------------------------------------------------------------------------
# daemon-restart disk round-trip — diarized + speaker_conf survive → speaker pass
# ---------------------------------------------------------------------------

def test_diarized_conf_survives_ledger_roundtrip_and_speaker_pass_composes(tmp_path):
    # P4-2 carry-forward verification: a diarized chunk with a SUB-PURITY segment
    # (conf 0.5 < 0.80) survives save→load, folds via append_chunk (which copies the
    # P4 fields + latches diarized), and the P4-2 speaker pass composes on the
    # reloaded-then-folded transcript — the sub-purity turn demotes to unknown →
    # speaker_unverified.
    chunk = _tx(
        _seg(1, "BP 120 over 80", start=0.0, end=5.0,
             speaker="clinician", cluster="SPEAKER_00", conf=0.5),
        diarized=True,
    )
    p = tmp_path / "e.transcript.json"
    save_ledger(p, chunk)
    reloaded = load_ledger(p)
    assert reloaded.diarized is True
    assert reloaded.segments[0].speaker_conf == 0.5
    assert reloaded.segments[0].speaker_cluster == "SPEAKER_00"

    acc = Transcript(source_id="acc", mode="synthetic")
    assert acc.append_chunk(reloaded, audio_offset_s=0.0, chunk_key="k1", seq=1) is True
    assert acc.diarized is True and acc.segments[0].speaker_conf == 0.5

    note = StructuredNote(objective=[Claim(claim="BP 120 over 80", source_spans=["S1"])])
    flags = check_speaker_attribution(note, acc, _config(purity=0.80))
    assert any(f.reason == SPEAKER_UNVERIFIED_REASON for f in flags)


# ---------------------------------------------------------------------------
# real pyannote engine — skip-gated, on-box only (mirrors the qwen IT gate)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("ALFRED_SCRIBE_DIARIZE_IT"),
    reason="real pyannote engine — set ALFRED_SCRIBE_DIARIZE_IT=1 on-box with the "
           "[scribe-diarize] extra, staged models, $ALFRED_SCRIBE_DIARIZE_PIPELINE_CONFIG "
           "(materialized) + $ALFRED_SCRIBE_DIARIZE_AUDIO (a wav)",
)
def test_real_pyannote_engine_smoke():
    pipeline_config = os.environ["ALFRED_SCRIBE_DIARIZE_PIPELINE_CONFIG"]
    audio = os.environ["ALFRED_SCRIBE_DIARIZE_AUDIO"]
    tx = _tx(_seg(1, "real speech", start=0.0, end=10.0), diarized=False)
    out = assign_speakers(
        _config(enabled=True, pipeline_config=pipeline_config), audio, tx,
    )
    assert out.diarized is True
    seg = out.segments[0]
    assert seg.speaker_cluster is not None            # a real cluster was assigned
    assert 0.0 <= seg.speaker_conf <= 1.0             # finite purity, never NaN
    assert seg.speaker == ROLE_UNKNOWN                # no enrollment → unknown (P4-4)
