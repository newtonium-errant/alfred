"""P4-1 diarization plumbing + the FAKE seam (scribe P4).

UNCONDITIONAL — no torch, no pyannote, no ``importorskip``. The fake backend +
pure primitives give the P4-1 plumbing full CI coverage BEFORE the heavy engine
lands (P4-4), and freeze the data shapes every later phase depends on. Covers:

  1. ``normalize_role`` fold table (the single source of truth; anything unknown
     — incl. ``SPEAKER_00`` / None / '' — folds fail-closed to ``unknown``);
  2. the fake role-tagged sidecar → resolved roles reach the ledger through
     ``append_chunk`` with ids re-minted + tags stripped;
  3. ``Transcript.diarized`` round-trips through ledger save/load AND ``delta()``,
     and ``append_chunk`` carries all three P4 segment fields;
  4. ``provider='off'`` is a true no-op — speaker stays None, diarized stays
     False, and the rendered note is BYTE-IDENTICAL to the P3 path;
  5. a diarize EXCEPTION degrades to speaker=None + a loud log and STILL folds
     (fail-open-for-availability — does NOT hold the encounter / fail the source);
  6. the sovereign barrier-a sibling (diarize provider allowlist) + the
     dispatch-set contract pin + the exit-78 dep guard.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import structlog

import alfred.distiller.backends.ollama as ollama_mod
import alfred.scribe.diarize as diarize_mod
from alfred.scribe import (
    ScribeState,
    accumulate_encounter,
    assign_speakers,
    compute_encounter_id,
    ensure_diarize_backend_available,
    generate_verified_note,
    ledger_path,
    load_from_unified,
    load_ledger,
    process_source,
    save_ledger,
    transcribe,
)
from alfred.scribe.diarize import (
    SCRIBE_DIARIZE_PROVIDERS,
    DiarizeError,
    MissingDiarizeDependency,
)
from alfred.scribe.transcript import (
    ROLE_CLINICIAN,
    ROLE_OTHER,
    ROLE_PATIENT,
    ROLE_UNKNOWN,
    Segment,
    Transcript,
    normalize_role,
)
from alfred.sovereign import SOVEREIGN_DIARIZE_ALLOWLIST, SovereignBoundaryError
from alfred.sovereign.boundary import validate_sovereign_boundary

# Obviously-fake test salt (NOT a real-provider-shaped secret) — the sovereign
# scribe fail-louds without one (P3-b1), so every fixture config carries it.
_SALT = "DUMMY_SCRIBE_TEST_SALT"


def _config(provider="fake", mode="synthetic", *, enabled=False, pipeline_config=""):
    return load_from_unified({"scribe": {
        "mode": mode,
        "encounter_salt": _SALT,
        "stt": {"provider": "fake"},
        "diarize": {
            "provider": provider, "enabled": enabled,
            "pipeline_config": pipeline_config,
        },
        "llm": {"base_url": "http://127.0.0.1:11434", "model": "m"},
    }})


def _write_sidecar(tmp_path, *lines, stem="enc1"):
    """Audio placeholder + fake-STT ``.txt`` sidecar (one line per segment)."""
    audio = tmp_path / f"{stem}.wav"
    audio.write_bytes(b"")
    (tmp_path / f"{stem}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return audio


def _write_chunk(enc_dir, seq, lines, *, synthetic=True, pad=3):
    """``chunk_NNN.wav`` + role-tagged ``.txt`` sidecar + ``.meta.json`` marker.
    Distinct bytes per seq so content-hashes differ."""
    enc_dir.mkdir(parents=True, exist_ok=True)
    name = f"chunk_{seq:0{pad}d}"
    (enc_dir / f"{name}.wav").write_bytes(f"audio-bytes-seq-{seq}".encode())
    (enc_dir / f"{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (enc_dir / f"{name}.meta.json").write_text(
        json.dumps({"synthetic": synthetic, "seq": seq}), encoding="utf-8"
    )


def _fake_ollama():
    """Canned qwen JSON (clean note, cites S1) — no real model."""
    async def _f(prompt, system=None, model="", endpoint="", **kw):
        return (
            json.dumps({
                "subjective": [{"claim": "Chest pain for 2 days", "source_spans": ["S1"]}],
                "objective": [], "assessment": [], "plan": [],
                "assessment_reasoning_stated": False,
            }),
            {"stop_reason": "stop", "prompt_eval_count": 500},
        )
    return _f


# ---------------------------------------------------------------------------
# 1. normalize_role — the single-source-of-truth fold table (fail-closed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("clinician", ROLE_CLINICIAN),
    ("Clinician", ROLE_CLINICIAN),
    ("DOCTOR", ROLE_CLINICIAN),
    ("provider", ROLE_CLINICIAN),
    ("patient", ROLE_PATIENT),
    ("  Patient  ", ROLE_PATIENT),
    ("caregiver", ROLE_OTHER),
    ("family", ROLE_OTHER),
    ("other", ROLE_OTHER),
    # EVERYTHING else folds fail-closed to unknown:
    ("SPEAKER_00", ROLE_UNKNOWN),
    ("SPEAKER_01", ROLE_UNKNOWN),
    ("nurse", ROLE_UNKNOWN),
    ("garbage", ROLE_UNKNOWN),
    (None, ROLE_UNKNOWN),
    ("", ROLE_UNKNOWN),
    ("   ", ROLE_UNKNOWN),
    (42, ROLE_UNKNOWN),          # non-str
])
def test_normalize_role_fold_table(raw, expected):
    assert normalize_role(raw) == expected


def test_normalize_role_never_leaks_a_known_role_for_junk():
    # A role-assigner leak (a raw cluster) must degrade to unknown, never a
    # silent known role.
    for junk in ("SPEAKER_00", "spk1", "cluster-3", None, "", "unknown"):
        assert normalize_role(junk) == ROLE_UNKNOWN


# ---------------------------------------------------------------------------
# 2. contract pins — dispatch set == sovereign allowlist
# ---------------------------------------------------------------------------

def test_diarize_provider_set_equals_sovereign_allowlist():
    assert SCRIBE_DIARIZE_PROVIDERS == SOVEREIGN_DIARIZE_ALLOWLIST
    assert SCRIBE_DIARIZE_PROVIDERS == frozenset({"off", "fake", "pyannote"})


# ---------------------------------------------------------------------------
# 3. the fake seam — role-tagged sidecar → resolved roles + tag stripped
# ---------------------------------------------------------------------------

def test_fake_diarize_assigns_roles_and_strips_tags(tmp_path):
    audio = _write_sidecar(
        tmp_path,
        "[CLIN] What brings you in today?",
        "[PT] Chest pain for two days.",
        "[OTHER] He has been resting a lot.",
        "No tag here.",
    )
    cfg = _config(provider="fake")
    tx = transcribe(cfg, audio, source_id="enc1")
    assert all(s.speaker is None for s in tx.segments)   # STT leaves speaker None

    tx = assign_speakers(cfg, audio, tx)
    assert tx.diarized is True
    assert [s.speaker for s in tx.segments] == [
        ROLE_CLINICIAN, ROLE_PATIENT, ROLE_OTHER, ROLE_UNKNOWN,
    ]
    # tag stripped from the resolved text; the untagged line is verbatim
    assert tx.segments[0].text == "What brings you in today?"
    assert tx.segments[1].text == "Chest pain for two days."
    assert tx.segments[3].text == "No tag here."


def test_fake_diarize_tag_case_insensitive(tmp_path):
    audio = _write_sidecar(tmp_path, "[clin] hi", "[Pt] ow")
    cfg = _config(provider="fake")
    tx = assign_speakers(cfg, audio, transcribe(cfg, audio, source_id="e"))
    assert [s.speaker for s in tx.segments] == [ROLE_CLINICIAN, ROLE_PATIENT]


def test_fake_diarize_unrecognized_bracket_is_untagged(tmp_path):
    # A bracketed token that is NOT a known role tag falls through the tokenizer
    # to the untagged path → unknown, and the text is left verbatim (the tag is
    # NOT stripped — only recognized [CLIN]/[PT]/[OTHER] strip).
    audio = _write_sidecar(tmp_path, "[FOO] mystery speaker", "[PT] a patient")
    cfg = _config(provider="fake")
    tx = assign_speakers(cfg, audio, transcribe(cfg, audio, source_id="e"))
    assert tx.segments[0].speaker == ROLE_UNKNOWN
    assert tx.segments[0].text == "[FOO] mystery speaker"   # unchanged
    assert tx.segments[1].speaker == ROLE_PATIENT
    assert tx.segments[1].text == "a patient"


def test_fake_diarize_emits_assigned_log(tmp_path):
    # Log-emission pin (feedback_log_emission_test_pattern): the assigned log must
    # be driven by the production path + carry the per-role breakdown.
    audio = _write_sidecar(tmp_path, "[CLIN] Hi", "[PT] Ow")
    cfg = _config(provider="fake")
    tx = transcribe(cfg, audio, source_id="enc1")
    with structlog.testing.capture_logs() as cap:
        assign_speakers(cfg, audio, tx)
    ev = [c for c in cap if c.get("event") == "scribe.diarize.assigned"]
    assert len(ev) == 1
    assert ev[0]["segments"] == 2
    assert ev[0]["clinician"] == 1 and ev[0]["patient"] == 1
    assert ev[0]["other"] == 0 and ev[0]["unknown"] == 0


def test_fake_diarize_missing_sidecar_raises(tmp_path):
    cfg = _config(provider="fake")
    tx = Transcript(source_id="e", mode="synthetic", segments=[
        Segment(id="S1", start_s=0, end_s=5, text="x"),
    ])
    with pytest.raises(DiarizeError):
        assign_speakers(cfg, tmp_path / "nope.wav", tx)


# ---------------------------------------------------------------------------
# 4. roles reach the LEDGER through append_chunk with ids re-minted
# ---------------------------------------------------------------------------

def test_roles_reach_ledger_through_append_chunk_reminted(tmp_path):
    enc = tmp_path / "encounterA"
    _write_chunk(enc, 1, ["[CLIN] Hello.", "[PT] I have a cough."])
    _write_chunk(enc, 2, ["[OTHER] Since Tuesday.", "Background noise."])
    cfg = _config(provider="fake")

    r = accumulate_encounter(enc, config=cfg)
    assert r.folded == 2 and r.held == 0 and r.decode_failed is False

    eid = compute_encounter_id(enc.name, salt=_SALT)
    led = load_ledger(ledger_path(enc, eid))
    # ids re-minted continuously across the two folded chunks
    assert [s.id for s in led.segments] == ["S1", "S2", "S3", "S4"]
    # the three roles (+ the untagged unknown) reached the ledger
    assert [s.speaker for s in led.segments] == [
        ROLE_CLINICIAN, ROLE_PATIENT, ROLE_OTHER, ROLE_UNKNOWN,
    ]
    # accumulated ledger latched the diarized gate (P4-2 reads the ledger)
    assert led.diarized is True
    # tag stripped in the persisted text
    assert led.segments[0].text == "Hello."


def test_append_chunk_carries_all_speaker_fields():
    acc = Transcript(source_id="e", mode="synthetic")
    chunk = Transcript(source_id="e", mode="synthetic", diarized=True, segments=[
        Segment(id="S9", start_s=0, end_s=5, text="hi",
                speaker=ROLE_CLINICIAN, speaker_cluster="SPEAKER_01", speaker_conf=0.77),
    ])
    assert acc.append_chunk(chunk, audio_offset_s=0.0, chunk_key="k1", seq=1)
    seg = acc.segments[0]
    assert seg.id == "S1"                    # re-minted at append
    assert seg.speaker == ROLE_CLINICIAN
    assert seg.speaker_cluster == "SPEAKER_01"
    assert seg.speaker_conf == 0.77
    assert acc.diarized is True              # latched from the folded diarized chunk


def test_append_chunk_undiarized_chunk_does_not_latch():
    acc = Transcript(source_id="e", mode="synthetic")
    chunk = Transcript(source_id="e", mode="synthetic", diarized=False, segments=[
        Segment(id="S1", start_s=0, end_s=5, text="hi"),
    ])
    assert acc.append_chunk(chunk, audio_offset_s=0.0, chunk_key="k1", seq=1)
    assert acc.diarized is False


# ---------------------------------------------------------------------------
# 5. Transcript.diarized round-trips (ledger save/load + delta())
# ---------------------------------------------------------------------------

def test_diarized_round_trips_through_ledger(tmp_path):
    tx = Transcript(source_id="e", mode="synthetic", diarized=True, segments=[
        Segment(id="S1", start_s=0, end_s=5, text="hi",
                speaker=ROLE_PATIENT, speaker_cluster="SPEAKER_00", speaker_conf=0.9),
    ])
    p = tmp_path / "e.transcript.json"
    save_ledger(p, tx)
    loaded = load_ledger(p)
    assert loaded.diarized is True
    seg = loaded.segments[0]
    assert seg.speaker == ROLE_PATIENT
    assert seg.speaker_cluster == "SPEAKER_00"
    assert seg.speaker_conf == 0.9


def test_undiarized_round_trips_false(tmp_path):
    tx = Transcript(source_id="e", mode="synthetic", segments=[
        Segment(id="S1", start_s=0, end_s=5, text="hi"),
    ])
    p = tmp_path / "e.transcript.json"
    save_ledger(p, tx)
    loaded = load_ledger(p)
    assert loaded.diarized is False
    assert loaded.segments[0].speaker is None
    assert loaded.segments[0].speaker_cluster is None
    assert loaded.segments[0].speaker_conf is None


def test_diarized_carries_through_delta():
    tx = Transcript(source_id="e", mode="synthetic", diarized=True, segments=[
        Segment(id="S1", start_s=0, end_s=5, text="hi", speaker=ROLE_PATIENT),
    ])
    d = tx.delta()
    assert d.diarized is True                # the frozen-contract delta() fix
    assert d.segments[0].speaker == ROLE_PATIENT
    # a fresh un-diarized transcript's delta stays False (the default carries)
    assert Transcript(source_id="e", mode="synthetic").delta().diarized is False


# ---------------------------------------------------------------------------
# 6. provider='off' — a true no-op; note BYTE-IDENTICAL to P3
# ---------------------------------------------------------------------------

def test_off_provider_is_a_noop(tmp_path):
    audio = _write_sidecar(tmp_path, "[PT] Chest pain.", "[CLIN] Since when?")
    cfg = _config(provider="off")
    tx = transcribe(cfg, audio, source_id="enc1")
    before = tx.to_dict()
    out = assign_speakers(cfg, audio, tx)
    assert out is tx                          # same object, untouched
    assert out.to_dict() == before            # byte-identical transcript
    assert all(s.speaker is None for s in out.segments)
    assert out.diarized is False


def test_off_provider_note_byte_identical_to_p3(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama())
    audio = _write_sidecar(
        tmp_path, "Patient reports chest pain for 2 days.", "Amoxicillin 500mg.",
    )
    cfg = _config(provider="off")
    stt_tx = transcribe(cfg, audio, source_id="enc1")

    # P3 baseline: note rendered straight from the STT transcript (no diarize).
    p3_note = asyncio.run(generate_verified_note(stt_tx.delta(), config=cfg, title="E"))
    # off path: diarize('off') is a no-op, then render.
    off_tx = assign_speakers(cfg, audio, stt_tx)
    off_note = asyncio.run(generate_verified_note(off_tx.delta(), config=cfg, title="E"))

    assert off_note.body == p3_note.body      # BYTE-IDENTICAL rendered note
    assert all(s.speaker is None for s in off_tx.segments)
    assert off_tx.diarized is False


# ---------------------------------------------------------------------------
# 7. fail-open-for-availability — a diarize exception folds un-attributed
# ---------------------------------------------------------------------------

def test_accumulate_diarize_failure_folds_unattributed(tmp_path, monkeypatch):
    def _boom(config, audio_path, chunk_tx, *, resolved=None):  # P4-5 — mirror the real kwarg
        raise RuntimeError("diarizer exploded")
    monkeypatch.setattr(diarize_mod, "assign_speakers", _boom)

    enc = tmp_path / "encB"
    _write_chunk(enc, 1, ["[PT] Chest pain."])
    cfg = _config(provider="fake")

    with structlog.testing.capture_logs() as cap:
        r = accumulate_encounter(enc, config=cfg)

    # STILL folded (fail-open) — NOT held, NOT a decode failure, NOT frozen.
    assert r.folded == 1
    assert r.held == 0 and r.decode_failed is False and r.frozen is False

    eid = compute_encounter_id(enc.name, salt=_SALT)
    led = load_ledger(ledger_path(enc, eid))
    assert len(led.segments) == 1
    assert led.segments[0].speaker is None    # degraded to un-attributed
    assert led.diarized is False              # diarize did not complete

    # loud log emitted, driven by the production path (log-emission pin)
    failed = [c for c in cap if c.get("event") == "scribe.diarize.failed"]
    assert len(failed) == 1
    assert failed[0]["error_class"] == "RuntimeError"
    assert failed[0]["seq"] == 1
    assert failed[0]["encounter_id"] == eid


def test_accumulate_pyannote_disabled_folds_unattributed(tmp_path):
    # P4-4: the real dispatch path (no monkeypatch) with provider=pyannote but
    # enabled=false (the default) is INERT (NOTE-1 kill-switch) → the chunk folds
    # UN-ATTRIBUTED, torch-free, no crash. (Pre-P4-4 this raised NotImplementedError
    # which the accumulator degraded; the observable end-state — folded, speaker
    # None, diarized False — is identical, now via the inert gate not an exception.)
    enc = tmp_path / "encP"
    _write_chunk(enc, 1, ["[PT] Chest pain."])
    r = accumulate_encounter(enc, config=_config(provider="pyannote"))  # enabled=False
    assert r.folded == 1 and r.held == 0
    eid = compute_encounter_id(enc.name, salt=_SALT)
    led = load_ledger(ledger_path(enc, eid))
    assert led.segments[0].speaker is None
    assert led.diarized is False


def test_process_source_diarize_failure_still_drafts(tmp_path, monkeypatch):
    monkeypatch.setattr(ollama_mod, "call_ollama_no_tools", _fake_ollama())

    def _boom(config, audio_path, chunk_tx):
        raise RuntimeError("boom")
    monkeypatch.setattr(diarize_mod, "assign_speakers", _boom)

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "enc1.wav").write_bytes(b"")
    (inbox / "enc1.txt").write_text("Patient reports chest pain for 2 days.\n", encoding="utf-8")
    (inbox / "enc1.meta.json").write_text(json.dumps({"synthetic": True}), encoding="utf-8")
    vault = tmp_path / "vault"
    state = ScribeState(tmp_path / "state.json")

    with structlog.testing.capture_logs() as cap:
        outcome = asyncio.run(process_source(
            inbox / "enc1.wav", config=_config(provider="fake"),
            state=state, vault_path=vault,
        ))
    assert outcome == "drafted"               # did NOT fail (fail-open, not held)

    failed = [c for c in cap if c.get("event") == "scribe.diarize.failed"]
    assert len(failed) == 1
    assert failed[0]["error_class"] == "RuntimeError"
    assert "source_id" in failed[0]


# ---------------------------------------------------------------------------
# 8. dispatch fail-closed — pyannote NOTE-1 enabled-gate / fail-loud, unknown
#    provider refused (P4-4 real engine; the NotImplemented stub is gone)
# ---------------------------------------------------------------------------

def test_assign_speakers_pyannote_disabled_is_inert(tmp_path):
    # NOTE-1: provider=pyannote + enabled=false → INERT (returned untouched, like
    # off). No torch, no raise.
    audio = _write_sidecar(tmp_path, "hi")
    tx = transcribe(_config(provider="fake"), audio, source_id="e")
    out = assign_speakers(_config(provider="pyannote", enabled=False), audio, tx)
    assert out is tx
    assert all(s.speaker is None for s in out.segments)
    assert out.diarized is False


def test_assign_speakers_pyannote_enabled_no_config_fails_loud(tmp_path):
    # provider=pyannote + enabled=true but no materialized pipeline_config → the
    # real-engine path fails LOUD (DiarizeError) rather than risk a hub GET. Reaches
    # _run_pyannote_pipeline's config check BEFORE any pyannote import (torch-free).
    audio = _write_sidecar(tmp_path, "hi")
    tx = transcribe(_config(provider="fake"), audio, source_id="e")
    with pytest.raises(DiarizeError):
        assign_speakers(_config(provider="pyannote", enabled=True), audio, tx)


def test_assign_speakers_unknown_provider_fails_closed(tmp_path):
    audio = _write_sidecar(tmp_path, "hi")
    cfg = _config(provider="fake")
    tx = transcribe(cfg, audio, source_id="e")
    cfg.diarize.provider = "cloud-magic"      # bypass the boundary to hit dispatch
    with pytest.raises(DiarizeError):
        assign_speakers(cfg, audio, tx)


# ---------------------------------------------------------------------------
# 9. exit-78 dep guard (ensure_diarize_backend_available)
# ---------------------------------------------------------------------------

def test_ensure_diarize_backend_off_fake_noop():
    ensure_diarize_backend_available(_config(provider="off"))   # no raise
    ensure_diarize_backend_available(_config(provider="fake"))  # no raise


def test_ensure_diarize_backend_pyannote_enabled_missing_raises():
    # pyannote.audio is NOT installed in torch-free CI → an ENABLED pyannote engine
    # must fail-loud (exit-78). NOTE-1: the dep-check gates on `enabled` too.
    with pytest.raises(MissingDiarizeDependency):
        ensure_diarize_backend_available(_config(provider="pyannote", enabled=True))


def test_ensure_diarize_backend_pyannote_disabled_noop():
    # NOTE-1: provider=pyannote + enabled=false is INERT → it must boot TORCH-FREE
    # (an operator disabling the engine is not forced to keep torch installed). No
    # raise even though pyannote.audio is absent.
    ensure_diarize_backend_available(_config(provider="pyannote", enabled=False))


# ---------------------------------------------------------------------------
# 10. sovereign barrier-a sibling — diarize provider on the local allowlist
# ---------------------------------------------------------------------------

def _sov_config(diarize_provider):
    cfg = {
        "sovereign": {"enabled": True},
        "scribe": {
            "stt": {"provider": "fake"},
            "llm": {"base_url": "http://127.0.0.1:11434"},
        },
    }
    if diarize_provider is not None:
        cfg["scribe"]["diarize"] = {"provider": diarize_provider}
    return cfg


@pytest.mark.parametrize("provider", ["off", "fake", "pyannote"])
def test_boundary_accepts_local_diarize_providers(provider):
    validate_sovereign_boundary(_sov_config(provider), env={})   # no raise


def test_boundary_absent_diarize_block_passes():
    # No diarize block → provider defaults to off → on the allowlist.
    validate_sovereign_boundary(_sov_config(None), env={})       # no raise


def test_boundary_refuses_cloud_diarize():
    with pytest.raises(SovereignBoundaryError) as ei:
        validate_sovereign_boundary(_sov_config("aws-transcribe"), env={})
    assert ei.value.reason == "barrier_a_diarize"


# ---------------------------------------------------------------------------
# 11. config _build_diarize — defaults, coercion, schema-tolerance
# ---------------------------------------------------------------------------

def test_build_diarize_defaults():
    d = load_from_unified({}).diarize
    assert d.provider == "off" and d.enabled is False
    # conservative fail-closed-HIGH placeholder thresholds
    assert d.match_threshold == 0.75
    assert d.separation_margin == 0.15
    assert d.purity_threshold == 0.80
    assert d.min_turn_s == 1.0


def test_build_diarize_coercion_and_unknown_key_drop():
    c = load_from_unified({"scribe": {"diarize": {
        "provider": "fake", "enabled": "true", "match_threshold": "0.9",
        "min_turn_s": "2", "bogus_key": "x",
    }}})
    assert c.diarize.provider == "fake"
    assert c.diarize.enabled is True
    assert c.diarize.match_threshold == 0.9
    assert c.diarize.min_turn_s == 2.0
    assert not hasattr(c.diarize, "bogus_key")     # unknown key dropped


def test_build_diarize_malformed_threshold_keeps_default():
    c = load_from_unified({"scribe": {"diarize": {"purity_threshold": "not-a-float"}}})
    assert c.diarize.purity_threshold == 0.80      # fail-closed-HIGH default kept


def test_build_diarize_non_dict_all_defaults():
    c = load_from_unified({"scribe": {"diarize": "garbage"}})
    assert c.diarize.provider == "off" and c.diarize.enabled is False
