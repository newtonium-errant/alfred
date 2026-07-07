"""V1 STT worker — inbound WebRTC audio → resample → chunk → provider.

``VoiceSttWorker`` sits between the WebRTC media plane (a second
``MediaRelay().subscribe(track)`` off the inbound mic, per the V0 seam) and a
:class:`~alfred.web.stt_stream.STTStreamProvider`. It owns three tasks:

* **reader** — pulls decoded ``av.AudioFrame``s off the relayed track,
  resamples 48 kHz → 16 kHz mono s16 (``av.AudioResampler``, imported LAZILY
  inside the task so this module stays import-light), and pushes bytes through
  a pure :class:`PcmChunker` (100 ms / 3200-byte chunks). Backpressure is a
  bounded ``Queue`` with DROP-OLDEST (stale audio is worthless to live STT;
  dropping newest would corrupt the speech tail right before EOU).
* **sender** — pulls chunks, lazily ``connect()``s the provider on the FIRST
  chunk (zero STT cost before media flows), ``feed()``s each; on the
  end-of-track sentinel it ``finalize()``s (flush the trailing final).
* **event-pump** — consumes normalized provider events: ``partial`` →
  ``on_partial``; ``final`` → accumulate; ``utterance_end`` → fire
  ``on_utterance`` (above the ``min_utterance_chars`` floor) or log
  ``utterance_empty``; fatal ``error`` → ``on_fatal`` (the manager closes the
  session — fail-honest, no zombie mic-dead sessions).

**No transcript text is ever logged** (chars/counts only). The ``resample_fn``
seam lets the reader logic be unit-tested WITHOUT ``av``/aiortc.
"""

from __future__ import annotations

import asyncio
import math
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from .stt_stream import (
    EVENT_ERROR,
    EVENT_FINAL,
    EVENT_PARTIAL,
    EVENT_UTTERANCE_END,
    STTEvent,
    STTStreamProvider,
)
from .utils import get_logger, pcm_rms

if TYPE_CHECKING:  # pragma: no cover - typing only
    pass

log = get_logger(__name__)

# Input-energy observability (contract §ILB). "Silence-in is invisible" — an
# idle / muted / dead mic streams frames but yields zero transcripts, which used
# to look identical to a healthy quiet session. Counts + energy only; NEVER
# transcript content.
_INPUT_QUIET_RMS_FLOOR = 30.0     # s16 RMS peak below this after N ms = ~silent
_INPUT_QUIET_AFTER_MS = 5000      # only judge quiet once enough audio has fed
# Shadow-capture per-utterance PCM ring cap (drop-oldest past this).
_SHADOW_PCM_MAX_S = 30


# --- pure byte-chunker -----------------------------------------------------


class PcmChunker:
    """Accumulate PCM bytes and emit fixed-size chunks. Pure.

    ``chunk_bytes = sample_rate * 2 * chunk_ms // 1000`` (2 bytes/sample,
    mono) — 3200 B at 16 kHz / 100 ms. Unit-testable without any media dep.
    """

    def __init__(self, sample_rate: int = 16000, chunk_ms: int = 100) -> None:
        self.chunk_bytes = sample_rate * 2 * chunk_ms // 1000
        self._buf = bytearray()

    def push(self, data: bytes) -> list[bytes]:
        """Append ``data``; return any newly-complete fixed-size chunks."""
        self._buf.extend(data)
        out: list[bytes] = []
        while len(self._buf) >= self.chunk_bytes:
            out.append(bytes(self._buf[: self.chunk_bytes]))
            del self._buf[: self.chunk_bytes]
        return out

    def flush(self) -> bytes:
        """Return + clear the sub-chunk tail remainder (may be empty)."""
        tail = bytes(self._buf)
        self._buf.clear()
        return tail


def pcm16_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw s16le MONO PCM in a WAV container. Pure stdlib (``wave`` into
    ``BytesIO``); no new dependency. Used by shadow-capture to hand the fed PCM
    to Groq's Whisper endpoint (which validates the file extension → WAV)."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)          # s16 = 2 bytes/sample
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


# --- worker ----------------------------------------------------------------


class VoiceSttWorker:
    """Per-session STT worker (reader + sender + event-pump)."""

    def __init__(
        self,
        *,
        provider: STTStreamProvider,
        voice_session_id: str,
        on_utterance: Callable[[str], Awaitable[None]],
        on_partial: Callable[[str], Awaitable[None]] | None = None,
        on_fatal: Callable[[STTEvent], Awaitable[None]] | None = None,
        min_utterance_chars: int = 3,
        queue_max_chunks: int = 50,
        chunk_ms: int = 100,
        sample_rate: int = 16000,
        resample_fn: Callable[[Any], list[bytes]] | None = None,
        hello_gate: bool = True,
        shadow_capture: Callable[[bytes, str, float], None] | None = None,
    ) -> None:
        self._provider = provider
        self._vid = voice_session_id
        self._on_utterance = on_utterance
        self._on_partial = on_partial
        self._on_fatal = on_fatal
        self._min_utterance_chars = min_utterance_chars
        self._sample_rate = sample_rate
        self._chunk_ms = chunk_ms
        self._resample_fn = resample_fn

        # Shadow-capture (STT test series, default-OFF). When wired, the sender
        # TEES each fed chunk into a bounded per-utterance PCM buffer and the
        # pump hands the snapshot to ``shadow_capture`` AFTER the unchanged live
        # ``on_utterance`` — fire-and-forget, never blocking the served turn.
        # None ⇒ the tee + snapshot + hook are all skipped (live path byte-
        # identical). Bound at ~30 s (drop-oldest keeps the speech tail near EOU).
        self._shadow_capture = shadow_capture
        self._utt_pcm = bytearray()
        self._utt_pcm_max = _SHADOW_PCM_MAX_S * sample_rate * 2

        # Hello-gate (contract §17b): the reader + relay wiring run immediately,
        # but the provider is NOT connected/fed until the client's DC hello
        # arrives (:meth:`allow_feed`). So a BROKEN datachannel streams ZERO
        # mic audio to the cloud STT and fires ZERO silent LLM turns — no audio
        # egress without a live feedback channel. ``hello_gate=False`` opens the
        # gate immediately (unit tests that drive feeding directly).
        self._feed_gate = asyncio.Event()
        if not hello_gate:
            self._feed_gate.set()
        self._waiting_hello_logged = False

        self._chunker = PcmChunker(sample_rate, chunk_ms)
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=queue_max_chunks)
        self._track: Any = None
        self._reader_task: asyncio.Task | None = None
        self._sender_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None

        self._closing = False
        self._aclose_started = False

        # stats (the worker_closed summary + close diagnosis)
        self._utterances = 0
        self._finals = 0
        self._partials = 0
        self._dropped = 0
        self._chunks_sent = 0

        # input-energy stats — RMS of the PCM actually fed to the provider.
        self._rms_sumsq = 0.0
        self._rms_samples = 0
        self._rms_peak = 0.0
        self._input_quiet_logged = False

    @property
    def _avg_rms(self) -> float:
        if not self._rms_samples:
            return 0.0
        return math.sqrt(self._rms_sumsq / self._rms_samples)

    @property
    def stats(self) -> dict:
        return {
            "utterances": self._utterances,
            "finals": self._finals,
            "partials": self._partials,
            "dropped_chunks": self._dropped,
            "chunks_sent": self._chunks_sent,
            "avg_rms": round(self._avg_rms, 1),
            "peak_rms": round(self._rms_peak, 1),
        }

    def _account_input_energy(self, chunk: bytes) -> None:
        """Fold one fed chunk into the RMS avg/peak + warn ONCE if the mic is
        near-silent after enough audio (contract §ILB). Counts/energy only."""
        n = len(chunk) // 2
        if n <= 0:
            return
        r = pcm_rms(chunk)
        self._rms_sumsq += (r * r) * n
        self._rms_samples += n
        if r > self._rms_peak:
            self._rms_peak = r
        if (not self._input_quiet_logged
                and self._chunks_sent * self._chunk_ms >= _INPUT_QUIET_AFTER_MS
                and self._rms_peak < _INPUT_QUIET_RMS_FLOOR):
            self._input_quiet_logged = True
            log.warning(
                "web.voice.stt.input_quiet",
                voice_session_id=self._vid,
                fed_ms=self._chunks_sent * self._chunk_ms,
                peak_rms=round(self._rms_peak, 1),
                floor=_INPUT_QUIET_RMS_FLOOR,
                detail="mic audio flowing but near-silent — check mic / mute",
            )

    # -- start --------------------------------------------------------------

    def start(self, stt_track: Any) -> None:
        """Spawn reader/sender/event-pump. Call once, from on_track. The
        reader blocks on ``recv()`` until ICE connects and media flows, and
        the provider connect is lazy (first chunk) — zero STT cost before the
        session is actually connected."""
        self._track = stt_track
        self._reader_task = asyncio.ensure_future(self._reader())
        self._sender_task = asyncio.ensure_future(self._sender())
        self._pump_task = asyncio.ensure_future(self._pump())
        log.info(
            "web.voice.stt.worker_started",
            voice_session_id=self._vid, provider=self._provider.provider_id,
        )

    # -- reader -------------------------------------------------------------

    def _frame_to_pcm(self, frame: Any, resampler: Any) -> list[bytes]:
        """Frame (or None flush) → list of resampled PCM byte buffers.

        PyAV plane-padding trap: a resampled ``AudioFrame``'s plane buffer is
        FFmpeg-padded (samples rounded up for SIMD alignment), so
        ``bytes(o.planes[0])`` yields ~64 EXTRA samples/frame of interleaved
        garbage beyond ``o.samples``. Feeding that to Deepgram put ~17 % padding
        into the PCM stream — a discontinuity every 20 ms that killed real
        phone-mic transcription (studio-clean probe speech survived it). Slice
        to the frame's ACTUAL sample count (mono s16 → 2 bytes/sample)."""
        if self._resample_fn is not None:
            return self._resample_fn(frame)
        outs = resampler.resample(frame)
        if not isinstance(outs, list):  # PyAV<10 returned a single frame
            outs = [outs] if outs is not None else []
        return [
            bytes(o.planes[0])[: o.samples * 2]
            for o in outs if o is not None and o.samples > 0
        ]

    async def _reader(self) -> None:
        resampler = None
        if self._resample_fn is None:
            import av  # lazy — module stays import-light
            resampler = av.AudioResampler(
                format="s16", layout="mono", rate=self._sample_rate,
            )
        try:
            while True:
                try:
                    frame = await self._track.recv()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — MediaStreamError = end-of-track
                    break
                for pcm in self._frame_to_pcm(frame, resampler):
                    for chunk in self._chunker.push(pcm):
                        self._enqueue(chunk)
            # end-of-track: flush the resampler + chunker tail.
            try:
                for pcm in self._frame_to_pcm(None, resampler):
                    for chunk in self._chunker.push(pcm):
                        self._enqueue(chunk)
            except Exception:  # noqa: BLE001 — flush best-effort
                pass
            tail = self._chunker.flush()
            if tail:
                self._enqueue(tail)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — unexpected reader death = no audio
            # Not the normal end-of-track (that's the inner break) — a resample
            # or chunker blow-up. Silent no-audio is this incident's class; log
            # loud. The finally still delivers the sentinel so the sender ends.
            log.warning(
                "web.voice.stt.reader_died",
                voice_session_id=self._vid,
                error_class=type(exc).__name__, fatal=True,
                detail="STT media reader died — no audio will reach the provider",
            )
        finally:
            # Deliver the end-of-track sentinel EVEN IF the bounded queue is at
            # capacity — drop-oldest keeps it full, and put_nowait raises
            # QueueFull (it never blocks, but it CAN raise). Mirror _enqueue's
            # drop-oldest so the sender always receives the sentinel + finalizes.
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()  # drop the oldest chunk
                except asyncio.QueueEmpty:  # pragma: no cover - race guard
                    pass
                self._queue.put_nowait(None)

    def _enqueue(self, chunk: bytes) -> None:
        """Drop-OLDEST on a full queue (stale audio is worthless to live STT)."""
        try:
            self._queue.put_nowait(chunk)
            return
        except asyncio.QueueFull:
            pass
        try:
            self._queue.get_nowait()  # drop the oldest chunk
        except asyncio.QueueEmpty:  # pragma: no cover - race guard
            pass
        self._dropped += 1
        if self._dropped == 1 or self._dropped % 50 == 0:
            log.warning(
                "web.voice.stt.backpressure_drop",
                voice_session_id=self._vid,
                dropped_chunks_total=self._dropped,
                queue_max=self._queue.maxsize,
            )
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:  # pragma: no cover - defensive
            pass

    # -- sender -------------------------------------------------------------

    def allow_feed(self) -> None:
        """Open the hello-gate — the client's DC hello arrived, so it is safe
        to connect + feed the STT provider (contract §17b). Wired from the
        turn driver's hello handler at worker-build time."""
        self._feed_gate.set()

    async def _await_hello_gate(self) -> None:
        if self._feed_gate.is_set():
            return  # hello already arrived (fast client) — feed immediately
        # Intentionally-left-blank: track is flowing but no hello yet — the STT
        # provider stays UNCONNECTED (no cloud audio egress). Logged once.
        if not self._waiting_hello_logged:
            self._waiting_hello_logged = True
            log.info(
                "web.voice.stt.waiting_hello",
                voice_session_id=self._vid,
                detail="track flowing but no DC hello — NOT connecting STT yet",
            )
        await self._feed_gate.wait()
        log.info("web.voice.stt.started_on_hello", voice_session_id=self._vid)

    async def _sender(self) -> None:
        try:
            # Hello-gate: do NOT touch the provider until the client's hello. The
            # reader keeps chunking (drop-oldest bounds the queue) while we wait.
            await self._await_hello_gate()
            connected = False
            while True:
                item = await self._queue.get()
                if item is None:  # end-of-track sentinel
                    break
                if not connected:
                    await self._provider.connect()  # lazy connect on first chunk
                    connected = True
                await self._provider.feed(item)
                self._chunks_sent += 1
                self._account_input_energy(item)
                if self._shadow_capture is not None:
                    # Tee the fed chunk into the per-utterance buffer (bounded,
                    # drop-oldest). Same loop as the pump snapshot → lock-free.
                    self._utt_pcm.extend(item)
                    if len(self._utt_pcm) > self._utt_pcm_max:
                        del self._utt_pcm[: len(self._utt_pcm) - self._utt_pcm_max]
            if connected:
                try:
                    await self._provider.finalize()
                except Exception:  # noqa: BLE001 — best-effort trailing flush
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — sender death = audio never reaches STT
            # E.g. a connect() handshake failure propagating raw: without this
            # the sender dies silently and the pump blocks forever on an empty
            # queue → zero transcripts, invisibly. Log loud + fail-honest.
            log.warning(
                "web.voice.stt.sender_died",
                voice_session_id=self._vid,
                error_class=type(exc).__name__, fatal=True,
                detail="STT sender died — audio not reaching the provider",
            )
            if self._on_fatal is not None and not self._closing:
                try:
                    await self._on_fatal(STTEvent(
                        type=EVENT_ERROR, reason="sender_died",
                        detail=type(exc).__name__, fatal=True))
                except Exception:  # noqa: BLE001 — on_fatal must not re-raise here
                    pass

    def _snapshot_utt_pcm(self) -> bytes:
        """Snapshot + clear the per-utterance PCM buffer (sync, same loop as the
        sender tee → lock-free). ``b""`` when shadow-capture is off."""
        if self._shadow_capture is None:
            return b""
        snap = bytes(self._utt_pcm)
        self._utt_pcm.clear()
        return snap

    # -- event-pump ---------------------------------------------------------

    async def _pump(self) -> None:
        try:
            await self._pump_events()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a dead pump = SILENTLY dead STT
            # THE live phone-test incident's class: the pump died (provider
            # events() blew up) and nothing consumed transcripts → zero results,
            # invisibly. NEVER let it vanish — log loud + fail-honest (surface
            # via on_fatal so the session closes instead of zombie-ing mute).
            log.warning(
                "web.voice.stt.pump_died",
                voice_session_id=self._vid,
                error_class=type(exc).__name__, fatal=True,
                detail="STT event pump died — no transcripts will flow",
            )
            if self._on_fatal is not None and not self._closing:
                try:
                    await self._on_fatal(STTEvent(
                        type=EVENT_ERROR, reason="pump_died",
                        detail=type(exc).__name__, fatal=True))
                except Exception:  # noqa: BLE001 — on_fatal must not re-raise here
                    pass

    async def _pump_events(self) -> None:
        buffer: list[str] = []
        async for ev in self._provider.events():
            if ev.type == EVENT_PARTIAL:
                self._partials += 1
                if self._on_partial is not None and not self._closing:
                    await self._on_partial(ev.text)
            elif ev.type == EVENT_FINAL:
                self._finals += 1
                if ev.text.strip():
                    buffer.append(ev.text.strip())
            elif ev.type == EVENT_UTTERANCE_END:
                text = " ".join(buffer).strip()
                buffer = []
                # Snapshot + RESET the per-utterance PCM (both the served and
                # the empty path reset it, so the next utterance never carries
                # this one's audio). b"" when shadow is off.
                utt_pcm = self._snapshot_utt_pcm()
                if len(text) >= self._min_utterance_chars:
                    self._utterances += 1
                    if self._closing:
                        # Teardown must not fire new chat turns.
                        log.info(
                            "web.voice.stt.utterance_discarded_on_close",
                            voice_session_id=self._vid, chars=len(text),
                        )
                    else:
                        log.info(
                            "web.voice.stt.utterance_end",
                            voice_session_id=self._vid,
                            trigger=ev.trigger, transcript_chars=len(text),
                        )
                        await self._on_utterance(text)   # LIVE TURN — unchanged
                        # Shadow AFTER the served turn: fire-and-forget. The hook
                        # (VoiceSttShadow.capture) owns isolation, but wrap the
                        # call too so even a buggy hook can NEVER kill the live
                        # pump (isolation is the #1 property).
                        if self._shadow_capture is not None and utt_pcm:
                            duration_s = len(utt_pcm) / (self._sample_rate * 2)
                            try:
                                self._shadow_capture(utt_pcm, text, duration_s)
                            except Exception:  # noqa: BLE001 — never break the pump
                                log.warning(
                                    "web.voice.stt.shadow_hook_raised",
                                    voice_session_id=self._vid,
                                )
                else:
                    # Intentionally-left-blank: endpointer fired on noise.
                    log.info(
                        "web.voice.stt.utterance_empty",
                        voice_session_id=self._vid, chars=len(text),
                    )
            elif ev.type == EVENT_ERROR and ev.fatal:
                log.warning(
                    "web.voice.stt.error",
                    voice_session_id=self._vid,
                    reason=ev.reason, detail=ev.detail, fatal=True,
                )
                if self._on_fatal is not None and not self._closing:
                    await self._on_fatal(ev)
                return  # provider ends events() after a fatal error

    # -- close --------------------------------------------------------------

    async def aclose(self, reason: str = "worker_close") -> None:
        """Idempotent teardown: closing flag → cancel+await tasks →
        provider.close() → worker_closed summary. Bounded by the caller
        (manager.close, 5 s)."""
        if self._aclose_started:
            return
        self._aclose_started = True
        self._closing = True

        tasks = [self._reader_task, self._sender_task, self._pump_task]
        for t in tasks:
            if t is not None:
                t.cancel()
        for t in tasks:
            if t is not None:
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        try:
            await self._provider.close()
        except Exception as exc:  # noqa: BLE001 — teardown must not raise
            log.warning(
                "web.voice.stt.close_error",
                voice_session_id=self._vid,
                error_class=type(exc).__name__,
            )
        log.info(
            "web.voice.stt.worker_closed",
            voice_session_id=self._vid,
            reason=reason,
            **self.stats,
        )
