"""Tests for ``alfred.web.routes_voice`` — V0 WebRTC voice routes.

UNCONDITIONAL (no aiortc — these are the regression pins per
feedback_regression_pin_unconditional). The fixture builds the REAL transport
app (``build_app`` → real ``auth_middleware``) with BOTH a ``web`` and a
``web_ingest`` peer token sharing ``allowed_clients: [web]`` — so the
escalation pin (a ``web_ingest`` token must NOT drive voice) is exercised
against the production peer NAME. aiortc is never imported: the manager is
either monkeypatched to a fake, or the aiortc-missing 503 path is forced via
a monkeypatched ``aiortc_available``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog

from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
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
from alfred.web import routes_voice
from alfred.web.auth import SESSION_HEADER, make_session_token
from alfred.web.config import (
    VoiceIceConfig,
    WebAuthConfig,
    WebConfig,
    WebUser,
    WebVoiceConfig,
)
from alfred.web.keys import KEY_WEB_VOICE_MANAGER
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import FakeAnthropicClient

# Obviously-fake test secrets — never a real provider prefix.
DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_INGEST_TOKEN = "DUMMY_WEB_INGEST_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
    "X-Alfred-Client": "web",
}
# The web_ingest token also carries allowed_clients [web] — it clears Layer 1
# but resolves transport_peer = "web_ingest", which the voice peer-pin refuses.
_INGEST_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_INGEST_TOKEN}",
    "X-Alfred-Client": "web",
}


def _session(name: str = "andrew") -> str:
    return make_session_token(
        name, "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168,
    )


def _headers(name: str = "andrew", *, ingest: bool = False, json: bool = True):
    base = dict(_INGEST_PEER_HEADERS if ingest else _PEER_HEADERS)
    base[SESSION_HEADER] = _session(name)
    if json:
        base["Content-Type"] = "application/json"
    return base


def _make_talker_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    for sub in ("session", "note"):
        (vault_dir / sub).mkdir()
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(api_key="test-key", model="claude-sonnet-4-6"),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800,
            state_path=str(tmp_path / "talker_state.json"),
        ),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", canonical="S.A.L.E.M."),
    )


def _transport_config() -> TransportConfig:
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                "web": AuthTokenEntry(
                    token=DUMMY_WEB_PEER_TOKEN, allowed_clients=["web"],
                ),
                "web_ingest": AuthTokenEntry(
                    token=DUMMY_WEB_INGEST_TOKEN, allowed_clients=["web"],
                ),
            }
        ),
        state=StateConfig(),
    )


def _voice_config(**overrides) -> WebVoiceConfig:
    base = dict(enabled=True, max_sessions=2, pipeline="echo", ice=VoiceIceConfig())
    base.update(overrides)
    return WebVoiceConfig(**base)


def _web_config(
    *, voice: WebVoiceConfig | None = None, mode: str = "session",
) -> WebConfig:
    return WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(
            session_secret="" if mode == "relay" else DUMMY_WEB_SIGNING_SECRET,
            mode=mode,
        ),
        voice=voice if voice is not None else _voice_config(),
    )


def _build_app(tmp_path: Path, web_config: WebConfig):
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    register_web_routes(
        app,
        web_config=web_config,
        web_auth_state=web_auth_state,
        anthropic_client=FakeAnthropicClient([]),
        state_mgr=state_mgr,
        talker_config=_make_talker_config(tmp_path),
        system_prompt_provider=lambda: "SYS",
        vault_context_str="CTX",
        allowed_user_ids=[1],
    )
    return app


def _route_paths(app) -> list[str]:
    return [
        r.resource.canonical
        for r in app.router.routes()
        if r.resource is not None
    ]


# ---------------------------------------------------------------------------
# Fake manager (injected in place of the real aiortc-backed VoiceSessionManager)
# ---------------------------------------------------------------------------


class _FakeSession:
    def __init__(self, vid: str, state: str = "connected") -> None:
        self.voice_session_id = vid
        self.connection_state = state


class _FakeManager:
    def __init__(self, voice_config) -> None:
        self._config = voice_config
        self.open_result = ("a" * 32, "v=0\r\nANSWER\r\n")
        self.open_exc: Exception | None = None
        self.close_result = True
        self._yours: list[_FakeSession] = []

    async def open_session(self, identity, sdp):
        if self.open_exc is not None:
            raise self.open_exc
        return self.open_result

    async def close_owned(self, vid, owner, reason):
        return self.close_result

    def sessions_for(self, owner):
        return list(self._yours)

    def age_seconds(self, session) -> float:
        return 12.0

    async def close_all(self, reason) -> None:  # on_shutdown drain (teardown)
        return None

    async def aclose(self) -> None:  # on_shutdown drain (teardown)
        return None

    def stop_reaper(self) -> None:
        return None


def _patch_available(monkeypatch, manager: _FakeManager | None) -> None:
    """Force the aiortc-available mount path + inject a fake manager."""
    monkeypatch.setattr(routes_voice, "aiortc_available", lambda: (True, ""))
    monkeypatch.setattr(
        routes_voice, "VoiceSessionManager", lambda voice: manager,
    )


# ---------------------------------------------------------------------------
# Mount gating
# ---------------------------------------------------------------------------


def test_voice_not_mounted_when_disabled(tmp_path) -> None:
    app = _build_app(tmp_path, _web_config(voice=_voice_config(enabled=False)))
    paths = _route_paths(app)
    assert not any(p.startswith("/voice") for p in paths)
    # Byte-identical: the rest of the web surface is untouched.
    assert "/chat/turn" in paths
    assert "/stt/transcribe" in paths


def test_voice_not_mounted_when_block_absent(tmp_path) -> None:
    # Default WebConfig() has an all-default (disabled) voice block.
    cfg = WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(session_secret=DUMMY_WEB_SIGNING_SECRET),
    )
    app = _build_app(tmp_path, cfg)
    assert not any(p.startswith("/voice") for p in _route_paths(app))


def test_voice_not_mounted_in_relay_mode(tmp_path) -> None:
    with structlog.testing.capture_logs() as cap:
        app = _build_app(tmp_path, _web_config(mode="relay"))
    assert not any(p.startswith("/voice") for p in _route_paths(app))
    disabled = [
        c for c in cap
        if c.get("event") == "web.voice.disabled" and c.get("reason") == "relay_mode"
    ]
    assert len(disabled) == 1


def test_voice_not_mounted_unknown_pipeline(tmp_path) -> None:
    with structlog.testing.capture_logs() as cap:
        app = _build_app(
            tmp_path, _web_config(voice=_voice_config(pipeline="assistant")),
        )
    assert not any(p.startswith("/voice") for p in _route_paths(app))
    disabled = [
        c for c in cap
        if c.get("event") == "web.voice.disabled"
        and c.get("reason") == "unknown_pipeline"
    ]
    assert len(disabled) == 1


def test_voice_mounted_when_enabled(tmp_path, monkeypatch) -> None:
    _patch_available(monkeypatch, _FakeManager(_voice_config()))
    app = _build_app(tmp_path, _web_config())
    paths = _route_paths(app)
    assert "/voice/offer" in paths
    assert "/voice/close" in paths
    assert "/voice/config" in paths


def test_ice_option_unapplied_logged(tmp_path, monkeypatch) -> None:
    _patch_available(monkeypatch, _FakeManager(_voice_config()))
    with structlog.testing.capture_logs() as cap:
        _build_app(
            tmp_path,
            _web_config(voice=_voice_config(ice=VoiceIceConfig(udp_port_range="1-2"))),
        )
    unapplied = [
        c for c in cap if c.get("event") == "web.voice.ice_option_unapplied"
    ]
    assert len(unapplied) == 1
    assert unapplied[0]["option"] == "udp_port_range"


# ---------------------------------------------------------------------------
# aiortc-missing 503 mode
# ---------------------------------------------------------------------------


@pytest.fixture
def unavailable_client(aiohttp_client, tmp_path, monkeypatch):
    monkeypatch.setattr(
        routes_voice, "aiortc_available", lambda: (False, "aiortc_missing"),
    )
    with structlog.testing.capture_logs() as cap:
        app = _build_app(tmp_path, _web_config())
    assert any(c.get("event") == "web.voice.unavailable" for c in cap)
    assert app[KEY_WEB_VOICE_MANAGER] is None
    return aiohttp_client(app)


async def test_offer_503_when_aiortc_missing(unavailable_client) -> None:
    client = await unavailable_client
    resp = await client.post(
        "/voice/offer", headers=_headers(),
        json={"sdp": "v=0\r\n", "type": "offer"},
    )
    assert resp.status == 503
    body = await resp.json()
    assert body["error"] == "voice_unavailable"
    assert body["reason"] == "aiortc_missing"


async def test_config_available_false_when_aiortc_missing(unavailable_client) -> None:
    client = await unavailable_client
    resp = await client.get("/voice/config", headers=_headers())
    assert resp.status == 200
    body = await resp.json()
    assert body["available"] is False
    assert body["reason"] == "aiortc_missing"
    assert body["yours"] == []


async def test_close_not_found_when_aiortc_missing(unavailable_client) -> None:
    client = await unavailable_client
    resp = await client.post(
        "/voice/close", headers=_headers(),
        json={"voice_session_id": "abc"},
    )
    assert resp.status == 200
    assert await resp.json() == {"closed": False, "reason": "not_found"}


# ---------------------------------------------------------------------------
# Auth gates (peer-pin escalation + session)
# ---------------------------------------------------------------------------


@pytest.fixture
async def voice_client(aiohttp_client, tmp_path, monkeypatch):
    """A voice-enabled app with a fake manager injected."""
    manager = _FakeManager(_voice_config())
    _patch_available(monkeypatch, manager)
    app = _build_app(tmp_path, _web_config())
    # The fake is already stashed at KEY_WEB_VOICE_MANAGER (register ran the
    # patched VoiceSessionManager pre-start); tests reach it there and tweak
    # its behavior. No post-start app mutation (which aiohttp deprecates).
    return await aiohttp_client(app)


async def test_offer_401_without_session(voice_client) -> None:
    resp = await voice_client.post(
        "/voice/offer", headers={**_PEER_HEADERS, "Content-Type": "application/json"},
        json={"sdp": "v=0\r\n", "type": "offer"},
    )
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


async def test_offer_401_wrong_peer_ingest_token(voice_client) -> None:
    """REGRESSION PIN — a web_ingest token (shares allowed_clients [web])
    clears Layer 1 as transport_peer=web_ingest but the voice peer-pin refuses
    it. Without the pin this would drive a full voice session (escalation)."""
    with structlog.testing.capture_logs() as cap:
        resp = await voice_client.post(
            "/voice/offer", headers=_headers(ingest=True),
            json={"sdp": "v=0\r\n", "type": "offer"},
        )
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"
    wrong = [c for c in cap if c.get("event") == "web.voice.wrong_peer"]
    assert len(wrong) == 1
    assert wrong[0]["peer"] == "web_ingest"
    assert wrong[0]["expected"] == "web"


async def test_config_401_wrong_peer(voice_client) -> None:
    resp = await voice_client.get("/voice/config", headers=_headers(ingest=True))
    assert resp.status == 401


async def test_close_401_wrong_peer(voice_client) -> None:
    resp = await voice_client.post(
        "/voice/close", headers=_headers(ingest=True),
        json={"voice_session_id": "x"},
    )
    assert resp.status == 401


# ---------------------------------------------------------------------------
# Offer validation matrix
# ---------------------------------------------------------------------------


async def test_offer_415_wrong_content_type(voice_client) -> None:
    resp = await voice_client.post(
        "/voice/offer",
        headers={**_PEER_HEADERS, SESSION_HEADER: _session(),
                 "Content-Type": "text/plain"},
        data="not json",
    )
    assert resp.status == 415
    assert (await resp.json())["error"] == "unsupported_media_type"


async def test_offer_400_bad_json(voice_client) -> None:
    resp = await voice_client.post(
        "/voice/offer", headers=_headers(), data="{not valid json",
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "bad_json"


async def test_offer_400_sdp_required(voice_client) -> None:
    resp = await voice_client.post(
        "/voice/offer", headers=_headers(), json={"type": "offer"},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "sdp_required"


async def test_offer_400_invalid_sdp_type(voice_client) -> None:
    resp = await voice_client.post(
        "/voice/offer", headers=_headers(),
        json={"sdp": "v=0\r\n", "type": "answer"},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_sdp_type"


async def test_offer_413_sdp_too_large(voice_client) -> None:
    big = "v=0\r\n" + ("a" * (128 * 1024 + 1))
    resp = await voice_client.post(
        "/voice/offer", headers=_headers(), json={"sdp": big, "type": "offer"},
    )
    assert resp.status == 413
    body = await resp.json()
    assert body["error"] == "sdp_too_large"
    assert body["max_bytes"] == 131072


async def test_offer_429_at_cap(voice_client) -> None:
    from alfred.web.voice_session import TooManySessions

    voice_client.app[KEY_WEB_VOICE_MANAGER].open_exc = TooManySessions(2)
    resp = await voice_client.post(
        "/voice/offer", headers=_headers(), json={"sdp": "v=0\r\n", "type": "offer"},
    )
    assert resp.status == 429
    body = await resp.json()
    assert body["error"] == "too_many_sessions"
    assert body["max_sessions"] == 2


async def test_offer_504_timeout(voice_client) -> None:
    from alfred.web.voice_session import VoiceOfferTimeout

    voice_client.app[KEY_WEB_VOICE_MANAGER].open_exc = VoiceOfferTimeout(10)
    resp = await voice_client.post(
        "/voice/offer", headers=_headers(), json={"sdp": "v=0\r\n", "type": "offer"},
    )
    assert resp.status == 504
    assert (await resp.json())["error"] == "voice_offer_timeout"


async def test_offer_502_negotiation_failed(voice_client) -> None:
    from alfred.web.voice_session import NegotiationFailed

    voice_client.app[KEY_WEB_VOICE_MANAGER].open_exc = NegotiationFailed("boom")
    resp = await voice_client.post(
        "/voice/offer", headers=_headers(), json={"sdp": "v=0\r\n", "type": "offer"},
    )
    assert resp.status == 502
    assert (await resp.json())["error"] == "negotiation_failed"


# ---------------------------------------------------------------------------
# Offer happy path
# ---------------------------------------------------------------------------


async def test_offer_happy_path_contract(voice_client) -> None:
    voice_client.app[KEY_WEB_VOICE_MANAGER].open_result = (
        "f" * 32, "v=0\r\nANSWER-WITH-CANDIDATES\r\n",
    )
    resp = await voice_client.post(
        "/voice/offer", headers=_headers(),
        json={"sdp": "v=0\r\nOFFER\r\n", "type": "offer"},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["voice_session_id"] == "f" * 32
    assert body["sdp"] == "v=0\r\nANSWER-WITH-CANDIDATES\r\n"
    assert body["type"] == "answer"
    assert "expires_at" in body and body["expires_at"].endswith("+00:00")


async def test_offer_session_key_accepted_and_logged(voice_client) -> None:
    with structlog.testing.capture_logs() as cap:
        resp = await voice_client.post(
            "/voice/offer", headers=_headers(),
            json={"sdp": "v=0\r\n", "type": "offer", "session_key": "abc123"},
        )
    assert resp.status == 200
    logs = [c for c in cap if c.get("event") == "web.voice.session_key_ignored"]
    assert len(logs) == 1
    assert logs[0]["length"] == 6


async def test_offer_unknown_extra_key_not_rejected(voice_client) -> None:
    # Validators MUST NOT reject unknown extra keys (§1.7).
    resp = await voice_client.post(
        "/voice/offer", headers=_headers(),
        json={"sdp": "v=0\r\n", "type": "offer", "future_field": {"x": 1}},
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


async def test_close_200_true_own(voice_client) -> None:
    voice_client.app[KEY_WEB_VOICE_MANAGER].close_result = True
    resp = await voice_client.post(
        "/voice/close", headers=_headers(), json={"voice_session_id": "abc"},
    )
    assert resp.status == 200
    assert await resp.json() == {"closed": True}


async def test_close_200_false_not_found(voice_client) -> None:
    voice_client.app[KEY_WEB_VOICE_MANAGER].close_result = False
    resp = await voice_client.post(
        "/voice/close", headers=_headers(), json={"voice_session_id": "unknown"},
    )
    assert resp.status == 200
    assert await resp.json() == {"closed": False, "reason": "not_found"}


async def test_close_415_wrong_content_type(voice_client) -> None:
    resp = await voice_client.post(
        "/voice/close",
        headers={**_PEER_HEADERS, SESSION_HEADER: _session(),
                 "Content-Type": "text/plain"},
        data='{"voice_session_id":"x"}',
    )
    assert resp.status == 415


async def test_close_400_id_required(voice_client) -> None:
    resp = await voice_client.post(
        "/voice/close", headers=_headers(), json={},
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "voice_session_id_required"


async def test_close_400_bad_json(voice_client) -> None:
    resp = await voice_client.post(
        "/voice/close", headers=_headers(), data="{bad",
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "bad_json"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


async def test_config_available_shape(voice_client) -> None:
    resp = await voice_client.get("/voice/config", headers=_headers())
    assert resp.status == 200
    body = await resp.json()
    assert body["available"] is True
    assert body["reason"] is None
    assert body["max_sessions"] == 2
    assert body["ice_servers"] == []
    assert body["yours"] == []


async def test_config_ice_servers_from_stun(aiohttp_client, tmp_path, monkeypatch) -> None:
    manager = _FakeManager(_voice_config())
    _patch_available(monkeypatch, manager)
    voice = _voice_config(
        ice=VoiceIceConfig(stun_servers=["stun:stun.l.google.com:19302"]),
    )
    app = _build_app(tmp_path, _web_config(voice=voice))
    client = await aiohttp_client(app)
    resp = await client.get("/voice/config", headers=_headers())
    body = await resp.json()
    assert body["ice_servers"] == [{"urls": ["stun:stun.l.google.com:19302"]}]


async def test_config_yours_scoped_to_caller(voice_client) -> None:
    mgr = voice_client.app[KEY_WEB_VOICE_MANAGER]
    mgr._yours = [_FakeSession("s1", "connected")]
    resp = await voice_client.get("/voice/config", headers=_headers())
    body = await resp.json()
    assert len(body["yours"]) == 1
    assert body["yours"][0]["voice_session_id"] == "s1"
    assert body["yours"][0]["connection_state"] == "connected"
    assert body["yours"][0]["age_seconds"] == 12


async def test_config_401_without_session(voice_client) -> None:
    resp = await voice_client.get("/voice/config", headers=_PEER_HEADERS)
    assert resp.status == 401
