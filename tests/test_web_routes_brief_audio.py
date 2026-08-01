"""GET /web/brief/audio route pins (Phase C3a — the interruptible player TTS).

Mirrors the #30 outbound-route test harness (transport app + web routes + the
production ``web`` / ``web_ingest`` peer keys). Gates:

  * PEER-PIN — a ``web_ingest`` token that clears Layer 1 is refused 401 BEFORE
    any read/synth (mirrors routes_brief).
  * Session — no valid session → 401.
  * ILB — no spooled narration → ``200 {state:"no_brief"}`` (never 404); tts key
    absent → ``200 {state:"tts_not_configured"}`` (honest disabled-audio).
  * Render — spool + tts → 200 ``audio/mpeg`` (cache-miss header), cache written.
  * CREDIT-GUARD (mutation-pinned) — a second request is a cache HIT that calls
    synthesize ZERO times (replay costs no credits).
  * Speed — the ``speed`` query param is forwarded to synthesize.

Tests run unconditionally per ``feedback_regression_pin_unconditional.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    TtsConfig,
    VaultConfig,
)
from alfred.telegram.state import StateManager
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState
from alfred.web.auth import SESSION_HEADER, make_session_token
from alfred.web.config import WebAuthConfig, WebConfig, WebUser
from alfred.web.outbound_store import write_latest
from alfred.web.routes_brief_audio import NARRATION_KIND
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse

DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_INGEST_TOKEN = "DUMMY_WEB_INGEST_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

_PEER_HEADERS = {"Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}", "X-Alfred-Client": "web"}
DATE = "2026-08-01"


def _session_headers() -> dict[str, str]:
    token = make_session_token("andrew", "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168)
    return {**_PEER_HEADERS, SESSION_HEADER: token}


def _ingest_session_headers() -> dict[str, str]:
    token = make_session_token("andrew", "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168)
    return {"Authorization": f"Bearer {DUMMY_WEB_INGEST_TOKEN}", "X-Alfred-Client": "web", SESSION_HEADER: token}


def _transport_config() -> TransportConfig:
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(tokens={
            "web": AuthTokenEntry(token=DUMMY_WEB_PEER_TOKEN, allowed_clients=["web"]),
            "web_ingest": AuthTokenEntry(token=DUMMY_WEB_INGEST_TOKEN, allowed_clients=["web"]),
        }),
        state=StateConfig(),
    )


def _talker_config(tmp_path: Path, *, with_tts: bool) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    cfg = TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(api_key="test-key", model="claude-sonnet-4-6"),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(state_path=str(tmp_path / "talker_state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", canonical="S.A.L.E.M."),
    )
    if with_tts:
        cfg.tts = TtsConfig(api_key="test-tts-key", voice_id="Rachel", model="eleven_turbo_v2_5")
    return cfg


def _build(tmp_path: Path, data_dir: str | None, *, with_tts: bool):
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    fake = FakeAnthropicClient([FakeResponse(content=[FakeBlock(type="text", text="hi")])])
    register_web_routes(
        app,
        web_config=WebConfig(
            enabled=True, users=[WebUser(name="andrew", role="owner")],
            auth=WebAuthConfig(session_secret=DUMMY_WEB_SIGNING_SECRET),
        ),
        web_auth_state=web_auth_state,
        anthropic_client=fake,
        state_mgr=state_mgr,
        talker_config=_talker_config(tmp_path, with_tts=with_tts),
        system_prompt_provider=lambda: "SYS",
        vault_context_str="CTX",
        allowed_user_ids=[1],
        data_dir=data_dir,
    )
    return app


def _spool_narration(data_dir: Path, *, text: str = "You have three things on today's plan.") -> None:
    payload = {
        "brief_date": DATE,
        "segments": [{"section_id": "day_state", "title": "State of your day", "text": text, "word_count": len(text.split())}],
        "total_words": len(text.split()),
        "empty": False,
    }
    write_latest(str(data_dir), NARRATION_KIND, DATE, json.dumps(payload))


class _SynthSpy:
    """Records synthesize calls; returns fixed bytes. The credit-guard pin
    asserts ``calls`` stays flat across a replay (cache hit → no synth)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, text, cfg, *, speed=None):
        self.calls.append({"text": text, "speed": speed})
        return b"FAKE_MP3_BYTES"


@pytest.fixture
async def client_with_tts(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    app = _build(tmp_path, str(data_dir), with_tts=True)
    app["_t_data_dir"] = str(data_dir)
    return await aiohttp_client(app)


# --- auth gates -------------------------------------------------------------


async def test_wrong_peer_rejected(aiohttp_client, tmp_path) -> None:
    data_dir = tmp_path / "data"; data_dir.mkdir()
    _spool_narration(data_dir)
    client = await aiohttp_client(_build(tmp_path, str(data_dir), with_tts=True))
    resp = await client.get("/web/brief/audio", headers=_ingest_session_headers())
    assert resp.status == 401
    assert (await resp.json())["error"] == "wrong_peer"


async def test_no_session_rejected(aiohttp_client, tmp_path) -> None:
    data_dir = tmp_path / "data"; data_dir.mkdir()
    _spool_narration(data_dir)
    client = await aiohttp_client(_build(tmp_path, str(data_dir), with_tts=True))
    resp = await client.get("/web/brief/audio", headers=_PEER_HEADERS)  # peer, no session
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


# --- ILB states -------------------------------------------------------------


async def test_no_brief_ilb(client_with_tts) -> None:
    """No spooled narration → 200 {state:no_brief} (never 404)."""
    resp = await client_with_tts.get("/web/brief/audio", headers=_session_headers())
    assert resp.status == 200
    assert (await resp.json()) == {"state": "no_brief"}


async def test_tts_not_configured_ilb(aiohttp_client, tmp_path) -> None:
    data_dir = tmp_path / "data"; data_dir.mkdir()
    _spool_narration(data_dir)
    client = await aiohttp_client(_build(tmp_path, str(data_dir), with_tts=False))
    resp = await client.get("/web/brief/audio", headers=_session_headers())
    assert resp.status == 200
    assert (await resp.json()) == {"state": "tts_not_configured"}


# --- render + credit guard --------------------------------------------------


async def test_renders_and_caches(client_with_tts, tmp_path, monkeypatch) -> None:
    _spool_narration(tmp_path / "data")
    spy = _SynthSpy()
    monkeypatch.setattr("alfred.telegram.tts.synthesize", spy)

    resp = await client_with_tts.get("/web/brief/audio", headers=_session_headers())
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "audio/mpeg"
    assert resp.headers["X-Brief-Audio-Cache"] == "miss"
    assert await resp.read() == b"FAKE_MP3_BYTES"
    assert len(spy.calls) == 1  # one synth
    # cache file written under <data_dir>/brief_audio/
    assert list((tmp_path / "data" / "brief_audio").glob("*.mp3"))


async def test_cache_hit_zero_synth_credit_guard(client_with_tts, tmp_path, monkeypatch) -> None:
    """MUTATION-PINNED credit guard: the second request is a cache HIT that
    calls synthesize ZERO additional times."""
    _spool_narration(tmp_path / "data")
    spy = _SynthSpy()
    monkeypatch.setattr("alfred.telegram.tts.synthesize", spy)

    first = await client_with_tts.get("/web/brief/audio", headers=_session_headers())
    assert first.headers["X-Brief-Audio-Cache"] == "miss"
    assert len(spy.calls) == 1

    second = await client_with_tts.get("/web/brief/audio", headers=_session_headers())
    assert second.status == 200
    assert second.headers["X-Brief-Audio-Cache"] == "hit"
    assert await second.read() == b"FAKE_MP3_BYTES"
    assert len(spy.calls) == 1  # NO second synth — the credit guard held


async def test_speed_param_forwarded(client_with_tts, tmp_path, monkeypatch) -> None:
    _spool_narration(tmp_path / "data")
    spy = _SynthSpy()
    monkeypatch.setattr("alfred.telegram.tts.synthesize", spy)

    resp = await client_with_tts.get("/web/brief/audio?speed=1.1", headers=_session_headers())
    assert resp.status == 200
    assert spy.calls[0]["speed"] == 1.1


async def test_speed_out_of_range_clamped(client_with_tts, tmp_path, monkeypatch) -> None:
    _spool_narration(tmp_path / "data")
    spy = _SynthSpy()
    monkeypatch.setattr("alfred.telegram.tts.synthesize", spy)

    resp = await client_with_tts.get("/web/brief/audio?speed=9.9", headers=_session_headers())
    assert resp.status == 200
    assert spy.calls[0]["speed"] == 1.2  # clamped to max


# --- narration JSON route (slides source) -----------------------------------


async def test_narration_json_served(client_with_tts, tmp_path) -> None:
    """The slides route returns the sectioned narration dict (no synth)."""
    _spool_narration(tmp_path / "data", text="You have three things on today's plan.")
    resp = await client_with_tts.get("/web/brief/narration", headers=_session_headers())
    assert resp.status == 200
    body = await resp.json()
    assert body["brief_date"] == DATE
    assert body["segments"][0]["section_id"] == "day_state"
    assert body["segments"][0]["text"] == "You have three things on today's plan."
    assert body["empty"] is False


async def test_narration_json_no_brief_ilb(client_with_tts) -> None:
    resp = await client_with_tts.get("/web/brief/narration", headers=_session_headers())
    assert resp.status == 200
    assert (await resp.json()) == {"state": "no_brief"}


async def test_narration_json_wrong_peer_rejected(aiohttp_client, tmp_path) -> None:
    data_dir = tmp_path / "data"; data_dir.mkdir()
    _spool_narration(data_dir)
    client = await aiohttp_client(_build(tmp_path, str(data_dir), with_tts=True))
    resp = await client.get("/web/brief/narration", headers=_ingest_session_headers())
    assert resp.status == 401
    assert (await resp.json())["error"] == "wrong_peer"
