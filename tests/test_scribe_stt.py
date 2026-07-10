"""Tests for the local segment-rich STT + transcript shape (scribe P2-b).

All synthetic. The CORE (dispatch, fake backend, transcript shape, dep-guard,
barrier-a lockstep, the NOTE-1 sole-caller invariant) gets UNCONDITIONAL
coverage via the fake backend + monkeypatched availability. The faster-whisper
REAL-model test is an INTEGRATION test gated on the [scribe] extra
(importorskip) — per feedback_regression_pin_unconditional, gating an
integration test on an optional dep is fine because the core logic is covered
unconditionally.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import alfred.scribe.stt as stt_mod
from alfred.scribe import load_from_unified
from alfred.scribe.stt import (
    SCRIBE_STT_PROVIDERS,
    MissingSTTDependency,
    STTError,
    ensure_backend_available,
    transcribe,
)
from alfred.scribe.transcript import Segment, Transcript, make_segment_id
from alfred.sovereign import (
    SOVEREIGN_STT_ALLOWLIST,
    SovereignBoundaryError,
    validate_sovereign_boundary,
)


def _cfg(provider="fake", mode="synthetic", model=""):
    return load_from_unified({"scribe": {
        "mode": mode,
        "stt": {"provider": provider, "model": model},
        "llm": {"base_url": "http://127.0.0.1:11434"},
    }})


def _write_sidecar(tmp_path, *lines):
    audio = tmp_path / "enc1.wav"
    audio.write_bytes(b"")  # placeholder; the fake backend reads the sidecar
    (tmp_path / "enc1.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return audio


# ---------------------------------------------------------------------------
# fake STT → the segment-rich Transcript shape
# ---------------------------------------------------------------------------

def test_fake_transcribe_segment_rich_shape(tmp_path):
    audio = _write_sidecar(
        tmp_path,
        "Synthetic patient reports chest pain for two days.",
        "Denies shortness of breath.",
        "Plan: order ECG and troponin.",
    )
    t = transcribe(_cfg(), audio, source_id="synth-enc1")
    assert isinstance(t, Transcript)
    assert t.source_id == "synth-enc1"
    assert t.mode == "synthetic"
    # P3/P4 slots present + at their P2 defaults
    assert t.version == 1
    assert t.processed_through_segment is None
    # stable ids S1..S3, timestamps, speaker=null (P4 slot)
    assert [s.id for s in t.segments] == ["S1", "S2", "S3"]
    assert t.segments[0].start_s == 0.0 and t.segments[0].end_s == 5.0
    assert t.segments[1].start_s == 5.0
    assert all(s.speaker is None for s in t.segments)
    assert "chest pain" in t.segments[0].text


def test_make_segment_id_is_one_indexed():
    assert make_segment_id(0) == "S1"
    assert make_segment_id(9) == "S10"


def test_fake_transcribe_reads_txt_path_directly(tmp_path):
    txt = tmp_path / "note.txt"
    txt.write_text("One line only.\n", encoding="utf-8")
    t = transcribe(_cfg(), txt, source_id="s")
    assert len(t.segments) == 1 and t.segments[0].id == "S1"


def test_fake_transcribe_missing_sidecar_fails_loud(tmp_path):
    with pytest.raises(STTError):
        transcribe(_cfg(), tmp_path / "nope.wav", source_id="s")


# ---------------------------------------------------------------------------
# provider dispatch + barrier-a lockstep
# ---------------------------------------------------------------------------

def test_dispatch_unknown_provider_fails_closed(tmp_path):
    # A non-allowlisted provider (barrier-a would refuse at load) also fails
    # closed at the STT dispatch — never silently reaches a cloud engine.
    audio = _write_sidecar(tmp_path, "hi")
    with pytest.raises(STTError):
        transcribe(_cfg(provider="groq"), audio, source_id="s")


def test_scribe_stt_providers_lockstep_with_barrier_a():
    # THE barrier-a lockstep: the STT dispatch set == the sovereign STT
    # allowlist, so every provider the boundary permits is dispatchable and
    # nothing else is.
    assert SCRIBE_STT_PROVIDERS == SOVEREIGN_STT_ALLOWLIST


@pytest.mark.parametrize("cloud", ["groq", "deepgram", "elevenlabs", "openai"])
def test_barrier_a_refuses_cloud_stt_in_sovereign_config(cloud):
    # THE barrier-a-refuses-cloud pin — a cloud STT provider in a sovereign
    # config is refused at load (before the pipeline ever runs).
    raw = {
        "sovereign": {"enabled": True},
        "scribe": {
            "mode": "synthetic",
            "stt": {"provider": cloud},
            "llm": {"base_url": "http://127.0.0.1:11434"},
        },
    }
    with pytest.raises(SovereignBoundaryError) as exc:
        validate_sovereign_boundary(raw, env={})
    assert exc.value.reason == "barrier_a"


@pytest.mark.parametrize("local", ["faster-whisper", "local-whisper", "fake"])
def test_barrier_a_allows_local_stt(local):
    raw = {
        "sovereign": {"enabled": True},
        "scribe": {
            "mode": "synthetic",
            "stt": {"provider": local},
            "llm": {"base_url": "http://127.0.0.1:11434"},
        },
    }
    validate_sovereign_boundary(raw, env={})  # no raise


# ---------------------------------------------------------------------------
# ensure_backend_available — the runner exit-78 dep guard
# ---------------------------------------------------------------------------

def test_ensure_backend_available_fake_is_noop():
    ensure_backend_available(_cfg(provider="fake"))  # no raise, no dep


@pytest.mark.parametrize("provider", ["faster-whisper", "local-whisper"])
def test_ensure_backend_available_missing_dep_raises(provider, monkeypatch):
    monkeypatch.setattr(stt_mod, "_faster_whisper_available", lambda: False)
    with pytest.raises(MissingSTTDependency):
        ensure_backend_available(_cfg(provider=provider))


@pytest.mark.parametrize("provider", ["faster-whisper", "local-whisper"])
def test_ensure_backend_available_present_dep_ok(provider, monkeypatch):
    monkeypatch.setattr(stt_mod, "_faster_whisper_available", lambda: True)
    ensure_backend_available(_cfg(provider=provider))  # no raise


# ---------------------------------------------------------------------------
# Transcript schema-tolerance (forward-compat load contract)
# ---------------------------------------------------------------------------

def test_transcript_from_dict_schema_tolerance():
    data = {
        "source_id": "s1", "mode": "synthetic", "version": 2,
        "processed_through_segment": "S3",
        "segments": [
            {"id": "S1", "start_s": 0.0, "end_s": 5.0, "text": "a", "speaker": "spk1"},
            {"id": "S2", "start_s": 5.0, "end_s": 10.0, "text": "b", "speaker": None},
        ],
        "future_field": "ignored",  # unknown top-level key dropped
    }
    t = Transcript.from_dict(data)
    assert t.source_id == "s1" and t.version == 2
    assert t.processed_through_segment == "S3"
    assert [s.id for s in t.segments] == ["S1", "S2"]
    assert t.segments[0].speaker == "spk1"  # P4 slot round-trips
    # round-trip
    assert Transcript.from_dict(t.to_dict()).to_dict() == t.to_dict()


def test_transcript_from_dict_missing_keys_default():
    t = Transcript.from_dict({"source_id": "s", "mode": "synthetic"})
    assert t.segments == [] and t.version == 1 and t.processed_through_segment is None


def test_segment_from_dict_drops_unknown():
    s = Segment.from_dict({"id": "S1", "start_s": 0.0, "end_s": 1.0, "text": "x", "extra": 9})
    assert s.id == "S1" and s.speaker is None


# ---------------------------------------------------------------------------
# NOTE-1 — scribe.attest() is the SOLE caller under the attest scope
# ---------------------------------------------------------------------------

# A CALLER introduces the attest scope by passing it to a vault op —
# ``scope=ATTEST_SCOPE`` or ``scope="stayc_clinical_attest"``. Doc/comment
# mentions of the scope name (schema.py, cli.py help, attestation.py docstring)
# are NOT callers, so the guard matches the ``scope=`` pattern specifically,
# not any substring.
import re as _re

_ATTEST_CALLER_RE = _re.compile(
    r"""scope\s*=\s*(ATTEST_SCOPE|["']stayc_clinical_attest["'])"""
)


def test_note1_attest_scope_sole_caller_guard():
    # THE P2-a invariant guard (mutation: add a second ``scope=ATTEST_SCOPE``
    # caller => fails). The self-attest prevention rests on "sole caller": any
    # OTHER code path passing the attest scope to a vault op would silently
    # reopen the bypass (the scope gate alone permits attested_by=stayc_scribe).
    # The ONLY production caller is scribe.attest() (scribe/attest.py).
    import alfred
    src = Path(alfred.__file__).resolve().parent
    allowed = {"scribe/attest.py"}
    offenders = []
    for py in src.rglob("*.py"):
        rel = py.relative_to(src).as_posix()
        if rel in allowed:
            continue
        if _ATTEST_CALLER_RE.search(py.read_text(encoding="utf-8")):
            offenders.append(rel)
    assert offenders == [], (
        f"NOTE-1: scribe.attest() must be the SOLE caller passing the "
        f"stayc_clinical_attest scope to a vault op. Unexpected caller(s): "
        f"{offenders} — a second caller reopens the self-attest bypass."
    )


# ---------------------------------------------------------------------------
# faster-whisper REAL model — INTEGRATION test (gated on the [scribe] extra)
# ---------------------------------------------------------------------------

def test_faster_whisper_real_model_smoke(tmp_path):
    pytest.importorskip("faster_whisper", reason="[scribe] extra not installed")
    import wave

    # A 1s silent mono WAV — exercises the real model-load + transcribe path
    # (VAD filters silence → 0 segments); asserts the Transcript shape returns.
    wav = tmp_path / "silence.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x00" * 16000)
    t = transcribe(_cfg(provider="faster-whisper", model="distil-large-v3"), wav, source_id="smoke")
    assert isinstance(t, Transcript)
    assert t.source_id == "smoke" and t.mode == "synthetic"
    assert all(seg.id.startswith("S") for seg in t.segments)
