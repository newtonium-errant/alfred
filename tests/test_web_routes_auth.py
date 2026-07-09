"""Tests for ``alfred.web.routes_auth`` — magic-link login + verify (Sub-arc B).

Drives /auth/login + /auth/verify through the real transport app. The
Resend send is monkeypatched to capture the magic link (so the test can
extract the token and complete the login→verify→session round-trip without
hitting the network).
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from aiohttp.test_utils import make_mocked_request  # noqa: F401 (parity import)

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
from alfred.web import routes_auth as auth_routes_mod
from alfred.web.auth import SESSION_HEADER, make_session_token
from alfred.web.config import WebAuthConfig, WebConfig, WebEmailConfig, WebUser
from alfred.web.keys import KEY_WEB_AUTH_STATE, KEY_WEB_CONFIG
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import FakeAnthropicClient

DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

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
            gap_timeout_seconds=1800, state_path=str(tmp_path / "talker_state.json")
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
                    token=DUMMY_WEB_PEER_TOKEN, allowed_clients=["web"]
                )
            }
        ),
        state=StateConfig(),
    )


def _web_config(*, email_configured: bool = True, base_url: str = "https://salem.example.com") -> WebConfig:
    email = (
        WebEmailConfig(
            provider="resend",
            api_key="DUMMY_RESEND_TEST_KEY",
            from_address="bot@example.com",
        )
        if email_configured
        else WebEmailConfig(api_key="", from_address="")
    )
    return WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner", email="andrew@example.com")],
        auth=WebAuthConfig(
            session_secret=DUMMY_WEB_SIGNING_SECRET,
            magic_link_ttl_minutes=15,
            session_ttl_hours=168,
            base_url=base_url,
        ),
        email=email,
    )


@pytest.fixture(autouse=True)
def _reset_login_rate_limiter():
    """Reset the module-level login rate limiter around EACH test.

    The limiter (bit b) is a process-global singleton; without this reset,
    repeat /auth/login POSTs for the same (client-ip, email) across tests
    accumulate and a later test would spuriously 429. Mirrors the STT dedup
    cache's "tests use a fresh instance" convention.
    """
    auth_routes_mod._LOGIN_RATE_LIMITER.clear()
    yield
    auth_routes_mod._LOGIN_RATE_LIMITER.clear()


@pytest.fixture
def captured_links(monkeypatch):
    """Monkeypatch the Resend send to capture the magic link (no network)."""
    links: list[str] = []

    async def _fake_send(cfg, to_email, link, *, instance_name=""):
        links.append(link)
        return True

    monkeypatch.setattr(auth_routes_mod, "send_magic_link", _fake_send)
    return links


async def _make_client(aiohttp_client, tmp_path, web_config):
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
    return await aiohttp_client(app)


def _token_from_link(link: str) -> str:
    return parse_qs(urlparse(link).query)["token"][0]


# ---------------------------------------------------------------------------
# /auth/login
# ---------------------------------------------------------------------------


async def test_login_email_required(aiohttp_client, tmp_path, captured_links) -> None:
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await client.post("/auth/login", json={}, headers=_PEER_HEADERS)
    assert r.status == 400
    assert (await r.json())["error"] == "email_required"


async def test_login_unknown_email_is_uniform_no_send(
    aiohttp_client, tmp_path, captured_links
) -> None:
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await client.post(
        "/auth/login", json={"email": "nobody@example.com"}, headers=_PEER_HEADERS
    )
    assert r.status == 200
    assert (await r.json())["status"] == "sent"  # uniform — no enumeration
    assert captured_links == []  # nothing actually sent


async def test_login_known_email_sends(aiohttp_client, tmp_path, captured_links) -> None:
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await client.post(
        "/auth/login", json={"email": "andrew@example.com"}, headers=_PEER_HEADERS
    )
    assert r.status == 200
    assert (await r.json())["status"] == "sent"
    assert len(captured_links) == 1
    assert "/auth/callback?token=" in captured_links[0]


async def test_login_email_not_configured_503(
    aiohttp_client, tmp_path, captured_links
) -> None:
    client = await _make_client(
        aiohttp_client, tmp_path, _web_config(email_configured=False)
    )
    r = await client.post(
        "/auth/login", json={"email": "andrew@example.com"}, headers=_PEER_HEADERS
    )
    assert r.status == 503
    assert (await r.json())["error"] == "email_not_configured"


async def test_login_unresolved_base_url_503(
    aiohttp_client, tmp_path, captured_links
) -> None:
    client = await _make_client(
        aiohttp_client, tmp_path, _web_config(base_url="${ALFRED_WEB_BASE_URL}")
    )
    r = await client.post(
        "/auth/login", json={"email": "andrew@example.com"}, headers=_PEER_HEADERS
    )
    assert r.status == 503


async def test_login_requires_peer_token(aiohttp_client, tmp_path, captured_links) -> None:
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    # No peer headers → Layer-1 middleware rejects.
    r = await client.post("/auth/login", json={"email": "andrew@example.com"})
    assert r.status == 401


# ---------------------------------------------------------------------------
# /auth/verify
# ---------------------------------------------------------------------------


async def test_login_then_verify_roundtrip(
    aiohttp_client, tmp_path, captured_links
) -> None:
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    await client.post(
        "/auth/login", json={"email": "andrew@example.com"}, headers=_PEER_HEADERS
    )
    token = _token_from_link(captured_links[0])

    r = await client.post("/auth/verify", json={"token": token}, headers=_PEER_HEADERS)
    assert r.status == 200
    body = await r.json()
    assert body["name"] == "andrew"
    assert body["role"] == "owner"
    assert body["exp"] > 0
    assert body["session_token"]

    # The minted session token works on a /chat/* route.
    headers = {**_PEER_HEADERS, SESSION_HEADER: body["session_token"]}
    r = await client.post("/chat/open", json={}, headers=headers)
    assert r.status == 200


async def test_verify_replay_rejected(aiohttp_client, tmp_path, captured_links) -> None:
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    await client.post(
        "/auth/login", json={"email": "andrew@example.com"}, headers=_PEER_HEADERS
    )
    token = _token_from_link(captured_links[0])

    r1 = await client.post("/auth/verify", json={"token": token}, headers=_PEER_HEADERS)
    assert r1.status == 200
    # Same link again → nonce already consumed → 401.
    r2 = await client.post("/auth/verify", json={"token": token}, headers=_PEER_HEADERS)
    assert r2.status == 401
    assert (await r2.json())["error"] == "invalid_or_expired"


async def test_verify_garbage_token_401(aiohttp_client, tmp_path, captured_links) -> None:
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await client.post(
        "/auth/verify", json={"token": "garbage"}, headers=_PEER_HEADERS
    )
    assert r.status == 401


async def test_verify_missing_token_401(aiohttp_client, tmp_path, captured_links) -> None:
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await client.post("/auth/verify", json={}, headers=_PEER_HEADERS)
    assert r.status == 401


async def test_verify_rejects_session_token_as_magic(
    aiohttp_client, tmp_path, captured_links
) -> None:
    # A session token presented to /auth/verify must fail the magic type guard.
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    session_token = make_session_token(
        "andrew", "owner", secret=DUMMY_WEB_SIGNING_SECRET, ttl_hours=168
    )
    r = await client.post(
        "/auth/verify", json={"token": session_token}, headers=_PEER_HEADERS
    )
    assert r.status == 401


# ---------------------------------------------------------------------------
# MINT-path secret guard (B2-NOTE→B3): both handlers MUST call
# resolve_signing_secret BEFORE minting, so a placeholder/empty secret can
# never sign a token. Routes can't be mounted with an unconfigured secret
# (register_web_routes guard + daemon boot check), so these drive the
# handlers directly with the deps stashed on a bare app to prove the
# in-handler guard fires before any make_*_token call.
# ---------------------------------------------------------------------------


class _FakeReq:
    def __init__(self, app: dict, body: dict) -> None:
        self.app = app
        self._body = body

    async def json(self) -> dict:
        return self._body


async def test_login_handler_resolves_secret_before_mint(tmp_path) -> None:
    # Reaches resolve_signing_secret only after the email/base_url/user gates
    # pass — so configure those, leave session_secret empty.
    cfg = WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner", email="andrew@example.com")],
        auth=WebAuthConfig(session_secret="", base_url="https://x.example.com"),
        email=WebEmailConfig(
            api_key="DUMMY_RESEND_TEST_KEY", from_address="bot@example.com"
        ),
    )
    app = {
        KEY_WEB_CONFIG: cfg,
        KEY_WEB_AUTH_STATE: WebAuthState.create(tmp_path / "n.json"),
    }
    req = _FakeReq(app, {"email": "andrew@example.com"})
    with pytest.raises(ValueError, match="session_secret"):
        await auth_routes_mod._handle_auth_login(req)


async def test_verify_handler_resolves_secret_before_mint(tmp_path) -> None:
    cfg = WebConfig(
        enabled=True,
        users=[WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(session_secret=""),
    )
    app = {
        KEY_WEB_CONFIG: cfg,
        KEY_WEB_AUTH_STATE: WebAuthState.create(tmp_path / "n.json"),
    }
    # Non-empty token clears the missing-token gate → reaches the secret guard.
    req = _FakeReq(app, {"token": "any-nonempty-token"})
    with pytest.raises(ValueError, match="session_secret"):
        await auth_routes_mod._handle_auth_verify(req)


# ---------------------------------------------------------------------------
# BIT (b) — /auth/login rate limit (integration through the real handler).
# ---------------------------------------------------------------------------


async def test_login_rate_limit_blocks_over_cap_and_logs(
    aiohttp_client, tmp_path, captured_links, monkeypatch
) -> None:
    import structlog

    # A fresh limiter with a tight cap + fake clock (no real sleeping).
    now = {"t": 1_000.0}
    limiter = auth_routes_mod._LoginRateLimiter(
        max_per_email=2, window_s=900, max_global=999, clock=lambda: now["t"]
    )
    monkeypatch.setattr(auth_routes_mod, "_LOGIN_RATE_LIMITER", limiter)

    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    body = {"email": "andrew@example.com"}

    # N sends allowed.
    for _ in range(2):
        r = await client.post("/auth/login", json=body, headers=_PEER_HEADERS)
        assert r.status == 200

    # The (N+1)th within the window → 429 + a logged rate-limit event (never
    # the raw email — a hashed prefix only).
    with structlog.testing.capture_logs() as captured:
        r = await client.post("/auth/login", json=body, headers=_PEER_HEADERS)
    assert r.status == 429
    assert (await r.json())["error"] == "rate_limited"
    events = [c for c in captured if c.get("event") == "web.auth.login_rate_limited"]
    assert len(events) == 1
    assert "email_sha" in events[0]  # hashed, not the raw address
    assert "andrew@example.com" not in str(events[0])

    # A send after the window elapses (injected clock) → allowed again.
    now["t"] += 901
    r = await client.post("/auth/login", json=body, headers=_PEER_HEADERS)
    assert r.status == 200


async def test_login_rate_limit_is_uniform_for_unknown_email(
    aiohttp_client, tmp_path, captured_links, monkeypatch
) -> None:
    # The limit fires BEFORE the user lookup, so a known and an unknown email
    # 429 identically — no enumeration via the rate-limit response.
    limiter = auth_routes_mod._LoginRateLimiter(max_per_email=1, max_global=999)
    monkeypatch.setattr(auth_routes_mod, "_LOGIN_RATE_LIMITER", limiter)
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    body = {"email": "nobody@example.com"}  # not on the allowlist
    r1 = await client.post("/auth/login", json=body, headers=_PEER_HEADERS)
    assert r1.status == 200
    r2 = await client.post("/auth/login", json=body, headers=_PEER_HEADERS)
    assert r2.status == 429


async def test_login_unknown_flood_does_not_lock_out_legit_login(
    aiohttp_client, tmp_path, captured_links, monkeypatch
) -> None:
    # FIX #6: a flood of UNKNOWN-email POSTs (which never send a magic link)
    # must NOT exhaust the GLOBAL send budget — so a subsequent LEGITIMATE
    # login is not 429'd. Small global ceiling; generous per-email so distinct
    # junk emails don't trip the per-email gate.
    limiter = auth_routes_mod._LoginRateLimiter(
        max_per_email=99, window_s=900, max_global=3
    )
    monkeypatch.setattr(auth_routes_mod, "_LOGIN_RATE_LIMITER", limiter)
    client = await _make_client(aiohttp_client, tmp_path, _web_config())

    # 10 distinct unknown emails — each returns the uniform "sent", none sends.
    for i in range(10):
        r = await client.post(
            "/auth/login",
            json={"email": f"junk{i}@nope.com"},
            headers=_PEER_HEADERS,
        )
        assert r.status == 200
    assert captured_links == []  # nothing actually sent → global untouched

    # The legitimate user is NOT locked out by the junk flood.
    r = await client.post(
        "/auth/login", json={"email": "andrew@example.com"}, headers=_PEER_HEADERS
    )
    assert r.status == 200
    assert len(captured_links) == 1  # the real send happened


# ---------------------------------------------------------------------------
# BIT (c) — magic-link next-param deep-link (integration through the handler).
# ---------------------------------------------------------------------------


def _next_from_link(link: str) -> str | None:
    qs = parse_qs(urlparse(link).query)
    return qs["next"][0] if "next" in qs else None


async def test_login_valid_next_round_trips_into_magic_link(
    aiohttp_client, tmp_path, captured_links
) -> None:
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await client.post(
        "/auth/login",
        json={"email": "andrew@example.com", "next": "/chat?instance=hypatia"},
        headers=_PEER_HEADERS,
    )
    assert r.status == 200
    assert len(captured_links) == 1
    # The emailed link carries the (URL-encoded) next; the callback decodes it.
    assert _next_from_link(captured_links[0]) == "/chat?instance=hypatia"


@pytest.mark.parametrize(
    "evil", ["//evil.com", "https://evil.com", "/\\evil.com", "javascript:alert(1)"]
)
async def test_login_open_redirect_next_falls_back_to_default(
    aiohttp_client, tmp_path, captured_links, evil
) -> None:
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await client.post(
        "/auth/login",
        json={"email": "andrew@example.com", "next": evil},
        headers=_PEER_HEADERS,
    )
    assert r.status == 200
    assert len(captured_links) == 1
    # An open-redirect next is stripped — the emailed link carries no next.
    assert _next_from_link(captured_links[0]) is None
    assert "next=" not in captured_links[0]


async def test_login_no_next_is_byte_identical_link(
    aiohttp_client, tmp_path, captured_links
) -> None:
    # Merge-inert: a login with NO next produces the pre-deep-link URL shape.
    client = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await client.post(
        "/auth/login", json={"email": "andrew@example.com"}, headers=_PEER_HEADERS
    )
    assert r.status == 200
    assert "next=" not in captured_links[0]
    assert "/auth/callback?token=" in captured_links[0]
