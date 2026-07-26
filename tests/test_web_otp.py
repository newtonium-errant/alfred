"""Tests for the OTP re-auth surface (parity #23 — iOS PWA cookie-jar fix).

Drives /auth/otp/request + /auth/otp/verify through the real transport app
(mirrors test_web_routes_auth.py's fixture style). The Resend send is
monkeypatched to capture the emailed code, so tests can complete the
request→verify→session round-trip without network.

Security-load-bearing coverage (each control is mutation-checked where the
task called for it):

1. DISABLED-BY-DEFAULT — ``otp_enabled`` defaults False and, when off, both
   /auth/otp/* routes answer 404 (feature inert until the operator flips
   ``web.auth.otp_enabled``). Flipping the dataclass default trips
   ``test_otp_disabled_by_default_dataclass`` directly.
2. SINGLE-USE BURN — a code that verified once can never verify again.
3. BRUTE-FORCE CAP — the ``otp_max_attempts``-th wrong try burns the code;
   a subsequent CORRECT code is rejected.
4. TTL — a code past ``otp_ttl_minutes`` is rejected (and burned).
5. HASH-ONLY — the raw 6-digit code appears nowhere in the persisted state
   file, the in-memory store, or the logs.
6. UNIFORM ERROR — bad-code / expired / unknown-email / exhausted all
   return the byte-identical 401 body (no oracle).
7. TOKEN PARITY — the OTP-minted session token is byte-shape-identical to
   the magic-link token (same claims) and drives /chat/* through the same
   require_web_session path.
8. MAGIC-LINK UNREGRESSED — covered by the untouched test_web_routes_auth
   suite (run alongside).
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
from alfred.web.auth import SESSION_HEADER, verify_session_token
from alfred.web.config import (
    WebAuthConfig,
    WebConfig,
    WebEmailConfig,
    WebUser,
    load_from_unified,
)
from alfred.web.routes_chat import register_web_routes
from alfred.web.state import WebAuthState

from tests.telegram.conftest import FakeAnthropicClient

DUMMY_WEB_PEER_TOKEN = "DUMMY_WEB_PEER_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
DUMMY_WEB_SIGNING_SECRET = "DUMMY_WEB_SIGNING_SECRET_FOR_TESTING_ONLY_0123456789"

_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_PEER_TOKEN}",
    "X-Alfred-Client": "web",
}

UNIFORM_401_BODY = {"error": "invalid_or_expired"}


def _make_talker_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    for sub in ("session", "task", "note", "project"):
        (vault_dir / sub).mkdir(exist_ok=True)
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


def _web_config(
    *,
    otp_enabled: bool = True,
    otp_max_attempts: int = 5,
    otp_ttl_minutes: int = 10,
    email_configured: bool = True,
) -> WebConfig:
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
            base_url="https://salem.example.com",
            otp_enabled=otp_enabled,
            otp_max_attempts=otp_max_attempts,
            otp_ttl_minutes=otp_ttl_minutes,
        ),
        email=email,
    )


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Reset BOTH module-level limiters around each test (process-global
    singletons; mirrors test_web_routes_auth's autouse reset)."""
    auth_routes_mod._LOGIN_RATE_LIMITER.clear()
    auth_routes_mod._OTP_RATE_LIMITER.clear()
    yield
    auth_routes_mod._LOGIN_RATE_LIMITER.clear()
    auth_routes_mod._OTP_RATE_LIMITER.clear()


@pytest.fixture
def captured_codes(monkeypatch):
    """Monkeypatch the Resend OTP send to capture the emailed code."""
    codes: list[str] = []

    async def _fake_send(cfg, to_email, code, *, instance_name=""):
        codes.append(code)
        return True

    monkeypatch.setattr(auth_routes_mod, "send_otp_code", _fake_send)
    return codes


async def _make_client(aiohttp_client, tmp_path, web_config):
    """Build the transport app with web routes; return (client, auth_state,
    state_file_path) so tests can reach the persisted store."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    state_file = tmp_path / "web_auth_state.json"
    web_auth_state = WebAuthState.create(state_file)
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
    return await aiohttp_client(app), web_auth_state, state_file


async def _request_code(client, email="andrew@example.com"):
    r = await client.post(
        "/auth/otp/request", json={"email": email}, headers=_PEER_HEADERS
    )
    return r


async def _verify(client, email="andrew@example.com", code="000000"):
    return await client.post(
        "/auth/otp/verify",
        json={"email": email, "code": code},
        headers=_PEER_HEADERS,
    )


def _wrong_code(code: str) -> str:
    """A syntactically-valid 6-digit code guaranteed != ``code``."""
    return f"{(int(code) + 1) % 1_000_000:06d}"


# ---------------------------------------------------------------------------
# (1) DISABLED-BY-DEFAULT — feature inert until the flag is flipped
# ---------------------------------------------------------------------------


def test_otp_disabled_by_default_dataclass() -> None:
    # MUTATION PIN: flipping the default to on in config.py trips this
    # directly — the feature must be inert on every instance until the
    # operator sets web.auth.otp_enabled: true.
    assert WebAuthConfig().otp_enabled is False
    assert WebAuthConfig().otp_max_attempts == 5
    assert WebAuthConfig().otp_ttl_minutes == 10


def test_otp_disabled_by_default_from_unified_yaml() -> None:
    # An operator YAML that never mentions otp_* loads with the flag OFF.
    cfg = load_from_unified(
        {"web": {"enabled": True, "auth": {"session_secret": "s"}}}
    )
    assert cfg.auth.otp_enabled is False
    # And the knobs parse when present.
    cfg2 = load_from_unified(
        {
            "web": {
                "auth": {
                    "otp_enabled": True,
                    "otp_max_attempts": 3,
                    "otp_ttl_minutes": 5,
                }
            }
        }
    )
    assert cfg2.auth.otp_enabled is True
    assert cfg2.auth.otp_max_attempts == 3
    assert cfg2.auth.otp_ttl_minutes == 5


async def test_otp_routes_404_when_disabled(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, _, _ = await _make_client(
        aiohttp_client, tmp_path, _web_config(otp_enabled=False)
    )
    r = await _request_code(client)
    assert r.status == 404
    assert (await r.json())["error"] == "otp_disabled"
    assert captured_codes == []  # nothing sent — routes are inert

    r = await _verify(client, code="123456")
    assert r.status == 404
    assert (await r.json())["error"] == "otp_disabled"


async def test_otp_disabled_uses_default_flag_off(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    # Drive with a config whose auth block leaves otp_enabled at the
    # DATACLASS DEFAULT — if the default ever flips on, this notices at the
    # route surface too (companion to the dataclass pin above).
    cfg = _web_config()
    cfg.auth.otp_enabled = WebAuthConfig().otp_enabled
    client, _, _ = await _make_client(aiohttp_client, tmp_path, cfg)
    assert (await _request_code(client)).status == 404
    assert (await _verify(client, code="123456")).status == 404


# ---------------------------------------------------------------------------
# Issuance — uniform response, rate limit, mailer gates
# ---------------------------------------------------------------------------


async def test_otp_request_known_email_sends_code(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, _, _ = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await _request_code(client)
    assert r.status == 200
    assert (await r.json())["status"] == "sent"
    assert len(captured_codes) == 1
    assert len(captured_codes[0]) == 6 and captured_codes[0].isdigit()


async def test_otp_request_unknown_email_uniform_no_send(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, _, _ = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await _request_code(client, email="nobody@example.com")
    assert r.status == 200
    assert (await r.json())["status"] == "sent"  # uniform — no enumeration
    assert captured_codes == []


async def test_otp_request_email_required(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, _, _ = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await client.post("/auth/otp/request", json={}, headers=_PEER_HEADERS)
    assert r.status == 400
    assert (await r.json())["error"] == "email_required"


async def test_otp_request_mailer_unconfigured_503(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, _, _ = await _make_client(
        aiohttp_client, tmp_path, _web_config(email_configured=False)
    )
    r = await _request_code(client)
    assert r.status == 503
    assert (await r.json())["error"] == "email_not_configured"


async def test_otp_request_requires_peer_token(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, _, _ = await _make_client(aiohttp_client, tmp_path, _web_config())
    r = await client.post(
        "/auth/otp/request", json={"email": "andrew@example.com"}
    )
    assert r.status == 401  # Layer-1 middleware rejects


async def test_otp_request_rate_limited_per_email(
    aiohttp_client, tmp_path, captured_codes, monkeypatch
) -> None:
    limiter = auth_routes_mod._LoginRateLimiter(max_per_email=2, max_global=999)
    monkeypatch.setattr(auth_routes_mod, "_OTP_RATE_LIMITER", limiter)
    client, _, _ = await _make_client(aiohttp_client, tmp_path, _web_config())
    for _ in range(2):
        assert (await _request_code(client)).status == 200
    r = await _request_code(client)
    assert r.status == 429
    assert (await r.json())["error"] == "rate_limited"
    # Uniform for unknown emails too (checked BEFORE the lookup).
    for _ in range(2):
        assert (await _request_code(client, email="junk@nope.com")).status == 200
    assert (await _request_code(client, email="junk@nope.com")).status == 429


async def test_otp_new_request_replaces_old_code(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    # A fresh request invalidates the previous code — only one live code
    # per email, so a stale intercepted code dies on re-request.
    client, _, _ = await _make_client(aiohttp_client, tmp_path, _web_config())
    await _request_code(client)
    await _request_code(client)
    old_code, new_code = captured_codes
    if old_code == new_code:  # 1-in-a-million collision; skip, don't flake
        pytest.skip("random codes collided")
    r = await _verify(client, code=old_code)
    assert r.status == 401
    r = await _verify(client, code=new_code)
    assert r.status == 200


# ---------------------------------------------------------------------------
# Verify — round-trip + (7) token parity with the magic-link session mint
# ---------------------------------------------------------------------------


async def test_otp_verify_roundtrip_and_token_parity(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, _, _ = await _make_client(aiohttp_client, tmp_path, _web_config())
    await _request_code(client)
    r = await _verify(client, code=captured_codes[0])
    assert r.status == 200
    body = await r.json()
    assert body["name"] == "andrew"
    assert body["role"] == "owner"
    assert body["exp"] > 0
    assert body["session_token"]

    # Byte-shape parity with the magic-link mint: same signer/claims/exp
    # scheme — the token decodes through the SAME verify_session_token and
    # carries exactly {t: session, u, r, exp}.
    payload = verify_session_token(
        body["session_token"], secret=DUMMY_WEB_SIGNING_SECRET
    )
    assert payload is not None
    assert payload == {
        "t": "session",
        "u": "andrew",
        "r": "owner",
        "exp": body["exp"],
    }

    # And it drives /chat/* through the same require_web_session path.
    headers = {**_PEER_HEADERS, SESSION_HEADER: body["session_token"]}
    r = await client.post("/chat/open", json={}, headers=headers)
    assert r.status == 200


# ---------------------------------------------------------------------------
# (2) SINGLE-USE BURN
# ---------------------------------------------------------------------------


async def test_otp_verified_code_cannot_be_reused(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, _, _ = await _make_client(aiohttp_client, tmp_path, _web_config())
    await _request_code(client)
    code = captured_codes[0]
    assert (await _verify(client, code=code)).status == 200
    r = await _verify(client, code=code)  # replay of a consumed code
    assert r.status == 401
    assert await r.json() == UNIFORM_401_BODY


# ---------------------------------------------------------------------------
# (3) BRUTE-FORCE CAP
# ---------------------------------------------------------------------------


async def test_otp_cap_burns_code_then_correct_code_rejected(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, _, _ = await _make_client(aiohttp_client, tmp_path, _web_config())
    await _request_code(client)
    code = captured_codes[0]
    wrong = _wrong_code(code)
    # 5 wrong attempts (the default otp_max_attempts) — each uniform 401.
    for _ in range(5):
        r = await _verify(client, code=wrong)
        assert r.status == 401
        assert await r.json() == UNIFORM_401_BODY
    # The cap burned the code: the NEXT attempt with the CORRECT code fails.
    r = await _verify(client, code=code)
    assert r.status == 401
    assert await r.json() == UNIFORM_401_BODY


async def test_otp_correct_code_within_cap_succeeds(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    # MUTATION PIN for the cap: an over-aggressive cap (burning before the
    # 5th attempt) would fail this — 4 wrong tries then the right code wins.
    client, _, _ = await _make_client(aiohttp_client, tmp_path, _web_config())
    await _request_code(client)
    code = captured_codes[0]
    for _ in range(4):
        assert (await _verify(client, code=_wrong_code(code))).status == 401
    assert (await _verify(client, code=code)).status == 200


# ---------------------------------------------------------------------------
# (4) TTL
# ---------------------------------------------------------------------------


async def test_otp_expired_code_rejected(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, auth_state, _ = await _make_client(
        aiohttp_client, tmp_path, _web_config()
    )
    await _request_code(client)
    code = captured_codes[0]
    # Age the stored entry past its TTL (server-side clock authority).
    entry = auth_state.otp_codes["andrew@example.com"]
    entry["exp"] = int(entry["exp"]) - (11 * 60 + 1)
    r = await _verify(client, code=code)
    assert r.status == 401
    assert await r.json() == UNIFORM_401_BODY
    # Expired entry was burned on sight.
    assert "andrew@example.com" not in auth_state.otp_codes


async def test_otp_exp_matches_configured_ttl(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    import time as _time

    client, auth_state, _ = await _make_client(
        aiohttp_client, tmp_path, _web_config(otp_ttl_minutes=10)
    )
    before = _time.time()
    await _request_code(client)
    after = _time.time()
    exp = auth_state.otp_codes["andrew@example.com"]["exp"]
    assert before + 600 - 2 <= exp <= after + 600 + 2


# ---------------------------------------------------------------------------
# (5) HASH-ONLY — raw code never persisted, never logged
# ---------------------------------------------------------------------------


async def test_otp_raw_code_not_in_state_or_logs(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    import structlog

    client, auth_state, state_file = await _make_client(
        aiohttp_client, tmp_path, _web_config()
    )
    with structlog.testing.capture_logs() as captured:
        await _request_code(client)
        code = captured_codes[0]
        r = await _verify(client, code=_wrong_code(code))
        assert r.status == 401

    # Not in the logs — any field of any event (request + failed verify).
    assert code not in str(captured)

    # Not in the in-memory store: the persisted value is an HMAC, not the code.
    # (Re-request so a live entry exists to inspect — the failed verify above
    # left it live at attempts=1.)
    entry = auth_state.otp_codes["andrew@example.com"]
    assert entry["code_hmac"] != code
    assert code not in json.dumps(auth_state.otp_codes)

    # Not in the state FILE on disk (grep the persisted bytes).
    on_disk = state_file.read_text(encoding="utf-8")
    assert code not in on_disk
    assert "code_hmac" in on_disk  # the hash IS persisted (durable across restart)


# ---------------------------------------------------------------------------
# (6) UNIFORM ERROR — every rejection is the identical 401 body
# ---------------------------------------------------------------------------


async def test_otp_all_failures_return_identical_401(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, auth_state, _ = await _make_client(
        aiohttp_client, tmp_path, _web_config()
    )
    await _request_code(client)
    code = captured_codes[0]
    wrong = _wrong_code(code)

    responses = []

    # (a) unknown email — no code was ever issued for it.
    responses.append(await _verify(client, email="nobody@example.com", code=wrong))
    # (b) bad code for a live entry.
    responses.append(await _verify(client, code=wrong))
    # (c) malformed code shape (not ^[0-9]{6}$).
    responses.append(await _verify(client, code="12345"))
    responses.append(await _verify(client, code="abcdef"))
    # (d) expired.
    auth_state.otp_codes["andrew@example.com"]["exp"] = 1
    responses.append(await _verify(client, code=code))
    # (e) exhausted: fresh code, burn it through the cap, then correct code.
    await _request_code(client)
    code2 = captured_codes[1]
    for _ in range(5):
        responses.append(await _verify(client, code=_wrong_code(code2)))
    responses.append(await _verify(client, code=code2))

    for r in responses:
        assert r.status == 401
        assert await r.json() == UNIFORM_401_BODY


# ---------------------------------------------------------------------------
# Removed-user lockout (mirrors the magic-link re-resolve behaviour)
# ---------------------------------------------------------------------------


async def test_otp_verify_rejects_user_removed_after_issue(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    cfg = _web_config()
    client, _, _ = await _make_client(aiohttp_client, tmp_path, cfg)
    await _request_code(client)
    cfg.users.clear()  # user removed from the live allowlist post-issue
    r = await _verify(client, code=captured_codes[0])
    assert r.status == 401
    assert await r.json() == UNIFORM_401_BODY


# ---------------------------------------------------------------------------
# State persistence — burn survives restart; schema tolerance
# ---------------------------------------------------------------------------


async def test_otp_burn_is_durable_across_reload(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    client, _, state_file = await _make_client(
        aiohttp_client, tmp_path, _web_config()
    )
    await _request_code(client)
    assert (await _verify(client, code=captured_codes[0])).status == 200
    # A fresh store loading the persisted file finds NO live code — the
    # single-use burn was saved before the token was minted.
    reloaded = WebAuthState.create(state_file)
    reloaded.load()
    assert reloaded.otp_codes == {}


def test_web_auth_state_load_tolerates_missing_otp_key(tmp_path) -> None:
    # An older state file (no otp_codes key) loads clean — schema tolerance.
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"version": 1, "nonces": {}}), encoding="utf-8")
    st = WebAuthState.create(p)
    st.load()
    assert st.otp_codes == {}
