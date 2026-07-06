"""DeepgramStreamProvider vs an in-process scripted aiohttp ws server.

UNCONDITIONAL (aiohttp is a hard dep; no aiortc/av). Exercises the real
ws_connect handshake (auth header + query params), binary audio receipt,
event normalization + speech_final/UtteranceEnd dedup, KeepAlive/Finalize/
CloseStream control frames, the 401→auth handshake classification, and
reconnect-once-then-fatal.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from alfred.web.config import WebVoiceSttConfig
from alfred.web.stt_deepgram import DeepgramStreamProvider
from alfred.web.stt_stream import (
    EVENT_ERROR,
    EVENT_FINAL,
    EVENT_PARTIAL,
    EVENT_UTTERANCE_END,
    STT_ERR_AUTH,
)


class _ScriptServer:
    """A scripted Deepgram-like ws server.

    ``per_binary`` is a list of response batches (list[dict]); the Nth binary
    frame received triggers the Nth batch. ``drop_after`` closes the socket
    abnormally after that many binary frames (reconnect test). ``status``
    forces a handshake HTTP status (e.g. 401)."""

    def __init__(self, *, per_binary=None, drop_after=None, status=None) -> None:
        self.per_binary = per_binary or []
        self.drop_after = drop_after
        self.status = status
        self.auth: str | None = None
        self.query: dict = {}
        self.controls: list[str] = []
        self.binary_count = 0
        self.connections = 0

    async def handler(self, request: web.Request) -> web.StreamResponse:
        if self.status is not None:
            raise web.HTTPUnauthorized() if self.status == 401 else web.HTTPBadRequest()
        self.connections += 1
        self.auth = request.headers.get("Authorization")
        self.query = dict(request.query)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        local_bin = 0
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                self.binary_count += 1
                local_bin += 1
                idx = self.binary_count - 1
                if idx < len(self.per_binary):
                    for out in self.per_binary[idx]:
                        await ws.send_str(json.dumps(out))
                if self.drop_after is not None and local_bin >= self.drop_after:
                    await ws.close(code=1011)  # abnormal → provider reconnects
                    return ws
            elif msg.type == web.WSMsgType.TEXT:
                self.controls.append(msg.data)
                if "CloseStream" in msg.data:
                    await ws.send_str(json.dumps({"type": "Metadata"}))
                    await ws.close()
                    return ws
        return ws


async def _server(script: _ScriptServer) -> TestServer:
    app = web.Application()
    app.router.add_get("/v1/listen", script.handler)
    server = TestServer(app)
    await server.start_server()
    return server


def _cfg() -> WebVoiceSttConfig:
    return WebVoiceSttConfig(provider="deepgram", api_key="test-dg-key")


def _provider(script_server: _ScriptServer, server: TestServer, **kw) -> DeepgramStreamProvider:
    base = f"http://127.0.0.1:{server.port}"
    return DeepgramStreamProvider(_cfg(), base_url=base, **kw)


async def _collect(provider: DeepgramStreamProvider) -> list:
    got = []

    async def drain():
        async for ev in provider.events():
            got.append(ev)

    task = asyncio.ensure_future(drain())
    return got, task


# ---------------------------------------------------------------------------
# Handshake: auth header + query params
# ---------------------------------------------------------------------------


async def test_auth_header_and_params_sent() -> None:
    script = _ScriptServer()
    server = await _server(script)
    try:
        prov = _provider(script, server)
        await prov.connect()
        await prov.feed(b"\x00" * 3200)
        await asyncio.sleep(0.05)
        await prov.close()
        assert script.auth == "Token test-dg-key"
        assert script.query.get("interim_results") == "true"
        assert script.query.get("model") == "nova-3"
        assert script.query.get("encoding") == "linear16"
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Event normalization + dedup
# ---------------------------------------------------------------------------


async def test_results_normalize_to_events() -> None:
    script = _ScriptServer(per_binary=[
        [{"type": "Results", "is_final": False,
          "channel": {"alternatives": [{"transcript": "hel"}]}}],
        [{"type": "Results", "is_final": True, "speech_final": True,
          "channel": {"alternatives": [{"transcript": "hello there"}]}}],
    ])
    server = await _server(script)
    try:
        prov = _provider(script, server)
        got, task = await _collect(prov)
        await prov.connect()
        await prov.feed(b"\x00" * 3200)
        await prov.feed(b"\x11" * 3200)
        await asyncio.sleep(0.1)
        await prov.close()
        await task
        types = [e.type for e in got]
        assert EVENT_PARTIAL in types
        assert EVENT_FINAL in types
        assert EVENT_UTTERANCE_END in types
        eou = [e for e in got if e.type == EVENT_UTTERANCE_END]
        assert eou[0].trigger == "speech_final"
    finally:
        await server.close()


async def test_speech_final_dedups_utterance_end_fallback() -> None:
    # A speech_final closes the utterance; a following UtteranceEnd message is
    # deduped (the provider suppresses it — finals_since_eou already reset).
    script = _ScriptServer(per_binary=[
        [{"type": "Results", "is_final": True, "speech_final": True,
          "channel": {"alternatives": [{"transcript": "hi"}]}},
         {"type": "UtteranceEnd"}],
    ])
    server = await _server(script)
    try:
        prov = _provider(script, server)
        got, task = await _collect(prov)
        await prov.connect()
        await prov.feed(b"\x00" * 3200)
        await asyncio.sleep(0.1)
        await prov.close()
        await task
        eou = [e for e in got if e.type == EVENT_UTTERANCE_END]
        assert len(eou) == 1  # the fallback was deduped
        assert eou[0].trigger == "speech_final"
    finally:
        await server.close()


async def test_utterance_end_fallback_fires_without_speech_final() -> None:
    script = _ScriptServer(per_binary=[
        [{"type": "Results", "is_final": True,
          "channel": {"alternatives": [{"transcript": "hi"}]}},
         {"type": "UtteranceEnd"}],
    ])
    server = await _server(script)
    try:
        prov = _provider(script, server)
        got, task = await _collect(prov)
        await prov.connect()
        await prov.feed(b"\x00" * 3200)
        await asyncio.sleep(0.1)
        await prov.close()
        await task
        eou = [e for e in got if e.type == EVENT_UTTERANCE_END]
        assert len(eou) == 1
        assert eou[0].trigger == "utterance_end_fallback"
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Close drain (CloseStream + Metadata)
# ---------------------------------------------------------------------------


async def test_close_sends_closestream() -> None:
    script = _ScriptServer()
    server = await _server(script)
    try:
        prov = _provider(script, server)
        await prov.connect()
        await prov.feed(b"\x00" * 3200)
        await asyncio.sleep(0.05)
        await prov.finalize()
        await prov.close()
        await asyncio.sleep(0.05)
        assert any("Finalize" in c for c in script.controls)
        assert any("CloseStream" in c for c in script.controls)
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Handshake error classification
# ---------------------------------------------------------------------------


async def test_401_handshake_is_auth_error() -> None:
    from alfred.web.stt_deepgram import _HandshakeFailed
    script = _ScriptServer(status=401)
    server = await _server(script)
    try:
        prov = _provider(script, server)
        with pytest.raises(_HandshakeFailed) as exc:
            await prov.connect()
        assert exc.value.reason == STT_ERR_AUTH
        assert exc.value.status == 401
    finally:
        await server.close()


# ---------------------------------------------------------------------------
# Reconnect-once then fatal
# ---------------------------------------------------------------------------


async def test_reconnect_once_resumes() -> None:
    # Server drops the ws after the 1st binary; provider reconnects (rearm
    # window generous) and the 2nd connection delivers events.
    script = _ScriptServer(
        per_binary=[
            [],  # 1st frame: nothing, then drop
            [{"type": "Results", "is_final": True, "speech_final": True,
              "channel": {"alternatives": [{"transcript": "after reconnect"}]}}],
        ],
        drop_after=1,
    )
    server = await _server(script)
    try:
        prov = _provider(script, server, reconnect_rearm_s=0.0)
        got, task = await _collect(prov)
        await prov.connect()
        await prov.feed(b"\x00" * 3200)   # triggers drop → reconnect
        await asyncio.sleep(0.2)
        await prov.feed(b"\x11" * 3200)   # on the reconnected ws
        await asyncio.sleep(0.15)
        await prov.close()
        await task
        assert script.connections >= 2   # reconnected
        finals = [e for e in got if e.type == EVENT_FINAL]
        assert any(e.text == "after reconnect" for e in finals)
    finally:
        await server.close()


async def test_reconnect_budget_exhausted_is_fatal() -> None:
    # Two drops within the re-arm window (rearm large) → budget exhausted →
    # fatal error event.
    script = _ScriptServer(per_binary=[[], []], drop_after=1)
    server = await _server(script)
    try:
        prov = _provider(script, server, reconnect_rearm_s=9999.0)
        got, task = await _collect(prov)
        await prov.connect()
        await prov.feed(b"\x00" * 3200)   # drop 1 → reconnect (budget→0)
        await asyncio.sleep(0.2)
        await prov.feed(b"\x11" * 3200)   # drop 2 → no budget → fatal
        await asyncio.sleep(0.2)
        await task  # ends after the fatal error + sentinel
        fatal = [e for e in got if e.type == EVENT_ERROR and e.fatal]
        assert len(fatal) == 1
        await prov.close()
    finally:
        await server.close()
