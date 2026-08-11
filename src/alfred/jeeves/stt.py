"""Cloud STT for cued windows — CONSUMING two existing seams (task #81).

Jeeves builds no STT stack and owns no vocabulary list. Two things already
exist and are used exactly as they are:

* :class:`~alfred.telegram.stt_backends.GroqWhisperBackend` — the wired
  ``whisper-large-v3`` path, the one the Q6 trial actually measured. It is
  CONSTRUCTED here, never reimplemented.
* :func:`~alfred.telegram.stt_vocab_learning.effective_vocab_terms` — **the
  single vocabulary seam**. Its own module says every consumer must call it
  rather than reading ``vocab_terms`` directly, because the union with
  operator-approved learned terms lives inside it. Reading the raw field
  would silently miss every term the operator ever approved, and the garage
  vocabulary — part numbers, tool names, supplier names — is precisely what
  generic Whisper mangles.

That makes Jeeves a CONSUMER of the #54 learned-vocabulary loop from day
one. It is not yet a CONTRIBUTOR: contributing needs a surface where a
Jeeves transcript can be corrected, which is the stage-3 review card
(ratification item 3 widened the loop to capture EDITS for exactly this
reason). Until that ships the loop is half-closed, and this docstring says
so rather than implying a loop that isn't there.

**JUDGE THE WHOLE WINDOW — the Q6 trial's load-bearing finding.** The trial's
conversation-only recording returned ``has_speech_signal=False`` while
containing 5,126 words of coherent speech: Whisper's first segment carried a
high ``no_speech_prob`` because the file opened on a long ambient stretch,
and the backend derives the flag from the most-silent segment. A cued
window starts up to 45 seconds BEFORE the operator spoke, so it opens on
ambience essentially every time. Gating on that flag would discard the
majority of real captures. Nothing here reads it as a gate — it is recorded
in telemetry and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .config import JeevesSttConfig

log = structlog.get_logger(__name__)

# Why a transcription produced nothing usable. Each has a different remedy,
# which is the whole reason they are separate strings.
STT_SKIP_UNCONFIGURED = "stt_unconfigured"
STT_SKIP_EMPTY_WINDOW = "empty_window"
STT_EMPTY_RESULT = "empty_transcript"
STT_FAILED = "stt_failed"


@dataclass(frozen=True)
class JeevesTranscript:
    """A cued window's transcription outcome.

    ``text`` empty with ``ok`` True means the engine ran and heard nothing —
    a real, distinct outcome from the engine having failed (``ok`` False),
    and the two must never render the same way to an operator.
    """

    text: str
    ok: bool
    reason: str = ""
    backend_id: str = ""
    #: Opaque per-engine confidence (Whisper ``avg_logprob``). NEVER
    #: compared across engines; carried for telemetry only.
    confidence_raw: float | None = None
    #: Recorded, NEVER gated on. See the module docstring.
    has_speech_signal: bool | None = None
    latency_ms: int = 0
    vocab_terms_used: int = 0


def wav_bytes(pcm: bytes, sample_rate: int, sample_width: int, channels: int) -> bytes:
    """Wrap raw PCM in a minimal RIFF/WAVE header.

    Hand-rolled rather than via ``wave`` because that module wants a file
    object and a temp file on disk — for a package whose second fence is
    "cued audio is never persisted", routing every capture through a
    filesystem write to add 44 bytes would be an own goal.
    """
    byte_rate = sample_rate * channels * sample_width
    block_align = channels * sample_width
    data_len = len(pcm)
    header = b"".join([
        b"RIFF",
        (36 + data_len).to_bytes(4, "little"),
        b"WAVEfmt ",
        (16).to_bytes(4, "little"),      # PCM fmt chunk size
        (1).to_bytes(2, "little"),       # audio format = PCM
        channels.to_bytes(2, "little"),
        sample_rate.to_bytes(4, "little"),
        byte_rate.to_bytes(4, "little"),
        block_align.to_bytes(2, "little"),
        (sample_width * 8).to_bytes(2, "little"),
        b"data",
        data_len.to_bytes(4, "little"),
    ])
    return header + pcm


def resolve_vocab(config: JeevesSttConfig) -> list[str]:
    """The vocabulary that will actually bias this transcription.

    Delegates to the single seam. The import is local because
    ``stt_vocab_learning`` is a talker-side module and this package must stay
    importable on a capture device that has no talker configured.
    """
    from alfred.telegram.stt_vocab_learning import effective_vocab_terms

    terms = effective_vocab_terms(config)
    if not config.vocab_decided_path:
        # Intentionally-left-blank: "no learned terms" and "the decided store
        # is missing" are different facts, and only one of them needs fixing.
        log.info(
            "jeeves.stt.vocab_static_only",
            static_terms=len(terms),
            detail="jeeves.stt.vocab_decided_path is unset, so only the "
                   "static vocab_terms list biases transcription — no "
                   "operator-approved learned terms are being applied.",
        )
    return terms


def build_backend(config: JeevesSttConfig):
    """Construct the wired Groq backend for this config, or ``None``.

    ``None`` when no API key is configured — the caller reports
    ``stt_unconfigured`` rather than attempting a call that can only 401.
    """
    if not config.api_key:
        return None
    from alfred.telegram.stt_backends import GroqWhisperBackend

    return GroqWhisperBackend(
        api_key=config.api_key,
        model=config.model,
        language=config.language,
        timeout_s=config.timeout_seconds,
        backend_id="jeeves-groq-whisper",
    )


async def transcribe_window(
    audio: bytes,
    *,
    config: JeevesSttConfig,
    sample_rate: int,
    sample_width: int,
    channels: int,
    backend=None,
) -> JeevesTranscript:
    """Transcribe one cued window.

    ``backend`` is injectable so the suite exercises this whole path — vocab
    resolution, WAV framing, the empty/failed distinction, and the
    never-gate-on-speech-flag rule — without a network or an API key.
    """
    if not audio:
        log.warning(
            "jeeves.stt.skipped",
            reason=STT_SKIP_EMPTY_WINDOW,
            detail="a cued window carried no audio; nothing was sent to STT",
        )
        return JeevesTranscript(text="", ok=False, reason=STT_SKIP_EMPTY_WINDOW)

    engine = backend if backend is not None else build_backend(config)
    if engine is None:
        log.error(
            "jeeves.stt.skipped",
            reason=STT_SKIP_UNCONFIGURED,
            detail="jeeves.stt.api_key is empty — a cued window was captured "
                   "and then DISCARDED because there is nowhere to transcribe "
                   "it. Set the key or set jeeves.wake.provider to 'off'.",
        )
        return JeevesTranscript(text="", ok=False, reason=STT_SKIP_UNCONFIGURED)

    vocab = resolve_vocab(config)
    payload = wav_bytes(audio, sample_rate, sample_width, channels)

    from alfred.telegram.stt_backends import SttError

    try:
        result = await engine.transcribe(payload, "audio/wav", vocab)
    except SttError as exc:
        log.warning(
            "jeeves.stt.failed",
            reason=STT_FAILED,
            error_class=getattr(exc, "error_class", "unknown"),
            detail=str(exc)[:200],
            backend_id=getattr(exc, "backend_id", ""),
        )
        return JeevesTranscript(
            text="", ok=False, reason=STT_FAILED,
            backend_id=getattr(exc, "backend_id", ""),
        )
    except Exception as exc:  # noqa: BLE001 — a capture must not kill the loop
        log.warning(
            "jeeves.stt.failed",
            reason=STT_FAILED,
            error_type=type(exc).__name__,
            detail=str(exc)[:200],
        )
        return JeevesTranscript(text="", ok=False, reason=STT_FAILED)

    text = (result.text or "").strip()
    # NOTE: ``result.has_speech_signal`` is deliberately NOT consulted here.
    # See the module docstring — the Q6 trial produced a False on a file with
    # 5,126 words, and every cued window opens on ambience by construction.
    if not text:
        log.info(
            "jeeves.stt.empty",
            reason=STT_EMPTY_RESULT,
            backend_id=result.backend_id,
            has_speech_signal=result.has_speech_signal,
            latency_ms=result.latency_ms,
            detail="the engine ran and returned no words — a real outcome, "
                   "not a failure",
        )
        return JeevesTranscript(
            text="", ok=True, reason=STT_EMPTY_RESULT,
            backend_id=result.backend_id,
            confidence_raw=result.confidence_raw,
            has_speech_signal=result.has_speech_signal,
            latency_ms=result.latency_ms,
            vocab_terms_used=len(vocab),
        )

    log.info(
        "jeeves.stt.transcribed",
        backend_id=result.backend_id,
        # Length, never content. The transcript itself never reaches a log.
        text_chars=len(text),
        confidence_raw=result.confidence_raw,
        has_speech_signal=result.has_speech_signal,
        latency_ms=result.latency_ms,
        vocab_terms=len(vocab),
    )
    return JeevesTranscript(
        text=text, ok=True,
        backend_id=result.backend_id,
        confidence_raw=result.confidence_raw,
        has_speech_signal=result.has_speech_signal,
        latency_ms=result.latency_ms,
        vocab_terms_used=len(vocab),
    )
