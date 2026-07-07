"""Unit tests for ``alfred.web.voice_stt`` — PcmChunker + VoiceSttWorker.

UNCONDITIONAL (no av/aiortc): the ``resample_fn`` seam replaces
``av.AudioResampler`` and a fake track + scripted provider drive the worker
logic — chunk math, drop-oldest backpressure, EOU accumulation, the
min-chars floor, lazy connect, sentinel→finalize, on_fatal, aclose idempotence
+ closing-flag suppression, stats.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog

from alfred.web.stt_stream import (
    EVENT_ERROR,
    EVENT_FINAL,
    EVENT_PARTIAL,
    EVENT_UTTERANCE_END,
    STTEvent,
    STTStreamProvider,
)
from alfred.web.voice_stt import PcmChunker, VoiceSttWorker


# ---------------------------------------------------------------------------
# PcmChunker — pure byte math
# ---------------------------------------------------------------------------


def test_chunker_chunk_size() -> None:
    assert PcmChunker(16000, 100).chunk_bytes == 3200
    assert PcmChunker(16000, 20).chunk_bytes == 640


def test_chunker_emits_complete_chunks() -> None:
    c = PcmChunker(16000, 100)
    assert c.push(b"\x00" * 1600) == []          # half a chunk, nothing yet
    out = c.push(b"\x00" * 1600)                 # completes one 3200-B chunk
    assert len(out) == 1 and len(out[0]) == 3200
    assert c.flush() == b""


def test_chunker_multiple_chunks_and_tail() -> None:
    c = PcmChunker(16000, 100)
    out = c.push(b"\x00" * 7000)  # 2 full chunks (6400) + 600 tail
    assert [len(x) for x in out] == [3200, 3200]
    assert len(c.flush()) == 600


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _ScriptedProvider(STTStreamProvider):
    """A provider whose event stream the test drives directly."""

    provider_id = "scripted"

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.connect_calls = 0
        self.feeds = 0
        self.finalize_calls = 0
        self.closed = False

    async def connect(self) -> None:
        self.connect_calls += 1

    async def feed(self, chunk: bytes) -> None:
        self.feeds += 1

    async def finalize(self) -> None:
        self.finalize_calls += 1

    async def close(self) -> None:
        self.closed = True
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
    """Yields the given frames, then raises (simulating MediaStreamError)."""

    def __init__(self, frames: list) -> None:
        self._frames = list(frames)

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        raise RuntimeError("end-of-track")


def _worker(provider, *, on_utterance=None, on_partial=None, on_fatal=None,
            min_utterance_chars=3, queue_max_chunks=50, hello_gate=False):
    async def _noop(_):
        return None
    return VoiceSttWorker(
        provider=provider,
        voice_session_id="v1",
        on_utterance=on_utterance or _noop,
        on_partial=on_partial,
        on_fatal=on_fatal,
        min_utterance_chars=min_utterance_chars,
        queue_max_chunks=queue_max_chunks,
        resample_fn=lambda f: [f] if isinstance(f, (bytes, bytearray)) else [],
        hello_gate=hello_gate,  # tests feed immediately unless a gate test
    )


# ---------------------------------------------------------------------------
# Drop-oldest backpressure
# ---------------------------------------------------------------------------


async def test_enqueue_drop_oldest_counts_and_logs() -> None:
    w = _worker(_ScriptedProvider(), queue_max_chunks=2)
    with structlog.testing.capture_logs() as cap:
        for i in range(5):
            w._enqueue(bytes([i]))
    assert w.stats["dropped_chunks"] == 3
    # The queue holds the NEWEST 2 (drop-oldest).
    remaining = [w._queue.get_nowait() for _ in range(w._queue.qsize())]
    assert remaining == [bytes([3]), bytes([4])]
    drops = [c for c in cap if c.get("event") == "web.voice.stt.backpressure_drop"]
    assert len(drops) == 1  # first drop logged (rate-limited)
    assert drops[0]["dropped_chunks_total"] == 1


# ---------------------------------------------------------------------------
# EOU accumulation + min-chars floor
# ---------------------------------------------------------------------------


async def test_eou_accumulates_finals() -> None:
    prov = _ScriptedProvider()
    got: list[str] = []

    async def on_utt(text):
        got.append(text)

    w = _worker(prov, on_utterance=on_utt)
    w.start(_FakeTrack([]))
    prov.emit(STTEvent(type=EVENT_FINAL, text="hello"))
    prov.emit(STTEvent(type=EVENT_FINAL, text="world"))
    prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
    await asyncio.sleep(0.03)
    assert got == ["hello world"]
    assert w.stats["finals"] == 2 and w.stats["utterances"] == 1
    await w.aclose()


async def test_eou_below_floor_is_empty_signal() -> None:
    prov = _ScriptedProvider()
    got: list[str] = []

    async def on_utt(text):
        got.append(text)

    w = _worker(prov, on_utterance=on_utt, min_utterance_chars=3)
    w.start(_FakeTrack([]))
    with structlog.testing.capture_logs() as cap:
        prov.emit(STTEvent(type=EVENT_FINAL, text="ok"))  # 2 chars < 3
        prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
        await asyncio.sleep(0.03)
    assert got == []  # below floor — no turn
    empties = [c for c in cap if c.get("event") == "web.voice.stt.utterance_empty"]
    assert len(empties) == 1
    await w.aclose()


async def test_partials_forwarded() -> None:
    prov = _ScriptedProvider()
    partials: list[str] = []

    async def on_p(text):
        partials.append(text)

    w = _worker(prov, on_partial=on_p)
    w.start(_FakeTrack([]))
    prov.emit(STTEvent(type=EVENT_PARTIAL, text="hel"))
    prov.emit(STTEvent(type=EVENT_PARTIAL, text="hello"))
    await asyncio.sleep(0.03)
    assert partials == ["hel", "hello"]
    assert w.stats["partials"] == 2
    await w.aclose()


# ---------------------------------------------------------------------------
# Reader/sender: lazy connect + finalize
# ---------------------------------------------------------------------------


async def test_lazy_connect_and_finalize_on_end() -> None:
    prov = _ScriptedProvider()
    w = _worker(prov)
    # One 3200-B frame → exactly one chunk; track then ends.
    w.start(_FakeTrack([b"\x00" * 3200]))
    await asyncio.sleep(0.05)
    assert prov.connect_calls == 1   # lazy connect on the first chunk
    assert prov.feeds == 1
    assert prov.finalize_calls == 1  # flush on end-of-track sentinel
    assert w.stats["chunks_sent"] == 1
    await w.aclose()


async def test_no_connect_when_no_audio() -> None:
    prov = _ScriptedProvider()
    w = _worker(prov)
    w.start(_FakeTrack([]))  # empty track → no chunks
    await asyncio.sleep(0.03)
    assert prov.connect_calls == 0  # zero STT cost when no media flows
    await w.aclose()


async def test_end_of_track_sentinel_delivered_when_queue_full() -> None:
    # NOTE 2: with a bounded queue kept full by drop-oldest, the reader's
    # end-of-track sentinel must STILL reach the sender. WITHOUT the drop-oldest
    # retry, the reader's put_nowait(None) raises QueueFull → the sentinel is
    # lost → the sender hangs forever on queue.get(). WITH the fix both the
    # reader and sender tasks complete. (The synchronous fake track bursts, so
    # drop-oldest discards the audio and nothing is fed — that's fine; this test
    # pins SENTINEL DELIVERY, not feed count.)
    prov = _ScriptedProvider()
    w = _worker(prov, queue_max_chunks=1)  # queue at capacity after one chunk
    frames = [b"\x00" * 3200 for _ in range(6)]
    w.start(_FakeTrack(frames))
    await asyncio.wait_for(w._reader_task, timeout=5)   # would RAISE without the fix
    await asyncio.wait_for(w._sender_task, timeout=5)   # would HANG without the fix
    assert w._sender_task.done() and not w._sender_task.cancelled()
    await w.aclose()


# ---------------------------------------------------------------------------
# Hello-gate (contract §17b) — no cloud STT egress until the client hello
# ---------------------------------------------------------------------------


async def test_hello_gate_blocks_feed_until_allowed() -> None:
    prov = _ScriptedProvider()
    w = _worker(prov, hello_gate=True)
    with structlog.testing.capture_logs() as cap:
        # Track is flowing (chunks are produced) but NO hello yet.
        w.start(_FakeTrack([b"\x00" * 3200, b"\x11" * 3200]))
        await asyncio.sleep(0.05)
        assert prov.connect_calls == 0   # provider untouched — no cloud egress
        assert prov.feeds == 0
        waiting = [c for c in cap if c.get("event") == "web.voice.stt.waiting_hello"]
        assert len(waiting) == 1         # ILB — waiting-for-hello signalled once
        # Now the DC hello arrives → feeding begins.
        w.allow_feed()
        await asyncio.sleep(0.05)
    assert prov.connect_calls == 1
    assert prov.feeds >= 1
    started = [c for c in cap if c.get("event") == "web.voice.stt.started_on_hello"]
    assert len(started) == 1
    await w.aclose()


# ---------------------------------------------------------------------------
# on_fatal
# ---------------------------------------------------------------------------


async def test_on_fatal_fires_on_fatal_error_event() -> None:
    prov = _ScriptedProvider()
    fatals: list = []

    async def on_fatal(ev):
        fatals.append(ev)

    w = _worker(prov, on_fatal=on_fatal)
    w.start(_FakeTrack([]))
    prov.emit(STTEvent(type=EVENT_ERROR, reason="network", detail="1011", fatal=True))
    await asyncio.sleep(0.03)
    assert len(fatals) == 1
    assert fatals[0].reason == "network"
    await w.aclose()


# ---------------------------------------------------------------------------
# aclose idempotence + closing-flag suppression
# ---------------------------------------------------------------------------


async def test_aclose_idempotent_single_summary() -> None:
    prov = _ScriptedProvider()
    w = _worker(prov)
    w.start(_FakeTrack([]))
    with structlog.testing.capture_logs() as cap:
        await w.aclose()
        await w.aclose()  # second is a no-op
    summaries = [c for c in cap if c.get("event") == "web.voice.stt.worker_closed"]
    assert len(summaries) == 1
    assert prov.closed is True


async def test_closing_flag_suppresses_utterance() -> None:
    prov = _ScriptedProvider()
    got: list[str] = []

    async def on_utt(text):
        got.append(text)

    w = _worker(prov, on_utterance=on_utt)
    w._closing = True  # simulate teardown-in-progress before the event arrives
    w.start(_FakeTrack([]))
    with structlog.testing.capture_logs() as cap:
        prov.emit(STTEvent(type=EVENT_FINAL, text="hello there"))
        prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
        await asyncio.sleep(0.03)
    assert got == []  # teardown must not fire new chat turns
    discarded = [
        c for c in cap if c.get("event") == "web.voice.stt.utterance_discarded_on_close"
    ]
    assert len(discarded) == 1
    await w.aclose()


async def test_worker_closed_summary_carries_stats() -> None:
    prov = _ScriptedProvider()
    w = _worker(prov)
    w.start(_FakeTrack([]))
    with structlog.testing.capture_logs() as cap:
        await w.aclose(reason="test_reason")
    summary = [c for c in cap if c.get("event") == "web.voice.stt.worker_closed"][0]
    assert summary["reason"] == "test_reason"
    assert "utterances" in summary and "dropped_chunks" in summary


# ---------------------------------------------------------------------------
# Input-energy observability (silence-in is invisible) — §ILB
# ---------------------------------------------------------------------------


async def test_worker_closed_carries_input_rms() -> None:
    import array
    prov = _ScriptedProvider()
    w = _worker(prov)
    # A constant-amplitude 8000 tone: every s16 sample = 8000 → RMS = 8000.
    tone = array.array("h", [8000] * (1600 * 2)).tobytes()   # 2 full chunks
    w.start(_FakeTrack([tone]))
    await asyncio.wait_for(w._reader_task, timeout=5)
    await asyncio.wait_for(w._sender_task, timeout=5)
    with structlog.testing.capture_logs() as cap:
        await w.aclose()
    summary = [c for c in cap if c.get("event") == "web.voice.stt.worker_closed"][0]
    assert summary["peak_rms"] == 8000.0
    assert summary["avg_rms"] == 8000.0
    # A loud mic never trips the quiet warning.
    assert not any(c.get("event") == "web.voice.stt.input_quiet" for c in cap)


async def test_input_quiet_warns_once_after_silence() -> None:
    prov = _ScriptedProvider()
    # Wide queue so drop-oldest doesn't shave the fed count below the threshold.
    w = _worker(prov, queue_max_chunks=200)
    # 55 chunks (chunk = 100 ms) of digital silence → > _INPUT_QUIET_AFTER_MS
    # with peak RMS 0 → one input_quiet warning (deduped, not per-chunk).
    silence = b"\x00\x00" * (1600 * 55)
    with structlog.testing.capture_logs() as cap:
        w.start(_FakeTrack([silence]))
        await asyncio.wait_for(w._reader_task, timeout=5)
        await asyncio.wait_for(w._sender_task, timeout=5)
        await w.aclose()
    quiet = [c for c in cap if c.get("event") == "web.voice.stt.input_quiet"]
    assert len(quiet) == 1
    assert quiet[0]["peak_rms"] == 0.0
    assert quiet[0]["fed_ms"] >= 5000


# ---------------------------------------------------------------------------
# Pump-before-lazy-connect (the live phone-test root cause)
# ---------------------------------------------------------------------------


class _EagerQueueProvider(STTStreamProvider):
    """Mimics the FIXED Deepgram: events queue EAGER in __init__, connect is
    lazy. The worker's pump reads events() BEFORE the sender connect()s, so an
    eager queue is what makes that ordering safe (blocks empty until feed)."""

    provider_id = "eager"

    def __init__(self) -> None:
        self._events: asyncio.Queue = asyncio.Queue()   # EAGER (the fix)
        self.connected = False

    async def connect(self) -> None:
        self.connected = True

    async def feed(self, chunk: bytes) -> None:
        self._events.put_nowait(STTEvent(type=EVENT_PARTIAL, text="hel"))
        self._events.put_nowait(STTEvent(type=EVENT_FINAL, text="hello there"))
        self._events.put_nowait(STTEvent(type=EVENT_UTTERANCE_END, trigger="t"))

    async def finalize(self) -> None:
        pass

    async def close(self) -> None:
        self._events.put_nowait(None)

    async def events(self):
        while True:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev


class _LazyQueueProvider(STTStreamProvider):
    """Mimics the PRE-FIX Deepgram: events queue created in connect(), None
    before. events() → ``await None.get()`` → AttributeError → the pump dies."""

    provider_id = "lazy"

    def __init__(self) -> None:
        self._events = None   # LAZY — THE bug

    async def connect(self) -> None:
        self._events = asyncio.Queue()

    async def feed(self, chunk: bytes) -> None:
        if self._events is not None:
            self._events.put_nowait(STTEvent(type=EVENT_PARTIAL, text="x"))

    async def finalize(self) -> None:
        pass

    async def close(self) -> None:
        if self._events is not None:
            self._events.put_nowait(None)

    async def events(self):
        while True:
            ev = await self._events.get()   # AttributeError if _events is None
            if ev is None:
                return
            yield ev


def _worker_direct(provider, **kw):
    async def _noop(_):
        return None
    return VoiceSttWorker(
        provider=provider, voice_session_id="v1",
        on_utterance=kw.pop("on_utterance", None) or _noop,
        on_fatal=kw.pop("on_fatal", None),
        resample_fn=lambda f: [f] if isinstance(f, (bytes, bytearray)) else [],
        hello_gate=False, **kw,
    )


async def test_deepgram_events_queue_is_eager_before_connect() -> None:
    # Fix pin: the real DeepgramStreamProvider must create _events/_ready in
    # __init__ (not connect()) so the worker pump can read events() before the
    # sender lazily connects. Pre-fix: both None → pump AttributeError → silent.
    from alfred.web.config import WebVoiceSttConfig
    from alfred.web.stt_deepgram import DeepgramStreamProvider

    prov = DeepgramStreamProvider(
        WebVoiceSttConfig(provider="deepgram", api_key="x", model="nova-3"),
        voice_session_id="v")
    assert prov._events is not None
    assert prov._ready is not None

    # events() must be safe to start awaiting BEFORE connect (blocks, no raise).
    async def _consume():
        async for ev in prov.events():
            return ev.type

    task = asyncio.ensure_future(_consume())
    await asyncio.sleep(0.02)
    assert not task.done()                       # pump alive, blocked on empty q
    prov._events.put_nowait(STTEvent(type=EVENT_PARTIAL, text="hi"))
    assert await asyncio.wait_for(task, timeout=1) == EVENT_PARTIAL


async def test_worker_pump_before_connect_reaches_callbacks() -> None:
    # Positive: with an EAGER-queue provider, the pump (spawned before connect)
    # reaches the callbacks — the whole point of the fix.
    got: list[str] = []

    async def on_utt(text: str) -> None:
        got.append(text)

    prov = _EagerQueueProvider()
    w = _worker_direct(prov, on_utterance=on_utt)
    w.start(_FakeTrack([b"\x01\x02" * 1600]))    # one 3200 B chunk
    await asyncio.wait_for(w._reader_task, timeout=5)
    await asyncio.wait_for(w._sender_task, timeout=5)
    await asyncio.sleep(0.05)                     # let the pump drain
    assert prov.connected
    assert "hello there" in got
    await w.aclose()


async def test_worker_pump_death_is_loud_and_fail_honest() -> None:
    # Hardening pin (the incident's SILENT nature): a LAZY-queue provider makes
    # the pump die (AttributeError) — pre-fix that vanished (0 transcripts, no
    # log). Now it MUST log web.voice.stt.pump_died AND fail-honest via on_fatal
    # so the session closes instead of zombie-ing mute.
    fatal: list = []

    async def on_fatal(ev) -> None:
        fatal.append(ev)

    prov = _LazyQueueProvider()          # _events stays None (never connected)
    w = _worker_direct(prov, on_fatal=on_fatal)
    # Drive the pump DIRECTLY (deterministic — no race with the sender's connect):
    # it reads events() on the None queue → AttributeError, exactly the incident.
    with structlog.testing.capture_logs() as cap:
        await w._pump()
    died = [c for c in cap if c.get("event") == "web.voice.stt.pump_died"]
    assert len(died) == 1
    assert died[0]["error_class"] == "AttributeError"
    assert died[0]["fatal"] is True
    # Fail-honest: the dead pump surfaced via on_fatal (session closes, not mute).
    assert len(fatal) == 1 and fatal[0].reason == "pump_died"


# ---------------------------------------------------------------------------
# Resampler return-shape coercion (PyAV<10 fallback)
# ---------------------------------------------------------------------------


async def test_frame_to_pcm_coerces_non_list_resample_return() -> None:
    # PyAV>=10 (av>=14, pinned by aiortc>=1.14) returns a LIST from resample();
    # PyAV<10 returned a single frame. The reader must coerce a single-frame
    # return into a 1-element list rather than crash — this pins that defensive
    # coercion so a future refactor of _frame_to_pcm can't silently drop it.
    class _Frame:
        def __init__(self, data: bytes) -> None:
            self.planes = [data]
            self.samples = len(data) // 2   # s16 mono; sliced to samples*2

    class _SingleFrameResampler:
        def resample(self, frame):
            return _Frame(b"\x01\x02")  # single frame, NOT a list

    class _NoneResampler:
        def resample(self, frame):
            return None  # some flush calls yield nothing

    w = VoiceSttWorker(  # real reader path (resample_fn=None)
        provider=_ScriptedProvider(),
        voice_session_id="v1",
        on_utterance=lambda _t: None,
    )
    assert w._frame_to_pcm(object(), _SingleFrameResampler()) == [b"\x01\x02"]
    assert w._frame_to_pcm(object(), _NoneResampler()) == []
