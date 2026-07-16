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

import importlib.util
import math
import os
import sys
from pathlib import Path

import pytest
import yaml

import alfred.scribe.diarize as diarize_mod
import alfred.scripts.stage_diarize_models as stage_mod
from alfred.scribe.config import load_from_unified
from alfred.scribe.diarize import (
    AudioDecodeError,
    DiarizeError,
    MissingDiarizeDependency,
    assign_speakers,
    ensure_diarize_backend_available,
)
from alfred.scripts.stage_diarize_models import (
    DIARIZATION_REPO,
    EMBEDDING_REPO,
    MATERIALIZED_CONFIG_NAME,
    SEGMENTATION_REPO,
)
from alfred.scribe.ledger import ledger_path, load_ledger, save_ledger
from alfred.scribe.notegen import Claim, StructuredNote
from alfred.scribe.speaker_attribution import (
    SPEAKER_UNVERIFIED_REASON,
    check_speaker_attribution,
)
from alfred.scribe.transcript import ROLE_CLINICIAN, ROLE_UNKNOWN, Segment, Transcript
from alfred.scripts.stage_diarize_models import (
    _pick_local_model_path,
    materialize_pipeline_config,
    resolve_token,
)

_SALT = "DUMMY_SCRIBE_TEST_SALT"


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

@pytest.mark.parametrize("enrollment", ["", "/some/enrollment"])
def test_cluster_to_role_is_unknown_in_p44(enrollment):
    # P4-4 end-state: no P4-5 matcher exists, so EVERY cluster resolves unknown
    # (fail-closed), whether or not an enrollment_dir is set.
    cfg = _config(enrollment_dir=enrollment)
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
# Audio DECODE — torchaudio (ffmpeg) → mono waveform dict (P4-4 activation fix)
#
# THE BUG this closes: pyannote's default path-loader (soundfile/libsndfile) cannot decode
# webm/opus (PWA) or mp4/AAC (iPhone) — the ONLY formats real devices produce — so every
# real clip fail-opened to ZERO attribution, invisibly (the IT test fed a WAV). We now
# decode via torchaudio and hand pyannote an in-memory waveform dict. torchaudio is
# torch-heavy + absent from base CI, so the DECODE ITSELF is proven on-box (the updated IT
# test + the torchaudio-gated test below); these torch-free tests pin the SHAPE / dispatch
# / fail-open logic — the non-trivial half the WAV IT test never exercised.
# ---------------------------------------------------------------------------

class _FakeWave:
    """Torch-free stand-in for a torchaudio waveform: ``(channels, time)`` with a
    ``.mean(dim, keepdim)`` that returns a mono ``_FakeWave``. Records whether ``mean()``
    ran, so a test can prove downmix happened (or didn't) without torch."""

    def __init__(self, channels, samples, *, meaned=False):
        self._shape = (channels, samples)
        self.meaned = meaned

    @property
    def ndim(self):
        return len(self._shape)

    @property
    def shape(self):
        return self._shape

    def mean(self, dim, keepdim):
        assert dim == 0 and keepdim is True, (dim, keepdim)   # the EXACT downmix call
        return _FakeWave(1, self._shape[1], meaned=True)


def test_to_mono_passes_a_mono_waveform_through_untouched():
    w = _FakeWave(1, 16000)
    out = diarize_mod._to_mono(w)
    assert out is w                          # no mean() on an already-mono signal
    assert out.shape == (1, 16000)


def test_to_mono_downmixes_a_stereo_waveform():
    # THE channel-handling branch a MONO test clip would silently skip: (2, T) -> (1, T)
    # via mean(dim=0, keepdim=True). A stereo device clip must not reach pyannote as 2ch.
    out = diarize_mod._to_mono(_FakeWave(2, 16000))
    assert out.shape == (1, 16000) and out.meaned is True


def test_to_mono_rejects_zero_channels():
    with pytest.raises(AudioDecodeError):
        diarize_mod._to_mono(_FakeWave(0, 16000))


def test_to_mono_rejects_non_2d():
    class _OneD:
        ndim = 1
        shape = (16000,)
    with pytest.raises(AudioDecodeError):
        diarize_mod._to_mono(_OneD())


def test_decode_audio_downmixes_and_passes_sample_rate_through(monkeypatch):
    # _decode_audio with a FAKE torchaudio: a (2, T) load @ 48k -> mono (1, T), sr passed
    # through UNCHANGED (pyannote resamples internally; we do not).
    import types
    fake = types.ModuleType("torchaudio")
    fake.load = lambda path: (_FakeWave(2, 24000), 48000)
    monkeypatch.setitem(sys.modules, "torchaudio", fake)
    wave, sr = diarize_mod._decode_audio("/x/clip.webm")
    assert wave.shape == (1, 24000) and wave.meaned is True
    assert sr == 48000 and isinstance(sr, int)


def test_decode_audio_wraps_a_decode_failure_as_audio_decode_error(monkeypatch):
    # A torchaudio decode failure (corrupt/unsupported container) -> AudioDecodeError, which
    # IS a DiarizeError IS an Exception, so the pipeline's broad fail-open catch still
    # degrades to speaker=None. The original error is chained (__cause__) for triage.
    import types
    boom = RuntimeError("ffmpeg: could not demux")
    fake = types.ModuleType("torchaudio")

    def _raise(path):
        raise boom

    fake.load = _raise
    monkeypatch.setitem(sys.modules, "torchaudio", fake)
    with pytest.raises(AudioDecodeError) as ei:
        diarize_mod._decode_audio("/x/clip.mp4")
    assert ei.value.__cause__ is boom
    assert isinstance(ei.value, DiarizeError)          # -> upstream fail-open catches it


def test_decode_audio_missing_torchaudio_is_a_missing_dependency(monkeypatch):
    # torchaudio absent -> MissingDiarizeDependency (daemon exit-78 class), never a silent
    # fall-through. Forced deterministically (sys.modules[x]=None makes `import x` raise).
    monkeypatch.setitem(sys.modules, "torchaudio", None)
    with pytest.raises(MissingDiarizeDependency):
        diarize_mod._decode_audio("/x/clip.webm")


def test_run_pyannote_pipeline_hands_a_waveform_dict_not_the_path(monkeypatch, tmp_path):
    # THE REGRESSION PIN for this bug. The engine must receive an IN-MEMORY waveform dict,
    # NEVER the raw path — pipeline(path) routes through libsndfile, which cannot decode
    # webm/mp4. A mutant reverting to `pipeline(str(audio_path))` makes captured['arg'] the
    # path string and fails here.
    cfg_file = tmp_path / "materialized.yaml"
    cfg_file.write_text("pipeline: {params: {}}\n", encoding="utf-8")
    monkeypatch.setattr(diarize_mod, "_validate_materialized_config_local", lambda p: None)
    monkeypatch.setattr(diarize_mod, "_decode_audio", lambda p: ("WAVE", 48000))
    captured = {}

    class _FakePipeline:
        def __call__(self, arg):
            captured["arg"] = arg
            return "ANNOTATION"

    monkeypatch.setattr(diarize_mod, "_load_pipeline_cached", lambda p: _FakePipeline())
    monkeypatch.setattr(diarize_mod, "_turns_from_annotation",
                        lambda ann: [(0.0, 1.0, "S00")] if ann == "ANNOTATION" else [])
    turns = diarize_mod._run_pyannote_pipeline(
        _config(enabled=True, pipeline_config=str(cfg_file)), "/enc/audio.webm")
    assert captured["arg"] == {"waveform": "WAVE", "sample_rate": 48000}
    assert not isinstance(captured["arg"], (str, Path))     # NOT the raw path
    assert turns == [(0.0, 1.0, "S00")]


def test_enabled_engine_boot_gate_requires_torchaudio(monkeypatch, tmp_path):
    # torchaudio is a hard runtime dep of the real engine now (the decode path). An enabled
    # pyannote with pyannote.audio present but torchaudio ABSENT must fail LOUD at boot
    # (exit 78) — NOT fail-open on every real clip.
    cfg_file = tmp_path / "m.yaml"
    cfg_file.write_text("pipeline: {}\n", encoding="utf-8")
    monkeypatch.setattr(diarize_mod, "_pyannote_available", lambda: True)
    monkeypatch.setattr(diarize_mod, "_torchaudio_available", lambda: False)
    with pytest.raises(MissingDiarizeDependency, match="torchaudio"):
        ensure_diarize_backend_available(
            _config(enabled=True, pipeline_config=str(cfg_file)))


def test_enabled_engine_boot_gate_passes_when_both_deps_present(monkeypatch, tmp_path):
    # ...and with BOTH deps present + a materialized config on disk, the gate passes.
    cfg_file = tmp_path / "m.yaml"
    cfg_file.write_text("pipeline: {}\n", encoding="utf-8")
    monkeypatch.setattr(diarize_mod, "_pyannote_available", lambda: True)
    monkeypatch.setattr(diarize_mod, "_torchaudio_available", lambda: True)
    ensure_diarize_backend_available(
        _config(enabled=True, pipeline_config=str(cfg_file)))   # must not raise


# --- torchaudio-gated real decode (skips in base CI; runs where the extra is installed) --

_DIARIZE_FIXTURES = Path(__file__).parent / "fixtures" / "diarize"


@pytest.mark.skipif(
    importlib.util.find_spec("torchaudio") is None,
    reason="torchaudio decode test — needs the [scribe-diarize] extra (torch-heavy, not in "
           "base CI). Runs on-box / in any venv with the extra.",
)
@pytest.mark.parametrize("container", ["webm", "m4a"])
def test_decode_audio_decodes_real_device_containers(container):
    # THE decode proof (torchaudio-gated): a REAL webm/opus + mp4/AAC clip -> a finite mono
    # (1, T) waveform + positive sample rate — the exact path pyannote's default loader
    # CANNOT walk. Skips until the committed fixtures exist (a ~1s real-speech clip per
    # container; must be recorded/transcoded where ffmpeg is present — see the ship note).
    fixture = _DIARIZE_FIXTURES / f"short_speech.{container}"
    if not fixture.is_file():
        pytest.skip(f"missing decode fixture {fixture} (commit a ~1s real clip per container)")
    wave, sr = diarize_mod._decode_audio(fixture)
    assert wave.ndim == 2 and wave.shape[0] == 1     # mono (1, T)
    assert wave.shape[1] > 0 and sr > 0
    import math
    assert math.isfinite(float(wave.abs().sum()))    # real samples, no NaN/inf


# ---------------------------------------------------------------------------
# real pyannote engine — skip-gated, on-box only (mirrors the qwen IT gate)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("ALFRED_SCRIBE_DIARIZE_IT"),
    reason="real pyannote engine — set ALFRED_SCRIBE_DIARIZE_IT=1 on-box with the "
           "[scribe-diarize] extra, staged models, $ALFRED_SCRIBE_DIARIZE_PIPELINE_CONFIG "
           "(materialized) + a real-speech clip PER CONTAINER "
           "($ALFRED_SCRIBE_DIARIZE_AUDIO wav, $ALFRED_SCRIBE_DIARIZE_AUDIO_WEBM, "
           "$ALFRED_SCRIBE_DIARIZE_AUDIO_MP4)",
)
@pytest.mark.parametrize("audio_env", [
    "ALFRED_SCRIBE_DIARIZE_AUDIO",         # wav control (the original — always passed)
    "ALFRED_SCRIBE_DIARIZE_AUDIO_WEBM",    # webm/opus — the PWA container (THE bug)
    "ALFRED_SCRIBE_DIARIZE_AUDIO_MP4",     # mp4/AAC — the iPhone container (THE bug)
])
def test_real_pyannote_engine_smoke(audio_env):
    # Runs the REAL engine over EACH device container. The WAV control always ran; webm +
    # mp4 are the formats that fail-opened to ZERO attribution under the old path-loader —
    # THIS is the on-box regression pin (infra points each env var at a real clip). A
    # container whose env var is unset skips, so a box can add them incrementally.
    audio = os.environ.get(audio_env)
    if not audio:
        pytest.skip(f"{audio_env} unset — point it at a real-speech clip in that container")
    pipeline_config = os.environ["ALFRED_SCRIBE_DIARIZE_PIPELINE_CONFIG"]
    tx = _tx(_seg(1, "real speech", start=0.0, end=10.0), diarized=False)
    out = assign_speakers(
        _config(enabled=True, pipeline_config=pipeline_config), audio, tx,
    )
    assert out.diarized is True
    seg = out.segments[0]
    assert seg.speaker_cluster is not None            # a real cluster was assigned
    assert 0.0 <= seg.speaker_conf <= 1.0             # finite purity, never NaN
    assert seg.speaker == ROLE_UNKNOWN                # no enrollment → unknown (P4-4)


# ===========================================================================
# QA round hardenings (A/B/C/D) — mutation-proof where noted
# ===========================================================================

# --- B1: invalid turn bounds skipped (non-finite / end<=start) --------------

@pytest.mark.parametrize("bad_turn", [
    (float("nan"), 10.0, "BAD"),
    (0.0, float("nan"), "BAD"),
    (float("inf"), 10.0, "BAD"),
    (8.0, 3.0, "BAD"),          # end <= start
    (5.0, 5.0, "BAD"),          # zero-duration
])
def test_b1_invalid_turn_does_not_corrupt_real_assignment(bad_turn):
    # A NaN/inf/inverted turn must NOT claim the segment: the real S00 turn wins,
    # purity 1.0. Mutant that must fail: dropping the turn-bounds validation (the
    # bad turn then steals overlap → wrong cluster / purity).
    turns = [bad_turn, (0.0, 10.0, "S00")]
    cl, pur = diarize_mod._dominant_cluster_over_intervals([(0.0, 10.0)], turns)
    assert cl == "S00" and pur == 1.0


def test_b1_apply_logs_dropped_invalid_turns(caplog):
    import structlog
    tx = _tx(_seg(1, start=0.0, end=10.0), diarized=False)
    with structlog.testing.capture_logs() as caps:
        diarize_mod._apply_diarization(_config(), tx, [(float("nan"), 1.0, "BAD"), (0.0, 10.0, "S00")])
    dropped = [c for c in caps if c.get("event") == "scribe.diarize.invalid_turns_dropped"]
    assert len(dropped) == 1 and dropped[0]["dropped"] == 1


# --- B2: same-cluster duplicate/overlapping turns merged (no double-count) ---

def test_b2_duplicate_same_cluster_turns_do_not_inflate_purity():
    # Two identical S00 turns + one S01 turn over [0,10]. WITHOUT merge, S00's time
    # double-counts → purity 0.75 (could clear an 0.7 threshold). Merged → 0.6.
    # Mutant that must fail: dropping the per-cluster interval merge.
    turns = [(0.0, 6.0, "S00"), (0.0, 6.0, "S00"), (6.0, 10.0, "S01")]
    cl, pur = diarize_mod._dominant_cluster_over_intervals([(0.0, 10.0)], turns)
    assert cl == "S00" and abs(pur - 0.6) < 1e-9


# --- B3: coverage floor — mostly-silence segment degrades to conf 0 ----------

def test_b3_low_coverage_degrades_conf():
    # Segment [0,10] with only [0,2] labeled → coverage 0.2 < 0.3 floor → conf 0.0
    # (fail-safe → unknown at P4-2), NOT purity 1.0. Mutant that must fail: dropping
    # the coverage floor.
    cl, pur = diarize_mod._dominant_cluster_over_intervals([(0.0, 10.0)], [(0.0, 2.0, "S00")])
    assert cl == "S00" and pur == 0.0


def test_b3_coverage_at_floor_not_degraded():
    # coverage exactly at the floor (3s labeled / 10s) is NOT degraded (>= floor).
    cl, pur = diarize_mod._dominant_cluster_over_intervals([(0.0, 10.0)], [(0.0, 3.0, "S00")])
    assert cl == "S00" and pur == 1.0


# --- B4: _guard_conf is actually WIRED through _apply_diarization ------------

def test_b4_apply_clamps_nonfinite_conf_from_engine(monkeypatch):
    # A NaN purity from the alignment must arrive CLAMPED (0.0) on the segment —
    # pins the _guard_conf CALL inside _apply_diarization. Mutant that must fail:
    # assigning the raw purity without _guard_conf.
    tx = _tx(_seg(1, start=0.0, end=5.0), diarized=False)
    monkeypatch.setattr(
        diarize_mod, "_dominant_cluster_over_intervals",
        lambda intervals, turns: ("S00", float("nan")),
    )
    diarize_mod._apply_diarization(_config(), tx, [(0.0, 5.0, "S00")])
    assert tx.segments[0].speaker_conf == 0.0
    assert tx.segments[0].speaker_cluster == "S00"


# --- A4: boot gate fails loud on enabled pyannote + missing pipeline_config ---

def test_a4_boot_gate_missing_config_fails_loud(monkeypatch):
    # enabled pyannote with pipeline_config unset → boot fails loud (exit 78). In
    # torch-free CI the dep-checks fire first, so pretend BOTH deps are present to
    # isolate the CONFIG gate. Mutant that must fail: dropping the config branch.
    monkeypatch.setattr(diarize_mod, "_pyannote_available", lambda: True)
    monkeypatch.setattr(diarize_mod, "_torchaudio_available", lambda: True)
    with pytest.raises(MissingDiarizeDependency):
        ensure_diarize_backend_available(_config(enabled=True, pipeline_config=""))


def test_a4_boot_gate_nonexistent_config_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setattr(diarize_mod, "_pyannote_available", lambda: True)
    monkeypatch.setattr(diarize_mod, "_torchaudio_available", lambda: True)
    with pytest.raises(MissingDiarizeDependency):
        ensure_diarize_backend_available(
            _config(enabled=True, pipeline_config=str(tmp_path / "nope.yaml")))


def test_a4_boot_gate_present_config_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(diarize_mod, "_pyannote_available", lambda: True)
    monkeypatch.setattr(diarize_mod, "_torchaudio_available", lambda: True)
    cfg_file = tmp_path / "pipe.yaml"
    cfg_file.write_text("x", encoding="utf-8")
    ensure_diarize_backend_available(_config(enabled=True, pipeline_config=str(cfg_file)))  # no raise


# --- A3: pre-import local-path validation of the materialized config ---------

def test_a3_validate_rejects_repo_id_config(tmp_path):
    # A config whose model refs are REPO IDS (operator mispointed at the original
    # snapshot config.yaml) fails loud pre-pyannote-import — closes the hub-GET hole.
    bad = tmp_path / "orig.yaml"
    bad.write_text(yaml.safe_dump(_pcfg()), encoding="utf-8")  # repo-id refs
    with pytest.raises(DiarizeError):
        diarize_mod._validate_materialized_config_local(bad)


def test_a3_validate_accepts_local_paths(tmp_path):
    seg = tmp_path / "seg.bin"; seg.write_bytes(b"x")
    emb = tmp_path / "emb.bin"; emb.write_bytes(b"x")
    good = _pcfg()
    good["pipeline"]["params"]["segmentation"] = str(seg)
    good["pipeline"]["params"]["embedding"] = str(emb)
    cfg = tmp_path / "materialized.yaml"
    cfg.write_text(yaml.safe_dump(good), encoding="utf-8")
    diarize_mod._validate_materialized_config_local(cfg)   # no raise


# --- C1: the word-CAPABLE core is the P4-5 word-threading precondition -------

def test_c1_word_level_seam_produces_per_interval_assignment():
    # P4-5 precondition (C1): the overlap core produces a CORRECT per-cluster
    # assignment from an interval-LIST (words), not just a single span — the seam
    # P4-5 threads STT word timings into works before P4-5 needs it. 3 words in S00
    # (1.5s), 1 word in S01 (1.0s) → S00 dominant, purity 1.5/2.5 = 0.6.
    turns = [(0.0, 5.0, "S00"), (5.0, 10.0, "S01")]
    words = [(0.5, 1.0), (2.0, 2.5), (3.0, 3.5), (7.0, 8.0)]
    cl, pur = diarize_mod._dominant_cluster_over_intervals(words, turns)
    assert cl == "S00" and abs(pur - 0.6) < 1e-9


# --- D1: token single-line validation ---------------------------------------

def test_d1_multiline_token_file_rejected(tmp_path):
    f = tmp_path / "tok"
    f.write_text("tok\nextra\n", encoding="utf-8")   # interior newline
    with pytest.raises(RuntimeError):
        stage_mod.resolve_token(token_file=f, env={})


def test_d1_interior_space_token_rejected():
    with pytest.raises(RuntimeError):
        stage_mod.resolve_token(token_file=None, env={"HF_TOKEN": "tok with space"})


# --- D2: stage() + main() (mocked snapshot download — the token-echo bug lived here) ---

def _fake_snapshots(tmp_path):
    seg = tmp_path / "seg"; seg.mkdir()
    (seg / "pytorch_model.bin").write_bytes(b"x")
    emb = tmp_path / "emb"; emb.mkdir()
    (emb / "pytorch_model.bin").write_bytes(b"x")
    diar = tmp_path / "diar"; diar.mkdir()
    (diar / "config.yaml").write_text(yaml.safe_dump(_pcfg()), encoding="utf-8")
    return {SEGMENTATION_REPO: seg, EMBEDDING_REPO: emb, DIARIZATION_REPO: diar}


def _install_fake_download(monkeypatch, mapping):
    def _fake(repo_id, *, hf_home, token):
        return mapping[repo_id]
    monkeypatch.setattr(stage_mod, "_snapshot_download", _fake)


def test_d2_stage_writes_materialized_config_with_local_paths(tmp_path, monkeypatch):
    snaps = _fake_snapshots(tmp_path)
    _install_fake_download(monkeypatch, snaps)
    out = tmp_path / "materialized.yaml"
    written = stage_mod.stage(hf_home=tmp_path / "hf", token="DUMMY", out_path=out)
    assert written == out
    m = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert m["pipeline"]["params"]["segmentation"] == str(snaps[SEGMENTATION_REPO] / "pytorch_model.bin")
    assert m["pipeline"]["params"]["embedding"] == str(snaps[EMBEDDING_REPO] / "pytorch_model.bin")
    # the materialized config must pass the runtime local-path validation
    diarize_mod._validate_materialized_config_local(out)


def test_d2_stage_fails_loud_on_missing_snapshot_config(tmp_path, monkeypatch):
    snaps = _fake_snapshots(tmp_path)
    (snaps[DIARIZATION_REPO] / "config.yaml").unlink()   # snapshot without the config
    _install_fake_download(monkeypatch, snaps)
    with pytest.raises(RuntimeError):
        stage_mod.stage(hf_home=tmp_path / "hf", token="DUMMY", out_path=tmp_path / "o.yaml")


def test_d2_main_default_out_and_printed_path_matches_stage(tmp_path, capsys, monkeypatch):
    _install_fake_download(monkeypatch, _fake_snapshots(tmp_path))
    monkeypatch.setenv("HF_TOKEN", "DUMMYTOKEN")
    hf = tmp_path / "hf"
    rc = stage_mod.main(["--hf-home", str(hf)])
    assert rc == 0
    default_out = hf / MATERIALIZED_CONFIG_NAME
    assert default_out.is_file()                         # written at the default path
    out = capsys.readouterr().out
    assert f"pipeline_config: {default_out}" in out      # the operator copy-paste value matches
    # idempotent re-run
    assert stage_mod.main(["--hf-home", str(hf)]) == 0


def test_d2_main_returns_1_on_stage_failure_token_not_logged(tmp_path, capsys, monkeypatch):
    def _boom(repo_id, *, hf_home, token):
        raise RuntimeError("network down")
    monkeypatch.setattr(stage_mod, "_snapshot_download", _boom)
    monkeypatch.setenv("HF_TOKEN", "SECRETTOKEN123")
    rc = stage_mod.main(["--hf-home", str(tmp_path / "hf")])
    assert rc == 1
    cap = capsys.readouterr()
    assert "SECRETTOKEN123" not in cap.out and "SECRETTOKEN123" not in cap.err


def test_d2_main_returns_2_on_missing_token(tmp_path, monkeypatch):
    monkeypatch.delenv("HF_TOKEN", raising=False)
    assert stage_mod.main(["--hf-home", str(tmp_path / "hf")]) == 2


# --- D4: YAML-null pipeline_config coerces to "" (not "None") ----------------

def test_d4_yaml_null_pipeline_config_is_empty_string():
    cfg = load_from_unified({"scribe": {"diarize": {
        "provider": "pyannote", "enabled": True, "pipeline_config": None,
    }}})
    assert cfg.diarize.pipeline_config == ""


# ===========================================================================
# P4-5c — real per-cluster embedding EXTRACTION (fake seam + pure logic)
#
# The placeholder that returned {} (blocking ALL clinician attribution) is now real.
# These pin the TORCH-FREE half — turn-grouping, merge, the net-speech floor, the
# engine-compat dim belt, the decode-degrade path, the extractor marker, and the
# min_turn_s COUPLING — against the fake embed seam + mocked decode/torch. The real
# wespeaker forward pass is the on-box IT at the bottom.
# ===========================================================================

import structlog as _structlog

from alfred.scribe import embed_voice
from alfred.scribe.enrollment import ResolvedEnrollment

_EMBED_DIM = embed_voice.EMBED_DIM


def _resolved(centroids, *, dim=None):
    return ResolvedEnrollment(
        user="np_jamie", preset_id="pst-0000000000000-0000000000000000",
        centroid_version=1, centroids=centroids,
        embedding_dim=dim if dim is not None else _EMBED_DIM,
    )


# --- _pool_turns_by_cluster: grouping + merge + net-speech floor (pure) ------

def test_pool_groups_by_cluster_and_merges_overlaps():
    # Two S00 turns (one overlapping the other) + one S01 turn. S00's overlap is MERGED
    # (counted once), so its net is 4.0 (0-4), not 6.0; S01 is 3.0.
    turns = [(0.0, 3.0, "S00"), (2.0, 4.0, "S00"), (5.0, 8.0, "S01")]
    pooled = diarize_mod._pool_turns_by_cluster(turns, min_speech_s=1.0)
    assert pooled["S00"] == [(0.0, 4.0)]              # merged, not two spans
    assert pooled["S01"] == [(5.0, 8.0)]


def test_pool_floor_omits_below_keeps_at_and_above():
    # net < floor → OMITTED; net == floor → kept; net > floor → kept.
    turns = [(0.0, 0.8, "LOW"), (0.0, 1.0, "AT"), (0.0, 1.5, "HI")]
    pooled = diarize_mod._pool_turns_by_cluster(turns, min_speech_s=1.0)
    assert "LOW" not in pooled                        # 0.8 < 1.0 → dropped (noise floor)
    assert "AT" in pooled and "HI" in pooled          # >= floor kept


def test_pool_sums_multiple_subfloor_turns_over_the_floor():
    # THE net-per-cluster refinement: 3 individually-sub-floor turns (0.5s each, NON-adjacent
    # so not merged into one) SUM to 1.5s net → the cluster clears the 1.0 floor and is kept.
    turns = [(0.0, 0.5, "C"), (2.0, 2.5, "C"), (4.0, 4.5, "C")]
    pooled = diarize_mod._pool_turns_by_cluster(turns, min_speech_s=1.0)
    assert "C" in pooled and len(pooled["C"]) == 3    # three separate sub-floor spans, kept


# --- fake-seam extraction: dim, determinism, floor omission ------------------

def test_fake_extraction_returns_unit_256_per_kept_cluster():
    cfg = _config(provider="fake")
    turns = [(0.0, 5.0, "S00"), (5.0, 10.0, "S01")]
    emb = diarize_mod._cluster_embeddings_for(
        cfg, None, turns, expected_dim=_EMBED_DIM, source_id="enc")
    assert set(emb) == {"S00", "S01"}
    for v in emb.values():
        assert len(v) == _EMBED_DIM
        assert abs(math.sqrt(sum(x * x for x in v)) - 1.0) < 1e-9


def test_fake_extraction_is_deterministic():
    cfg = _config(provider="fake")
    turns = [(0.0, 5.0, "S00")]
    a = diarize_mod._cluster_embeddings_for(cfg, None, turns, expected_dim=_EMBED_DIM)
    b = diarize_mod._cluster_embeddings_for(cfg, None, turns, expected_dim=_EMBED_DIM)
    assert a == b


def test_fake_extraction_below_floor_cluster_omitted():
    cfg = _config(provider="fake")
    # S00 clears the 1.0 floor, S01 (0.5s) does not → only S00 embedded.
    turns = [(0.0, 5.0, "S00"), (5.0, 5.5, "S01")]
    emb = diarize_mod._cluster_embeddings_for(cfg, None, turns, expected_dim=_EMBED_DIM)
    assert set(emb) == {"S00"}


# --- intentionally-left-blank taxonomy: the explicit-log branches ------------

def test_extraction_no_turns_logs_and_empty():
    cfg = _config(provider="fake")
    with _structlog.testing.capture_logs() as caps:
        emb = diarize_mod._cluster_embeddings_for(cfg, None, [], expected_dim=_EMBED_DIM,
                                                  source_id="enc")
    assert emb == {}
    ev = [c for c in caps if c.get("event") == "scribe.diarize.extraction_empty"]
    assert len(ev) == 1 and ev[0]["reason"] == "no_turns"


def test_extraction_all_below_floor_logs_no_cluster_above_floor():
    cfg = _config(provider="fake")
    turns = [(0.0, 0.4, "A"), (1.0, 1.3, "B")]         # both sub-floor
    with _structlog.testing.capture_logs() as caps:
        emb = diarize_mod._cluster_embeddings_for(cfg, None, turns, expected_dim=_EMBED_DIM,
                                                  source_id="enc")
    assert emb == {}
    ev = [c for c in caps if c.get("event") == "scribe.diarize.extraction_empty"]
    assert len(ev) == 1 and ev[0]["reason"] == "no_cluster_above_floor"
    assert ev[0]["min_turn_s"] == 1.0                  # pins the field flows into the log


def test_extraction_engine_dim_mismatch_omits_and_logs():
    # The engine-compat BELT: the fake seam always emits 256-dim, so an expected_dim of 128
    # (a preset from an incompatible engine) mismatches → cluster OMITTED (never handed to
    # the matcher as a wrong-dim vector cosine would silently score 0.0) + explicit logs.
    cfg = _config(provider="fake")
    turns = [(0.0, 5.0, "S00")]
    with _structlog.testing.capture_logs() as caps:
        emb = diarize_mod._cluster_embeddings_for(cfg, None, turns, expected_dim=128,
                                                  source_id="enc")
    assert emb == {}                                   # nothing survives the dim guard
    mism = [c for c in caps if c.get("event") == "scribe.diarize.extraction_engine_mismatch"]
    assert len(mism) == 1 and mism[0]["expected_dim"] == 128 and mism[0]["clusters_omitted"] == 1
    empty = [c for c in caps if c.get("event") == "scribe.diarize.extraction_empty"]
    assert len(empty) == 1 and empty[0]["reason"] == "engine_mismatch"


# --- pyannote seam: decode-degrade, no-audio-path, slicing math (mocked torch) ---

def test_pyannote_extraction_decode_failure_degrades_not_raises(monkeypatch):
    # A decode failure inside extraction DEGRADES to {} + an explicit log (chunk still folds
    # diarized, all-unknown), never propagates — the fold is not crashed.
    def _boom(path):
        raise diarize_mod.AudioDecodeError("ffmpeg could not demux")
    monkeypatch.setattr(diarize_mod, "_decode_audio", _boom)
    # torch is imported lazily in the pyannote seam; provide a stub so CI never needs it.
    monkeypatch.setitem(sys.modules, "torch", __import__("types").ModuleType("torch"))
    cfg = _config(provider="pyannote", enabled=True, pipeline_config="/x.yaml")
    turns = [(0.0, 5.0, "S00")]
    with _structlog.testing.capture_logs() as caps:
        emb = diarize_mod._cluster_embeddings_for(cfg, "/enc/chunk.webm", turns,
                                                  expected_dim=_EMBED_DIM, source_id="enc")
    assert emb == {}
    ev = [c for c in caps if c.get("event") == "scribe.diarize.extraction_empty"]
    assert len(ev) == 1 and ev[0]["reason"] == "decode_failed"


def test_pyannote_extraction_no_audio_path_degrades(monkeypatch):
    monkeypatch.setitem(sys.modules, "torch", __import__("types").ModuleType("torch"))
    cfg = _config(provider="pyannote", enabled=True, pipeline_config="/x.yaml")
    with _structlog.testing.capture_logs() as caps:
        emb = diarize_mod._cluster_embeddings_for(cfg, None, [(0.0, 5.0, "S00")],
                                                  expected_dim=_EMBED_DIM, source_id="enc")
    assert emb == {}
    ev = [c for c in caps if c.get("event") == "scribe.diarize.extraction_empty"]
    assert len(ev) == 1 and ev[0]["reason"] == "no_audio_path"


class _SliceRec:
    """Torch-free mono-waveform stand-in that RECORDS the (start, stop) sample ranges sliced
    out of it — lets a test pin the slicing math (round(t*sr), clamp) without torch."""

    def __init__(self, total):
        self._total = total
        self.slices: list[tuple[int, int]] = []

    @property
    def shape(self):
        return (1, self._total)

    def __getitem__(self, key):
        _, tsl = key                       # key == (slice(None), slice(a, b))
        self.slices.append((tsl.start, tsl.stop))
        return ("SLICE", tsl.start, tsl.stop)


def test_pyannote_extraction_slicing_math_and_pooling(monkeypatch):
    # Pin the slicing math: merged intervals → sample indices round(t*sr), CLAMPED to the
    # decoded length, one embed call per cluster, torch.cat for a multi-span pool. All mocked.
    sr = 16000
    wave = _SliceRec(total=sr * 10)                    # a 10s mono clip
    monkeypatch.setattr(diarize_mod, "_decode_audio", lambda p: (wave, sr))
    fake_torch = __import__("types").ModuleType("torch")
    fake_torch.cat = lambda parts, dim: ("CAT", tuple(parts), dim)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    seen = []
    monkeypatch.setattr(embed_voice, "embed_waveform",
                        lambda cfg, w, s: seen.append((w, s)) or _unit_vec(_EMBED_DIM))
    cfg = _config(provider="pyannote", enabled=True, pipeline_config="/x.yaml")
    # S00 = two NON-adjacent merged spans (→ 2 slices → cat); S99 = one span clamped past end.
    turns = [(1.0, 2.0, "S00"), (5.0, 6.0, "S00"), (9.0, 12.0, "S99")]
    emb = diarize_mod._cluster_embeddings_for(cfg, "/enc/c.webm", turns,
                                              expected_dim=_EMBED_DIM, source_id="enc")
    assert set(emb) == {"S00", "S99"}
    # slicing indices: round(t*sr), the 12.0s bound CLAMPED to total=160000.
    assert wave.slices == [(16000, 32000), (80000, 96000), (144000, 160000)]
    # S00 pooled via torch.cat (2 slices); the embedder was called once per cluster.
    assert len(seen) == 2 and seen[0][1] == sr
    assert seen[0][0][0] == "CAT"                      # S00's pooled wave is the cat marker


# --- _apply_diarization end-to-end: match + extractor marker (fake seam) ------

def _fake_centroid_for(cfg, cluster, intervals):
    payload = diarize_mod._fake_cluster_payload(cluster, intervals)
    return embed_voice.embed_windows(cfg, [payload])[0]


def test_apply_diarization_real_extraction_resolves_clinician(monkeypatch):
    # END-TO-END through _apply_diarization with a fake-provider resolved: the enrolled
    # centroid IS the fake extraction vector for S00's pooled speech → S00 self-matches
    # (cosine 1.0) → clinician; S01 (a different voice) → unknown. Pins that the extraction
    # output actually drives the K=2 match, and match_sink carries the telemetry + marker.
    cfg = _config(provider="fake")
    turns = [(0.0, 5.0, "SPEAKER_00"), (5.0, 10.0, "SPEAKER_01")]
    centroid = _fake_centroid_for(cfg, "SPEAKER_00", [(0.0, 5.0)])
    tx = _tx(_seg(1, start=0.0, end=5.0), _seg(2, start=5.0, end=10.0), diarized=False)
    sink: dict = {}
    diarize_mod._apply_diarization(cfg, tx, turns, resolved=_resolved([centroid]),
                                   audio_path=None, match_sink=sink)
    assert tx.segments[0].speaker == ROLE_CLINICIAN     # S00 self-match
    assert tx.segments[1].speaker == ROLE_UNKNOWN       # S01 other voice
    assert sink["matched"] is True and sink["best_cosine"] > 0.75
    assert sink["extractor"] == diarize_mod.EXTRACTOR_VERSION


def test_apply_diarization_stamps_extractor_even_when_extraction_empty():
    # The marker means 'extractor WIRED', not 'match succeeded': an all-below-floor chunk
    # (extraction {} → all-unknown) STILL stamps extractor. This is the exact placeholder-era
    # discriminator case — a real fingerprint, best_cosine 0.0, but distinguishable by marker.
    cfg = _config(provider="fake")
    turns = [(0.0, 0.4, "S00"), (1.0, 1.4, "S01")]      # all sub-floor → extraction empty
    tx = _tx(_seg(1, start=0.0, end=0.4), diarized=False)
    sink: dict = {}
    diarize_mod._apply_diarization(cfg, tx, turns, resolved=_resolved([_unit_vec(_EMBED_DIM)]),
                                   audio_path=None, match_sink=sink)
    assert sink["matched"] is False and sink["best_cosine"] == 0.0
    assert sink["extractor"] == diarize_mod.EXTRACTOR_VERSION   # wired, even with no match
    assert all(s.speaker == ROLE_UNKNOWN for s in tx.segments)


def test_apply_diarization_no_resolved_does_not_stamp_extractor_or_extract(monkeypatch):
    # Without enrollment (resolved is None) the extraction NEVER runs and no marker is stamped
    # — the 'no enrollment' branch stays byte-identical to the P4-4 all-unknown end-state.
    called = {"n": 0}
    monkeypatch.setattr(diarize_mod, "_cluster_embeddings_for",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {})
    tx = _tx(_seg(1, start=0.0, end=5.0), diarized=False)
    sink: dict = {}
    diarize_mod._apply_diarization(_config(), tx, [(0.0, 5.0, "S00")],
                                   resolved=None, match_sink=sink)
    assert called["n"] == 0 and "extractor" not in sink   # extraction not invoked
    assert tx.segments[0].speaker == ROLE_UNKNOWN


def test_apply_diarization_extraction_raise_leaves_chunk_foldable_unattributed(monkeypatch):
    # NOTE-2 at the P4-5c EXTRACTION site: extraction runs INSIDE the `resolved is not None`
    # branch, BEFORE stage/commit — an UNEXPECTED (torch-OOM-class) raise from embed_waveform
    # must PROPAGATE (fail-open), leaving the chunk UNTOUCHED (speaker None, diarized False) so
    # the pipeline folds it un-attributed, AND must NOT stamp the sink's extractor marker (a
    # CRASHED extraction never produced a health-eligible row — if it stamped, a poisoned
    # all-unknown row would count toward 5b health, the exact failure the marker prevents).
    # Distinct from the staging-raise + engine-raise pins above: those pass resolved=None and
    # so NEVER enter the extraction branch this pins.
    sr = 16000
    wave = _SliceRec(total=sr * 10)
    monkeypatch.setattr(diarize_mod, "_decode_audio", lambda p: (wave, sr))
    monkeypatch.setitem(sys.modules, "torch", __import__("types").ModuleType("torch"))

    def _oom(cfg, w, s):
        raise RuntimeError("torch OOM mid-extraction")

    monkeypatch.setattr(embed_voice, "embed_waveform", _oom)
    cfg = _config(provider="pyannote", enabled=True, pipeline_config="/x.yaml")
    tx = _tx(_seg(1, start=0.0, end=5.0), _seg(2, start=5.0, end=10.0), diarized=False)
    sink: dict = {}
    with pytest.raises(RuntimeError):
        diarize_mod._apply_diarization(
            cfg, tx, [(0.0, 5.0, "SPEAKER_00")],
            resolved=_resolved([_unit_vec(_EMBED_DIM)]),
            audio_path="/enc/c.webm", match_sink=sink,
        )
    assert all(
        s.speaker is None and s.speaker_cluster is None and s.speaker_conf is None
        for s in tx.segments
    )
    assert tx.diarized is False
    assert "extractor" not in sink   # a crashed extraction never stamps a health-eligible row


# --- the min_turn_s COUPLING pin (one knob, two consumers) -------------------

def test_min_turn_s_couples_extraction_floor_and_eligible_denominator():
    # ONE calibrate knob (config.diarize.min_turn_s) governs BOTH the per-cluster extraction
    # floor (diarize._pool_turns_by_cluster) AND the per-segment 5b eligible denominator
    # (pipeline._eligible_turns). Pin that both read the SAME field and MOVE TOGETHER, so a
    # future split of the field is a conscious act, not silent drift.
    from alfred.scribe.pipeline import _eligible_turns

    turns = [(0.0, 1.5, "C")]                          # a single 1.5s cluster
    seg = [_seg(1, start=0.0, end=1.5)]                # a single 1.5s segment
    # default 1.0 → the 1.5s cluster is embeddable AND the 1.5s segment is eligible.
    cfg1 = _config()
    assert cfg1.diarize.min_turn_s == 1.0
    assert "C" in diarize_mod._pool_turns_by_cluster(turns, min_speech_s=cfg1.diarize.min_turn_s)
    assert _eligible_turns(seg, cfg1.diarize.min_turn_s) == 1
    # raise the ONE field to 2.0 → the SAME 1.5s cluster falls below the extraction floor AND
    # the SAME 1.5s segment falls out of the eligible denominator. Both move, from one knob.
    cfg2 = _config()
    cfg2.diarize.min_turn_s = 2.0
    assert diarize_mod._pool_turns_by_cluster(turns, min_speech_s=cfg2.diarize.min_turn_s) == {}
    assert _eligible_turns(seg, cfg2.diarize.min_turn_s) == 0


# --- P4-5c real extraction → real centroid match — on-box IT (skip-gated) -----

@pytest.mark.skipif(
    not os.environ.get("ALFRED_SCRIBE_DIARIZE_IT"),
    reason="real wespeaker extraction → match — set ALFRED_SCRIBE_DIARIZE_IT=1 on-box with "
           "the [scribe-diarize] extra, $ALFRED_SCRIBE_DIARIZE_PIPELINE_CONFIG (materialized) "
           "+ the committed tests/fixtures/diarize/short_speech.{webm,m4a,wav}",
)
@pytest.mark.parametrize("container", ["webm", "m4a", "wav"])
def test_real_extraction_matches_enrolled_centroid_on_box(container):
    # THE on-box proof: enroll a centroid from a real clip (the enrollment window path), then
    # run the REAL per-cluster extraction over the SAME clip as chunk audio (one cluster
    # spanning it) and match — SAME speaker → best_cosine > tau, matched=True. This is the
    # pre-deploy proxy for the live acceptance (re-running Jamie's encounter on the box).
    fixture = _DIARIZE_FIXTURES / f"short_speech.{container}"
    if not fixture.is_file():
        pytest.skip(f"missing extraction fixture {fixture}")
    pipeline_config = os.environ["ALFRED_SCRIBE_DIARIZE_PIPELINE_CONFIG"]
    cfg = _config(provider="pyannote", enabled=True, pipeline_config=pipeline_config)
    # (1) enroll a real centroid from the clip (the same embedder the extraction reuses).
    import soundfile as _sf  # noqa: F401 — proves the audio libs are on-box; skip if absent
    from alfred.scribe import enrollment as _en
    window = fixture.read_bytes()
    centroid = _en.spherical_mean_centroid(embed_voice.embed_windows(cfg, [window]))
    # (2) extract from the SAME clip as chunk audio — one cluster spanning the whole clip.
    wave, sr = diarize_mod._decode_audio(fixture)
    dur = wave.shape[1] / sr
    turns = [(0.0, float(dur), "SPEAKER_00")]
    emb = diarize_mod._cluster_embeddings_for(
        cfg, fixture, turns, expected_dim=_EMBED_DIM, source_id="it")
    assert set(emb) == {"SPEAKER_00"} and len(emb["SPEAKER_00"]) == _EMBED_DIM
    # (3) match the extracted cluster against the enrolled centroid — SAME speaker.
    m = diarize_mod.match_cluster_roles(
        emb, [centroid],
        tau=cfg.diarize.match_threshold, delta=cfg.diarize.separation_margin)
    assert m.best_cosine > cfg.diarize.match_threshold      # a strong same-speaker match
    assert m.matched is True and m.roles["SPEAKER_00"] == ROLE_CLINICIAN


def _unit_vec(dim):
    """A deterministic pseudo-random UNIT vector of length ``dim`` (fixture centroid)."""
    import hashlib as _h
    import struct as _st
    out = []
    seed = _h.sha256(b"p4-5c-unit-vec").digest()
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
