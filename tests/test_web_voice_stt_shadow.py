"""Tests for web voice STT shadow-capture (Increment 1).

Covers, all UNCONDITIONAL (no av/aiortc — the ``resample_fn`` seam + a scripted
provider drive the worker):
  * ``pcm16_to_wav`` purity + round-trip through ``wave.open``
  * ``noise_metrics`` purity (silence floor vs loud peak)
  * live-turn byte-IDENTICAL when shadow is disabled (no tee / snapshot / hook)
  * the per-utterance buffer snapshots to the hook + resets per utterance
  * drop-oldest ring bound
  * shadow fires AFTER the live ``on_utterance`` + a raising hook never kills the pump
  * GC-safe module-level ``_SHADOW_TASKS`` retention
  * ``VoiceSttShadow.capture`` writes a well-formed corpus record + the .wav,
    and never raises into the caller when Groq fails

Plus a NETWORK-GATED real-Groq gate (``skipif`` no ``GROQ_API_KEY``) — the
mandatory real-provider integration pass (feedback_real_provider_integration_gate):
it proves the REAL Groq endpoint ACCEPTS a WAV sent as ``voice.wav`` (the
prerequisite mime→filename fix — a ``.bin`` body is rejected) and round-trips.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import wave
from pathlib import Path

import pytest
import structlog

from alfred.telegram.stt_backends import SttError, SttResult
from alfred.web.stt_stream import (
    EVENT_FINAL,
    EVENT_UTTERANCE_END,
    STTEvent,
    STTStreamProvider,
)
from alfred.web.voice_stt import VoiceSttWorker, pcm16_to_wav
from alfred.web.voice_stt_shadow import (
    VoiceSttShadow,
    _SHADOW_TASKS,
    divergence,
    noise_metrics,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _sine_pcm(freq: float, secs: float, rate: int = 16000, amp: int = 12000) -> bytes:
    import math
    n = int(rate * secs)
    out = bytearray()
    for i in range(n):
        v = int(amp * math.sin(2 * math.pi * freq * i / rate))
        out += int(v).to_bytes(2, "little", signed=True)
    return bytes(out)


def test_pcm16_to_wav_roundtrips_through_wave() -> None:
    pcm = _sine_pcm(440, 0.05)          # 800 samples
    wav = pcm16_to_wav(pcm, 16000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    with wave.open(io.BytesIO(wav), "rb") as r:
        assert r.getnchannels() == 1
        assert r.getsampwidth() == 2
        assert r.getframerate() == 16000
        assert r.readframes(r.getnframes()) == pcm   # bytes preserved exactly


def test_pcm16_to_wav_empty() -> None:
    wav = pcm16_to_wav(b"", 16000)
    with wave.open(io.BytesIO(wav), "rb") as r:
        assert r.getnframes() == 0


def test_noise_metrics_silence_low_speech_high() -> None:
    quiet = b"\x00\x00" * 16000            # 1 s of pure silence
    qp, qa, qf = noise_metrics(quiet, 16000)
    assert qp == 0.0 and qa == 0.0 and qf == 0.0

    loud = _sine_pcm(300, 1.0, amp=20000)
    lp, la, lf = noise_metrics(loud, 16000)
    assert lp > 1000 and la > 1000        # a strong tone reads high everywhere
    assert lp >= la                        # peak window >= whole-buffer avg


def test_noise_metrics_empty() -> None:
    assert noise_metrics(b"", 16000) == (0.0, 0.0, 0.0)


def test_divergence_matches_harness_semantics() -> None:
    assert divergence("hello world", "hello world") == 0.0
    assert divergence("", "") == 0.0
    assert 0.0 < divergence("hello world", "hello there") <= 1.0


# ---------------------------------------------------------------------------
# Worker test doubles (minimal, mirror test_web_voice_stt_worker.py)
# ---------------------------------------------------------------------------


class _ScriptedProvider(STTStreamProvider):
    provider_id = "scripted"

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.feeds = 0

    async def connect(self) -> None:
        pass

    async def feed(self, chunk: bytes) -> None:
        self.feeds += 1

    async def finalize(self) -> None:
        pass

    async def close(self) -> None:
        self.q.put_nowait(None)

    async def events(self):
        while True:
            ev = await self.q.get()
            if ev is None:
                return
            yield ev

    def emit(self, ev: STTEvent) -> None:
        self.q.put_nowait(ev)


class _FakeTrack:
    def __init__(self, frames: list) -> None:
        self._frames = list(frames)

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        raise RuntimeError("end-of-track")


def _worker(provider, *, on_utterance, shadow_capture=None, sample_rate=16000):
    return VoiceSttWorker(
        provider=provider,
        voice_session_id="v1",
        on_utterance=on_utterance,
        min_utterance_chars=3,
        sample_rate=sample_rate,
        resample_fn=lambda f: [f] if isinstance(f, (bytes, bytearray)) else [],
        hello_gate=False,
        shadow_capture=shadow_capture,
    )


# ---------------------------------------------------------------------------
# Live-turn isolation — the #1 property
# ---------------------------------------------------------------------------


async def test_shadow_disabled_is_byte_identical_no_buffer() -> None:
    """shadow_capture=None ⇒ no tee, no snapshot, live turn unchanged."""
    prov = _ScriptedProvider()
    got: list[str] = []

    async def on_utt(t):
        got.append(t)

    w = _worker(prov, on_utterance=on_utt, shadow_capture=None)
    w.start(_FakeTrack([b"\x00" * 3200]))
    await asyncio.sleep(0.03)
    prov.emit(STTEvent(type=EVENT_FINAL, text="hello world"))
    prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
    await asyncio.sleep(0.03)
    assert got == ["hello world"]
    assert w._snapshot_utt_pcm() == b""     # no buffer accumulated
    assert len(w._utt_pcm) == 0
    await w.aclose()


def test_snapshot_resets_per_utterance_deterministic() -> None:
    """Method-level: snapshot returns + CLEARS, so utterance N+1 never carries
    utterance N's audio."""
    prov = _ScriptedProvider()

    async def _n(_):
        return None

    w = _worker(prov, on_utterance=_n, shadow_capture=lambda *a: None)
    w._utt_pcm.extend(b"AAAA")
    assert w._snapshot_utt_pcm() == b"AAAA"
    assert len(w._utt_pcm) == 0
    w._utt_pcm.extend(b"BBBB")
    assert w._snapshot_utt_pcm() == b"BBBB"   # no "AAAA"


async def test_buffer_snapshots_to_hook_and_fires_after_on_utterance() -> None:
    """Full worker: the fed PCM reaches the hook, AFTER the live on_utterance."""
    prov = _ScriptedProvider()
    order: list[str] = []
    seen: dict = {}

    async def on_utt(t):
        order.append("live")

    def hook(pcm, text, dur):
        order.append("shadow")
        seen["pcm"] = pcm
        seen["text"] = text
        seen["dur"] = dur

    w = _worker(prov, on_utterance=on_utt, shadow_capture=hook)
    w.start(_FakeTrack([b"\x01" * 3200]))
    await asyncio.sleep(0.03)
    prov.emit(STTEvent(type=EVENT_FINAL, text="hello world"))
    prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
    await asyncio.sleep(0.03)
    assert order == ["live", "shadow"]        # shadow strictly AFTER the turn
    assert seen["pcm"] == b"\x01" * 3200
    assert seen["text"] == "hello world"
    assert abs(seen["dur"] - 3200 / (16000 * 2)) < 1e-6
    await w.aclose()


async def test_drop_oldest_ring_bound() -> None:
    prov = _ScriptedProvider()
    seen: dict = {}

    async def on_utt(t):
        return None

    def hook(pcm, text, dur):
        seen["pcm"] = pcm

    w = _worker(prov, on_utterance=on_utt, shadow_capture=hook)
    w._utt_pcm_max = 3200                       # cap to one chunk
    w.start(_FakeTrack([b"A" * 3200, b"B" * 3200]))   # 2 chunks → cap drops A
    await asyncio.sleep(0.04)
    prov.emit(STTEvent(type=EVENT_FINAL, text="hello world"))
    prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
    await asyncio.sleep(0.03)
    assert seen["pcm"] == b"B" * 3200           # newest kept, oldest dropped
    await w.aclose()


async def test_raising_hook_never_kills_the_pump() -> None:
    prov = _ScriptedProvider()
    got: list[str] = []

    async def on_utt(t):
        got.append(t)

    def bad_hook(pcm, text, dur):
        raise RuntimeError("shadow blew up")

    w = _worker(prov, on_utterance=on_utt, shadow_capture=bad_hook)
    w.start(_FakeTrack([b"\x02" * 3200]))
    await asyncio.sleep(0.03)
    with structlog.testing.capture_logs() as cap:
        prov.emit(STTEvent(type=EVENT_FINAL, text="one two three"))
        prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
        await asyncio.sleep(0.03)
        # A SECOND utterance still processes — the pump survived the raise.
        prov.emit(STTEvent(type=EVENT_FINAL, text="four five six"))
        prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
        await asyncio.sleep(0.03)
    assert got == ["one two three", "four five six"]
    raised = [c for c in cap if c.get("event") == "web.voice.stt.shadow_hook_raised"]
    assert len(raised) >= 1
    await w.aclose()


# ---------------------------------------------------------------------------
# VoiceSttShadow — the capture path
# ---------------------------------------------------------------------------


class _FakeGroq:
    backend_id = "groq-whisper"
    timeout_s = 10.0

    def __init__(self, *, text="groq heard this", raises=None, latency_ms=42):
        self._text = text
        self._raises = raises
        self._lat = latency_ms
        self.calls: list = []

    async def transcribe(self, audio, mime, vocab):
        self.calls.append((audio, mime, list(vocab)))
        if self._raises is not None:
            raise self._raises
        return SttResult(text=self._text, backend_id="groq-whisper",
                         tier="comparable", latency_ms=self._lat)


def _shadow(tmp_path: Path, groq, vocab=("RRTS", "Fergus")) -> VoiceSttShadow:
    return VoiceSttShadow(
        groq_backend=groq, vocab=list(vocab), corpus_dir=str(tmp_path),
        instance_name="Salem", voice_session_id="vs-1", sample_rate=16000,
    )


async def _drain_shadow_tasks() -> None:
    for _ in range(50):
        pending = [t for t in list(_SHADOW_TASKS) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)
        await asyncio.sleep(0)


async def test_capture_writes_wellformed_corpus_record(tmp_path: Path) -> None:
    groq = _FakeGroq(text="the rrts report")
    sh = _shadow(tmp_path, groq)
    pcm = _sine_pcm(300, 0.2, amp=18000)
    sh.capture(pcm, "the arts report", 0.2)     # deepgram streaming final
    await _drain_shadow_tasks()

    corpus = tmp_path / "corpus.jsonl"
    lines = [ln for ln in corpus.read_text().splitlines() if ln.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    # harness-contract keys
    assert rec["groq"]["text"] == "the rrts report"
    assert rec["groq"]["latency_ms"] == 42 and rec["groq"]["error"] is None
    assert rec["deepgram"]["text"] == "the arts report"
    assert rec["divergence"] > 0.0
    assert rec["audio_file"].endswith(".wav")
    # web additions
    assert rec["instance"] == "Salem" and rec["voice_session_id"] == "vs-1"
    assert set(rec["noise"]) == {
        "utt_peak_rms", "utt_avg_rms", "noise_floor", "noise_floor_ema", "noisy"}
    # the .wav landed + is a real WAV of the fed PCM
    wav = (tmp_path / rec["audio_file"]).read_bytes()
    with wave.open(io.BytesIO(wav), "rb") as r:
        assert r.readframes(r.getnframes()) == pcm
    # Groq was called with WAV mime + the vocab (parity with served path)
    assert groq.calls and groq.calls[0][1] == "audio/wav"
    assert groq.calls[0][2] == ["RRTS", "Fergus"]


async def test_capture_never_raises_when_groq_fails(tmp_path: Path) -> None:
    groq = _FakeGroq(raises=SttError("auth", "bad key", backend_id="groq-whisper"))
    sh = _shadow(tmp_path, groq)
    # Must not raise into the caller (fire-and-forget + top-level catch-all).
    sh.capture(_sine_pcm(300, 0.1), "deepgram text", 0.1)
    await _drain_shadow_tasks()
    rec = json.loads((tmp_path / "corpus.jsonl").read_text().splitlines()[0])
    assert rec["groq"]["error"] == "auth" and rec["groq"]["text"] == ""
    assert rec["deepgram"]["text"] == "deepgram text"   # still recorded


async def test_capture_task_is_gc_safe_and_discarded(tmp_path: Path) -> None:
    sh = _shadow(tmp_path, _FakeGroq())
    before = len(_SHADOW_TASKS)
    sh.capture(_sine_pcm(300, 0.05), "x y z", 0.05)
    assert len(_SHADOW_TASKS) == before + 1     # retained while in-flight
    await _drain_shadow_tasks()
    assert len(_SHADOW_TASKS) == before         # discarded on done


# ---------------------------------------------------------------------------
# Real-provider integration gate (network-gated) — MANDATORY per
# feedback_real_provider_integration_gate. Proves the REAL Groq endpoint
# accepts a WAV sent as voice.wav (the prereq mime→filename fix) + round-trips.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not os.environ.get("GROQ_API_KEY"),
                    reason="real Groq gate: set GROQ_API_KEY")
async def test_real_groq_accepts_wav_and_roundtrips() -> None:
    from alfred.telegram.stt_backends import GroqWhisperBackend
    backend = GroqWhisperBackend(api_key=os.environ["GROQ_API_KEY"])
    wav = pcm16_to_wav(_sine_pcm(440, 1.2), 16000)   # decodable, valid WAV
    # Must NOT raise (a .bin body would 400 → SttError). Empty text is fine —
    # a tone has no words; the gate proves ACCEPT + decode + schema round-trip.
    result = await backend.transcribe(wav, "audio/wav", [])
    assert isinstance(result, SttResult)
    assert isinstance(result.text, str)


# A checked-in short SPEECH clip ("The quick brown fox jumps over the lazy dog.",
# 16k mono WAV) lets the gate assert real word-level transcription — the fuller
# real-provider check the tone-accept test can't make.
_SPEECH_WAV = Path(__file__).parent / "fixtures" / "stt" / "short_speech.wav"
# Distinctive content words from the pangram — any one appearing (case-
# insensitive substring, so Whisper punctuation like "fox," / "dog." is fine)
# proves the WAV was truly transcribed, not just accepted.
_SPEECH_WORDS = ("quick", "brown", "fox", "jumps", "lazy", "dog")


@pytest.mark.skipif(
    not (os.environ.get("GROQ_API_KEY") and _SPEECH_WAV.exists()),
    reason="real Groq speech gate: set GROQ_API_KEY + drop tests/fixtures/stt/short_speech.wav",
)
async def test_real_groq_transcribes_speech_to_words() -> None:
    from alfred.telegram.stt_backends import GroqWhisperBackend
    backend = GroqWhisperBackend(api_key=os.environ["GROQ_API_KEY"])
    result = await backend.transcribe(_SPEECH_WAV.read_bytes(), "audio/wav", [])
    text = result.text.lower()
    hits = [w for w in _SPEECH_WORDS if w in text]
    # Surface the real transcript so the run's actual output is visible (-s).
    print(f"\n[real-groq speech] transcript={result.text!r} matched={hits}")
    assert hits, f"no recognizable pangram word in real Groq transcript: {result.text!r}"
