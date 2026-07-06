"""Unit tests for ``alfred.web.voice_turns`` — the VoiceTurnDriver.

UNCONDITIONAL (no aiortc): a duck-typed FakeChannel + an injected
``run_turn_streaming_fn`` (scripted async generator) + a fake StateManager
drive the DC protocol, latest-wins queue, KEY_WEB_INFLIGHT guard (incl. the
§1.2 re-verify), and cancellation. The per-event loop body is await-free, so
these pin the wire contract deterministically.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import structlog

from alfred.web.config import WebAuthConfig, WebConfig, WebUser
from alfred.web.identity import WebIdentity, synthetic_chat_id
from alfred.web.voice_turns import (
    EVENT_VERSION,
    MAX_DC_EVENT_BYTES,
    TURN_SLOT_WAIT_S,
    TurnDeps,
    VoiceTurnDriver,
)

_OWNER = synthetic_chat_id("andrew")
_KEY = "sess-key-abc"


class FakeChannel:
    def __init__(self, ready: str = "open") -> None:
        self.readyState = ready
        self.bufferedAmount = 0
        self.sent: list[dict] = []

    def send(self, data: str) -> None:
        import json
        self.sent.append(json.loads(data))


def _active(key: str = _KEY, transcript=None) -> dict:
    return {
        "session_id": key,
        "chat_id": _OWNER,
        "started_at": "2026-01-01T00:00:00+00:00",
        "last_message_at": "2026-01-01T00:00:00+00:00",
        "model": "claude-sonnet-4-6",
        "transcript": transcript if transcript is not None else [],
    }


class _FakeState:
    def __init__(self, active: dict | None) -> None:
        self._active = active

    def get_active(self, chat_id):
        return self._active

    def set_active(self, chat_id, session):
        self._active = session

    def save(self):
        pass


def _scripted_rts(chunks: list[dict]):
    """A fake run_turn_streaming: appends user+assistant turns (so ts
    extraction works) and yields the scripted chunks."""

    async def rts(**kw):
        session = kw["session"]
        rts.calls.append(kw)
        session.transcript.append({
            "role": "user", "content": kw["user_message"],
            "_ts": "2026-01-01T00:00:00+00:00", "_kind": kw.get("user_kind"),
        })
        for c in chunks:
            yield c
        reply = next((c.get("reply", "") for c in chunks if c.get("type") == "final"), "")
        session.transcript.append({
            "role": "assistant", "content": reply,
            "_ts": "2026-01-01T00:00:01+00:00",
        })

    rts.calls = []
    return rts


def _web_config(users_multi: bool = False) -> WebConfig:
    users = [WebUser(name="andrew", role="owner")]
    if users_multi:
        users.append(WebUser(name="ben", role="ops"))
    return WebConfig(enabled=True, users=users, auth=WebAuthConfig(session_secret="x" * 40))


def _deps(state, *, rts, in_flight=None, key=_KEY) -> TurnDeps:
    return TurnDeps(
        client=object(),
        state_mgr=state,
        talker_config=SimpleNamespace(anthropic=SimpleNamespace(model="m")),
        web_config=_web_config(),
        system_prompt_provider=lambda: "SYS",
        vault_context_str="CTX",
        in_flight=in_flight if in_flight is not None else set(),
        identity=WebIdentity(user="andrew", role="owner", synthetic_chat_id=_OWNER),
        chat_session_key=key,
        run_turn_streaming_fn=rts,
    )


async def _wait_for(ch: FakeChannel, types: set[str], timeout: float = 1.0) -> dict:
    """Wait until a frame of one of ``types`` appears; return it."""
    deadline = asyncio.get_event_loop().time() + timeout
    seen = 0
    while asyncio.get_event_loop().time() < deadline:
        for frame in ch.sent[seen:]:
            if frame.get("type") in types or frame.get("state") in types:
                return frame
        seen = len(ch.sent)
        await asyncio.sleep(0.005)
    raise AssertionError(f"no frame of {types} within {timeout}s; got {ch.sent}")


def _hello(driver: VoiceTurnDriver) -> None:
    import json
    driver.on_client_message(json.dumps({"v": 1, "type": "hello"}))


# ---------------------------------------------------------------------------
# Hello-gate
# ---------------------------------------------------------------------------


async def test_hello_gate_drops_pre_hello_then_ready() -> None:
    ch = FakeChannel()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts([])), "v1")
    driver.attach_channel(ch)
    # Emit before hello → dropped (counted), nothing on the wire.
    driver.emit({"v": 1, "type": "stt_partial", "text": "x"})
    assert ch.sent == []
    _hello(driver)
    ready = ch.sent[0]
    assert ready["type"] == "state" and ready["state"] == "ready"
    assert ready["chat_session_key"] == _KEY and ready["voice_session_id"] == "v1"
    await driver.aclose()


async def test_repeat_hello_idempotent() -> None:
    ch = FakeChannel()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts([])), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    _hello(driver)
    assert len([f for f in ch.sent if f.get("state") == "ready"]) == 1
    await driver.aclose()


async def test_hello_callback_fires_on_hello() -> None:
    # §17b: the STT worker's allow_feed is registered here; it must fire when
    # hello arrives (and immediately if hello already came).
    ch = FakeChannel()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts([])), "v1")
    driver.attach_channel(ch)
    fired: list[int] = []
    driver.add_hello_callback(lambda: fired.append(1))
    assert fired == []            # not yet — no hello
    _hello(driver)
    assert fired == [1]           # released on hello
    # A callback registered AFTER hello fires immediately.
    driver.add_hello_callback(lambda: fired.append(2))
    assert fired == [1, 2]
    await driver.aclose()


# ---------------------------------------------------------------------------
# Full-turn ordering + v:1 + contract
# ---------------------------------------------------------------------------


async def test_full_turn_ordering_and_versioning() -> None:
    chunks = [
        {"type": "text", "text": "Let me look. "},
        {"type": "tool", "tool": "vault_search", "iteration": 1},
        {"type": "text", "text": "Found it."},
        {"type": "final", "reply": "Let me look. Found it."},
    ]
    ch = FakeChannel()
    state = _FakeState(_active())
    rts = _scripted_rts(chunks)
    driver = VoiceTurnDriver(_deps(state, rts=rts), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("what is it")
    await _wait_for(ch, {"turn_final"})

    types = [f["type"] for f in ch.sent]
    # ready, stt_final, turn_started, turn_text, turn_tool, turn_text, turn_final
    assert types[:3] == ["state", "stt_final", "turn_started"]
    assert "turn_tool" in types
    assert types[-1] == "turn_final"
    # every frame carries v:1 and fits the size cap
    import json
    for f in ch.sent:
        assert f["v"] == EVENT_VERSION
        assert len(json.dumps(f).encode()) <= MAX_DC_EVENT_BYTES
    # turn_text seq is monotonic from 0
    seqs = [f["seq"] for f in ch.sent if f["type"] == "turn_text"]
    assert seqs == [0, 1]
    final = ch.sent[-1]
    assert final["reply"] == "Let me look. Found it."
    assert final["truncated"] is False
    assert final["ts"] == "2026-01-01T00:00:01+00:00"
    assert final["user_ts"] == "2026-01-01T00:00:00+00:00"
    # engine got channel=web, user_kind=voice
    assert rts.calls[0]["channel"] == "web"
    assert rts.calls[0]["user_kind"] == "voice"
    assert rts.calls[0]["on_event"] is None
    await driver.aclose()


async def test_exactly_one_turn_tool_per_tool_call() -> None:
    # on_event residual pin — no double-emit (contract §1.13).
    chunks = [
        {"type": "tool", "tool": "vault_search", "iteration": 1},
        {"type": "final", "reply": "done"},
    ]
    ch = FakeChannel()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts(chunks)), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("go")
    await _wait_for(ch, {"turn_final"})
    tools = [f for f in ch.sent if f["type"] == "turn_tool"]
    assert len(tools) == 1
    assert tools[0]["tool"] == "vault_search"
    await driver.aclose()


async def test_oversize_turn_final_truncated() -> None:
    big = "x" * (MAX_DC_EVENT_BYTES + 100)
    ch = FakeChannel()
    driver = VoiceTurnDriver(
        _deps(_FakeState(_active()), rts=_scripted_rts([{"type": "final", "reply": big}])),
        "v1",
    )
    driver.attach_channel(ch)
    _hello(driver)
    with structlog.testing.capture_logs() as cap:
        await driver.submit_utterance("go")
        final = await _wait_for(ch, {"turn_final"})
    assert final["reply"] == ""  # trimmed
    assert final["truncated"] is True
    assert final["reply_chars"] == len(big)
    trunc = [c for c in cap if c.get("event") == "web.voice.dc_event_truncated"]
    assert len(trunc) == 1
    await driver.aclose()


# ---------------------------------------------------------------------------
# Drop policy — channel not open mid-turn
# ---------------------------------------------------------------------------


async def test_channel_closed_midturn_completes_and_counts_drops() -> None:
    ch = FakeChannel()
    state = _FakeState(_active())
    rts = _scripted_rts([{"type": "text", "text": "hi"}, {"type": "final", "reply": "hi"}])
    driver = VoiceTurnDriver(_deps(state, rts=rts), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    ch.readyState = "closed"  # channel dies before the turn
    with structlog.testing.capture_logs() as cap:
        await driver.submit_utterance("go")
        await asyncio.sleep(0.1)  # let the turn run to completion
    # The turn RAN (engine invoked + transcript persisted) despite the dead
    # channel; nothing raised.
    assert len(rts.calls) == 1
    drops = [c for c in cap if c.get("event") == "web.voice.dc_drop"]
    assert drops  # drops counted + logged, never silent
    await driver.aclose()


# ---------------------------------------------------------------------------
# Client-frame validation (§1.3)
# ---------------------------------------------------------------------------


async def test_client_frame_validation() -> None:
    import json
    ch = FakeChannel()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts([])), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    with structlog.testing.capture_logs() as cap:
        driver.on_client_message(b"\x00\x01")  # binary
        driver.on_client_message("x" * 5000)   # oversize
        driver.on_client_message("{not json")  # malformed
        driver.on_client_message(json.dumps({"v": 1, "type": "frobnicate"}))  # unknown
        driver.on_client_message(json.dumps({"v": 1, "type": "frobnicate"}))  # again → latched
    events = {c.get("event") for c in cap}
    assert "web.voice.dc_binary_ignored" in events
    assert "web.voice.dc_client_frame_oversize" in events
    assert "web.voice.dc_malformed_client" in events
    unknown = [c for c in cap if c.get("event") == "web.voice.dc_unknown_client_type"]
    assert len(unknown) == 1  # once per type
    await driver.aclose()


async def test_client_frame_wrong_version_dropped() -> None:
    # §17b.v: v:1 strict — a missing / other version is dropped, never
    # dispatched (so a v:2 hello would NOT unlock the hello-gate).
    import json
    ch = FakeChannel()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts([])), "v1")
    driver.attach_channel(ch)
    fired: list[int] = []
    driver.add_hello_callback(lambda: fired.append(1))
    with structlog.testing.capture_logs() as cap:
        driver.on_client_message(json.dumps({"type": "hello"}))       # no v
        driver.on_client_message(json.dumps({"v": 2, "type": "hello"}))  # wrong v
    assert fired == []                                    # hello-gate NOT released
    assert ch.sent == []                                  # no ready ack
    wrong = [c for c in cap if c.get("event") == "web.voice.dc_wrong_version"]
    assert len(wrong) == 1                                # latched once
    await driver.aclose()


# ---------------------------------------------------------------------------
# Session binding re-verify (§1.2)
# ---------------------------------------------------------------------------


async def test_binding_reverify_gone_drops_turn() -> None:
    # active session's id no longer matches the bound key → no_such_session,
    # NO turn ran.
    ch = FakeChannel()
    state = _FakeState(_active(key="a-DIFFERENT-session"))
    rts = _scripted_rts([{"type": "final", "reply": "should not run"}])
    driver = VoiceTurnDriver(_deps(state, rts=rts, key=_KEY), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("go")
    err = await _wait_for(ch, {"error"})
    assert err["code"] == "no_such_session"
    assert rts.calls == []  # engine never invoked
    await driver.aclose()


async def test_binding_reverify_gone_during_slot_wait() -> None:
    # The literal W1 race: /chat/open replaces the active session WHILE the
    # driver is waiting for the KEY_WEB_INFLIGHT slot. The re-verify AFTER the
    # wait must catch it → no_such_session, engine never runs.
    ch = FakeChannel()
    in_flight = {_KEY}  # slot held → the driver waits
    state = _FakeState(_active(key=_KEY))
    rts = _scripted_rts([{"type": "final", "reply": "should not run"}])
    driver = VoiceTurnDriver(_deps(state, rts=rts, in_flight=in_flight, key=_KEY), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("go")
    await asyncio.sleep(0.1)  # driver is now blocked waiting on the slot
    assert rts.calls == []
    # A concurrent /chat/open replaced the active session mid-wait.
    state.set_active(_OWNER, _active(key="replaced-by-chat-open"))
    in_flight.discard(_KEY)  # free the slot → driver reserves, then re-verifies
    err = await _wait_for(ch, {"error"})
    assert err["code"] == "no_such_session"
    assert rts.calls == []  # the during-wait replacement was caught
    assert _KEY not in in_flight  # slot released
    await driver.aclose()


async def test_engine_error_detail_truncated_to_1024() -> None:
    # NOTE 1: a giant engine exception must NOT produce a >1024-char detail
    # (the FE zod caps at 1024 and would drop the whole error frame).
    ch = FakeChannel()

    async def boom_rts(**kw):
        raise RuntimeError("X" * 5000)
        yield  # generator

    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=boom_rts), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("go")
    err = await _wait_for(ch, {"error"})
    assert err["code"] == "engine_error"
    assert len(err["detail"]) == 1024  # truncated, not dropped
    await driver.aclose()


# ---------------------------------------------------------------------------
# In-flight guard (shared KEY_WEB_INFLIGHT)
# ---------------------------------------------------------------------------


async def test_inflight_waits_then_runs_once() -> None:
    ch = FakeChannel()
    in_flight = {_KEY}  # a concurrent /chat/turn holds the slot
    state = _FakeState(_active())
    rts = _scripted_rts([{"type": "final", "reply": "ok"}])
    driver = VoiceTurnDriver(_deps(state, rts=rts, in_flight=in_flight), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("go")
    await asyncio.sleep(0.3)
    assert rts.calls == []  # still waiting on the slot
    in_flight.discard(_KEY)  # slot freed
    await _wait_for(ch, {"turn_final"})
    assert len(rts.calls) == 1
    assert _KEY not in in_flight  # released in finally
    await driver.aclose()


async def test_inflight_timeout_drops_turn(monkeypatch) -> None:
    import alfred.web.voice_turns as vt
    monkeypatch.setattr(vt, "TURN_SLOT_WAIT_S", 0.2)
    ch = FakeChannel()
    in_flight = {_KEY}  # never freed
    rts = _scripted_rts([{"type": "final", "reply": "x"}])
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=rts, in_flight=in_flight), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("go")
    err = await _wait_for(ch, {"error"}, timeout=2.0)
    assert err["code"] == "turn_slot_timeout"
    assert rts.calls == []
    await driver.aclose()


async def test_finally_releases_inflight_on_engine_error() -> None:
    ch = FakeChannel()
    in_flight: set = set()

    async def boom_rts(**kw):
        raise RuntimeError("engine boom")
        yield  # make it a generator

    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=boom_rts, in_flight=in_flight), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("go")
    err = await _wait_for(ch, {"error"})
    assert err["code"] == "engine_error"
    assert _KEY not in in_flight  # released despite the error
    await driver.aclose()


# ---------------------------------------------------------------------------
# Latest-wins depth-1 queue
# ---------------------------------------------------------------------------


async def test_latest_wins_supersede() -> None:
    ch = FakeChannel()
    gate = asyncio.Event()

    async def slow_rts(**kw):
        session = kw["session"]
        session.transcript.append({"role": "user", "content": kw["user_message"], "_ts": "t"})
        slow_rts.calls.append(kw["user_message"])
        await gate.wait()  # hold turn A open
        yield {"type": "final", "reply": "done:" + kw["user_message"]}
        session.transcript.append({"role": "assistant", "content": "x", "_ts": "t2"})
    slow_rts.calls = []

    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=slow_rts), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("A")       # A starts, blocks on gate
    await asyncio.sleep(0.05)
    await driver.submit_utterance("B")       # queued (slot empty) — no supersede
    await driver.submit_utterance("C")       # supersedes B
    superseded = await _wait_for(ch, {"superseded"})
    assert superseded["state"] == "superseded"
    gate.set()                               # let A finish
    await asyncio.sleep(0.1)
    # A ran, then C (NOT B); A was never cancelled by new speech.
    assert slow_rts.calls[0] == "A"
    assert "C" in slow_rts.calls
    assert "B" not in slow_rts.calls
    await driver.aclose()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_client_cancel_midturn() -> None:
    import json
    ch = FakeChannel()
    in_flight: set = set()
    started = asyncio.Event()

    async def hang_rts(**kw):
        session = kw["session"]
        session.transcript.append({"role": "user", "content": kw["user_message"], "_ts": "t"})
        started.set()
        await asyncio.sleep(3600)  # hang until cancelled
        yield {"type": "final", "reply": "never"}

    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=hang_rts, in_flight=in_flight), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("go")
    await asyncio.wait_for(started.wait(), 1.0)
    driver.on_client_message(json.dumps({"v": 1, "type": "cancel"}))
    cancelled = await _wait_for(ch, {"turn_cancelled"})
    assert cancelled["state"] == "turn_cancelled"
    assert _KEY not in in_flight  # released
    # loop still alive — a subsequent utterance runs
    driver._deps.run_turn_streaming_fn  # sanity
    await driver.aclose()


async def test_external_aclose_midturn_drops_queue_and_releases() -> None:
    # Simulates manager.close() mid-turn (external task.cancel path, §1.16).
    ch = FakeChannel()
    in_flight: set = set()
    started = asyncio.Event()

    async def hang_rts(**kw):
        kw["session"].transcript.append({"role": "user", "content": "x", "_ts": "t"})
        started.set()
        await asyncio.sleep(3600)
        yield {"type": "final", "reply": "never"}

    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=hang_rts, in_flight=in_flight), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("first")
    await asyncio.wait_for(started.wait(), 1.0)
    await driver.submit_utterance("queued")  # sits in the depth-1 slot
    with structlog.testing.capture_logs() as cap:
        await asyncio.wait_for(driver.aclose(reason="daemon_shutdown"), 5.0)
    assert _KEY not in in_flight
    dropped = [c for c in cap if c.get("event") == "web.voice.queued_utterance_dropped"]
    assert len(dropped) == 1


async def test_stale_cancel_ignored() -> None:
    import json
    ch = FakeChannel()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts([])), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    with structlog.testing.capture_logs() as cap:
        driver.on_client_message(json.dumps({"v": 1, "type": "cancel", "turn_id": "nonexistent"}))
    stale = [c for c in cap if c.get("event") == "web.voice.cancel_stale"]
    assert len(stale) == 1
    await driver.aclose()


# ---------------------------------------------------------------------------
# V2 TTS driver hooks (recording stub worker; no aiortc/av)
# ---------------------------------------------------------------------------


class _StubTts:
    """Records the driver's sync/await-free calls into the TTS worker."""

    def __init__(self, max_chars: int = 4000) -> None:
        self.max_chars_per_turn = max_chars
        self.calls: list = []

    def begin_turn(self, turn_id: str) -> None:
        self.calls.append(("begin", turn_id))

    def feed_text(self, turn_id: str, text: str) -> None:
        self.calls.append(("feed", turn_id, text))

    def end_of_reply(self, turn_id: str) -> None:
        self.calls.append(("end", turn_id))

    def interrupt_speech(self, reason: str) -> None:
        self.calls.append(("interrupt", reason))


def _types(ch: FakeChannel) -> list[str]:
    return [f["type"] for f in ch.sent]


def test_tts_driver_api_is_sync() -> None:
    import inspect
    for name in ("interrupt_speech", "_utterance_while_speaking", "attach_tts",
                 "on_speaking_started", "on_speaking_done", "on_tts_fatal"):
        assert not inspect.iscoroutinefunction(getattr(VoiceTurnDriver, name))


async def test_tts_feed_order_and_end_once() -> None:
    chunks = [
        {"type": "text", "text": "Hello. "},
        {"type": "text", "text": "World."},
        {"type": "final", "reply": "Hello. World."},
    ]
    ch = FakeChannel()
    stub = _StubTts()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts(chunks)), "v1")
    driver.attach_tts(stub)
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("hi")
    await _wait_for(ch, {"turn_final"})
    assert stub.calls[0][0] == "begin"
    feeds = [c for c in stub.calls if c[0] == "feed"]
    assert [c[2] for c in feeds] == ["Hello. ", "World."]
    assert len([c for c in stub.calls if c[0] == "end"]) == 1
    await driver.aclose()


async def test_tts_no_worker_is_v1_byte_identical() -> None:
    # No attach_tts → all hooks no-op; turn_final has NO tts_chars key.
    ch = FakeChannel()
    driver = VoiceTurnDriver(
        _deps(_FakeState(_active()), rts=_scripted_rts([{"type": "final", "reply": "ok"}])), "v1")
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("hi")
    final = await _wait_for(ch, {"turn_final"})
    assert "tts_chars" not in final and "tts_capped" not in final
    await driver.aclose()


async def test_tts_turn_final_carries_spoken_delta() -> None:
    ch = FakeChannel()
    driver = VoiceTurnDriver(
        _deps(_FakeState(_active()), rts=_scripted_rts(
            [{"type": "text", "text": "spoken"}, {"type": "final", "reply": "spoken"}])), "v1")
    driver.attach_tts(_StubTts())
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("hi")
    final = await _wait_for(ch, {"turn_final"})
    assert final["tts_chars"] == len("spoken") and final["tts_capped"] is False
    await driver.aclose()


async def test_tts_char_cap_whole_sentence() -> None:
    chunks = [
        {"type": "text", "text": "x" * 10},
        {"type": "text", "text": "y" * 10},
        {"type": "text", "text": "z" * 10},   # would cross the cap → not fed
        {"type": "final", "reply": "x" * 10 + "y" * 10 + "z" * 10},
    ]
    ch = FakeChannel()
    stub = _StubTts(max_chars=20)
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts(chunks)), "v1")
    driver.attach_tts(stub)
    driver.attach_channel(ch)
    _hello(driver)
    with structlog.testing.capture_logs() as cap:
        await driver.submit_utterance("hi")
        final = await _wait_for(ch, {"turn_final"})
    feeds = [c for c in stub.calls if c[0] == "feed"]
    assert len(feeds) == 2                          # whole-sentence, never crosses
    assert len([c for c in stub.calls if c[0] == "end"]) == 1   # still flushed
    assert final["tts_chars"] == 20 and final["tts_capped"] is True
    capped = [c for c in cap if c.get("event") == "web.voice.tts.turn_capped"]
    assert len(capped) == 1
    await driver.aclose()


async def test_tts_cap_is_clean_prefix_stop() -> None:
    # QA NOTE 1: once capped, a LATER SHORT sentence must NOT slip under the cap
    # (the skipped long one wasn't counted) — otherwise spoken-prefix → gap →
    # resume. Cap is a clean prefix stop: zero further feed after the cap.
    chunks = [
        {"type": "text", "text": "x" * 15},   # fed (15 ≤ 20)
        {"type": "text", "text": "y" * 10},   # 25 > 20 → caps, not fed
        {"type": "text", "text": "z" * 3},    # 15+3=18 ≤ 20 — but capped ⇒ NOT fed
        {"type": "final", "reply": "x" * 15 + "y" * 10 + "z" * 3},
    ]
    ch = FakeChannel()
    stub = _StubTts(max_chars=20)
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts(chunks)), "v1")
    driver.attach_tts(stub)
    driver.attach_channel(ch)
    _hello(driver)
    with structlog.testing.capture_logs() as cap:
        await driver.submit_utterance("hi")
        final = await _wait_for(ch, {"turn_final"})
    feeds = [c[2] for c in stub.calls if c[0] == "feed"]
    assert feeds == ["x" * 15]                       # ONLY the prefix — no later short
    assert final["tts_capped"] is True
    assert final["tts_chars"] == 15                  # the skipped short didn't count
    capped = [c for c in cap if c.get("event") == "web.voice.tts.turn_capped"]
    assert len(capped) == 1                          # logged exactly once
    await driver.aclose()


async def test_tts_half_duplex_discards_while_speaking() -> None:
    ch = FakeChannel()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts([])), "v1")
    driver.attach_tts(_StubTts())
    driver.attach_channel(ch)
    _hello(driver)
    driver.on_speaking_started("spk")               # gate ON
    with structlog.testing.capture_logs() as cap:
        await driver.submit_utterance("heard you")
        await asyncio.sleep(0.05)
    # stt_final STILL emitted (honesty) + a distinct utterance_discarded event.
    assert "stt_final" in _types(ch)
    assert "utterance_discarded" in _types(ch)
    assert driver._pending is None                  # NOT queued
    assert "turn_started" not in _types(ch)         # no turn ran
    disc = [c for c in cap if c.get("event") == "web.voice.utterance_discarded_speaking"]
    assert len(disc) == 1
    await driver.aclose()


async def test_tts_fatal_fails_open_gate() -> None:
    ch = FakeChannel()
    driver = VoiceTurnDriver(
        _deps(_FakeState(_active()), rts=_scripted_rts([{"type": "final", "reply": "ok"}])), "v1")
    driver.attach_tts(_StubTts())
    driver.attach_channel(ch)
    _hello(driver)
    driver.on_speaking_started("spk")
    driver.on_tts_fatal(SimpleNamespace(reason="auth"))   # clears the gate
    assert driver._speaking_turn_id is None
    # Next utterance runs a turn (not discarded).
    await driver.submit_utterance("go")
    await _wait_for(ch, {"turn_final"})
    await driver.aclose()


async def test_tts_fatal_latches_once_and_stops_feeding() -> None:
    ch = FakeChannel()
    stub = _StubTts()
    driver = VoiceTurnDriver(
        _deps(_FakeState(_active()), rts=_scripted_rts(
            [{"type": "text", "text": "hi"}, {"type": "final", "reply": "hi"}])), "v1")
    driver.attach_tts(stub)
    driver.attach_channel(ch)
    _hello(driver)
    driver.on_tts_fatal(SimpleNamespace(reason="auth"))
    driver.on_tts_fatal(SimpleNamespace(reason="auth"))   # latched — no 2nd emit
    await driver.submit_utterance("go")
    await _wait_for(ch, {"turn_final"})
    errs = [f for f in ch.sent if f.get("type") == "error" and f.get("code") == "tts_unavailable"]
    assert len(errs) == 1                            # once per session
    assert not any(c[0] in ("begin", "feed", "end") for c in stub.calls)  # latched off
    await driver.aclose()


async def test_tts_new_turn_flushes_stale_audio() -> None:
    ch = FakeChannel()
    stub = _StubTts()
    driver = VoiceTurnDriver(
        _deps(_FakeState(_active()), rts=_scripted_rts([{"type": "final", "reply": "ok"}])), "v1")
    driver.attach_tts(stub)
    driver.attach_channel(ch)
    _hello(driver)
    # Simulate the real scenario: utterance B was queued during turn A's
    # LLM-thinking window (before A spoke), A then started speaking + completed
    # (inflight released) with its audio STILL draining. B runs next while
    # _speaking_turn_id is set — the half-duplex gate is bypassed because B was
    # queued pre-speech, so queue B directly (not via the now-gated submit).
    driver._speaking_turn_id = "old"                 # a prior turn still draining
    driver._pending = ("uid-b", "go")
    driver._wake.set()
    await _wait_for(ch, {"turn_final"})
    assert ("interrupt", "new_turn") in stub.calls
    # speaking_done(old) emitted BEFORE the new turn_started.
    done_idx = next(i for i, f in enumerate(ch.sent) if f.get("type") == "speaking_done")
    start_idx = next(i for i, f in enumerate(ch.sent) if f.get("type") == "turn_started")
    assert done_idx < start_idx
    await driver.aclose()


async def test_tts_client_cancel_speaking_done_before_turn_cancelled() -> None:
    import json
    ch = FakeChannel()
    started = asyncio.Event()

    async def hang_rts(**kw):
        kw["session"].transcript.append({"role": "user", "content": "x", "_ts": "t"})
        started.set()
        await asyncio.sleep(3600)
        yield {"type": "final", "reply": "never"}

    stub = _StubTts()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=hang_rts), "v1")
    driver.attach_tts(stub)
    driver.attach_channel(ch)
    _hello(driver)
    await driver.submit_utterance("go")
    await asyncio.wait_for(started.wait(), 1.0)
    ts = next(f for f in ch.sent if f["type"] == "turn_started")
    driver.on_speaking_started(ts["turn_id"])        # simulate first audio
    driver.on_client_message(json.dumps({"v": 1, "type": "cancel"}))
    await _wait_for(ch, {"turn_cancelled"})
    assert ("interrupt", "client_cancel") in stub.calls
    done_idx = next(i for i, f in enumerate(ch.sent) if f.get("type") == "speaking_done")
    canc_idx = next(i for i, f in enumerate(ch.sent) if f.get("state") == "turn_cancelled")
    assert done_idx < canc_idx                       # speaking_done → turn_cancelled
    await driver.aclose()


async def test_tts_v3_seam_is_single_dispatch_point() -> None:
    # Pin that barge-in is a body-swap at _utterance_while_speaking: monkeypatch
    # it to interrupt + queue and observe the turn runs (audio cancelled).
    ch = FakeChannel()
    stub = _StubTts()
    driver = VoiceTurnDriver(
        _deps(_FakeState(_active()), rts=_scripted_rts([{"type": "final", "reply": "barged"}])), "v1")
    driver.attach_tts(stub)
    driver.attach_channel(ch)
    _hello(driver)

    def _barge(uid, text):
        driver.interrupt_speech("barge_in")
        driver._pending = (uid, text)
        driver._wake.set()

    driver._utterance_while_speaking = _barge
    driver.on_speaking_started("spk")
    await driver.submit_utterance("interrupt me")
    await _wait_for(ch, {"turn_final"})
    assert ("interrupt", "barge_in") in stub.calls   # V3 body used the same funnel
    await driver.aclose()


# ---------------------------------------------------------------------------
# V3 barge-in — the §1.6 ratified event-ordering table (4 cases + pins)
# ---------------------------------------------------------------------------

from alfred.web.barge_in import BargeSettings   # noqa: E402


def _barge_settings(**over) -> BargeSettings:
    base = dict(
        enabled=True, too_early_ms=700, min_words=2, min_chars=6,
        echo_threshold=0.8, echo_grace_s=2.0,
        interrupt_phrases=frozenset({"stop", "salem"}),
        backchannel_phrases=frozenset({"yeah", "ok"}),
    )
    base.update(over)
    return BargeSettings(**base)


def _barge_driver(rts, clk, **sover) -> VoiceTurnDriver:
    return VoiceTurnDriver(
        _deps(_FakeState(_active()), rts=rts), "v1",
        barge=_barge_settings(**sover), clock=lambda: clk[0],
    )


def _two_phase_rts(gate: asyncio.Event):
    """T1 (first call) hangs on ``gate``; T2 (second call) completes."""
    async def rts(**kw):
        rts.calls.append(kw["user_message"])
        kw["session"].transcript.append(
            {"role": "user", "content": kw["user_message"], "_ts": "t"})
        if len(rts.calls) == 1:
            await gate.wait()
        yield {"type": "final", "reply": "done:" + kw["user_message"]}
        kw["session"].transcript.append({"role": "assistant", "content": "x", "_ts": "t2"})
    rts.calls = []
    return rts


def _types(ch: FakeChannel) -> list[str]:
    return [f.get("type") for f in ch.sent]


def _one_speaking_done(ch: FakeChannel) -> None:
    assert sum(1 for f in ch.sent if f.get("type") == "speaking_done") == 1


# --- CASE 1: POST-FINAL × CONFIRM ---
async def test_barge_case1_post_final_confirm() -> None:
    clk = [1000.0]
    ch, stub = FakeChannel(), _StubTts()
    d = _barge_driver(_scripted_rts([{"type": "final", "reply": "the answer here"}]), clk)
    d.attach_tts(stub)
    d.attach_channel(ch)
    _hello(d)
    d.on_speaking_started("t1")                 # post-final: no in-flight turn
    clk[0] = 1002.0                             # past too_early
    await d.submit_utterance("what about the quarterly budget")
    await _wait_for(ch, {"turn_final"})
    t = _types(ch)
    assert t.index("stt_final") < t.index("speaking_done") < t.index("turn_started")
    sd = next(f for f in ch.sent if f["type"] == "speaking_done")
    assert sd["reason"] == "barged_in"
    _one_speaking_done(ch)
    await d.aclose()


# --- CASE 2: PRE-FINAL × CONFIRM (+ no double speaking_done, ruling 4) ---
async def test_barge_case2_pre_final_confirm_one_speaking_done() -> None:
    clk = [1000.0]
    gate = asyncio.Event()
    ch, stub = FakeChannel(), _StubTts()
    d = _barge_driver(_two_phase_rts(gate), clk)
    d.attach_tts(stub)
    d.attach_channel(ch)
    _hello(d)
    await d.submit_utterance("first")           # T1 starts + hangs
    ts = await _wait_for(ch, {"turn_started"})
    d.on_speaking_started(ts["turn_id"])        # T1 speaking (pre-final)
    clk[0] = 1005.0
    await d.submit_utterance("what about the quarterly budget")   # barge confirm → cancels T1
    await _wait_for(ch, {"turn_final"})          # T2 completes
    t = _types(ch)
    assert t.index("stt_final") < t.index("speaking_done")
    assert "turn_cancelled" in [f.get("state") for f in ch.sent]  # T1 cancelled
    # ruling 4: the CancelledError branch's interrupt_speech is a wire no-op.
    _one_speaking_done(ch)
    gate.set()
    await d.aclose()


# --- CASE 3: POST-FINAL × VETO ---
async def test_barge_case3_veto_backchannel_no_wedge() -> None:
    clk = [1000.0]
    ch, stub = FakeChannel(), _StubTts()
    d = _barge_driver(_scripted_rts([]), clk)
    d.attach_tts(stub)
    d.attach_channel(ch)
    _hello(d)
    d.on_speaking_started("t1")
    clk[0] = 1002.0
    # Force a Stage-A latch so the veto must rescue the pill (speaking_done).
    d._barge_utt_id = None
    d._utt_id = "u1"
    d._stage_a("what about the quarterly budget")   # passes → interrupt audio + latch
    assert d._barge_utt_id == "u1"
    await d.submit_utterance("yeah")                # final = backchannel → VETO
    await asyncio.sleep(0.02)
    t = _types(ch)
    assert "stt_final" in t and "utterance_discarded" in t
    sd = [f for f in ch.sent if f.get("type") == "speaking_done"]
    assert len(sd) == 1 and sd[0]["reason"] == "barged_in"   # lifecycle rescue
    assert d._speaking_turn_id is None              # pill un-stuck (no wedge)
    await d.aclose()


async def test_barge_case3_veto_echo_silent_but_lifecycle() -> None:
    # In-window echo veto (Stage A fired): Option A — speaking_done only, no
    # stt_final / notice surfaced (ruling 2).
    clk = [1000.0]
    ch, stub = FakeChannel(), _StubTts()
    d = _barge_driver(_scripted_rts([]), clk)
    d.attach_tts(stub)
    d.attach_channel(ch)
    _hello(d)
    d._spoken_text = "the quarterly report shows revenue grew twelve percent"
    d.on_speaking_started("t1")
    clk[0] = 1002.0
    d._utt_id = "u1"
    d._barge_utt_id = "u1"                           # simulate Stage-A latch
    with structlog.testing.capture_logs() as cap:
        await d.submit_utterance(d._spoken_text)     # final = echo → VETO
        await asyncio.sleep(0.02)
    sd = [f for f in ch.sent if f.get("type") == "speaking_done"]
    assert len(sd) == 1 and sd[0]["reason"] == "barged_in"
    # No stt_final / utterance_discarded for the echo utterance.
    assert not any(f.get("type") == "utterance_discarded" for f in ch.sent)
    late = [c for c in cap if c.get("event") == "web.voice.barge.late_suppressed"]
    assert late and late[0]["reason"] == "echo"
    await d.aclose()


# --- STORM BREAKER (§1.8) ---
async def test_barge_storm_disables_session() -> None:
    clk = [1000.0]
    ch = FakeChannel()
    d = _barge_driver(_scripted_rts([{"type": "final", "reply": "ok answer here"}]), clk)
    d.attach_tts(_StubTts())
    d.attach_channel(ch)
    _hello(d)
    with structlog.testing.capture_logs() as cap:
        for _ in range(3):
            d.on_speaking_started("t")
            clk[0] += 0.8                            # past too_early (700ms), <2s
            await d.submit_utterance("what about the quarterly budget")
            await _wait_for(ch, {"turn_final"})
            clk[0] += 0.01
    assert d._barge_disabled_session is True
    assert any(c.get("event") == "web.voice.barge.storm_disabled" for c in cap)
    await d.aclose()


# --- GRACE WINDOW late echo (§1.5) ---
async def test_barge_grace_window_late_echo_suppressed() -> None:
    clk = [1000.0]
    ch = FakeChannel()
    d = _barge_driver(_scripted_rts([{"type": "final", "reply": "x"}]), clk)
    d.attach_tts(_StubTts())
    d.attach_channel(ch)
    _hello(d)
    d._spoken_text = "the quarterly report shows revenue grew twelve percent"
    d.on_speaking_started("t1")                     # a real speaking window …
    d.on_speaking_done("t1", "drained")             # … then drain opens the grace window
    with structlog.testing.capture_logs() as cap:
        await d.submit_utterance(d._spoken_text)     # not speaking, but echo in grace
        await asyncio.sleep(0.02)
    assert d._pending is None                        # not queued
    assert not any(f.get("type") == "stt_final" for f in ch.sent)   # silent
    late = [c for c in cap if c.get("event") == "web.voice.barge.late_suppressed"]
    assert late and late[0]["reason"] == "echo"
    await d.aclose()


# --- Missed-barge signal: cancel log carries last_suppressed_utt (§1.9b(c)) ---
async def test_barge_cancel_carries_last_suppressed() -> None:
    import json
    clk = [1000.0]
    ch = FakeChannel()
    d = _barge_driver(_scripted_rts([]), clk)
    d.attach_tts(_StubTts())
    d.attach_channel(ch)
    _hello(d)
    d.on_speaking_started("t1")
    clk[0] = 1002.0
    await d.submit_utterance("yeah")             # backchannel → suppressed (utt id set)
    with structlog.testing.capture_logs() as cap:
        d.on_client_message(json.dumps({"v": 1, "type": "cancel"}))
    cancels = [c for c in cap if str(c.get("event", "")).startswith("web.voice.cancel")
               or c.get("event") == "web.voice.client_cancel"]
    assert cancels and cancels[0].get("last_suppressed_utt")   # the missed utterance id
    await d.aclose()


# --- DISABLED ARM = V2 byte-identical (regression pin, §1.12) ---
async def test_barge_disabled_is_v2_discard() -> None:
    ch = FakeChannel()
    driver = VoiceTurnDriver(_deps(_FakeState(_active()), rts=_scripted_rts([])), "v1",
                             barge=_barge_settings(enabled=False))
    driver.attach_tts(_StubTts())
    driver.attach_channel(ch)
    _hello(driver)
    driver.on_speaking_started("t1")
    with structlog.testing.capture_logs() as cap:
        await driver.submit_utterance("what about the quarterly budget")
        await asyncio.sleep(0.02)
    # V2 behaviour: stt_final + utterance_discarded, NO barge events.
    assert "stt_final" in _types(ch) and "utterance_discarded" in _types(ch)
    assert not any(str(c.get("event", "")).startswith("web.voice.barge") for c in cap)
    await driver.aclose()
