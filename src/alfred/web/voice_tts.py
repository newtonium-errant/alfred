"""V2 TTS plane — the playout track source + the reply-text worker.

This module holds BOTH halves of the TTS talk-back plane (contract §2):

:class:`TTSPlayoutSource` — the session-lifetime outbound WebRTC track source.
When ``web.voice.tts`` is enabled the outbound track's ``recv()`` delegates to
it for the whole session (NO runtime source swap — silence-fill when idle,
speech when queued), so the ``voice_session`` pts/format-continuity hazard is
satisfied BY CONSTRUCTION: every frame is ``s16 / mono / 48000 Hz / 960-sample``
and ``pts`` is a running sample counter assigned at EMISSION (``+960`` every
``recv()``; ``flush()`` drops queued audio but NEVER touches the counter →
monotonic forever). Pacing is track-is-the-clock (aiortc's sender awaits
``recv()`` and doesn't pace). Backpressure is block-don't-drop; ``flush()`` is
the sync cancellation / V3-barge-in primitive (drop + resampler reset + 5 ms
fade-in + generation gate so a frame blocked mid-enqueue is dropped, not
appended post-flush). The ``frame_factory`` / ``resample_fn`` / ``clock`` /
``sleep`` seams make it unit-testable WITHOUT av / aiortc.

:class:`VoiceTtsWorker` — the TTS mirror of ``VoiceSttWorker``: it owns a
:class:`~alfred.web.tts_stream.TTSStreamProvider` + a :class:`TTSPlayoutSource`
and bridges the turn driver's SYNC / await-free world (contract §1.16) to the
provider's async world. The driver-facing surface is entirely sync
(``put_nowait`` onto a bounded command queue):

* :meth:`begin_turn` — pre-warm the provider WS (connect hides in the LLM gap).
* :meth:`feed_text` — enqueue a reply sentence chunk (drop-NEWEST on overflow —
  a coherent spoken prefix beats a mid-reply splice; the ONLY drop point).
* :meth:`end_of_reply` — flush the turn (force final generation + drain).
* :meth:`interrupt_speech` — the V3 primitive (contract §1.7): flush the PLAYOUT
  buffer FIRST (unblocks a pump stuck on enqueue), THEN abort the provider turn.

Two owned tasks: a SENDER (commands → provider) and a PUMP (provider events →
playout + speaking / degrade callbacks). Degrade policy (contract §1.4): a
fatal (AUTH / BAD_REQUEST) provider error, or 3 consecutive transient
(NETWORK / RATE_LIMIT) turn failures, escalates to :meth:`on_fatal` which
latches TTS off for the session — TTS failure NEVER closes the session (the
text reply plane is fully functional; asymmetry vs STT's fail-honest close).
Per-turn transient errors are LOG-ONLY (no DC event, contract §1.2).

No reply text or PCM is ever logged (chars / bytes / ms / counts only).
"""

from __future__ import annotations

import array
import asyncio
import time
from typing import Any, Awaitable, Callable

from .tts_stream import EVENT_AUDIO, EVENT_ERROR, EVENT_TURN_DONE, TTSEvent
from .utils import get_logger

log = get_logger(__name__)

# --- playout track constants -----------------------------------------------
TRACK_RATE = 48000
TRACK_SAMPLES = 960                 # 20 ms (aiortc AUDIO_PTIME)
FRAME_BYTES = TRACK_SAMPLES * 2     # 1920 (s16 mono)
_SILENCE_FRAME = b"\x00" * FRAME_BYTES
_FADE_SAMPLES = 240                 # 5 ms linear ease-in after a flush

_CMD_QUEUE_MAX = 128         # ~sentences; far above the per-turn char cap
_CONSECUTIVE_FATAL = 3       # consecutive transient turn failures → latch (§1.4)


# ---------------------------------------------------------------------------
# Playout track source (§1.5) — the session-lifetime outbound audio source
# ---------------------------------------------------------------------------


class _AvResampler:
    """Default (source_rate → 48 k) resampler wrapping ``av.AudioResampler``.

    Only built when ``resample_fn`` is not injected AND the source rate isn't
    already 48 k. ``__call__(pcm|None) -> bytes`` (None = drain/flush)."""

    def __init__(self, source_rate: int) -> None:
        import av  # lazy — keeps the module importable without av
        from fractions import Fraction

        self._av = av
        self._Fraction = Fraction
        self._r = av.AudioResampler(format="s16", layout="mono", rate=TRACK_RATE)
        self._src = source_rate
        self._pts = 0

    def __call__(self, pcm: bytes | None) -> bytes:
        if pcm is None:
            frames = self._r.resample(None)
        else:
            n = len(pcm) // 2
            if n == 0:
                return b""
            frame = self._av.AudioFrame(format="s16", layout="mono", samples=n)
            frame.sample_rate = self._src
            frame.pts = self._pts
            frame.time_base = self._Fraction(1, self._src)
            self._pts += n
            frame.planes[0].update(pcm)
            frames = self._r.resample(frame)
        return b"".join(bytes(f.planes[0]) for f in (frames or []))


def _default_frame_factory(pcm: bytes, pts: int) -> Any:
    """Build a real ``av.AudioFrame`` (s16/mono/48k/960) — lazy av import."""
    import av
    from fractions import Fraction

    frame = av.AudioFrame(format="s16", layout="mono", samples=TRACK_SAMPLES)
    frame.planes[0].update(pcm)
    frame.sample_rate = TRACK_RATE
    frame.pts = pts
    frame.time_base = Fraction(1, TRACK_RATE)
    return frame


class TTSPlayoutSource:
    """The outbound track source for a TTS-enabled session's whole lifetime."""

    def __init__(
        self,
        *,
        source_rate: int,
        voice_session_id: str = "",
        max_buffer_seconds: float = 30.0,
        frame_factory: Callable[[bytes, int], Any] | None = None,
        resample_fn: Callable[[bytes | None], bytes] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._source_rate = source_rate
        self._vid = voice_session_id
        self._max_buffer_bytes = int(max_buffer_seconds * TRACK_RATE * 2)
        self._frame_factory = frame_factory or _default_frame_factory
        self._clock = clock
        self._sleep = sleep

        if resample_fn is not None:
            self._resample_fn = resample_fn
        elif source_rate == TRACK_RATE:
            self._resample_fn = None            # identity fast-path
        else:
            self._resample_fn = _AvResampler(source_rate)

        self._buf = bytearray()
        # (absolute append position at mark, turn_id) — fires on_turn_played
        # once consumption reaches the position.
        self._markers: list[tuple[int, str]] = []
        self._append_pos = 0
        self._consume_pos = 0
        self._speaking_turn: str | None = None
        self._fade_pending = False
        self._closed = False

        self._start: float | None = None
        self._samples_out = 0
        self._space = asyncio.Event()
        self._space.set()
        # Generation gate (contract §1.6): bumped on flush so a frame that was
        # BLOCKED mid-enqueue_pcm at the buffer cap is DROPPED on wake, not
        # appended post-flush (the "zero post-flush speech frames" invariant).
        self._gen = 0

        # stats
        self._frames_out = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._underruns = 0
        self._dropped_ms = 0

        # Fired (turn_id) when a marked turn's last frame is emitted. Wired by
        # the worker to bridge speaking_done{drained} to the driver.
        self.on_turn_played: Callable[[str], None] | None = None

    # -- ingest -------------------------------------------------------------

    def _resample(self, pcm: bytes) -> bytes:
        if self._resample_fn is None:
            return pcm
        return self._resample_fn(pcm)

    async def enqueue_pcm(self, turn_id: str, pcm: bytes) -> None:
        """Resample-once then append; AWAITS buffer space at the cap (block,
        never drop). Wakes on consumption or :meth:`flush`."""
        resampled = self._resample(pcm)
        if not resampled:
            return
        gen = self._gen
        while not self._closed and len(self._buf) >= self._max_buffer_bytes:
            self._space.clear()
            if len(self._buf) < self._max_buffer_bytes or self._closed:
                break
            await self._space.wait()
            if self._gen != gen:
                return   # flushed while we were blocked → drop this frame (§1.6)
        if self._closed or self._gen != gen:
            return
        if self._speaking_turn is None:
            self._speaking_turn = turn_id
        self._buf.extend(resampled)
        self._append_pos += len(resampled)

    def mark_end_of_turn(self, turn_id: str) -> None:
        """Drain the resampler tail + append a turn-boundary marker (sync)."""
        if self._resample_fn is not None:
            try:
                tail = self._resample_fn(None)
            except Exception:  # noqa: BLE001 — drain best-effort
                tail = b""
            if tail:
                self._buf.extend(tail)
                self._append_pos += len(tail)
        self._markers.append((self._append_pos, turn_id))

    def flush(self, reason: str = "") -> int:
        """Drop ALL queued audio + markers; reset the resampler; arm the
        fade-in; wake blocked producers. Returns dropped audio ms. pts is
        untouched — silence resumes at the same monotonic counter."""
        dropped_bytes = len(self._buf)
        dropped_ms = int(dropped_bytes / (TRACK_RATE * 2) * 1000)
        self._buf.clear()
        self._markers.clear()
        self._consume_pos = self._append_pos
        self._speaking_turn = None
        self._gen += 1                       # drop any frame blocked mid-enqueue
        self._dropped_ms += dropped_ms
        if self._resample_fn is not None and hasattr(self._resample_fn, "_r"):
            # Reset the persistent av resampler so a stale tail can't bleed
            # into the next turn after a flush (§1.5).
            try:
                self._resample_fn(None)  # drain + discard
            except Exception:  # noqa: BLE001
                pass
        self._fade_pending = True
        self._space.set()  # loop-safe wake of a blocked enqueue_pcm
        if dropped_ms:
            log.info(
                "web.voice.tts.flush", voice_session_id=self._vid,
                reason=reason, dropped_ms=dropped_ms,
            )
        return dropped_ms

    @property
    def speaking(self) -> bool:
        """Playout-active signal (facet-2 STT echo-gate / V3 barge-in)."""
        return self._speaking_turn is not None or bool(self._buf)

    @property
    def stats(self) -> dict:
        return {
            "frames_out": self._frames_out,
            "speech_frames": self._speech_frames,
            "silence_frames": self._silence_frames,
            "underruns": self._underruns,
            "dropped_ms": self._dropped_ms,
            "buffered_ms": int(len(self._buf) / (TRACK_RATE * 2) * 1000),
        }

    # -- track source contract ----------------------------------------------

    async def recv(self) -> Any:
        await self._pace()
        pcm, is_speech = self._pull_frame()
        frame = self._frame_factory(pcm, self._samples_out)
        self._samples_out += TRACK_SAMPLES
        self._frames_out += 1
        if is_speech:
            self._speech_frames += 1
        else:
            self._silence_frames += 1
        return frame

    async def _pace(self) -> None:
        if self._start is None:
            self._start = self._clock()
            return
        target = self._start + self._samples_out / TRACK_RATE
        delay = target - self._clock()
        if delay > 0:
            await self._sleep(delay)

    def _fire_drained_markers(self) -> None:
        while self._markers and self._consume_pos >= self._markers[0][0]:
            _, turn_id = self._markers.pop(0)
            if self._speaking_turn == turn_id:
                self._speaking_turn = None
            if self.on_turn_played is not None:
                try:
                    self.on_turn_played(turn_id)
                except Exception:  # noqa: BLE001 — a bad callback must not wedge recv
                    log.warning(
                        "web.voice.tts.turn_played_cb_error",
                        voice_session_id=self._vid,
                    )

    def _pull_frame(self) -> tuple[bytes, bool]:
        self._fire_drained_markers()
        avail = len(self._buf)
        if avail == 0:
            if self._markers or self._speaking_turn is not None:
                # A turn is mid-play but its audio hasn't arrived → underrun.
                self._underruns += 1
                if self._underruns == 1 or self._underruns % 50 == 0:
                    log.warning(
                        "web.voice.tts.underrun", voice_session_id=self._vid,
                        underruns=self._underruns,
                    )
            return _SILENCE_FRAME, False

        next_marker = self._markers[0][0] if self._markers else None
        to_marker = (next_marker - self._consume_pos) if next_marker is not None else None

        if avail >= FRAME_BYTES and (to_marker is None or to_marker >= FRAME_BYTES):
            chunk = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            self._consume_pos += FRAME_BYTES
        else:
            # Turn tail (marker within this frame) or a sub-frame remainder:
            # emit up to the marker (or all of buf) and zero-pad to a frame.
            take = avail if to_marker is None else min(avail, to_marker)
            if take <= 0:
                return _SILENCE_FRAME, False
            chunk = bytes(self._buf[:take]) + _SILENCE_FRAME[take:]
            del self._buf[:take]
            self._consume_pos += take

        if len(self._buf) < self._max_buffer_bytes:
            self._space.set()
        if self._fade_pending:
            chunk = self._apply_fade_in(chunk)
            self._fade_pending = False
        return chunk, True

    @staticmethod
    def _apply_fade_in(pcm: bytes) -> bytes:
        """Linear 5 ms ease-in on the first frame after a flush (zero-cross
        de-click). Pure stdlib s16 math."""
        samples = array.array("h")
        samples.frombytes(pcm)
        n = min(_FADE_SAMPLES, len(samples))
        for i in range(n):
            samples[i] = int(samples[i] * i / n)
        return samples.tobytes()

    # -- close --------------------------------------------------------------

    def close(self) -> None:
        """Release a blocked producer + drop the buffer (sync; the track is
        torn down by the pc close)."""
        self._closed = True
        self._buf.clear()
        self._markers.clear()
        self._space.set()


# ---------------------------------------------------------------------------
# TTS worker (§1.3) — reply text → provider → playout bridge
# ---------------------------------------------------------------------------


class VoiceTtsWorker:
    """Per-session TTS worker (sender + pump). Sole owner of the provider +
    playout. Driver-facing methods are sync/await-free."""

    def __init__(
        self,
        *,
        provider: Any,
        playout: Any,
        voice_session_id: str,
        on_speaking_started: Callable[[str], None] | None = None,
        on_speaking_done: Callable[[str, str], None] | None = None,
        on_fatal: Callable[[TTSEvent], None] | None = None,
        max_chars_per_turn: int = 4096,
    ) -> None:
        self._provider = provider
        self._playout = playout
        self._vid = voice_session_id
        # The per-turn spoken cap is ENFORCED in the driver feed guard (contract
        # §1.3 — the driver owns turn semantics); the worker just carries the
        # normalized value so the driver can read it at ``attach_tts``.
        self.max_chars_per_turn = max_chars_per_turn
        self._on_speaking_started = on_speaking_started
        self._on_speaking_done = on_speaking_done
        self._on_fatal = on_fatal

        self._playout.on_turn_played = self._on_turn_played

        self._cmds: asyncio.Queue = asyncio.Queue(maxsize=_CMD_QUEUE_MAX)
        self._sender_task: asyncio.Task | None = None
        self._pump_task: asyncio.Task | None = None

        self._active_turn_id: str | None = None   # set synchronously at begin
        self._cancelled_turn: str | None = None
        self._speaking_turn: str | None = None     # audio started, not yet drained
        self._degraded = False
        self._closing = False
        self._aclose_started = False

        # stats
        self._turns_spoken = 0
        self._chars_fed = 0
        self._sentences_fed = 0
        self._feed_overflow = 0
        self._audio_bytes = 0
        self._consecutive_failures = 0

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def stats(self) -> dict:
        return {
            "turns_spoken": self._turns_spoken,
            "sentences_fed": self._sentences_fed,
            "chars_fed": self._chars_fed,
            "feed_overflow": self._feed_overflow,
            "audio_bytes": self._audio_bytes,
            "playout": self._playout.stats,
        }

    def start(self) -> None:
        self._sender_task = asyncio.ensure_future(self._sender())
        self._pump_task = asyncio.ensure_future(self._pump())
        log.info(
            "web.voice.tts.worker_started", voice_session_id=self._vid,
            provider=getattr(self._provider, "provider_id", "?"),
            source_rate=getattr(self._provider, "output_rate", 0),
        )

    # -- sync, await-free driver API (contract §1.16) -----------------------

    def begin_turn(self, turn_id: str) -> None:
        if self._closing or self._degraded:
            return
        self._active_turn_id = turn_id
        self._cancelled_turn = None
        self._enqueue_cmd(("begin", turn_id, ""))

    def feed_text(self, turn_id: str, text: str) -> None:
        if self._closing or self._degraded or turn_id != self._active_turn_id:
            return
        self._enqueue_cmd(("feed", turn_id, text))

    def end_of_reply(self, turn_id: str) -> None:
        if self._closing or self._degraded:
            return
        self._enqueue_cmd(("end", turn_id, ""))

    def interrupt_speech(self, reason: str) -> None:
        """V3 primitive: flush the PLAYOUT FIRST (unblocks the pump), THEN abort
        the provider turn (contract §1.7). Sync — callable from await-free
        contexts."""
        # 1. Flush playout first so a pump blocked on enqueue_pcm can drain.
        self._playout.flush(reason)
        # 2. SYNCHRONOUS provider cancel (contract §1.2 / reg-W1) — sets the
        #    provider's _cancelled + interrupt event RIGHT NOW (not via the
        #    sender queue, which is circular when the sender is blocked in the
        #    end_of_reply drain). Closes the late-audio-resurrection hazard: the
        #    recv loop drops further audio, and the drain breaks at once.
        try:
            self._provider.request_cancel()
        except Exception:  # noqa: BLE001 — a bad provider must not wedge the driver
            log.warning("web.voice.tts.request_cancel_error", voice_session_id=self._vid)
        # 3. Mark the current turn cancelled + purge queued commands.
        self._cancelled_turn = self._active_turn_id
        self._drain_cmd_queue()
        # 4. Belt-and-braces: queue a provider cancel_turn for full ws teardown
        #    (idempotent — request_cancel already broke the drain).
        self._enqueue_cmd(("cancel", self._active_turn_id or "", ""))
        log.info(
            "web.voice.tts.interrupted", voice_session_id=self._vid,
            reason=reason, turn_id=self._active_turn_id or "",
        )

    def _enqueue_cmd(self, cmd: tuple) -> None:
        try:
            self._cmds.put_nowait(cmd)
        except asyncio.QueueFull:
            # Drop-NEWEST (contract §1.3): a coherent spoken prefix beats a
            # mid-reply splice. Only feed/text commands realistically overflow.
            self._feed_overflow += 1
            if self._feed_overflow == 1 or self._feed_overflow % 50 == 0:
                log.warning(
                    "web.voice.tts.feed_overflow", voice_session_id=self._vid,
                    dropped_total=self._feed_overflow,
                )

    def _drain_cmd_queue(self) -> None:
        while True:
            try:
                self._cmds.get_nowait()
            except asyncio.QueueEmpty:
                return

    # -- sender task --------------------------------------------------------

    async def _sender(self) -> None:
        while True:
            kind, turn_id, text = await self._cmds.get()
            if kind == "__stop__":
                return
            try:
                if kind == "cancel":
                    await self._provider.cancel_turn()
                elif turn_id == self._cancelled_turn:
                    continue  # stale command for a cancelled turn — drop
                elif kind == "begin":
                    await self._provider.begin_turn(turn_id)
                elif kind == "feed":
                    self._sentences_fed += 1
                    self._chars_fed += len(text)
                    await self._provider.feed_text(text)
                elif kind == "end":
                    await self._provider.end_of_reply()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a provider hiccup must not kill the sender
                log.warning(
                    "web.voice.tts.sender_error", voice_session_id=self._vid,
                    error_class=type(exc).__name__, kind=kind,
                )

    # -- pump task ----------------------------------------------------------

    async def _pump(self) -> None:
        async for ev in self._provider.events():
            if ev.type == EVENT_ERROR:
                self._handle_error(ev)
                if ev.fatal:
                    return  # fatal is the last event
                continue
            if ev.turn_id and ev.turn_id == self._cancelled_turn:
                continue  # drop audio / done for a cancelled turn
            if ev.type == EVENT_AUDIO:
                if self._speaking_turn != ev.turn_id:
                    self._speaking_turn = ev.turn_id
                    self._emit_speaking_started(ev.turn_id)
                self._audio_bytes += len(ev.pcm)
                await self._playout.enqueue_pcm(ev.turn_id, ev.pcm)
            elif ev.type == EVENT_TURN_DONE:
                self._consecutive_failures = 0
                self._turns_spoken += 1
                log.info(
                    "web.voice.tts.turn_done", voice_session_id=self._vid,
                    turn_id=ev.turn_id,
                )
                if self._speaking_turn == ev.turn_id:
                    # Audio played → drain marker fires speaking_done{drained}.
                    self._playout.mark_end_of_turn(ev.turn_id)
                # else: zero-audio turn — no speaking_started fired, nothing to close.

    def _handle_error(self, ev: TTSEvent) -> None:
        if ev.fatal:
            log.warning(
                "web.voice.tts.latched_off", voice_session_id=self._vid,
                reason=ev.reason, detail=ev.detail,
            )
            self._end_speaking(ev.turn_id, "error")
            self._degrade(ev)
            return
        # Transient per-turn error — LOG-ONLY (contract §1.2/§1.4), retry next turn.
        self._consecutive_failures += 1
        log.info(
            "web.voice.tts.turn_degraded", voice_session_id=self._vid,
            reason=ev.reason, detail=ev.detail,
            consecutive=self._consecutive_failures,
        )
        self._end_speaking(ev.turn_id, "error")
        if self._consecutive_failures >= _CONSECUTIVE_FATAL:
            log.warning(
                "web.voice.tts.latched_off", voice_session_id=self._vid,
                reason=ev.reason, detail="consecutive_failures",
            )
            self._degrade(ev)

    def _degrade(self, ev: TTSEvent) -> None:
        if self._degraded:
            return
        self._degraded = True
        self._playout.flush("degrade")
        if self._on_fatal is not None:
            try:
                self._on_fatal(ev)
            except Exception:  # noqa: BLE001
                log.warning("web.voice.tts.fatal_cb_error", voice_session_id=self._vid)

    def _end_speaking(self, turn_id: str, reason: str) -> None:
        """Close an in-progress speaking window (flush + speaking_done)."""
        if self._speaking_turn is not None and (not turn_id or self._speaking_turn == turn_id):
            done_turn = self._speaking_turn
            self._speaking_turn = None
            self._playout.flush(reason)
            self._emit_speaking_done(done_turn, reason)

    # -- playout drain callback ---------------------------------------------

    def _on_turn_played(self, turn_id: str) -> None:
        if self._speaking_turn == turn_id:
            self._speaking_turn = None
            self._emit_speaking_done(turn_id, "drained")

    # -- callback bridges ---------------------------------------------------

    def _emit_speaking_started(self, turn_id: str) -> None:
        if self._on_speaking_started is not None:
            try:
                self._on_speaking_started(turn_id)
            except Exception:  # noqa: BLE001
                log.warning("web.voice.tts.speaking_cb_error", voice_session_id=self._vid)

    def _emit_speaking_done(self, turn_id: str, reason: str) -> None:
        if self._on_speaking_done is not None:
            try:
                self._on_speaking_done(turn_id, reason)
            except Exception:  # noqa: BLE001
                log.warning("web.voice.tts.speaking_cb_error", voice_session_id=self._vid)

    # -- close --------------------------------------------------------------

    async def aclose(self, reason: str = "tts_close") -> None:
        """Idempotent teardown, bounded by ``_drain_pipeline`` (5 s)."""
        if self._aclose_started:
            return
        self._aclose_started = True
        self._closing = True
        self._playout.flush("close")

        tasks = [self._sender_task, self._pump_task]
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
                "web.voice.tts.close_error", voice_session_id=self._vid,
                error_class=type(exc).__name__,
            )
        self._playout.close()
        log.info(
            "web.voice.tts.worker_closed", voice_session_id=self._vid,
            reason=reason, **self.stats,
        )
