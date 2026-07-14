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
import yaml

import alfred.scribe.diarize as diarize_mod
import alfred.scripts.stage_diarize_models as stage_mod
from alfred.scribe.config import load_from_unified
from alfred.scribe.diarize import (
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
    # torch-free CI the dep-check fires first, so pretend pyannote is present to
    # isolate the CONFIG gate. Mutant that must fail: dropping the config branch.
    monkeypatch.setattr(diarize_mod, "_pyannote_available", lambda: True)
    with pytest.raises(MissingDiarizeDependency):
        ensure_diarize_backend_available(_config(enabled=True, pipeline_config=""))


def test_a4_boot_gate_nonexistent_config_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setattr(diarize_mod, "_pyannote_available", lambda: True)
    with pytest.raises(MissingDiarizeDependency):
        ensure_diarize_backend_available(
            _config(enabled=True, pipeline_config=str(tmp_path / "nope.yaml")))


def test_a4_boot_gate_present_config_passes(monkeypatch, tmp_path):
    monkeypatch.setattr(diarize_mod, "_pyannote_available", lambda: True)
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
