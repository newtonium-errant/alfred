"""Cloud STT for cued windows (task #81, stage 1).

Two things must be pinned here and nowhere else:

* **The vocabulary seam is really consumed.** A helper that resolves learned
  terms but is never called from the production path is the recurring trap —
  the helper suite goes green with the production call deleted. So the pins
  below drive ``transcribe_window`` and assert the terms reached the BACKEND,
  not that the resolver works in isolation.
* **``has_speech_signal`` is never a gate.** The Q6 trial returned False on a
  file with 5,126 words of coherent speech, and every cued window opens on
  ambience by construction.
"""

from __future__ import annotations

import json

import pytest
import structlog

from alfred.jeeves import stt
from alfred.jeeves.config import JeevesSttConfig
from alfred.telegram.stt_backends import (
    STT_ERR_RATE_LIMIT,
    SttError,
    SttResult,
)
# Imported rather than hand-written: the decided store's row vocabulary
# belongs to the seam, and a fixture that spells it independently would go
# green against a build that never read the store at all.
from alfred.telegram.stt_vocab_learning import DECISION_APPROVE, DECISION_REJECT

PCM = b"\x01\x02" * 8000        # 1 s of 16 kHz mono 16-bit


class RecordingBackend:
    """A stub Groq backend that records exactly what it was handed."""

    def __init__(self, result: SttResult | None = None, raises: Exception | None = None):
        self.result = result
        self.raises = raises
        self.calls: list[tuple[bytes, str, list[str]]] = []

    async def transcribe(self, audio: bytes, mime: str, vocab: list[str]) -> SttResult:
        self.calls.append((audio, mime, list(vocab)))
        if self.raises is not None:
            raise self.raises
        return self.result or SttResult(
            text="the bearing is a 6203", backend_id="stub", tier="comparable",
        )


async def run(audio=PCM, config=None, backend=None):
    return await stt.transcribe_window(
        audio,
        config=config or JeevesSttConfig(api_key="DUMMY_GROQ_TEST_KEY"),
        sample_rate=16000, sample_width=2, channels=1,
        backend=backend,
    )


# ---------------------------------------------------------------------------
# WAV framing
# ---------------------------------------------------------------------------


def test_the_pcm_is_wrapped_in_a_valid_riff_header():
    """The ring holds headerless PCM; Groq wants a container. Hand-rolled so
    a capture never has to touch the filesystem to gain 44 bytes."""
    out = stt.wav_bytes(PCM, 16000, 2, 1)
    assert out[:4] == b"RIFF"
    assert out[8:12] == b"WAVE"
    assert out[36:40] == b"data"
    assert int.from_bytes(out[40:44], "little") == len(PCM)
    assert int.from_bytes(out[24:28], "little") == 16000    # sample rate
    assert int.from_bytes(out[34:36], "little") == 16       # bits per sample
    assert out[44:] == PCM


async def test_the_backend_receives_the_wrapped_audio_as_wav():
    backend = RecordingBackend()
    await run(backend=backend)
    sent, mime, _ = backend.calls[0]
    assert mime == "audio/wav"
    assert sent[:4] == b"RIFF"
    assert sent.endswith(PCM)


# ---------------------------------------------------------------------------
# The vocabulary seam — WIRING pins, not helper pins
# ---------------------------------------------------------------------------


async def test_the_static_vocab_reaches_the_backend():
    backend = RecordingBackend()
    config = JeevesSttConfig(
        api_key="DUMMY_GROQ_TEST_KEY", vocab_terms=["RRTS", "6203 bearing"],
    )
    await run(config=config, backend=backend)
    _, _, vocab = backend.calls[0]
    assert vocab == ["RRTS", "6203 bearing"]


async def test_operator_APPROVED_learned_terms_reach_the_backend(tmp_path):
    """THE SEAM, wired. Reading ``vocab_terms`` directly instead of calling
    ``effective_vocab_terms`` would pass every other test in this file and
    silently lose every term the operator ever approved — which for the
    garage vocabulary (part numbers, tool names, suppliers) is exactly what
    generic Whisper mangles."""
    decided = tmp_path / "decided.jsonl"
    decided.write_text(
        json.dumps({"type": DECISION_APPROVE, "term": "Timken"}) + "\n"
        + json.dumps({"type": DECISION_APPROVE, "term": "circlip"}) + "\n",
        encoding="utf-8",
    )
    backend = RecordingBackend()
    config = JeevesSttConfig(
        api_key="DUMMY_GROQ_TEST_KEY",
        vocab_terms=["RRTS"],
        vocab_decided_path=str(decided),
    )
    await run(config=config, backend=backend)

    _, _, vocab = backend.calls[0]
    assert "RRTS" in vocab
    assert "Timken" in vocab
    assert "circlip" in vocab


async def test_a_LATER_rejection_retracts_an_earlier_approval(tmp_path):
    """Proves the seam is REPLAYING the decision store, not just reading the
    last line or unioning every term it sees.

    Note the semantics this pins, which are the seam's and not obvious: a
    rejection retracts a LEARNED term, it does not delete a term the
    operator configured by hand in ``vocab_terms``. The static list is his
    own decision and the review card never proposed it.
    """
    decided = tmp_path / "decided.jsonl"
    decided.write_text(
        json.dumps({"type": DECISION_APPROVE, "term": "Timken"}) + "\n"
        + json.dumps({"type": DECISION_APPROVE, "term": "circlip"}) + "\n"
        + json.dumps({"type": DECISION_REJECT, "term": "Timken"}) + "\n",
        encoding="utf-8",
    )
    backend = RecordingBackend()
    config = JeevesSttConfig(
        api_key="DUMMY_GROQ_TEST_KEY",
        vocab_terms=["RRTS"],
        vocab_decided_path=str(decided),
    )
    await run(config=config, backend=backend)
    _, _, vocab = backend.calls[0]
    assert "Timken" not in vocab      # approved, then retracted
    assert "circlip" in vocab         # approved, still standing
    assert "RRTS" in vocab            # the operator's own, untouched


def test_an_unset_decided_path_says_only_static_terms_apply():
    """Intentionally-left-blank: 'no learned terms' and 'the decided store is
    missing' are different facts and only one needs fixing."""
    with structlog.testing.capture_logs() as captured:
        stt.resolve_vocab(JeevesSttConfig(vocab_terms=["RRTS"]))
    events = [c for c in captured
              if c.get("event") == "jeeves.stt.vocab_static_only"]
    assert len(events) == 1
    assert events[0]["static_terms"] == 1


async def test_the_vocab_size_is_reported_on_the_transcript():
    backend = RecordingBackend()
    config = JeevesSttConfig(
        api_key="DUMMY_GROQ_TEST_KEY", vocab_terms=["a", "b", "c"],
    )
    result = await run(config=config, backend=backend)
    assert result.vocab_terms_used == 3


# ---------------------------------------------------------------------------
# The Q6 trial finding — judge the WHOLE window
# ---------------------------------------------------------------------------


async def test_a_false_speech_signal_does_NOT_discard_the_transcript():
    """Q6 TRIAL, the load-bearing finding. The conversation-only recording
    came back has_speech_signal=False on 5,126 words — the backend derives
    the flag from the most-silent segment, and a cued window opens on up to
    45 seconds of ambience every single time. Gating on it would discard the
    majority of real captures."""
    backend = RecordingBackend(SttResult(
        text="the bearing is the wrong size, order the 6203",
        backend_id="stub", tier="comparable",
        has_speech_signal=False,          # the trial's exact artefact
        confidence_raw=-0.28,
    ))
    result = await run(backend=backend)

    assert result.ok is True
    assert result.text == "the bearing is the wrong size, order the 6203"
    # Recorded for telemetry, and recorded ONLY.
    assert result.has_speech_signal is False


async def test_a_true_speech_signal_changes_nothing_either():
    """The flag has no behavioural effect in EITHER direction — which is what
    'never a gate' means."""
    backend = RecordingBackend(SttResult(
        text="same words", backend_id="stub", tier="comparable",
        has_speech_signal=True,
    ))
    result = await run(backend=backend)
    assert result.ok is True
    assert result.text == "same words"


# ---------------------------------------------------------------------------
# Empty vs failed — two outcomes that must never render the same
# ---------------------------------------------------------------------------


async def test_an_empty_result_is_OK_with_a_reason():
    """The engine ran and heard nothing. A real outcome, not a failure."""
    backend = RecordingBackend(SttResult(
        text="", backend_id="stub", tier="comparable", has_speech_signal=False,
    ))
    with structlog.testing.capture_logs() as captured:
        result = await run(backend=backend)
    assert result.ok is True
    assert result.text == ""
    assert result.reason == stt.STT_EMPTY_RESULT
    assert [c for c in captured if c.get("event") == "jeeves.stt.empty"]


async def test_a_classified_failure_is_NOT_ok():
    backend = RecordingBackend(raises=SttError(
        STT_ERR_RATE_LIMIT, "HTTP 429: quota", backend_id="stub",
    ))
    with structlog.testing.capture_logs() as captured:
        result = await run(backend=backend)
    assert result.ok is False
    assert result.reason == stt.STT_FAILED
    events = [c for c in captured if c.get("event") == "jeeves.stt.failed"]
    assert len(events) == 1
    assert events[0]["error_class"] == STT_ERR_RATE_LIMIT


async def test_an_unexpected_exception_does_not_kill_the_capture_loop():
    """A capture device must keep listening through a backend that
    misbehaves in a way nobody classified."""
    backend = RecordingBackend(raises=RuntimeError("something odd"))
    result = await run(backend=backend)
    assert result.ok is False
    assert result.reason == stt.STT_FAILED


async def test_an_empty_window_is_never_sent():
    backend = RecordingBackend()
    with structlog.testing.capture_logs() as captured:
        result = await run(audio=b"", backend=backend)
    assert backend.calls == []
    assert result.reason == stt.STT_SKIP_EMPTY_WINDOW
    events = [c for c in captured if c.get("event") == "jeeves.stt.skipped"]
    assert events[0]["reason"] == stt.STT_SKIP_EMPTY_WINDOW


async def test_no_api_key_reports_unconfigured_rather_than_failing_auth():
    """The operator paid nothing and learns the actual problem. Reaching the
    network to be told 401 would cost a round trip to say the same thing
    less clearly."""
    with structlog.testing.capture_logs() as captured:
        result = await stt.transcribe_window(
            PCM, config=JeevesSttConfig(api_key=""),
            sample_rate=16000, sample_width=2, channels=1,
        )
    assert result.ok is False
    assert result.reason == stt.STT_SKIP_UNCONFIGURED
    events = [c for c in captured if c.get("event") == "jeeves.stt.skipped"]
    assert events[0]["log_level"] == "error"


def test_build_backend_returns_none_without_a_key():
    assert stt.build_backend(JeevesSttConfig(api_key="")) is None


def test_build_backend_constructs_the_wired_groq_backend():
    """CONSUMES the existing backend rather than reimplementing it."""
    from alfred.telegram.stt_backends import GroqWhisperBackend

    engine = stt.build_backend(JeevesSttConfig(
        api_key="DUMMY_GROQ_TEST_KEY", model="whisper-large-v3", language="en",
    ))
    assert isinstance(engine, GroqWhisperBackend)
    assert engine.model == "whisper-large-v3"
    # verbose_json is what surfaces avg_logprob and no_speech_prob at all.
    assert engine.response_format == "verbose_json"


# ---------------------------------------------------------------------------
# The transcript never reaches a log
# ---------------------------------------------------------------------------


async def test_the_success_log_carries_a_length_never_the_words():
    secret = "the safe combination is nine four two"
    backend = RecordingBackend(SttResult(
        text=secret, backend_id="stub", tier="comparable",
    ))
    with structlog.testing.capture_logs() as captured:
        await run(backend=backend)
    for entry in captured:
        rendered = " ".join(str(v) for v in entry.values())
        assert "combination" not in rendered
        assert "nine four two" not in rendered
    events = [c for c in captured if c.get("event") == "jeeves.stt.transcribed"]
    assert events[0]["text_chars"] == len(secret)


async def test_whitespace_only_output_counts_as_empty():
    backend = RecordingBackend(SttResult(
        text="   \n  ", backend_id="stub", tier="comparable",
    ))
    result = await run(backend=backend)
    assert result.text == ""
    assert result.reason == stt.STT_EMPTY_RESULT
    assert result.ok is True


async def test_the_opaque_confidence_is_carried_but_not_compared():
    backend = RecordingBackend(SttResult(
        text="words", backend_id="stub", tier="comparable",
        confidence_raw=-0.247, confidence_kind="logprob",
    ))
    result = await run(backend=backend)
    assert result.confidence_raw == pytest.approx(-0.247)
