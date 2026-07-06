"""V2 streaming-TTS provider seam — normalized events + the fake provider.

The TTS plane (``voice_tts.VoiceTtsWorker``) consumes a
:class:`TTSStreamProvider` that emits NORMALIZED events (audio / turn_done /
error), NOT raw provider messages — the mirror of ``stt_stream`` but with the
feed direction inverted (feed=text, events=PCM audio). This module holds:

* :class:`TTSEvent` + the ``EVENT_*`` constants — the normalized vocabulary;
* :class:`TTSStreamProvider` — the provider ABC (ElevenLabs, fake, and a
  future Cartesia all implement the same six methods);
* :class:`FakeTTSProvider` — deterministic 440 Hz tone (duration proportional
  to fed text) for the keyless dev box AND the unconditional unit tests;
* :func:`normalize_tts_settings` — the pure clamp helper (mount-time).

**ZERO optional-dep imports** (no aiortc / av / aiohttp / httpx). The batch STT
error taxonomy is re-exported as ``TTS_ERR_*`` with the SAME string values so
the morning STT/TTS rollup buckets errors identically (precedent: the STT
plane's ``stt_stream`` re-export).

Per-turn lifecycle (contract §1.3): ``begin_turn`` (state reset; for a real
provider it PRE-WARMS the WS so the connect latency hides inside the LLM's
turn_started→first-sentence gap) → ``feed_text`` * → ``end_of_reply`` (flush:
force the final generation + drain to turn_done) → (next ``begin_turn``).
``cancel_turn`` aborts synthesis ASAP and suppresses the turn's remaining
events. Invariants (docstring-pinned, mirroring the STT ABC): ``events()`` is
single-consumer; a fatal error is ALWAYS the last event; ``events()``
terminates after ``close()`` or a fatal error; PCM/text is NEVER logged; a
zero-``feed_text`` turn still gets its ``turn_done`` on ``end_of_reply`` (no
connection is opened).
"""

from __future__ import annotations

import array
import asyncio
import dataclasses
import math
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from alfred.telegram.stt_backends import (  # same string values → same rollup buckets
    STT_ERR_AUTH as TTS_ERR_AUTH,
    STT_ERR_BAD_REQUEST as TTS_ERR_BAD_REQUEST,
    STT_ERR_NETWORK as TTS_ERR_NETWORK,
    STT_ERR_RATE_LIMIT as TTS_ERR_RATE_LIMIT,
    STT_ERR_UNKNOWN as TTS_ERR_UNKNOWN,
)

from .utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import WebVoiceTtsConfig

log = get_logger(__name__)

__all__ = [
    "EVENT_AUDIO", "EVENT_TURN_DONE", "EVENT_ERROR",
    "TTSEvent", "TTSStreamProvider", "FakeTTSProvider", "normalize_tts_settings",
    "TTS_ERR_AUTH", "TTS_ERR_BAD_REQUEST", "TTS_ERR_NETWORK",
    "TTS_ERR_RATE_LIMIT", "TTS_ERR_UNKNOWN",
    "VALID_OUTPUT_FORMATS", "rate_from_output_format",
]

EVENT_AUDIO = "audio"
EVENT_TURN_DONE = "turn_done"
EVENT_ERROR = "error"

# Provider output formats the playout resampler knows how to ingest.
VALID_OUTPUT_FORMATS: frozenset[str] = frozenset({
    "pcm_16000", "pcm_22050", "pcm_24000", "pcm_44100",
})


def rate_from_output_format(fmt: str) -> int:
    """``"pcm_24000"`` → ``24000``; unknown → 24000 (the safe default)."""
    try:
        return int(fmt.rsplit("_", 1)[1])
    except (ValueError, IndexError, AttributeError):
        return 24000


@dataclass
class TTSEvent:
    """A normalized TTS event.

    ``pcm`` (audio only) is raw s16le MONO @ ``provider.output_rate`` and is
    NEVER logged. ``detail`` on an error is a short classified status head,
    never provider text.
    """

    type: str
    turn_id: str = ""
    pcm: bytes = b""     # audio only
    reason: str = ""     # error only: TTS_ERR_* class
    detail: str = ""     # error only: short status head (class+status)
    fatal: bool = False  # error only: provider dead for the session → LAST event


class TTSStreamProvider(ABC):
    """Provider ABC — a single-consumer normalized TTS event stream.

    See the module docstring for the per-turn lifecycle + invariants.
    """

    provider_id: str = ""
    output_rate: int = 24000   # sample rate of EVENT_AUDIO pcm (s16le mono)

    @abstractmethod
    async def begin_turn(self, turn_id: str) -> None:
        """New turn: reset per-turn state. A real provider PRE-WARMS its WS
        here (TTFA hides the connect inside the LLM gap); the fake is state-only."""

    @abstractmethod
    async def feed_text(self, chunk: str) -> None:
        """Feed one reply text chunk. Lazy-connects on the first call per turn."""

    @abstractmethod
    async def end_of_reply(self) -> None:
        """Force final generation + drain until the turn's ``turn_done``."""

    @abstractmethod
    async def cancel_turn(self) -> None:
        """Abort synthesis ASAP; suppress the current turn's remaining events."""

    def request_cancel(self) -> None:
        """V3 barge primitive (contract §1.2) — SYNCHRONOUS, callable directly
        from ``worker.interrupt_speech`` (NOT via the sender queue, which made
        the V2 cancel mechanically circular). Abort the in-flight turn's
        synthesis ASAP without awaiting. Default: no-op (a provider with no
        interruptible I/O — the fake — has nothing to break)."""
        return None

    @abstractmethod
    async def close(self) -> None:
        """Session-final. Idempotent. Ends :meth:`events`."""

    @abstractmethod
    def events(self) -> AsyncIterator[TTSEvent]:
        """Single-consumer normalized event stream (see class docstring)."""


# --- Fake provider (deterministic; dev box + unit tests) -------------------


def _sine_pcm(n_samples: int, rate: int, tone_hz: int, amp: float) -> bytes:
    """``n_samples`` of a ``tone_hz`` sine at ``amp`` (0..1 of full scale),
    s16le mono. Pure stdlib (no numpy) so the fake stays zero-dep."""
    peak = int(amp * 32767)
    buf = array.array("h", (
        int(peak * math.sin(2 * math.pi * tone_hz * i / rate))
        for i in range(n_samples)
    ))
    return buf.tobytes()


class FakeTTSProvider(TTSStreamProvider):
    """Deterministic, keyless TTS: a 440 Hz tone whose duration is proportional
    to the fed text length (contract §1.11). Finite/bounded per turn (the V1
    fake-script lesson) — no wall-clock dependence, so it is reproducible in
    tests AND usable on the keyless dev box (``provider: fake``)."""

    provider_id = "fake"

    def __init__(
        self,
        *,
        rate: int = 24000,
        tone_hz: int = 440,
        ms_per_char: int = 50,
        max_turn_ms: int = 5000,
        chunk_ms: int = 250,
        voice_session_id: str = "",
        scripted_errors: dict[int, TTSEvent] | None = None,
    ) -> None:
        self.output_rate = rate
        self._tone_hz = tone_hz
        self._ms_per_char = ms_per_char
        self._max_turn_ms = max_turn_ms
        self._chunk_ms = chunk_ms
        self._vid = voice_session_id
        self._scripted_errors = scripted_errors or {}

        self._queue: asyncio.Queue[TTSEvent | None] | None = None
        self._closed = False
        self._turn_index = -1
        self._turn_id = ""
        self._chars_fed = 0
        self._cancelled_turn = False

    def _ensure_queue(self) -> "asyncio.Queue[TTSEvent | None]":
        # Lazily created so the provider can be constructed outside a loop.
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    async def begin_turn(self, turn_id: str) -> None:
        self._ensure_queue()
        self._turn_index += 1
        self._turn_id = turn_id
        self._chars_fed = 0
        self._cancelled_turn = False

    async def feed_text(self, chunk: str) -> None:
        if self._closed or self._cancelled_turn:
            return
        self._chars_fed += len(chunk)

    async def cancel_turn(self) -> None:
        self._cancelled_turn = True

    def request_cancel(self) -> None:
        # Sync barge primitive (§1.2): suppress the in-progress turn's audio.
        self._cancelled_turn = True

    async def end_of_reply(self) -> None:
        if self._closed or self._cancelled_turn:
            return
        q = self._ensure_queue()
        # Scripted error injection for this turn (fatal-path unit tests).
        scripted = self._scripted_errors.get(self._turn_index)
        if scripted is not None:
            ev = dataclasses.replace(scripted, turn_id=self._turn_id)
            q.put_nowait(ev)
            return
        # Zero-feed turn: turn_done only (no audio), no connection.
        if self._chars_fed == 0:
            q.put_nowait(TTSEvent(type=EVENT_TURN_DONE, turn_id=self._turn_id))
            return
        audio_ms = max(200, min(self._max_turn_ms, self._chars_fed * self._ms_per_char))
        n_chunks = max(1, math.ceil(audio_ms / self._chunk_ms))
        samples_per_chunk = self.output_rate * self._chunk_ms // 1000
        for _ in range(n_chunks):
            pcm = _sine_pcm(samples_per_chunk, self.output_rate, self._tone_hz, 0.3)
            q.put_nowait(TTSEvent(type=EVENT_AUDIO, turn_id=self._turn_id, pcm=pcm))
        q.put_nowait(TTSEvent(type=EVENT_TURN_DONE, turn_id=self._turn_id))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._ensure_queue().put_nowait(None)  # sentinel — ends events()

    async def events(self) -> AsyncIterator[TTSEvent]:
        q = self._ensure_queue()
        while True:
            ev = await q.get()
            if ev is None:
                return
            yield ev
            if ev.type == EVENT_ERROR and ev.fatal:
                return  # fatal is the last event


# --- Pure settings clamp (mount-time) --------------------------------------


def normalize_tts_settings(
    cfg: "WebVoiceTtsConfig",
) -> "tuple[WebVoiceTtsConfig, list[str]]":
    """Clamp TTS settings to provider-valid ranges. Pure — no logging, no I/O.

    Returns ``(normalized_copy, warnings)``; the mount code logs each warning
    as ``web.voice.tts.config_clamped``. Clamps (contract §1.13 / §1.5):

    * ``output_format`` → one of :data:`VALID_OUTPUT_FORMATS` else ``pcm_24000``;
    * ``max_tts_chars_per_turn`` → ``[200, 20000]``;
    * ``max_buffer_seconds`` → ``[5, 120]``;
    * ``inactivity_timeout_s`` → ``[20, 180]`` (ElevenLabs documented max 180).
    """
    out = dataclasses.replace(cfg)
    warnings: list[str] = []

    if cfg.output_format not in VALID_OUTPUT_FORMATS:
        warnings.append(
            f"output_format {cfg.output_format!r} clamped to pcm_24000"
        )
        out.output_format = "pcm_24000"

    chars = _clamp(cfg.max_tts_chars_per_turn, 200, 20000)
    if chars != cfg.max_tts_chars_per_turn:
        warnings.append(
            f"max_tts_chars_per_turn {cfg.max_tts_chars_per_turn} clamped to {chars}"
        )
    out.max_tts_chars_per_turn = chars

    buf = _clamp(cfg.max_buffer_seconds, 5, 120)
    if buf != cfg.max_buffer_seconds:
        warnings.append(
            f"max_buffer_seconds {cfg.max_buffer_seconds} clamped to {buf}"
        )
    out.max_buffer_seconds = buf

    inact = _clamp(cfg.inactivity_timeout_s, 20, 180)
    if inact != cfg.inactivity_timeout_s:
        warnings.append(
            f"inactivity_timeout_s {cfg.inactivity_timeout_s} clamped to {inact}"
        )
    out.inactivity_timeout_s = inact

    return out, warnings


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))
