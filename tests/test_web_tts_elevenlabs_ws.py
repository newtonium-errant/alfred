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
import pathlib

import aiohttp
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
                 no_final=False, connect_delay=0.0) -> None:
        self.audio = audio if audio is not None else [b"\x10\x11\x12\x13"]
        self.status = status
        self.drop_on_text = drop_on_text
        self.no_final = no_final   # send audio but never isFinal (drain-hang)
        self.connect_delay = connect_delay   # delay BEFORE prepare → client ws_connect hangs
        self.api_key: str | None = None
        self.query: dict = {}
        self.received: list = []
        self.connections = 0

    async def handler(self, request: web.Request) -> web.StreamResponse:
        if self.status is not None:
            raise web.HTTPUnauthorized() if self.status == 401 else web.HTTPBadRequest()
        if self.connect_delay:
            # Hold the 101 upgrade so the client's ws_connect stays suspended
            # (lets a test cancel the prewarm task mid-handshake).
            await asyncio.sleep(self.connect_delay)
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


async def test_request_cancel_during_prewarm_closes_session(monkeypatch) -> None:
    # sec-W4 / reg-W1: request_cancel() landing while the prewarm ws_connect is
    # SUSPENDED cancels _connect_task; CancelledError then propagates past
    # _open_turn_ws's except clauses WITHOUT closing the ClientSession (it was
    # never adopted into self._session). The try/finally must close the
    # un-adopted session — else it leaks (aiohttp "Unclosed client session").
    script = _ElevenScriptServer(connect_delay=30.0)   # ws_connect never completes
    server = await _server(script)
    created: list = []
    real_cls = aiohttp.ClientSession

    def _spy(*a, **k):
        s = real_cls(*a, **k)
        created.append(s)
        return s

    monkeypatch.setattr("alfred.web.tts_elevenlabs.aiohttp.ClientSession", _spy)
    try:
        prov = _provider(server)
        await prov.begin_turn("t1")            # prewarm starts; ws_connect suspends
        await asyncio.sleep(0.1)               # let the connect task reach ws_connect
        assert created, "prewarm never created a ClientSession"
        assert not created[0].closed           # still open, mid-handshake
        ct = prov._connect_task
        prov.request_cancel()                  # SYNC — cancels the suspended connect
        try:
            await ct                            # let the CancelledError + finally run
        except asyncio.CancelledError:
            pass
        assert created[0].closed is True       # the finally closed it — no leak
    finally:
        await prov.close()
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
    """The live-gate key — from the ENVIRONMENT only.

    D9 (operator ruling, 2026-08-04): this test is OPT-IN by export. It used to
    fall back to reading the repo's ``.env`` off disk when the variable was
    unset, which meant it never skipped: every full-suite run made a real,
    billable ElevenLabs turn.

    That fallback also went AROUND #16's whole defence. The suppression there
    neutralises ``dotenv.load_dotenv`` and the collection pin watches
    ``os.environ`` — both were still true and both were irrelevant, because this
    read the file itself and never touched ``os.environ``. Nothing could see it
    until #28's ``live_network`` inventory printed the destination.

    Environment-only, like the Groq siblings in test_web_voice_stt_shadow.py.
    Export the key to opt in; otherwise the skipif below skips.
    """
    return os.environ.get("ELEVENLABS_API_KEY", "")


# --- the gate itself (#43) --------------------------------------------------
# UNCONDITIONAL by design: these guard the opt-in gate, so they must not be
# skipped by the very gate they guard (feedback_regression_pin_unconditional).
# They use monkeypatch rather than raw os.environ writes — ELEVENLABS_API_KEY is
# in CLOUD_KEY_ENV_VARS, so a raw write would trip #28's cloud-key teardown
# check; monkeypatch reverts before that autouse teardown runs.


def test_live_key_is_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset variable → no key → the live test skips.

    Pre-fix this returned the real key read straight out of the repo's ``.env``,
    so the skipif never fired and every full-suite run spent money.
    """
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    # NEVER put `_live_key()` inside the assert. A failing assert renders its
    # operands AND every sub-expression: `assert len(_live_key()) == 0` reports
    # `where 51 = len('<the live key>')` / `where '<the live key>' = _live_key()`.
    # Comparing length is not enough on its own — the CALL has to happen outside
    # the statement, so the only thing pytest can render is a plain int.
    # It matters because this fails exactly when the regression exists, which is
    # when the output reaches CI logs and pasted tickets. Same standard this
    # file's own `test_no_key_or_text_in_logs` states. Measured both ways.
    key_len = len(_live_key())
    assert key_len == 0, "the .env fallback is back — the gate returned a key"


def test_live_key_honours_an_exported_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exporting the key is how the operator opts IN — that path still works."""
    monkeypatch.setenv("ELEVENLABS_API_KEY", "DUMMY_ELEVENLABS_TEST_KEY")
    # Same rule: a regression preferring the FILE over the environment would put
    # a real key on the left of this comparison, so the comparison happens
    # outside the assert and only its boolean result is rendered.
    honoured = _live_key() == "DUMMY_ELEVENLABS_TEST_KEY"
    assert honoured is True, "an exported ELEVENLABS_API_KEY was not honoured"


def test_live_key_touches_no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gate reads os.environ and NOTHING else.

    The structural half of the pin, and the one that holds on any machine: the
    env-only assertion above is only meaningful where a ``.env`` carrying the key
    actually exists (it does on the dev box — that is why this was live). This
    one fails on the pre-fix code anywhere, because that code called
    ``Path.exists()`` before deciding.

    The spies RECORD and delegate rather than raising. Raising inside a patched
    ``Path`` method looks tidier but fails catastrophically: pytest's own
    traceback machinery calls ``Path.exists()`` while building the failure
    report, so the sentinel fires again from inside pytest and the run dies with
    an INTERNALERROR that aborts every remaining test. Measured, restoring the
    fallback. Recording keeps the regression a single readable red test.

    Mutation: restore the ``.env`` fallback → this fails on `touched`, naming the
    path it reached for.
    """
    touched: list[str] = []
    real_exists, real_read = pathlib.Path.exists, pathlib.Path.read_text

    def _spy_exists(self: pathlib.Path, *a: object, **kw: object) -> bool:
        touched.append(f"exists({self})")
        return real_exists(self, *a, **kw)

    def _spy_read(self: pathlib.Path, *a: object, **kw: object) -> str:
        touched.append(f"read_text({self})")
        return real_read(self, *a, **kw)

    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.setattr(pathlib.Path, "exists", _spy_exists)
    monkeypatch.setattr(pathlib.Path, "read_text", _spy_read)

    # Snapshot around the call ONLY, so unrelated pytest-internal Path use
    # before or after cannot pollute the observation.
    touched.clear()
    result_len = len(_live_key())  # length only — see the env-only pin above
    observed = list(touched)

    # Filesystem assertion FIRST, deliberately: it reports PATHS, which are safe
    # to print, and under the regression it is the one that fires — so the
    # readable failure names the file that was reached rather than anything from
    # inside it.
    assert observed == [], f"_live_key reached the filesystem: {observed}"
    assert result_len == 0, "the gate returned a key with no environment variable set"


@pytest.mark.live_network
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
