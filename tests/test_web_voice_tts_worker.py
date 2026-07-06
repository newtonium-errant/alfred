"""Unit tests for ``alfred.web.voice_tts`` — the VoiceTtsWorker.

UNCONDITIONAL (no aiortc/av/aiohttp): a scripted provider (events driven by the
test) + a recording fake playout drive the sync/await-free driver API, the
speaking callbacks, the degrade taxonomy (transient vs fatal; session NEVER
closed), command-queue overflow drop-newest, cancel ordering, and aclose.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
import structlog

from alfred.web.tts_stream import EVENT_AUDIO, EVENT_ERROR, EVENT_TURN_DONE, TTSEvent
from alfred.web.voice_tts import VoiceTtsWorker


class _ScriptedProvider:
    provider_id = "scripted"
    output_rate = 24000

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()
        self.begin_calls: list = []
        self.feed_calls: list = []
        self.end_calls = 0
        self.cancel_calls = 0
        self.closed = False

    async def begin_turn(self, turn_id: str) -> None:
        self.begin_calls.append(turn_id)

    async def feed_text(self, chunk: str) -> None:
        self.feed_calls.append(chunk)

    async def end_of_reply(self) -> None:
        self.end_calls += 1

    async def cancel_turn(self) -> None:
        self.cancel_calls += 1

    async def close(self) -> None:
        self.closed = True
        self.q.put_nowait(None)

    async def events(self):
        while True:
            ev = await self.q.get()
            if ev is None:
                return
            yield ev
            if ev.type == EVENT_ERROR and ev.fatal:
                return

    def emit(self, ev: TTSEvent) -> None:
        self.q.put_nowait(ev)


class _FakePlayout:
    def __init__(self) -> None:
        self.on_turn_played = None
        self.enqueued: list = []
        self.marks: list = []
        self.flushes: list = []
        self.closed = False

    async def enqueue_pcm(self, turn_id, pcm):
        self.enqueued.append((turn_id, len(pcm)))

    def mark_end_of_turn(self, turn_id):
        self.marks.append(turn_id)

    def flush(self, reason=""):
        self.flushes.append(reason)
        return 0

    def close(self):
        self.closed = True

    @property
    def stats(self):
        return {"frames_out": 0}


def _worker(provider, playout, *, started=None, done=None, fatal=None):
    return VoiceTtsWorker(
        provider=provider, playout=playout, voice_session_id="v1",
        on_speaking_started=(started.append if started is not None else None),
        on_speaking_done=((lambda t, r: done.append((t, r))) if done is not None else None),
        on_fatal=(fatal.append if fatal is not None else None),
    )


# ---------------------------------------------------------------------------
# Sync-seam pin (contract §1.16)
# ---------------------------------------------------------------------------


def test_driver_api_is_sync() -> None:
    for name in ("begin_turn", "feed_text", "end_of_reply", "interrupt_speech"):
        assert not inspect.iscoroutinefunction(getattr(VoiceTtsWorker, name))


# ---------------------------------------------------------------------------
# Feed ordering + speaking callbacks
# ---------------------------------------------------------------------------


async def test_feed_and_end_reach_provider() -> None:
    prov, playout = _ScriptedProvider(), _FakePlayout()
    w = _worker(prov, playout)
    w.start()
    w.begin_turn("t1")
    w.feed_text("t1", "Hello. ")
    w.feed_text("t1", "World.")
    w.end_of_reply("t1")
    await asyncio.sleep(0.05)
    assert prov.begin_calls == ["t1"]
    assert prov.feed_calls == ["Hello. ", "World."]
    assert prov.end_calls == 1
    await w.aclose()


async def test_speaking_started_and_drained() -> None:
    prov, playout = _ScriptedProvider(), _FakePlayout()
    started, done = [], []
    w = _worker(prov, playout, started=started, done=done)
    w.start()
    w.begin_turn("t1")
    prov.emit(TTSEvent(type=EVENT_AUDIO, turn_id="t1", pcm=b"\x00" * 100))
    prov.emit(TTSEvent(type=EVENT_TURN_DONE, turn_id="t1"))
    await asyncio.sleep(0.05)
    assert started == ["t1"]
    assert playout.marks == ["t1"]          # audio played → marked for drain
    # Simulate the playout draining the turn's last frame.
    playout.on_turn_played("t1")
    assert done == [("t1", "drained")]
    await w.aclose()


async def test_zero_audio_turn_no_speaking() -> None:
    prov, playout = _ScriptedProvider(), _FakePlayout()
    started, done = [], []
    w = _worker(prov, playout, started=started, done=done)
    w.start()
    w.begin_turn("t1")
    prov.emit(TTSEvent(type=EVENT_TURN_DONE, turn_id="t1"))   # no audio
    await asyncio.sleep(0.05)
    assert started == []
    assert playout.marks == []              # nothing to drain
    await w.aclose()


# ---------------------------------------------------------------------------
# Cancel — flush FIRST, then abort provider (contract §1.7)
# ---------------------------------------------------------------------------


async def test_interrupt_flushes_then_aborts() -> None:
    prov, playout = _ScriptedProvider(), _FakePlayout()
    w = _worker(prov, playout)
    w.start()
    w.begin_turn("t1")
    prov.emit(TTSEvent(type=EVENT_AUDIO, turn_id="t1", pcm=b"\x00" * 100))
    await asyncio.sleep(0.03)
    w.interrupt_speech("client_cancel")
    await asyncio.sleep(0.03)
    assert playout.flushes and playout.flushes[0] == "client_cancel"
    assert prov.cancel_calls == 1
    # A stale audio event for the cancelled turn is dropped.
    prov.emit(TTSEvent(type=EVENT_AUDIO, turn_id="t1", pcm=b"\x00" * 100))
    await asyncio.sleep(0.03)
    assert len(playout.enqueued) == 1       # the post-cancel audio was NOT enqueued
    await w.aclose()


# ---------------------------------------------------------------------------
# Degrade taxonomy — session NEVER closed
# ---------------------------------------------------------------------------


async def test_fatal_error_degrades_not_closes() -> None:
    prov, playout = _ScriptedProvider(), _FakePlayout()
    fatal = []
    w = _worker(prov, playout, fatal=fatal)
    w.start()
    w.begin_turn("t1")
    prov.emit(TTSEvent(type=EVENT_ERROR, turn_id="t1", reason="auth", fatal=True))
    await asyncio.sleep(0.05)
    assert len(fatal) == 1 and fatal[0].reason == "auth"
    assert w.degraded is True
    # Degraded → further feeds are no-ops (no session close — the worker holds
    # no manager ref, degrade is the whole story).
    w.feed_text("t1", "ignored")
    await asyncio.sleep(0.02)
    assert "ignored" not in prov.feed_calls
    await w.aclose()


async def test_transient_error_is_per_turn_until_three() -> None:
    prov, playout = _ScriptedProvider(), _FakePlayout()
    fatal = []
    w = _worker(prov, playout, fatal=fatal)
    w.start()
    with structlog.testing.capture_logs() as cap:
        prov.emit(TTSEvent(type=EVENT_ERROR, turn_id="t1", reason="network", fatal=False))
        prov.emit(TTSEvent(type=EVENT_ERROR, turn_id="t2", reason="network", fatal=False))
        await asyncio.sleep(0.05)
        assert fatal == []                  # 2 consecutive → still per-turn
        prov.emit(TTSEvent(type=EVENT_ERROR, turn_id="t3", reason="network", fatal=False))
        await asyncio.sleep(0.05)
    assert len(fatal) == 1                   # 3rd consecutive → latch
    degraded = [c for c in cap if c.get("event") == "web.voice.tts.turn_degraded"]
    assert len(degraded) == 3
    await w.aclose()


async def test_turn_done_resets_consecutive_failures() -> None:
    prov, playout = _ScriptedProvider(), _FakePlayout()
    fatal = []
    w = _worker(prov, playout, fatal=fatal)
    w.start()
    prov.emit(TTSEvent(type=EVENT_ERROR, turn_id="t1", reason="network", fatal=False))
    prov.emit(TTSEvent(type=EVENT_ERROR, turn_id="t2", reason="network", fatal=False))
    prov.emit(TTSEvent(type=EVENT_TURN_DONE, turn_id="t3"))   # a success resets
    prov.emit(TTSEvent(type=EVENT_ERROR, turn_id="t4", reason="network", fatal=False))
    await asyncio.sleep(0.05)
    assert fatal == []                       # reset broke the streak
    await w.aclose()


# ---------------------------------------------------------------------------
# Command-queue overflow drop-newest
# ---------------------------------------------------------------------------


async def test_feed_overflow_drops_newest_and_counts() -> None:
    prov, playout = _ScriptedProvider(), _FakePlayout()
    w = _worker(prov, playout)
    # Do NOT start() → the sender never drains → the bounded queue fills.
    w.begin_turn("t1")
    for _ in range(300):
        w.feed_text("t1", "x")
    assert w.stats["feed_overflow"] > 0


# ---------------------------------------------------------------------------
# aclose
# ---------------------------------------------------------------------------


async def test_aclose_idempotent_closes_provider() -> None:
    prov, playout = _ScriptedProvider(), _FakePlayout()
    w = _worker(prov, playout)
    w.start()
    with structlog.testing.capture_logs() as cap:
        await w.aclose(reason="teardown")
        await w.aclose()                     # no-op
    assert prov.closed is True
    assert playout.closed is True
    summaries = [c for c in cap if c.get("event") == "web.voice.tts.worker_closed"]
    assert len(summaries) == 1
    assert summaries[0]["reason"] == "teardown"
