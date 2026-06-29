"""Tests for ``alfred.web.routes_chat`` — chat over HTTP (Sub-arc A).

Sub-arc A proves ``run_turn`` over HTTP behind the EXISTING transport peer
token (no web auth yet). Uses aiohttp's ``aiohttp_client`` fixture to spin
up the real transport ``Application`` with web routes mounted, plus the
shared ``FakeAnthropicClient`` so no network calls happen.
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
from alfred.web.config import WebConfig, WebUser
from alfred.web.identity import synthetic_chat_id
from alfred.web.routes_chat import (
    _flatten_transcript_for_web,
    _handle_chat_history,
    register_web_routes,
)

from tests.telegram.conftest import (  # shared fake SDK client
    FakeAnthropicClient,
    FakeBlock,
    FakeResponse,
)

# Obviously-fake peer token — never a real provider prefix (builder.md
# GitGuardian rule).
DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
    "X-Alfred-Client": "web",
}


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


@pytest.fixture
async def web_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """A transport app with web routes mounted + a one-reply fake client."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)

    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    talker_config = _make_talker_config(tmp_path)
    fake = FakeAnthropicClient(
        [FakeResponse(content=[FakeBlock(type="text", text="hello from salem")])]
    )
    web_config = WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner", email="a@example.com")],
    )
    register_web_routes(
        app,
        web_config=web_config,
        anthropic_client=fake,
        state_mgr=state_mgr,
        talker_config=talker_config,
        system_prompt_provider=lambda: "SYSTEM PROMPT",
        vault_context_str="VAULT CONTEXT",
        allowed_user_ids=[1],
    )
    # Stash refs for assertions (set pre-start while the app is mutable).
    app["_t_state_mgr"] = state_mgr
    app["_t_talker_config"] = talker_config
    return await aiohttp_client(app)


# ---------------------------------------------------------------------------
# Peer-auth (Layer 1) — the existing transport gate still guards web routes
# ---------------------------------------------------------------------------


async def test_chat_route_requires_peer_token(web_client) -> None:
    # No Authorization header → the existing auth_middleware rejects.
    resp = await web_client.post("/chat/open", json={"user": "andrew"})
    assert resp.status == 401


async def test_chat_route_rejects_wrong_peer_token(web_client) -> None:
    resp = await web_client.post(
        "/chat/open",
        json={"user": "andrew"},
        headers={"Authorization": "Bearer wrong", "X-Alfred-Client": "web"},
    )
    assert resp.status == 401


# ---------------------------------------------------------------------------
# Round-trip: open → turn → history
# ---------------------------------------------------------------------------


async def test_open_turn_history_roundtrip(web_client) -> None:
    # open
    r = await web_client.post(
        "/chat/open", json={"user": "andrew"}, headers=_PEER_HEADERS
    )
    assert r.status == 200
    session_key = (await r.json())["session_key"]
    assert session_key

    # turn — drives run_turn with the fake client
    r = await web_client.post(
        "/chat/turn",
        json={"user": "andrew", "session_key": session_key, "message": "hi there"},
        headers=_PEER_HEADERS,
    )
    assert r.status == 200
    body = await r.json()
    assert body["reply"] == "hello from salem"
    assert body["session_key"] == session_key

    # history — user + assistant turns surfaced, tool plumbing flattened out
    r = await web_client.get(
        f"/chat/history/{session_key}?user=andrew", headers=_PEER_HEADERS
    )
    assert r.status == 200
    turns = (await r.json())["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[0]["text"] == "hi there"
    assert turns[1]["text"] == "hello from salem"
    assert all(t["ts"] for t in turns)  # _ts stamped


async def test_session_persisted_under_synthetic_id(web_client) -> None:
    state_mgr = web_client.app["_t_state_mgr"]
    r = await web_client.post(
        "/chat/open", json={"user": "andrew"}, headers=_PEER_HEADERS
    )
    session_key = (await r.json())["session_key"]
    active = state_mgr.get_active(synthetic_chat_id("andrew"))
    assert active is not None
    assert active["session_id"] == session_key
    assert active["chat_id"] == synthetic_chat_id("andrew")


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


async def test_turn_unknown_session_404(web_client) -> None:
    await web_client.post(
        "/chat/open", json={"user": "andrew"}, headers=_PEER_HEADERS
    )
    r = await web_client.post(
        "/chat/turn",
        json={"user": "andrew", "session_key": "not-a-real-key", "message": "hi"},
        headers=_PEER_HEADERS,
    )
    assert r.status == 404
    assert (await r.json())["error"] == "no_such_session"


async def test_turn_missing_message_400(web_client) -> None:
    r = await web_client.post(
        "/chat/open", json={"user": "andrew"}, headers=_PEER_HEADERS
    )
    session_key = (await r.json())["session_key"]
    r = await web_client.post(
        "/chat/turn",
        json={"user": "andrew", "session_key": session_key, "message": "   "},
        headers=_PEER_HEADERS,
    )
    assert r.status == 400
    assert (await r.json())["error"] == "message_required"


async def test_unknown_user_403(web_client) -> None:
    r = await web_client.post(
        "/chat/open", json={"user": "stranger"}, headers=_PEER_HEADERS
    )
    assert r.status == 403
    assert (await r.json())["error"] == "unknown_user"


async def test_history_unknown_session_404(web_client) -> None:
    r = await web_client.get(
        "/chat/history/nope?user=andrew", headers=_PEER_HEADERS
    )
    assert r.status == 404


# ---------------------------------------------------------------------------
# Close-then-open archives the prior session (Telegram parity)
# ---------------------------------------------------------------------------


async def test_reopen_archives_prior_session(web_client) -> None:
    state_mgr = web_client.app["_t_state_mgr"]
    talker_config = web_client.app["_t_talker_config"]

    r = await web_client.post(
        "/chat/open", json={"user": "andrew"}, headers=_PEER_HEADERS
    )
    first_key = (await r.json())["session_key"]

    # Re-open → prior session closed + archived as a session/ record.
    r = await web_client.post(
        "/chat/open", json={"user": "andrew"}, headers=_PEER_HEADERS
    )
    second_key = (await r.json())["session_key"]
    assert second_key != first_key

    # Prior session recorded in closed_sessions + a session/ file written.
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
        # tool_result turn (role user, no text) → dropped
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}],
            "_ts": "2026-06-29T00:00:02Z",
        },
        {"role": "assistant", "content": "final answer", "_ts": "2026-06-29T00:00:03Z"},
        {"role": "system", "content": "ignored"},  # non user/assistant → dropped
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
    """An empty (fresh) session's history logs ``web.chat.history_empty``."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    talker_config = _make_talker_config(tmp_path)
    register_web_routes(
        app,
        web_config=WebConfig(enabled=True, users=[WebUser(name="andrew", role="owner")]),
        anthropic_client=FakeAnthropicClient([]),
        state_mgr=state_mgr,
        talker_config=talker_config,
        system_prompt_provider=lambda: "SYS",
        vault_context_str="CTX",
        allowed_user_ids=[1],
    )
    # Open a fresh (empty-transcript) session.
    sess = open_session(state_mgr, synthetic_chat_id("andrew"), model="claude-sonnet-4-6")

    req = make_mocked_request(
        "GET",
        f"/chat/history/{sess.session_id}",
        headers={"X-Web-User": "andrew"},
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
# Opt-in inertness: disabled / absent web config mounts NOTHING
# ---------------------------------------------------------------------------


def _chat_routes(app) -> list[str]:
    return [
        r.resource.canonical
        for r in app.router.routes()
        if r.resource is not None and r.resource.canonical.startswith("/chat")
    ]


def test_register_web_routes_disabled_mounts_nothing(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    mounted = register_web_routes(
        app,
        web_config=WebConfig(enabled=False, users=[WebUser(name="andrew")]),
        anthropic_client=FakeAnthropicClient([]),
        state_mgr=StateManager(tmp_path / "s.json"),
        talker_config=_make_talker_config(tmp_path),
        system_prompt_provider=lambda: "SYS",
        vault_context_str="CTX",
        allowed_user_ids=[1],
    )
    assert mounted is False
    assert _chat_routes(app) == []


def test_register_web_routes_none_config_mounts_nothing(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    mounted = register_web_routes(
        app,
        web_config=None,
        anthropic_client=FakeAnthropicClient([]),
        state_mgr=StateManager(tmp_path / "s.json"),
        talker_config=_make_talker_config(tmp_path),
        system_prompt_provider=lambda: "SYS",
        vault_context_str="CTX",
    )
    assert mounted is False
    assert _chat_routes(app) == []


def test_register_web_routes_enabled_mounts_three(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    mounted = register_web_routes(
        app,
        web_config=WebConfig(enabled=True, users=[WebUser(name="andrew")]),
        anthropic_client=FakeAnthropicClient([]),
        state_mgr=StateManager(tmp_path / "s.json"),
        talker_config=_make_talker_config(tmp_path),
        system_prompt_provider=lambda: "SYS",
        vault_context_str="CTX",
        allowed_user_ids=[1],
    )
    assert mounted is True
    assert set(_chat_routes(app)) == {
        "/chat/open",
        "/chat/turn",
        "/chat/history/{session_key}",
    }


def test_register_web_routes_collision_fails_loud(tmp_path, monkeypatch) -> None:
    from alfred.web import routes_chat as routes_mod

    monkeypatch.setattr(routes_mod, "check_synthetic_id_collisions",
                        lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    with pytest.raises(ValueError, match="boom"):
        register_web_routes(
            app,
            web_config=WebConfig(enabled=True, users=[WebUser(name="andrew")]),
            anthropic_client=FakeAnthropicClient([]),
            state_mgr=StateManager(tmp_path / "s.json"),
            talker_config=_make_talker_config(tmp_path),
            system_prompt_provider=lambda: "SYS",
            vault_context_str="CTX",
            allowed_user_ids=[1],
        )
    # Failed guard → no routes mounted.
    assert _chat_routes(app) == []
