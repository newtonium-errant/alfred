"""Tests for ``alfred.web.tts_elevenlabs`` — pure helpers + scripted ws server.

PURE (unconditional): build_elevenlabs_url / parse_elevenlabs_message.
WS (aiohttp is a hard dep): a scripted in-process ElevenLabs-like ws server
exercises the per-turn connection — happy turn, 401→fatal AUTH, mid-turn drop→
non-fatal, keepalive, cancel-closes-without-drain, close idempotent, and the
key/text hygiene assertion (no api key / fed text in captured logs).
LIVE (skipif no ELEVENLABS_API_KEY): one short real turn (<$0.05) — the only
test catching tier / voice / format rejections.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path

import pytest
import structlog
from aiohttp import web
from aiohttp.test_utils import TestServer

from alfred.web.config import WebVoiceTtsConfig
from alfred.web.tts_elevenlabs import (
    ElevenLabsStreamProvider,
    _HandshakeFailed,
    build_elevenlabs_url,
    parse_elevenlabs_message,
)
from alfred.web.tts_stream import (
    EVENT_AUDIO,
    EVENT_ERROR,
    EVENT_TURN_DONE,
    TTS_ERR_AUTH,
    TTS_ERR_NETWORK,
)


# ---------------------------------------------------------------------------
# Pure: URL builder
# ---------------------------------------------------------------------------


def test_url_has_params_and_no_key() -> None:
    url = build_elevenlabs_url(
        voice_id="VID", model_id="eleven_flash_v2_5", output_format="pcm_24000",
        auto_mode=True, inactivity_timeout_s=180, zero_retention=False,
    )
    assert url.startswith("wss://api.elevenlabs.io/v1/text-to-speech/VID/stream-input?")
    assert "model_id=eleven_flash_v2_5" in url
    assert "output_format=pcm_24000" in url
    assert "auto_mode=true" in url
    assert "inactivity_timeout=180" in url
    assert "enable_logging" not in url            # zero_retention off
    assert "api_key" not in url and "xi-api-key" not in url


def test_url_zero_retention_adds_enable_logging_false() -> None:
    url = build_elevenlabs_url(
        voice_id="v", model_id="m", output_format="pcm_24000",
        auto_mode=False, inactivity_timeout_s=20, zero_retention=True,
    )
    assert "enable_logging=false" in url
    assert "auto_mode=false" in url


# ---------------------------------------------------------------------------
# Pure: message parser
# ---------------------------------------------------------------------------


def test_parse_audio_base64() -> None:
    raw = json.dumps({"audio": base64.b64encode(b"\x01\x02\x03").decode()})
    m = parse_elevenlabs_message(raw)
    assert m.pcm == b"\x01\x02\x03" and m.is_final is False and m.error == ""


def test_parse_is_final() -> None:
    m = parse_elevenlabs_message(json.dumps({"isFinal": True}))
    assert m.is_final is True and m.pcm == b""


def test_parse_error_captured() -> None:
    m = parse_elevenlabs_message(json.dumps({"error": "quota_exceeded", "code": 1008}))
    assert m.error == "quota_exceeded" and m.code == 1008


def test_parse_ignores_alignment_and_unknown() -> None:
    raw = json.dumps({
        "audio": base64.b64encode(b"\x00\x00").decode(),
        "normalizedAlignment": {"chars": ["a"]}, "future_key": 1,
    })
    m = parse_elevenlabs_message(raw)
    assert m.pcm == b"\x00\x00"


def test_parse_malformed_is_empty() -> None:
    assert parse_elevenlabs_message("{not json").pcm == b""
    assert parse_elevenlabs_message("[]").error == ""


# ---------------------------------------------------------------------------
# Scripted ws server
# ---------------------------------------------------------------------------


class _ElevenScriptServer:
    def __init__(self, *, audio=None, status=None, drop_on_text=False,
                 no_final=False) -> None:
        self.audio = audio if audio is not None else [b"\x10\x11\x12\x13"]
        self.status = status
        self.drop_on_text = drop_on_text
        self.no_final = no_final   # send audio but never isFinal (drain-hang)
        self.api_key: str | None = None
        self.query: dict = {}
        self.received: list = []
        self.connections = 0

    async def handler(self, request: web.Request) -> web.StreamResponse:
        if self.status is not None:
            raise web.HTTPUnauthorized() if self.status == 401 else web.HTTPBadRequest()
        self.connections += 1
        self.api_key = request.headers.get("xi-api-key")
        self.query = dict(request.query)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            self.received.append(data)
            text = data.get("text")
            if self.drop_on_text and text not in (None, "", " "):
                await ws.close(code=1011)       # abnormal mid-turn drop
                return ws
            if text == "":                       # CloseConnection / flush
                for chunk in self.audio:
                    await ws.send_str(json.dumps({
                        "audio": base64.b64encode(chunk).decode(),
                    }))
                if self.no_final:
                    await asyncio.sleep(60)      # hang — the drain must be interrupted
                    return ws
                await ws.send_str(json.dumps({"isFinal": True}))
                await ws.close()
                return ws
        return ws


async def _server(script: _ElevenScriptServer) -> TestServer:
    app = web.Application()
    app.router.add_get("/v1/text-to-speech/{voice_id}/stream-input", script.handler)
    server = TestServer(app)
    await server.start_server()
    return server


def _cfg() -> WebVoiceTtsConfig:
    return WebVoiceTtsConfig(provider="elevenlabs", api_key="test-el-key", voice="Rachel")


def _provider(server: TestServer, **kw) -> ElevenLabsStreamProvider:
    return ElevenLabsStreamProvider(
        _cfg(), base_url=f"http://127.0.0.1:{server.port}", **kw,
    )


async def _collect(prov: ElevenLabsStreamProvider):
    got: list = []

    async def drain():
        async for ev in prov.events():
            got.append(ev)

    return got, asyncio.ensure_future(drain())


async def test_happy_turn_audio_then_done() -> None:
    script = _ElevenScriptServer(audio=[b"\x01\x02", b"\x03\x04"])
    server = await _server(script)
    try:
        prov = _provider(server)
        got, task = await _collect(prov)
        await prov.begin_turn("t1")
        await prov.feed_text("Hello there.")
        await prov.end_of_reply()
        await asyncio.sleep(0.1)
        await prov.close()
        await task
        assert script.api_key == "test-el-key"    # header auth, not URL
        types = [e.type for e in got]
        assert types.count(EVENT_AUDIO) == 2
        assert types[-1] == EVENT_TURN_DONE
        # Initialize carried voice_settings; a text chunk was sent.
        assert any("voice_settings" in m for m in script.received)
        assert any(m.get("text") == "Hello there. " for m in script.received)
    finally:
        await server.close()


async def test_401_handshake_is_fatal_auth() -> None:
    script = _ElevenScriptServer(status=401)
    server = await _server(script)
    try:
        prov = _provider(server)
        got, task = await _collect(prov)
        await prov.begin_turn("t1")
        await prov.feed_text("hi")             # awaits the failed connect
        await asyncio.sleep(0.05)
        await task                              # events() ends after fatal
        assert len(got) == 1
        assert got[0].type == EVENT_ERROR and got[0].fatal is True
        assert got[0].reason == TTS_ERR_AUTH
    finally:
        await server.close()


async def test_midturn_drop_is_transient() -> None:
    script = _ElevenScriptServer(drop_on_text=True)
    server = await _server(script)
    try:
        prov = _provider(server)
        got, task = await _collect(prov)
        await prov.begin_turn("t1")
        await prov.feed_text("hi")             # server drops after this
        await asyncio.sleep(0.1)
        await prov.close()
        await task
        errs = [e for e in got if e.type == EVENT_ERROR]
        assert len(errs) == 1
        assert errs[0].reason == TTS_ERR_NETWORK and errs[0].fatal is False
    finally:
        await server.close()


async def test_keepalive_space_after_silence() -> None:
    script = _ElevenScriptServer()
    server = await _server(script)
    try:
        prov = _provider(server, keepalive_interval_s=0.02)
        got, task = await _collect(prov)
        await prov.begin_turn("t1")
        await prov.feed_text("hi")
        await asyncio.sleep(0.2)                # idle → keepalive spaces fire
        await prov.cancel_turn()
        await prov.close()
        await task
        spaces = [m for m in script.received if m.get("text") == " "]
        assert len(spaces) >= 2                 # Initialize + ≥1 keepalive
    finally:
        await server.close()


async def test_cancel_closes_without_turn_done() -> None:
    script = _ElevenScriptServer()
    server = await _server(script)
    try:
        prov = _provider(server)
        got, task = await _collect(prov)
        await prov.begin_turn("t1")
        await prov.feed_text("hi")
        await prov.cancel_turn()               # abort — no flush, no drain
        await asyncio.sleep(0.05)
        await prov.close()
        await task
        assert not any(e.type == EVENT_TURN_DONE for e in got)
    finally:
        await server.close()


async def test_close_idempotent() -> None:
    script = _ElevenScriptServer()
    server = await _server(script)
    try:
        prov = _provider(server)
        await prov.begin_turn("t1")
        await prov.close()
        await prov.close()                      # no raise
    finally:
        await server.close()


async def test_request_cancel_safe_before_any_turn() -> None:
    # reg-W1: request_cancel must be a clean no-op before any begin_turn
    # (the interrupt event is created at construction, not in begin_turn).
    prov = ElevenLabsStreamProvider(_cfg())
    prov.request_cancel()          # must NOT raise AttributeError
    prov.request_cancel()          # idempotent
    await prov.close()


async def test_request_cancel_breaks_drain_no_timeout_error() -> None:
    # §1.2 / D2-13a: request_cancel during end_of_reply's drain breaks it AT ONCE
    # (not after the 30 s bound) and emits NO drain_timeout error (protects the
    # 3-strike fatal latch).
    script = _ElevenScriptServer(audio=[b"\x01\x02"], no_final=True)
    server = await _server(script)
    try:
        prov = _provider(server)
        got, task = await _collect(prov)
        await prov.begin_turn("t1")
        await prov.feed_text("hi")
        eor = asyncio.ensure_future(prov.end_of_reply())   # would drain 30 s
        await asyncio.sleep(0.1)                            # audio arrives, drain starts
        prov.request_cancel()                              # SYNC — breaks the drain
        await asyncio.wait_for(eor, timeout=3.0)           # completes promptly, not 30 s
        await prov.close()
        await task
        errs = [e for e in got if e.type == EVENT_ERROR]
        assert errs == []                                  # no drain_timeout error
    finally:
        await server.close()


async def test_no_key_or_text_in_logs() -> None:
    script = _ElevenScriptServer(status=401)
    server = await _server(script)
    try:
        prov = _provider(server)
        with structlog.testing.capture_logs() as cap:
            got, task = await _collect(prov)
            await prov.begin_turn("t1")
            await prov.feed_text("secret reply text")
            await asyncio.sleep(0.05)
            await task
        blob = json.dumps(cap)
        assert "test-el-key" not in blob
        assert "secret reply text" not in blob
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# LIVE — real ElevenLabs, one short turn (skipif no key). Catches tier/voice/
# format rejections the scripted server can't (contract §2 / §3 gate).
# ---------------------------------------------------------------------------


def _live_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if key:
        return key
    env_path = Path("/home/andrew/alfred/.env")
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("ELEVENLABS_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


@pytest.mark.skipif(not _live_key(), reason="no ELEVENLABS_API_KEY (dev-only live gate)")
async def test_live_one_turn() -> None:
    cfg = WebVoiceTtsConfig(
        provider="elevenlabs", api_key=_live_key(), model="eleven_flash_v2_5",
        voice="Rachel", output_format="pcm_24000",
    )
    prov = ElevenLabsStreamProvider(cfg, voice_session_id="live")
    got: list = []

    async def drain():
        async for ev in prov.events():
            got.append(ev)

    task = asyncio.ensure_future(drain())
    try:
        await prov.begin_turn("live1")
        await prov.feed_text("Testing one two three.")
        await prov.end_of_reply()
        await asyncio.wait_for(_wait_done(got), timeout=30)
    finally:
        await prov.close()
        await task
    audio = [e for e in got if e.type == EVENT_AUDIO]
    fatal = [e for e in got if e.type == EVENT_ERROR and e.fatal]
    assert not fatal, f"live TTS rejected: {[(e.reason, e.detail) for e in fatal]}"
    assert audio, "no audio from live ElevenLabs"
    assert sum(len(e.pcm) for e in audio) > 1000   # real PCM, not a stub
    assert any(e.type == EVENT_TURN_DONE for e in got)


async def _wait_done(got: list) -> None:
    while not any(e.type in (EVENT_TURN_DONE, EVENT_ERROR) and getattr(e, "fatal", True)
                  for e in got):
        if any(e.type == EVENT_TURN_DONE for e in got):
            return
        await asyncio.sleep(0.05)
