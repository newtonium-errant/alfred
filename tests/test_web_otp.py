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
from alfred.web.auth import SESSION_HEADER, hash_otp_email, verify_session_token
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
    otp_email_lockout_max_failures: int = 20,
    otp_email_lockout_window_minutes: int = 15,
    otp_email_lockout_cooldown_minutes: int = 15,
    email_configured: bool = True,
    users: list | None = None,
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
        users=(
            users
            if users is not None
            else [WebUser(name="andrew", role="owner", email="andrew@example.com")]
        ),
        auth=WebAuthConfig(
            session_secret=DUMMY_WEB_SIGNING_SECRET,
            magic_link_ttl_minutes=15,
            session_ttl_hours=168,
            base_url="https://salem.example.com",
            otp_enabled=otp_enabled,
            otp_max_attempts=otp_max_attempts,
            otp_ttl_minutes=otp_ttl_minutes,
            otp_email_lockout_max_failures=otp_email_lockout_max_failures,
            otp_email_lockout_window_minutes=otp_email_lockout_window_minutes,
            otp_email_lockout_cooldown_minutes=otp_email_lockout_cooldown_minutes,
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


async def test_otp_mint_role_is_live_resolved_not_hardcoded(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    """G1: the minted session token's role is LIVE-resolved from the user's
    config entry (identity.role), NOT hardcoded to owner. A NON-owner user must
    get a NON-owner token — the parity test above can't catch a hardcoded-owner
    mint because its fixture only has an owner. Guards the ``agent=salem``
    hardcoding-drift class on this token-minting surface (a role-escalation gap).
    """
    cfg = _web_config(
        users=[WebUser(name="member1", role="member", email="member@example.com")]
    )
    client, _, _ = await _make_client(aiohttp_client, tmp_path, cfg)
    await _request_code(client, email="member@example.com")
    r = await _verify(client, email="member@example.com", code=captured_codes[0])
    assert r.status == 200
    body = await r.json()
    assert body["role"] == "member"  # response role reflects the live entry

    # LOAD-BEARING: the TOKEN's embedded role (``r``) is the credential — a
    # hardcoded mint role would encode owner-scope for this limited user.
    payload = verify_session_token(
        body["session_token"], secret=DUMMY_WEB_SIGNING_SECRET
    )
    assert payload is not None
    assert payload["r"] == "member"
    assert payload["u"] == "member1"


async def test_otp_token_exp_matches_session_ttl(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    """G2: the OTP session token's ``exp`` ≈ now + ``session_ttl_hours`` (parity
    with the magic-link mint), not a hardcoded/short TTL. The parity test only
    asserts ``exp > 0``, which a mutated ``ttl_hours=1`` would still pass."""
    import time

    cfg = _web_config()  # session_ttl_hours=168
    client, _, _ = await _make_client(aiohttp_client, tmp_path, cfg)
    await _request_code(client)
    before = int(time.time())
    r = await _verify(client, code=captured_codes[0])
    after = int(time.time())
    assert r.status == 200
    payload = verify_session_token(
        (await r.json())["session_token"], secret=DUMMY_WEB_SIGNING_SECRET
    )
    assert payload is not None
    ttl_seconds = 168 * 3600
    exp = int(payload["exp"])
    # exp == mint_time + ttl, and mint_time ∈ [before, after]. Tight tolerance.
    assert before + ttl_seconds - 2 <= exp <= after + ttl_seconds + 2


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


# ===========================================================================
# #38 — IP-INDEPENDENT per-email failed-verify lockout (brute-force ceiling)
#
# Closes the ONE low-sev gap #23 flagged: the per-code 5-attempt cap +
# (client_ip, email) issuance limiter are bypassable by IP rotation (rotate
# client_ip → dodge the per-email issuance cap → ~100 guesses/15min vs 1e6).
# The lockout counter is keyed on the NORMALIZED EMAIL ONLY (never client_ip)
# so IP rotation cannot widen the guess budget. It is layered ON TOP of the
# #23 controls (which stay green above) — never replacing them.
# ===========================================================================

_EMAIL_HASH = hash_otp_email("andrew@example.com", secret=DUMMY_WEB_SIGNING_SECRET)


def test_otp_email_lockout_defaults_dataclass() -> None:
    # MUTATION PIN: the conservative defaults. 20 sits far above a real user's
    # fumble (bounded by the per-code cap of 5) and far below the ~100/15min
    # IP-rotation ceiling. Windows default to 15 minutes.
    c = WebAuthConfig()
    assert c.otp_email_lockout_max_failures == 20
    assert c.otp_email_lockout_window_minutes == 15
    assert c.otp_email_lockout_cooldown_minutes == 15


def test_otp_email_lockout_config_from_unified() -> None:
    # An operator YAML that never mentions the knobs loads the defaults; and
    # the knobs parse when present.
    default = load_from_unified({"web": {"auth": {"session_secret": "s"}}})
    assert default.auth.otp_email_lockout_max_failures == 20
    cfg = load_from_unified(
        {
            "web": {
                "auth": {
                    "otp_email_lockout_max_failures": 7,
                    "otp_email_lockout_window_minutes": 3,
                    "otp_email_lockout_cooldown_minutes": 9,
                }
            }
        }
    )
    assert cfg.auth.otp_email_lockout_max_failures == 7
    assert cfg.auth.otp_email_lockout_window_minutes == 3
    assert cfg.auth.otp_email_lockout_cooldown_minutes == 9


async def test_otp_email_lockout_closes_ip_rotation_bypass(
    aiohttp_client, tmp_path, captured_codes, monkeypatch
) -> None:
    """THE #38 point — the failed-verify ceiling is IP-INDEPENDENT.

    Faithful to the REAL attack (#38 fix — count-only-live-code-failures): the
    attacker submits WRONG codes against a LIVE outstanding code for ONE target,
    each guess from a DIFFERENT client_ip (rotating IP to dodge the
    ``(client_ip, email)`` issuance limiter, which is the bypass #38 closes).
    The per-code cap (5) burns a code, so the attacker re-requests — every
    wrong-guess-against-a-live-code counts against the IP-INDEPENDENT email
    ceiling regardless of IP. After the threshold the email locks, and a fresh
    CORRECT code from yet another IP is still rejected.

    (Pre-fix this test drove no-live-code guesses; post-fix those correctly
    record nothing — see the flood-eviction pin — so it must drive live-code
    failures to be a faithful attack.)

    MUTATION PIN: fold ``client_ip`` into ``record_otp_email_failure``'s key
    and this goes RED — the failures below are spread one-per-IP, so a
    per-(ip,email) counter never reaches the threshold (``otp_email_locked``
    on the pure-email hash stays False), and the correct code at the end would
    (wrongly) succeed (200 != 401).
    """
    import itertools

    ip_seq = itertools.count()
    monkeypatch.setattr(
        auth_routes_mod, "_client_ip", lambda request: f"10.0.0.{next(ip_seq)}"
    )
    cfg = _web_config(otp_email_lockout_max_failures=8)  # per-code cap stays 5
    client, auth_state, _ = await _make_client(aiohttp_client, tmp_path, cfg)

    # 8 wrong-code-against-a-LIVE-code failures for ONE email, each from a
    # DIFFERENT client_ip. Keep a live code in front of every guess: the
    # per-code cap of 5 burns a code, so re-request when it's gone (IP rotation
    # dodges the issuance limiter — exactly the bypass being closed).
    live = ""
    for _ in range(8):
        if auth_state.peek_otp("andrew@example.com") is None:
            await _request_code(client)
            live = captured_codes[-1]
        assert (await _verify(client, code=_wrong_code(live))).status == 401
    # The IP-independent per-email counter reached the threshold and locked,
    # even though every failure came from a distinct IP.
    assert auth_state.otp_email_locked(_EMAIL_HASH)

    # Locked regardless of IP: issue a fresh, LIVE code and present the CORRECT
    # value from yet another IP — still the uniform 401.
    await _request_code(client)
    correct = captured_codes[-1]
    r = await _verify(client, code=correct)
    assert r.status == 401
    assert await r.json() == UNIFORM_401_BODY


async def test_otp_locked_email_verify_indistinguishable_from_wrong_code(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    # NO ENUMERATION ORACLE: a locked-out email's verify response is
    # byte-identical to an ordinary wrong-code 401, and issuance stays uniform.
    cfg = _web_config(otp_email_lockout_max_failures=5)
    client, _, _ = await _make_client(aiohttp_client, tmp_path, cfg)

    # Reference shape: an ordinary wrong-code 401 against a live code (this is
    # also email failure #1).
    await _request_code(client)
    code = captured_codes[0]
    ref = await _verify(client, code=_wrong_code(code))
    ref_pair = (ref.status, await ref.json())
    assert ref_pair == (401, UNIFORM_401_BODY)

    # Drive to the threshold (4 more → count 5 → locked).
    for _ in range(4):
        assert (await _verify(client, code=_wrong_code(code))).status == 401

    # A verify against the now-locked email is byte-identical to the wrong-code
    # reference (status AND body) — no locked-vs-not oracle. Correct code too.
    locked = await _verify(client, code=code)
    assert (locked.status, await locked.json()) == ref_pair

    # Issuance for a locked email stays the uniform {status: sent}.
    req = await _request_code(client)
    assert req.status == 200
    assert (await req.json())["status"] == "sent"


async def test_otp_no_false_lockout_for_legit_fumble(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    # NO FALSE LOCKOUT: a real user who fumbles 1-2 codes then types the right
    # one succeeds and is NOT locked (well under the default-20 threshold).
    client, auth_state, _ = await _make_client(
        aiohttp_client, tmp_path, _web_config()
    )
    await _request_code(client)
    code = captured_codes[0]
    assert (await _verify(client, code=_wrong_code(code))).status == 401
    assert (await _verify(client, code=_wrong_code(code))).status == 401
    assert (await _verify(client, code=code)).status == 200
    assert not auth_state.otp_email_locked(_EMAIL_HASH)
    assert _EMAIL_HASH not in auth_state.otp_email_failures  # cleared on success


async def test_otp_lockout_self_heals_after_cooldown(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    # SELF-HEAL: after the window/cooldown elapses the email can verify again.
    cfg = _web_config(otp_email_lockout_max_failures=5)
    client, auth_state, _ = await _make_client(aiohttp_client, tmp_path, cfg)

    # Drive to lockout with WRONG guesses against a LIVE code (post-fix, only
    # live-code failures count). The per-code cap is 5, so 5 wrong guesses both
    # burn the code AND reach the email threshold.
    await _request_code(client)
    wrong = _wrong_code(captured_codes[0])
    for _ in range(5):
        assert (await _verify(client, code=wrong)).status == 401
    assert auth_state.otp_email_locked(_EMAIL_HASH)

    # A live, correct code is still rejected while locked (lock gate is BEFORE
    # peek, so the code is never touched/burned).
    await _request_code(client)
    code = captured_codes[-1]
    assert (await _verify(client, code=code)).status == 401

    # Simulate the cooldown + window elapsing (server-side clock authority).
    entry = auth_state.otp_email_failures[_EMAIL_HASH]
    entry["locked_until"] = 1  # in the past
    entry["window_start"] = 1  # window elapsed → fresh budget
    assert not auth_state.otp_email_locked(_EMAIL_HASH)

    # The SAME live code now verifies — the lockout self-healed.
    r = await _verify(client, code=code)
    assert r.status == 200


async def test_otp_success_resets_email_failure_counter(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    # RESET-ON-SUCCESS: a successful verify clears the email's failure counter.
    cfg = _web_config(otp_email_lockout_max_failures=5)
    client, auth_state, _ = await _make_client(aiohttp_client, tmp_path, cfg)

    await _request_code(client)
    code = captured_codes[0]
    # Three wrong guesses (per-code cap is 5, so the code stays live).
    for _ in range(3):
        assert (await _verify(client, code=_wrong_code(code))).status == 401
    assert int(auth_state.otp_email_failures[_EMAIL_HASH]["count"]) == 3

    # A correct code clears the counter entirely (fresh budget).
    assert (await _verify(client, code=code)).status == 200
    assert _EMAIL_HASH not in auth_state.otp_email_failures


async def test_otp_email_lockout_durable_across_reload(
    aiohttp_client, tmp_path, captured_codes
) -> None:
    # The lock is PERSISTED — a daemon restart must not grant a fresh guess
    # budget (an attacker can't reset the lock by triggering a restart).
    cfg = _web_config(otp_email_lockout_max_failures=5)
    client, _, state_file = await _make_client(aiohttp_client, tmp_path, cfg)
    # Drive to lockout with WRONG guesses against a LIVE code (post-fix, only
    # live-code failures count); 5 wrong == per-code cap == email threshold.
    await _request_code(client)
    wrong = _wrong_code(captured_codes[0])
    for _ in range(5):
        assert (await _verify(client, code=wrong)).status == 401
    reloaded = WebAuthState.create(state_file)
    reloaded.load()
    assert reloaded.otp_email_locked(_EMAIL_HASH)


async def test_otp_flood_of_no_live_code_emails_cannot_evict_real_target_lockout(
    aiohttp_client, tmp_path, captured_codes, monkeypatch
) -> None:
    """FLOOD-EVICTION REGRESSION PIN — the load-bearing #38-fix guard.

    THE DEFECT (pre-fix): ``otp_email_failures`` was recorded on the no-live-code
    verify path, so a caller with the BFF peer token could drive
    /auth/otp/verify for arbitrarily many DISTINCT junk emails (each a fresh
    no-live-code failure → a new entry). Flooding > the bounded store's cap
    evicted the entry with the oldest activity — and a real ACCUMULATING target
    (``window_start`` in the past, ``locked_until`` == 0) is precisely the
    "oldest" shape, so it was the eviction victim on every insert → its counter
    reset before reaching the threshold → it NEVER locked → the IP-rotation
    bypass re-opened via email-flooding. (/auth/otp/verify has no other verify-
    side rate limiter, so this per-email lockout is the SOLE ceiling.)

    THE FIX: record a per-email failure ONLY when a LIVE code was present, so a
    no-live-code guess creates ZERO entries and the store holds only emails that
    were actually issued a code (⊆ allowlisted operators) — structurally
    un-floodable.

    This pin reproduces the exact attack shape and asserts the fix:
      (1) the target is driven partway to lockout with genuine live-code
          failures (the accumulating "oldest" victim shape);
      (2) a flood of distinct junk no-live-code emails (>> a shrunk store cap)
          creates ZERO new entries — the store still holds ONLY the target;
      (3) one more genuine live-code failure tips the target over the threshold
          → it LOCKS despite the flood.

    MUTATION-VERIFY (performed out-of-band, then reverted): restore
    ``record_otp_email_failure`` on the no-live-code path in routes_auth (~:615)
    and this goes RED — the flood fills the shrunk store (assertion 2 fails: the
    key set is no longer just the target) AND evicts the accumulating target
    (whose ``window_start`` is oldest), so the final failure resets it to
    count=1 and it never locks (assertion 3 fails).
    """
    import alfred.web.state as state_mod

    # Shrink the bounded store so a small, fast flood exceeds it — the eviction
    # dynamic that the mutation would exploit is then reproducible in-test.
    monkeypatch.setattr(state_mod, "_MAX_EMAIL_FAILURE_KEYS", 8)

    cfg = _web_config(otp_email_lockout_max_failures=5, otp_max_attempts=5)
    client, auth_state, _ = await _make_client(aiohttp_client, tmp_path, cfg)

    # (1) Drive the REAL target most of the way to lockout with genuine
    # wrong-code-against-a-LIVE-code failures. Its counter is now the
    # "accumulating" eviction-victim shape: window_start in the past,
    # locked_until == 0, count just under threshold. The code stays live
    # (4 < per-code cap of 5).
    await _request_code(client)  # target andrew@example.com (allowlisted)
    wrong = _wrong_code(captured_codes[0])
    for _ in range(4):
        assert (await _verify(client, code=wrong)).status == 401
    assert int(auth_state.otp_email_failures[_EMAIL_HASH]["count"]) == 4
    assert not auth_state.otp_email_locked(_EMAIL_HASH)

    # (2) FLOOD: 40 distinct junk emails (>> the shrunk store cap of 8), each
    # with NO outstanding code → each hits the no-live-code path. Post-fix each
    # records NOTHING, so the store never grows and the target is never at risk
    # of eviction. Every response is the same uniform 401 (no oracle).
    for i in range(40):
        r = await _verify(client, email=f"junk{i}@nope.com", code="000000")
        assert r.status == 401
        assert await r.json() == UNIFORM_401_BODY
    # The store holds ONLY the real target — the flood created zero entries.
    assert set(auth_state.otp_email_failures) == {_EMAIL_HASH}
    assert int(auth_state.otp_email_failures[_EMAIL_HASH]["count"]) == 4

    # (3) One more genuine live-code failure tips the target over the threshold
    # → it LOCKS. Pre-fix the flood would have evicted this counter (resetting
    # it to count=1), so it would NEVER reach the threshold.
    assert (await _verify(client, code=wrong)).status == 401
    assert auth_state.otp_email_locked(_EMAIL_HASH)


def test_web_auth_state_load_tolerates_missing_otp_email_failures_key(
    tmp_path,
) -> None:
    # A pre-#38 state file (no otp_email_failures key) loads clean → empty.
    p = tmp_path / "s.json"
    p.write_text(
        json.dumps({"version": 1, "nonces": {}, "otp_codes": {}}),
        encoding="utf-8",
    )
    st = WebAuthState.create(p)
    st.load()
    assert st.otp_email_failures == {}
