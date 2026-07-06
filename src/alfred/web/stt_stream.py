"""V1 streaming-STT provider seam — normalized events + the fake provider.

The STT plane (``voice_stt.VoiceSttWorker``) consumes a
:class:`STTStreamProvider` that emits NORMALIZED events (partial / final /
utterance_end / error), NOT raw provider messages. This module holds:

* :class:`STTEvent` + the ``EVENT_*`` constants — the normalized vocabulary;
* :class:`STTStreamProvider` — the provider ABC (Deepgram, fake, and future
  local-Whisper / Deepgram-Flux all implement these four methods);
* :class:`FakeStreamProvider` — a deterministic, feed-count-driven provider
  for the keyless dev box AND the unconditional unit tests;
* :func:`normalize_stt_settings` — the pure clamp helper (mount-time).

**ZERO optional-dep imports** (no aiortc / av / aiohttp) — fully
unconditional-testable. The batch STT error taxonomy (``STT_ERR_*``) is
re-exported from :mod:`alfred.telegram.stt_backends` so the streaming plane
buckets errors into the SAME classes the morning STT rollup already knows.

Forward-compat (contract §1.16): the ABC abstracts at the normalized-event
level so a Deepgram-Flux (``/v2/listen`` EndOfTurn→utterance_end) or a
local-Whisper provider swaps in without touching the worker. Two provider
invariants every implementation MUST honor:

* **EOU detection is provider-side.** ``utterance_end`` is emitted by the
  provider (Deepgram's speech_final / UtteranceEnd; a local-Whisper provider
  MUST ship its own VAD to emit it) — the worker never guesses end-of-turn.
* **Finals are immutable once emitted.** A ``final`` event's text is never
  revised; corrections arrive as a NEW utterance, never an edit.
"""

from __future__ import annotations

import asyncio
import dataclasses
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from alfred.telegram.stt_backends import (  # reuse the batch taxonomy verbatim
    STT_ERR_AUTH,
    STT_ERR_BAD_REQUEST,
    STT_ERR_NETWORK,
    STT_ERR_RATE_LIMIT,
    STT_ERR_UNKNOWN,
)

from .utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import WebVoiceSttConfig

log = get_logger(__name__)

# Re-export so downstream (voice_stt, tests) import the taxonomy from one place.
__all__ = [
    "EVENT_PARTIAL", "EVENT_FINAL", "EVENT_UTTERANCE_END", "EVENT_ERROR",
    "STTEvent", "STTStreamProvider", "FakeUtterance", "FakeStreamProvider",
    "normalize_stt_settings",
    "STT_ERR_AUTH", "STT_ERR_BAD_REQUEST", "STT_ERR_NETWORK",
    "STT_ERR_RATE_LIMIT", "STT_ERR_UNKNOWN",
]

EVENT_PARTIAL = "partial"
EVENT_FINAL = "final"
EVENT_UTTERANCE_END = "utterance_end"
EVENT_ERROR = "error"


@dataclass
class STTEvent:
    """A normalized STT event.

    ``text`` (partial/final only) is NEVER logged by consumers — transcripts
    reach the vault only via the normal turn path. ``detail`` on an error is a
    short grep-able head (HTTP/close status), never a transcript.
    """

    type: str
    text: str = ""
    trigger: str = ""   # utterance_end: "speech_final" | "utterance_end_fallback" | "finalize" | "fake"
    reason: str = ""    # error: STT_ERR_* class
    detail: str = ""    # error: short status head, never transcript
    fatal: bool = False  # error: unrecoverable → worker closes the session


class STTStreamProvider(ABC):
    """Provider ABC — a single-consumer normalized STT event stream.

    Lifecycle: :meth:`connect` → many :meth:`feed` → :meth:`finalize`
    (optional force-flush) → :meth:`close`. :meth:`events` yields normalized
    :class:`STTEvent`s and TERMINATES after ``close()`` or a fatal error
    event (a fatal error is always the LAST event).
    """

    provider_id: str = ""

    @abstractmethod
    async def connect(self) -> None:
        """Open the stream. Raises on an unrecoverable connect failure."""

    @abstractmethod
    async def feed(self, chunk: bytes) -> None:
        """Feed one PCM chunk (s16le mono @ configured rate). May await a
        bounded internal reconnect. Raises only when fatally dead."""

    @abstractmethod
    async def finalize(self) -> None:
        """Force-flush buffered audio into a final (provider-specific)."""

    @abstractmethod
    async def close(self) -> None:
        """Graceful end. Idempotent. Ends :meth:`events`."""

    @abstractmethod
    def events(self) -> AsyncIterator[STTEvent]:
        """Single-consumer normalized event stream (see class docstring)."""


# --- Fake provider (deterministic; dev box + unit tests) -------------------


@dataclass
class FakeUtterance:
    """One scripted utterance in a :class:`FakeStreamProvider` script."""

    chunks: int              # feed() calls to consume before firing
    partials: list[str] = field(default_factory=list)
    final: str = ""


def _default_fake_script() -> list[FakeUtterance]:
    """FINITE default (contract §1.7): 3 utterances then idle forever.

    A repeating default would fire a real LLM turn every ~2 s unattended on
    the dev box. After the third utterance the provider goes quiet and logs
    ``web.voice.stt.script_exhausted`` once.
    """
    return [
        FakeUtterance(chunks=20, partials=["testing"], final="testing one two"),
        FakeUtterance(chunks=20, partials=["hello"], final="hello there"),
        FakeUtterance(chunks=20, partials=["last"], final="last one"),
    ]


class FakeStreamProvider(STTStreamProvider):
    """Deterministic, FEED-COUNT-driven provider (zero wall clock).

    Utterances fire purely on ``feed()`` count regardless of audio content —
    documented, and exactly what makes it reproducible in tests AND usable on
    the keyless dev box (``provider: fake``). The default script is finite
    (§1.7).
    """

    provider_id = "fake"

    def __init__(
        self,
        script: list[FakeUtterance] | None = None,
        *,
        voice_session_id: str = "",
    ) -> None:
        self._script = script if script is not None else _default_fake_script()
        self._voice_session_id = voice_session_id
        self._idx = 0
        self._feeds = 0            # feeds consumed toward the current utterance
        self._queue: asyncio.Queue[STTEvent | None] | None = None
        self._closed = False
        self._exhausted_logged = False

    def _ensure_queue(self) -> "asyncio.Queue[STTEvent | None]":
        # Created lazily so the provider can be constructed outside a running
        # loop (the worker constructs it, then connect()s inside the loop).
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    async def connect(self) -> None:
        self._ensure_queue()

    def _emit_utterance(self, utt: FakeUtterance, trigger: str) -> None:
        q = self._ensure_queue()
        for p in utt.partials:
            q.put_nowait(STTEvent(type=EVENT_PARTIAL, text=p))
        q.put_nowait(STTEvent(type=EVENT_FINAL, text=utt.final))
        q.put_nowait(STTEvent(type=EVENT_UTTERANCE_END, trigger=trigger))

    async def feed(self, chunk: bytes) -> None:
        if self._closed:
            return
        self._ensure_queue()
        if self._idx >= len(self._script):
            if not self._exhausted_logged:
                self._exhausted_logged = True
                # Intentionally-left-blank: the fake ran out of script and is
                # now idle — observably distinct from a wedged provider.
                log.info(
                    "web.voice.stt.script_exhausted",
                    voice_session_id=self._voice_session_id,
                    utterances=len(self._script),
                    detail="fake STT script exhausted — idle (no further "
                           "utterances will fire)",
                )
            return
        self._feeds += 1
        utt = self._script[self._idx]
        if self._feeds >= utt.chunks:
            self._emit_utterance(utt, trigger="fake")
            self._idx += 1
            self._feeds = 0

    async def finalize(self) -> None:
        # Force the in-progress utterance's final immediately.
        if self._closed:
            return
        self._ensure_queue()
        if self._idx < len(self._script) and self._feeds > 0:
            self._emit_utterance(self._script[self._idx], trigger="finalize")
            self._idx += 1
            self._feeds = 0

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        q = self._ensure_queue()
        q.put_nowait(None)  # sentinel — ends events()

    async def events(self) -> AsyncIterator[STTEvent]:
        q = self._ensure_queue()
        while True:
            ev = await q.get()
            if ev is None:
                return
            yield ev


# --- Pure settings clamp (mount-time) --------------------------------------


def normalize_stt_settings(
    cfg: "WebVoiceSttConfig",
) -> "tuple[WebVoiceSttConfig, list[str]]":
    """Clamp STT settings to provider-valid ranges. Pure — no logging, no I/O.

    Returns ``(normalized_copy, warnings)``. Warnings are human-readable
    strings the mount code logs as ``web.voice.stt.config_clamped`` so a
    clamp is never silent. Clamps (contract §1.5):

    * ``endpointing_ms`` → ``[10, 5000]``;
    * ``utterance_end_ms`` → ``0`` (disabled) OR ``[1000, 5000]`` (Deepgram's
      documented max is 5000; sub-1000 silently no-ops at Deepgram, so clamp
      up + warn);
    * ``sample_rate`` → ``16000`` (the only V1-valid rate).
    """
    out = dataclasses.replace(cfg)
    warnings: list[str] = []

    ep = _clamp(cfg.endpointing_ms, 10, 5000)
    if ep != cfg.endpointing_ms:
        warnings.append(f"endpointing_ms {cfg.endpointing_ms} clamped to {ep}")
    out.endpointing_ms = ep

    ue = cfg.utterance_end_ms
    if ue != 0:
        clamped = _clamp(ue, 1000, 5000)
        if clamped != ue:
            warnings.append(f"utterance_end_ms {ue} clamped to {clamped}")
        out.utterance_end_ms = clamped

    if cfg.sample_rate != 16000:
        warnings.append(f"sample_rate {cfg.sample_rate} clamped to 16000")
        out.sample_rate = 16000

    return out, warnings


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))
