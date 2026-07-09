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

from .endpoint_hold import HOLD, EndpointHoldSettings, classify_tail
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
        endpoint_settings: "EndpointHoldSettings | None" = None,
        endpoint_telemetry: Callable[[dict], None] | None = None,
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

        # Adaptive endpointing (default-OFF). When ``endpoint_settings.enabled``,
        # each EVENT_UTTERANCE_END is a CANDIDATE: a pure ``classify_tail`` on the
        # buffer tail commits a complete thought in the same tick (zero added
        # latency) or arms a bounded CONCURRENT hold (self._hold_task) on a
        # mid-thought signal. The pump keeps consuming events during the hold; a
        # resuming partial/final cancels it and folds into the SAME buffer.
        # ``hold_gen`` is a session-monotonic supersede counter guarding
        # _commit_held against a resume racing the timer. When disabled the whole
        # branch is byte-identical to today (no classify, no task).
        self._endpoint = endpoint_settings
        self._endpoint_telemetry = endpoint_telemetry
        self._buffer: list[str] = []            # promoted from pump-local (:443)
        self._last_partial = ""
        self._hold_gen = 0                       # session-monotonic (NEVER reset)
        self._hold_task: asyncio.Task | None = None
        self._first_hold_at: float | None = None  # per-utterance (cumulative cap)
        self._hold_ms_applied = 0                # per-utterance
        self._resumed_within_hold = False        # per-utterance
        self._ever_held = False                  # per-utterance
        self._last_tail_features: dict | None = None  # per-utterance (telemetry)
        self._last_signal_category: str | None = None  # per-utterance attribution
        # LATCHED at the FIRST hold of the utterance — a resume overwrites
        # _last_* via a non-signal final classify_tail, so telemetry reads these
        # back to report WHAT TRIGGERED the (correct) resumed hold.
        self._hold_trigger_category: str | None = None
        self._hold_trigger_features: dict | None = None
        self._last_audio_at: float | None = None
        self._last_ev_trigger = ""

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
        self._buffer = []
        async for ev in self._provider.events():
            if ev.type == EVENT_PARTIAL:
                self._partials += 1
                self._last_partial = ev.text
                self._last_audio_at = self._now()
                self._note_resume()  # a partial during a hold = resume → cancel
                if self._on_partial is not None and not self._closing:
                    await self._on_partial(ev.text)
            elif ev.type == EVENT_FINAL:
                self._finals += 1
                self._last_audio_at = self._now()
                if ev.text.strip():
                    self._buffer.append(ev.text.strip())  # resumed finals fold in
                self._note_resume()
            elif ev.type == EVENT_UTTERANCE_END:
                await self._on_utterance_end(ev.trigger)
            elif ev.type == EVENT_ERROR and ev.fatal:
                log.warning(
                    "web.voice.stt.error",
                    voice_session_id=self._vid,
                    reason=ev.reason, detail=ev.detail, fatal=True,
                )
                if self._on_fatal is not None and not self._closing:
                    await self._on_fatal(ev)
                return  # provider ends events() after a fatal error

    # -- adaptive endpointing (hold/commit state machine) -------------------

    def _now(self) -> float:
        return asyncio.get_running_loop().time()

    async def _on_utterance_end(self, trigger: str) -> None:
        """The candidate-EOU decision point. Order: noise (empty, no hold) →
        closing (discard, no hold) → bypass (disabled / forced-flush → commit
        inline, byte-identical to today) → classify_tail → commit-inline or
        arm-hold. Every terminal path resets per-utterance state."""
        self._last_ev_trigger = trigger
        text = " ".join(self._buffer).strip()
        if len(text) < self._min_utterance_chars:
            # Intentionally-left-blank: endpointer fired on noise. NEVER hold.
            self._cancel_hold()
            self._snapshot_utt_pcm()
            self._reset_utt()
            log.info("web.voice.stt.utterance_empty",
                     voice_session_id=self._vid, chars=len(text))
            return
        if self._closing:
            # Teardown must not fire new chat turns; drop + cancel any hold.
            self._utterances += 1
            self._cancel_hold()
            self._snapshot_utt_pcm()
            self._reset_utt()
            log.info("web.voice.stt.utterance_discarded_on_close",
                     voice_session_id=self._vid, chars=len(text))
            return
        if (self._endpoint is None or not self._endpoint.enabled
                or trigger in ("finalize", "fake")):
            # DEFAULT-OFF / forced-flush → commit inline, no hold. When endpoint
            # is disabled this branch IS today's exact path → byte-identical.
            await self._commit_inline(trigger=trigger, decision=None)
            return
        result = classify_tail(text, self._last_partial, self._endpoint)
        self._last_tail_features = result.features
        self._last_signal_category = result.signal_category
        if result.decision == HOLD:
            await self._arm_or_commit_hold(trigger)
        else:
            await self._commit_inline(trigger=trigger, decision=result.decision)

    def _note_resume(self) -> None:
        """A partial/final arrived — if a hold is armed it's a resume: cancel it
        (the buffer already accumulated the resumed final → automatic fold). A
        no-op when endpointing is off (no hold is ever armed)."""
        if self._hold_task is not None:
            self._resumed_within_hold = True
            self._cancel_hold()

    def _cancel_hold(self) -> None:
        """Cancel any armed hold + bump ``hold_gen`` so an in-flight
        ``_commit_held`` supersede-check returns. Idempotent."""
        if self._hold_task is not None:
            self._hold_gen += 1
            if not self._hold_task.done():
                self._hold_task.cancel()
            self._hold_task = None

    def _reset_utt(self) -> None:
        """Reset per-utterance state after a terminal decision (guards against
        state bleeding into the next utterance). ``hold_gen`` is session-
        monotonic and is NEVER reset. The teed PCM is reset by the caller's
        ``_snapshot_utt_pcm``."""
        self._buffer = []
        self._last_partial = ""
        self._first_hold_at = None
        self._hold_ms_applied = 0
        self._resumed_within_hold = False
        self._ever_held = False
        self._last_tail_features = None
        self._last_signal_category = None
        self._hold_trigger_category = None
        self._hold_trigger_features = None

    async def _commit_inline(self, *, trigger: str, decision: str | None) -> None:
        """The single commit path — both the inline decision AND ``_commit_held``
        call this. Snapshots the (held+folded) PCM here so audio stays aligned
        with the committed text, fires the unchanged ``on_utterance``, then the
        shadow hook + features-only telemetry, then resets per-utterance state.
        ``decision=None`` = the bypass/disabled path (no telemetry, log fields
        byte-identical to today)."""
        text = " ".join(self._buffer).strip()
        committed_n = len(self._buffer)   # segment count being committed NOW
        utt_pcm = self._snapshot_utt_pcm()
        self._utterances += 1
        extra = ({"held": self._ever_held, "hold_ms": self._hold_ms_applied}
                 if decision is not None else {})
        log.info("web.voice.stt.utterance_end", voice_session_id=self._vid,
                 trigger=trigger, transcript_chars=len(text), **extra)
        await self._on_utterance(text)   # LIVE TURN — unchanged
        if self._shadow_capture is not None and utt_pcm:
            duration_s = len(utt_pcm) / (self._sample_rate * 2)
            try:
                self._shadow_capture(utt_pcm, text, duration_s)
            except Exception:  # noqa: BLE001 — never break the pump
                log.warning("web.voice.stt.shadow_hook_raised",
                            voice_session_id=self._vid)
        if decision is not None:
            self._emit_endpoint_telemetry()
        # LATE-FINAL CARRY-FORWARD (endpoint-hold #1). When this runs from the
        # DETACHED _commit_held timer task, the pump is free to append a NEW final
        # to self._buffer during the on_utterance await above (its EVENT_FINAL
        # branch; _note_resume is a no-op there — the timer already nulled
        # hold_task). A blanket _reset_utt would DROP that late final if its own
        # utterance_end has not fired yet. Buffer segments only ever APPEND (never
        # reorder), so [committed_n:] is EXACTLY the finals that arrived during the
        # await — preserve them so the resumed speech commits on its own EOU (its
        # teed PCM, already snapshot-cleared above, stays aligned with it). The
        # committed span [:committed_n] is dropped, so no double-fire. In the
        # inline pump-driven path the pump is blocked on this await, nothing
        # appends, carried is empty → byte-identical to today.
        carried = self._buffer[committed_n:]
        self._reset_utt()
        self._buffer = carried

    async def _arm_or_commit_hold(self, trigger: str) -> None:
        """Arm a bounded CONCURRENT hold (a detached cancellable Task — the pump
        keeps consuming events), unless the cumulative ceiling from
        ``first_hold_at`` is already reached → force-commit now."""
        self._cancel_hold()  # supersede any prior in-flight hold before re-deciding
        now = self._now()
        if self._first_hold_at is None:
            self._first_hold_at = now
            # Latch the TRIGGERING attribution at the FIRST hold of the utterance
            # — resumed-hold telemetry emits these (the final classify_tail
            # overwrites _last_*). In a rare MULTI-hold utterance
            # (hold→resume→hold→resume→commit) this DELIBERATELY attributes the
            # one per-utterance record to the FIRST trigger, NOT last/all-trigger:
            # the one-record-per-utterance model is code-reviewer-cleared as
            # defensible (not a defect). Pinned by
            # test_multi_hold_attributes_to_first_trigger — do not silently change.
            self._hold_trigger_category = self._last_signal_category
            self._hold_trigger_features = self._last_tail_features
        elapsed_ms = (now - self._first_hold_at) * 1000.0
        remaining_ms = self._endpoint.max_total_hold_ms - elapsed_ms
        if remaining_ms <= 0:
            # Cumulative ceiling → force-commit regardless of signal (deterministic
            # worst-case latency; an "and-um-and-uh" tail can never hang the turn).
            log.info("web.voice.stt.endpoint_hold_ceiling",
                     voice_session_id=self._vid, cum_ms=int(elapsed_ms))
            await self._commit_inline(trigger=trigger, decision=HOLD)
            return
        delay_ms = min(float(self._endpoint.base_extend_ms), remaining_ms)
        self._hold_ms_applied = int(elapsed_ms + delay_ms)
        self._ever_held = True
        self._hold_gen += 1
        gen = self._hold_gen
        self._hold_task = asyncio.ensure_future(
            self._hold_then_commit(gen, delay_ms / 1000.0))
        log.info("web.voice.stt.endpoint_hold_armed", voice_session_id=self._vid,
                 delay_ms=int(delay_ms), cum_ms=self._hold_ms_applied)

    async def _hold_then_commit(self, gen: int, delay_s: float) -> None:
        try:
            await asyncio.sleep(delay_s)
            await self._commit_held(gen)
        except asyncio.CancelledError:
            raise  # resume/close cancelled us — expected, no commit fires

    async def _commit_held(self, gen: int) -> None:
        """Timer expired with no resumption → commit the accumulated buffer.
        The ``hold_gen`` guard drops a stale timer that a resume/re-arm already
        superseded (race-safe on the single loop)."""
        if gen != self._hold_gen:
            return  # superseded
        self._hold_task = None
        if self._closing:
            self._snapshot_utt_pcm()
            self._reset_utt()
            return
        text = " ".join(self._buffer).strip()
        if len(text) < self._min_utterance_chars:
            self._snapshot_utt_pcm()
            self._reset_utt()
            return
        await self._commit_inline(trigger=self._last_ev_trigger, decision=HOLD)

    def _emit_endpoint_telemetry(self) -> None:
        """Fire the features-ONLY endpoint event (collect-only; applies nothing).
        Never the raw tail text — only the category booleans + decision/timing.

        For a RESUMED hold, ``_last_tail_features`` / ``_last_signal_category``
        were overwritten by the final non-signal ``classify_tail`` (the utterance
        committed on a non-signal tail after folding the resumed speech). Emit the
        LATCHED trigger attribution instead, so the record reports WHAT TRIGGERED
        the (correct) hold — otherwise a held record would read all-false /
        signal_category=None ("held but nothing fired") and the soak could not
        break resumed holds down per-signal (contract §6). The non-resumed path
        is already correct (no second classify); cumulative scalars stay as-is."""
        if self._endpoint_telemetry is None:
            return
        if self._resumed_within_hold and self._hold_trigger_features is not None:
            base_features = self._hold_trigger_features
            category = self._hold_trigger_category
        else:
            base_features = self._last_tail_features
            category = self._last_signal_category
        if base_features is None:
            return
        now = self._now()
        ms_silence = (int((now - self._last_audio_at) * 1000)
                      if self._last_audio_at is not None else 0)
        fields = dict(base_features)
        fields.update({
            "decision": HOLD if self._ever_held else "commit",
            "signal_category": category,   # per-signal attribution (latched on resume)
            "hold_ms_applied": self._hold_ms_applied,
            "resumed_within_hold": self._resumed_within_hold,
            "ms_trailing_silence_at_fire": ms_silence,
            "trigger": self._last_ev_trigger,
        })
        self._endpoint_telemetry(fields)   # the fire-and-forget emit hook

    # -- close --------------------------------------------------------------

    async def aclose(self, reason: str = "worker_close") -> None:
        """Idempotent teardown: closing flag → cancel+await tasks →
        provider.close() → worker_closed summary. Bounded by the caller
        (manager.close, 5 s)."""
        if self._aclose_started:
            return
        self._aclose_started = True
        self._closing = True
        self._cancel_hold()  # an armed endpoint hold must not fire during teardown

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
