"""Tests for ``alfred.web.routes_chat`` — chat over HTTP (Sub-arc B).

Sub-arc B: ``/chat/*`` now requires BOTH Layer-1 (transport peer token,
enforced by ``auth_middleware``) AND Layer-2 (a per-user instance-signed
``X-Alfred-Session`` token, verified by ``require_web_session``). Uses
aiohttp's ``aiohttp_client`` fixture to spin up the real transport
``Application`` with web routes mounted, plus the shared
``FakeAnthropicClient`` so no network calls happen.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from aiohttp.test_utils import make_mocked_request

from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
from alfred.telegram.session import open_session
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
from alfred.web.auth import SESSION_HEADER, USER_HEADER, make_session_token
from alfred.web.config import WebAuthConfig, WebConfig, WebUser
from alfred.web.identity import synthetic_chat_id
from alfred.web.routes_chat import (
    _flatten_transcript_for_web,
    _handle_chat_history,
    register_web_routes,
)
from alfred.web.state import WebAuthState

from tests.telegram.conftest import (  # shared fake SDK client
    FakeAnthropicClient,
    FakeBlock,
    FakeResponse,
)

# Obviously-fake test secrets — never a real provider prefix (builder.md
# GitGuardian rule).
DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
    "X-Alfred-Client": "web",
}


def _session_headers(name: str = "andrew", role: str = "owner") -> dict[str, str]:
    """Peer headers + a valid Layer-2 session token for ``name``."""
    token = make_session_token(
        name, role, secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    return {**_PEER_HEADERS, SESSION_HEADER: token}


def _make_talker_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    for sub in ("session", "task", "note", "project"):
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
    """Transport config carrying a dedicated ``web`` peer-token entry."""
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                "web": AuthTokenEntry(
                    token=DUMMY_WEB_PEER_TOKEN,
                    allowed_clients=["web"],
                ),
            }
        ),
        state=StateConfig(),
    )


def _web_config(users=None) -> WebConfig:
    return WebConfig(
        enabled=True,
        users=users if users is not None else [WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(session_secret=DUMMY_WEB_SIGNING_SECRET),
    )


def _relay_web_config(users=None) -> WebConfig:
    """A relay-mode web config — NO session_secret (no token minting)."""
    return WebConfig(
        enabled=True,
        users=users if users is not None else [WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(mode="relay", session_secret=""),
    )


@pytest.fixture
async def web_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """A transport app with web routes mounted + a one-reply fake client."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)

    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    talker_config = _make_talker_config(tmp_path)
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    fake = FakeAnthropicClient(
        [FakeResponse(content=[FakeBlock(type="text", text="hello from salem")])]
    )
    register_web_routes(
        app,
        web_config=_web_config(),
        web_auth_state=web_auth_state,
        anthropic_client=fake,
        state_mgr=state_mgr,
        talker_config=talker_config,
        system_prompt_provider=lambda: "SYSTEM PROMPT",
        vault_context_str="VAULT CONTEXT",
        allowed_user_ids=[1],
    )
    app["_t_state_mgr"] = state_mgr
    app["_t_talker_config"] = talker_config
    return await aiohttp_client(app)


@pytest.fixture
async def relay_web_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """A transport app with web routes mounted in RELAY mode.

    Relay mode: no session_secret, no /auth routes; the asserted
    ``X-Alfred-User`` header is the identity (gated by the Layer-1 peer
    token the transport app already enforces).
    """
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)

    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    talker_config = _make_talker_config(tmp_path)
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    fake = FakeAnthropicClient(
        [FakeResponse(content=[FakeBlock(type="text", text="hello from kalle")])]
    )
    register_web_routes(
        app,
        web_config=_relay_web_config(),
        web_auth_state=web_auth_state,
        anthropic_client=fake,
        state_mgr=state_mgr,
        talker_config=talker_config,
        system_prompt_provider=lambda: "SYSTEM PROMPT",
        vault_context_str="VAULT CONTEXT",
        allowed_user_ids=[1],
    )
    app["_t_state_mgr"] = state_mgr
    app["_t_talker_config"] = talker_config
    return await aiohttp_client(app)


def _relay_headers(name: str = "andrew") -> dict[str, str]:
    """Peer headers (Layer 1) + the asserted user name (relay identity)."""
    return {**_PEER_HEADERS, USER_HEADER: name}


# ---------------------------------------------------------------------------
# Layer 1 (peer token) + Layer 2 (session token) gates
# ---------------------------------------------------------------------------


async def test_chat_route_requires_peer_token(web_client) -> None:
    # No Authorization header → the existing auth_middleware rejects (Layer 1).
    resp = await web_client.post("/chat/open", json={})
    assert resp.status == 401


async def test_chat_route_rejects_wrong_peer_token(web_client) -> None:
    resp = await web_client.post(
        "/chat/open",
        json={},
        headers={"Authorization": "Bearer wrong", "X-Alfred-Client": "web"},
    )
    assert resp.status == 401


async def test_chat_requires_session_token(web_client) -> None:
    # Valid peer token but NO X-Alfred-Session → Layer 2 fail-closed 401.
    resp = await web_client.post("/chat/open", json={}, headers=_PEER_HEADERS)
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


async def test_chat_rejects_session_for_unlisted_user(web_client) -> None:
    # A validly-signed token for a user NOT in the allowlist → 401.
    headers = _session_headers("stranger", "owner")
    resp = await web_client.post("/chat/open", json={}, headers=headers)
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


# ---------------------------------------------------------------------------
# Round-trip: open → turn → history
# ---------------------------------------------------------------------------


async def test_open_turn_history_roundtrip(web_client) -> None:
    headers = _session_headers()
    r = await web_client.post("/chat/open", json={}, headers=headers)
    assert r.status == 200
    session_key = (await r.json())["session_key"]
    assert session_key

    r = await web_client.post(
        "/chat/turn",
        json={"session_key": session_key, "message": "hi there"},
        headers=headers,
    )
    assert r.status == 200
    body = await r.json()
    assert body["reply"] == "hello from salem"
    assert body["session_key"] == session_key

    r = await web_client.get(f"/chat/history/{session_key}", headers=headers)
    assert r.status == 200
    turns = (await r.json())["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["text"] == "hi there"
    assert turns[1]["text"] == "hello from salem"
    assert all(t["ts"] for t in turns)


async def test_turn_returns_per_turn_timestamps(web_client) -> None:
    """`/chat/turn` additively returns ``ts`` (assistant) + ``user_ts``
    (user), both from the existing ``_ts`` clock. Both always present,
    non-empty for a real turn, and byte-identical to what /chat/history
    later surfaces (live == resume)."""
    headers = _session_headers()
    r = await web_client.post("/chat/open", json={}, headers=headers)
    session_key = (await r.json())["session_key"]

    r = await web_client.post(
        "/chat/turn",
        json={"session_key": session_key, "message": "hi there"},
        headers=headers,
    )
    assert r.status == 200
    body = await r.json()
    assert "ts" in body and "user_ts" in body
    assert body["ts"], "assistant turn ts must be non-empty"
    assert body["user_ts"], "user turn ts must be non-empty"

    # Live == resume: the stamps match what history projects per turn.
    r = await web_client.get(f"/chat/history/{session_key}", headers=headers)
    turns = (await r.json())["turns"]
    assert turns[0]["ts"] == body["user_ts"]
    assert turns[-1]["ts"] == body["ts"]


async def test_turn_timestamps_present_in_log(web_client) -> None:
    """The turn_complete log carries assistant_ts/user_ts (observability)."""
    headers = _session_headers()
    r = await web_client.post("/chat/open", json={}, headers=headers)
    session_key = (await r.json())["session_key"]
    with structlog.testing.capture_logs() as captured:
        await web_client.post(
            "/chat/turn",
            json={"session_key": session_key, "message": "hi"},
            headers=headers,
        )
    done = [c for c in captured if c.get("event") == "web.chat.turn_complete"]
    assert len(done) == 1
    assert done[0]["assistant_ts"]
    assert done[0]["user_ts"]


async def test_session_persisted_under_synthetic_id(web_client) -> None:
    state_mgr = web_client.app["_t_state_mgr"]
    r = await web_client.post("/chat/open", json={}, headers=_session_headers())
    session_key = (await r.json())["session_key"]
    active = state_mgr.get_active(synthetic_chat_id("andrew"))
    assert active is not None
    assert active["session_id"] == session_key
    assert active["chat_id"] == synthetic_chat_id("andrew")


# ---------------------------------------------------------------------------
# Relay mode (cross-instance chat) — asserted X-Alfred-User identity
# ---------------------------------------------------------------------------


async def test_relay_requires_peer_token(relay_web_client) -> None:
    # Even in relay mode the Layer-1 peer token is mandatory (auth_middleware).
    resp = await relay_web_client.post(
        "/chat/open", json={}, headers={USER_HEADER: "andrew"}
    )
    assert resp.status == 401


async def test_relay_missing_user_header_fails_closed(relay_web_client) -> None:
    # Valid peer token but NO X-Alfred-User → fail-closed 401.
    resp = await relay_web_client.post("/chat/open", json={}, headers=_PEER_HEADERS)
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


async def test_relay_unknown_user_fails_closed(relay_web_client) -> None:
    # Valid peer token + an asserted name NOT in this instance's web.users.
    resp = await relay_web_client.post(
        "/chat/open", json={}, headers=_relay_headers("stranger")
    )
    assert resp.status == 401
    assert (await resp.json())["error"] == "invalid_session"


async def test_relay_session_token_does_not_authenticate(relay_web_client) -> None:
    # A signed session token must NOT authenticate in relay mode — only the
    # asserted X-Alfred-User header (gated by the peer token) does.
    token = make_session_token(
        "andrew", "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    resp = await relay_web_client.post(
        "/chat/open", json={}, headers={**_PEER_HEADERS, SESSION_HEADER: token}
    )
    assert resp.status == 401


async def test_relay_open_turn_history_roundtrip(relay_web_client) -> None:
    headers = _relay_headers()
    r = await relay_web_client.post("/chat/open", json={}, headers=headers)
    assert r.status == 200
    session_key = (await r.json())["session_key"]
    assert session_key

    r = await relay_web_client.post(
        "/chat/turn",
        json={"session_key": session_key, "message": "hi kalle"},
        headers=headers,
    )
    assert r.status == 200
    body = await r.json()
    assert body["reply"] == "hello from kalle"
    assert body["session_key"] == session_key

    r = await relay_web_client.get(
        f"/chat/history/{session_key}", headers=headers
    )
    assert r.status == 200
    turns = (await r.json())["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_turn_unknown_session_404(web_client) -> None:
    headers = _session_headers()
    await web_client.post("/chat/open", json={}, headers=headers)
    r = await web_client.post(
        "/chat/turn",
        json={"session_key": "not-a-real-key", "message": "hi"},
        headers=headers,
    )
    assert r.status == 404
    assert (await r.json())["error"] == "no_such_session"


async def test_turn_missing_message_400(web_client) -> None:
    headers = _session_headers()
    r = await web_client.post("/chat/open", json={}, headers=headers)
    session_key = (await r.json())["session_key"]
    r = await web_client.post(
        "/chat/turn",
        json={"session_key": session_key, "message": "   "},
        headers=headers,
    )
    assert r.status == 400
    assert (await r.json())["error"] == "message_required"


async def test_history_unknown_session_404(web_client) -> None:
    r = await web_client.get("/chat/history/nope", headers=_session_headers())
    assert r.status == 404


# ---------------------------------------------------------------------------
# Close-then-open archives the prior session (Telegram parity)
# ---------------------------------------------------------------------------


async def test_reopen_archives_prior_session(web_client) -> None:
    state_mgr = web_client.app["_t_state_mgr"]
    talker_config = web_client.app["_t_talker_config"]
    headers = _session_headers()

    r = await web_client.post("/chat/open", json={}, headers=headers)
    first_key = (await r.json())["session_key"]

    r = await web_client.post("/chat/open", json={}, headers=headers)
    second_key = (await r.json())["session_key"]
    assert second_key != first_key

    closed = state_mgr.state.get("closed_sessions", [])
    assert any(c["session_id"] == first_key for c in closed)
    session_dir = Path(talker_config.vault.path) / "session"
    assert list(session_dir.glob("*.md")), "expected an archived session record"


# ---------------------------------------------------------------------------
# Pure transcript-flatten helper
# ---------------------------------------------------------------------------


def test_flatten_transcript_drops_tool_plumbing() -> None:
    transcript = [
        {"role": "user", "content": "plain string turn", "_ts": "2026-06-29T00:00:00Z"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "let me check"},
                {"type": "tool_use", "id": "t1", "name": "vault_search", "input": {}},
            ],
            "_ts": "2026-06-29T00:00:01Z",
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}],
            "_ts": "2026-06-29T00:00:02Z",
        },
        {"role": "assistant", "content": "final answer", "_ts": "2026-06-29T00:00:03Z"},
        {"role": "system", "content": "ignored"},
    ]
    out = _flatten_transcript_for_web(transcript)
    assert out == [
        {"role": "user", "text": "plain string turn", "ts": "2026-06-29T00:00:00Z"},
        {"role": "assistant", "text": "let me check", "ts": "2026-06-29T00:00:01Z"},
        {"role": "assistant", "text": "final answer", "ts": "2026-06-29T00:00:03Z"},
    ]


def test_flatten_transcript_empty() -> None:
    assert _flatten_transcript_for_web([]) == []


# ---------------------------------------------------------------------------
# ILB: empty history emits the explicit "ran, nothing to surface" log
# ---------------------------------------------------------------------------


async def test_history_empty_emits_ilb_log(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    web_auth_state = WebAuthState.create(tmp_path / "web_auth_state.json")
    web_auth_state.load()
    register_web_routes(
        app,
        web_config=_web_config(),
        web_auth_state=web_auth_state,
        anthropic_client=FakeAnthropicClient([]),
        state_mgr=state_mgr,
        talker_config=_make_talker_config(tmp_path),
        system_prompt_provider=lambda: "SYS",
        vault_context_str="CTX",
        allowed_user_ids=[1],
    )
    sess = open_session(state_mgr, synthetic_chat_id("andrew"), model="claude-sonnet-4-6")

    token = make_session_token(
        "andrew", "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    req = make_mocked_request(
        "GET",
        f"/chat/history/{sess.session_id}",
        headers={SESSION_HEADER: token},
        match_info={"session_key": sess.session_id},
        app=app,
    )

    with structlog.testing.capture_logs() as captured:
        resp = await _handle_chat_history(req)

    assert resp.status == 200
    matches = [c for c in captured if c.get("event") == "web.chat.history_empty"]
    assert len(matches) == 1
    assert matches[0]["user"] == "andrew"
    assert matches[0]["session_key"] == sess.session_id


# ---------------------------------------------------------------------------
# Opt-in inertness + fail-loud startup guards
# ---------------------------------------------------------------------------


def _mounted_web_paths(app) -> list[str]:
    return [
        r.resource.canonical
        for r in app.router.routes()
        if r.resource is not None
        and (
            r.resource.canonical.startswith("/chat")
            or r.resource.canonical.startswith("/auth")
        )
    ]


def _register_kwargs(tmp_path, **overrides):
    base = dict(
        web_auth_state=WebAuthState.create(tmp_path / "web_auth_state.json"),
        anthropic_client=FakeAnthropicClient([]),
        state_mgr=StateManager(tmp_path / "s.json"),
        talker_config=_make_talker_config(tmp_path),
        system_prompt_provider=lambda: "SYS",
        vault_context_str="CTX",
        allowed_user_ids=[1],
    )
    base.update(overrides)
    return base


def test_register_web_routes_disabled_mounts_nothing(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    mounted = register_web_routes(
        app,
        web_config=WebConfig(enabled=False, users=[WebUser(name="andrew")]),
        **_register_kwargs(tmp_path),
    )
    assert mounted is False
    assert _mounted_web_paths(app) == []


def test_register_web_routes_none_config_mounts_nothing(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    mounted = register_web_routes(app, web_config=None, **_register_kwargs(tmp_path))
    assert mounted is False
    assert _mounted_web_paths(app) == []


def test_register_web_routes_enabled_mounts_five(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    mounted = register_web_routes(
        app, web_config=_web_config(), **_register_kwargs(tmp_path)
    )
    assert mounted is True
    assert set(_mounted_web_paths(app)) == {
        "/chat/open",
        "/chat/turn",
        "/chat/history/{session_key}",
        "/auth/login",
        "/auth/verify",
    }


def test_register_web_routes_collision_fails_loud(tmp_path, monkeypatch) -> None:
    from alfred.web import routes_chat as routes_mod

    monkeypatch.setattr(
        routes_mod,
        "check_synthetic_id_collisions",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")),
    )
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    with pytest.raises(ValueError, match="boom"):
        register_web_routes(app, web_config=_web_config(), **_register_kwargs(tmp_path))
    assert _mounted_web_paths(app) == []


def test_register_web_routes_unconfigured_secret_fails_loud(tmp_path) -> None:
    # Enabled but no signing secret → resolve_signing_secret guard aborts.
    # (Session mode — the default.)
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    cfg = WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(session_secret=""),
    )
    with pytest.raises(ValueError, match="session_secret"):
        register_web_routes(app, web_config=cfg, **_register_kwargs(tmp_path))
    assert _mounted_web_paths(app) == []


def test_register_web_routes_relay_skips_auth_and_secret_guard(tmp_path) -> None:
    # Relay mode mounts /chat/* WITHOUT a signing secret AND WITHOUT the
    # /auth login surface (relay instances never mint / verify tokens).
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    mounted = register_web_routes(
        app, web_config=_relay_web_config(), **_register_kwargs(tmp_path)
    )
    assert mounted is True
    paths = set(_mounted_web_paths(app))
    # /chat/* present, /auth/* absent.
    assert "/chat/open" in paths
    assert "/chat/turn" in paths
    assert "/chat/history/{session_key}" in paths
    assert "/auth/login" not in paths
    assert "/auth/verify" not in paths


def test_register_web_routes_relay_logs_no_auth(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    with structlog.testing.capture_logs() as captured:
        register_web_routes(
            app, web_config=_relay_web_config(), **_register_kwargs(tmp_path)
        )
    events = [c["event"] for c in captured]
    assert "web.routes.relay_mode_no_auth" in events
    registered = [c for c in captured if c.get("event") == "web.routes.registered"]
    assert len(registered) == 1
    assert registered[0]["mode"] == "relay"
