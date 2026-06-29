"""Tests for ``alfred.web.auth`` — token codec + require_web_session."""

from __future__ import annotations

import structlog
from aiohttp.test_utils import make_mocked_request

from alfred.web.auth import (
    SESSION_HEADER,
    USER_HEADER,
    make_magic_token,
    make_session_token,
    require_web_session,
    resolve_web_identity,
    verify_magic_token,
    verify_session_token,
)
from alfred.web.config import WebAuthConfig, WebConfig, WebUser
from alfred.web.identity import synthetic_chat_id

SECRET = "a-strong-random-test-secret"
NOW = 1_000_000_000.0


# ---------------------------------------------------------------------------
# Token round-trips
# ---------------------------------------------------------------------------


def test_magic_token_roundtrip() -> None:
    token, nonce = make_magic_token("andrew", secret=SECRET, ttl_minutes=15, now=NOW)
    payload = verify_magic_token(token, secret=SECRET, now=NOW + 60)
    assert payload is not None
    assert payload["t"] == "magic"
    assert payload["u"] == "andrew"
    assert payload["n"] == nonce
    assert payload["exp"] == int(NOW + 15 * 60)


def test_session_token_roundtrip() -> None:
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=168, now=NOW)
    payload = verify_session_token(token, secret=SECRET, now=NOW + 60)
    assert payload is not None
    assert payload["t"] == "session"
    assert payload["u"] == "andrew"
    assert payload["r"] == "owner"
    assert payload["exp"] == int(NOW + 168 * 3600)


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_magic_token_expired_rejected() -> None:
    token, _ = make_magic_token("andrew", secret=SECRET, ttl_minutes=15, now=NOW)
    assert verify_magic_token(token, secret=SECRET, now=NOW + 16 * 60) is None


def test_session_token_expired_rejected() -> None:
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=1, now=NOW)
    assert verify_session_token(token, secret=SECRET, now=NOW + 3601) is None


# ---------------------------------------------------------------------------
# Type-confusion guard (magic ↔ session)
# ---------------------------------------------------------------------------


def test_magic_token_not_accepted_as_session() -> None:
    token, _ = make_magic_token("andrew", secret=SECRET, ttl_minutes=15, now=NOW)
    assert verify_session_token(token, secret=SECRET, now=NOW + 60) is None


def test_session_token_not_accepted_as_magic() -> None:
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=1, now=NOW)
    assert verify_magic_token(token, secret=SECRET, now=NOW + 60) is None


# ---------------------------------------------------------------------------
# Signature / structural integrity
# ---------------------------------------------------------------------------


def test_wrong_secret_rejected() -> None:
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=1, now=NOW)
    assert verify_session_token(token, secret="different-secret", now=NOW + 60) is None


def test_tampered_payload_rejected() -> None:
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=1, now=NOW)
    payload_b64, _, sig_b64 = token.partition(".")
    # Swap in a different payload, keep the old signature → mismatch.
    other = make_session_token("ben", "ops", secret=SECRET, ttl_hours=1, now=NOW)
    other_payload_b64 = other.partition(".")[0]
    forged = f"{other_payload_b64}.{sig_b64}"
    assert verify_session_token(forged, secret=SECRET, now=NOW + 60) is None


def test_tampered_signature_rejected() -> None:
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=1, now=NOW)
    payload_b64 = token.partition(".")[0]
    assert verify_session_token(f"{payload_b64}.AAAA", secret=SECRET, now=NOW) is None


def test_malformed_tokens_rejected() -> None:
    for bad in ("", "no-dot", "a.b.c-extra-not-decodable", ".", "x."):
        assert verify_session_token(bad, secret=SECRET, now=NOW) is None
        assert verify_magic_token(bad, secret=SECRET, now=NOW) is None


# ---------------------------------------------------------------------------
# require_web_session — per-request Layer-2 resolution
# ---------------------------------------------------------------------------


def _web_config(secret: str = SECRET, users=None) -> WebConfig:
    return WebConfig(
        enabled=True,
        users=users if users is not None else [WebUser(name="andrew", role="owner")],
        auth=WebAuthConfig(session_secret=secret),
    )


def _req_with_token(token: str | None):
    headers = {SESSION_HEADER: token} if token is not None else {}
    return make_mocked_request("GET", "/chat/history/x", headers=headers)


def test_require_web_session_valid() -> None:
    cfg = _web_config()
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=168)
    ident = require_web_session(_req_with_token(token), cfg)
    assert ident is not None
    assert ident.user == "andrew"
    assert ident.role == "owner"
    assert ident.synthetic_chat_id == synthetic_chat_id("andrew")


def test_require_web_session_no_header() -> None:
    assert require_web_session(_req_with_token(None), _web_config()) is None


def test_require_web_session_bad_token() -> None:
    assert require_web_session(_req_with_token("garbage"), _web_config()) is None


def test_require_web_session_expired() -> None:
    cfg = _web_config()
    token = make_session_token(
        "andrew", "owner", secret=SECRET, ttl_hours=1, now=NOW - 10_000
    )
    assert require_web_session(_req_with_token(token), cfg) is None


def test_require_web_session_removed_user_locked_out() -> None:
    # Valid token, but the user is no longer in the allowlist → None.
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=168)
    cfg_without_andrew = _web_config(users=[WebUser(name="ben", role="ops")])
    assert require_web_session(_req_with_token(token), cfg_without_andrew) is None


def test_require_web_session_role_resolved_from_live_config() -> None:
    # Token says owner, config now says ops → live config wins.
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=168)
    cfg = _web_config(users=[WebUser(name="andrew", role="ops")])
    ident = require_web_session(_req_with_token(token), cfg)
    assert ident is not None
    assert ident.role == "ops"


def test_require_web_session_unresolved_secret_fails_closed_and_logs() -> None:
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=168)
    cfg = _web_config(secret="")  # unconfigured → resolve_signing_secret raises
    with structlog.testing.capture_logs() as captured:
        ident = require_web_session(_req_with_token(token), cfg)
    assert ident is None
    events = [c["event"] for c in captured]
    assert "web.auth.session_secret_unresolved_at_request" in events


# ---------------------------------------------------------------------------
# resolve_web_identity — mode-aware dispatcher (session vs relay)
# ---------------------------------------------------------------------------


def _relay_config(users=None) -> WebConfig:
    return WebConfig(
        enabled=True,
        users=users if users is not None else [WebUser(name="andrew", role="owner")],
        # Relay mode carries NO session_secret (no token minting).
        auth=WebAuthConfig(mode="relay", session_secret=""),
    )


def _req_with_user(name: str | None, *, peer: str = "web"):
    """Build a relay request. ``peer`` is the matched transport peer NAME
    that ``auth_middleware`` would have stashed; the relay path pins it to
    the chat ``web`` peer."""
    headers = {USER_HEADER: name} if name is not None else {}
    req = make_mocked_request("POST", "/chat/turn", headers=headers)
    req["transport_peer"] = peer
    return req


def test_resolve_web_identity_session_mode_uses_session_token() -> None:
    # mode=session (default) → the existing X-Alfred-Session path, unchanged.
    cfg = _web_config()  # mode defaults to "session"
    assert cfg.auth.mode == "session"
    token = make_session_token("andrew", "owner", secret=SECRET, ttl_hours=168)
    ident = resolve_web_identity(_req_with_token(token), cfg)
    assert ident is not None
    assert ident.user == "andrew"
    assert ident.role == "owner"
    assert ident.synthetic_chat_id == synthetic_chat_id("andrew")


def test_resolve_web_identity_session_mode_ignores_user_header() -> None:
    # In session mode an X-Alfred-User header must NOT authenticate — only
    # the signed session token does. A user header without a token → None.
    cfg = _web_config()
    req = make_mocked_request("POST", "/chat/turn", headers={USER_HEADER: "andrew"})
    assert resolve_web_identity(req, cfg) is None


def test_resolve_web_identity_relay_valid_user() -> None:
    cfg = _relay_config()
    ident = resolve_web_identity(_req_with_user("andrew"), cfg)
    assert ident is not None
    assert ident.user == "andrew"
    assert ident.role == "owner"
    assert ident.synthetic_chat_id == synthetic_chat_id("andrew")


def test_resolve_web_identity_relay_resolves_role_from_target_config() -> None:
    # The BFF asserts NAME only; the TARGET decides the role from its own
    # web.users (non-escalating). Andrew is "ops" on this target.
    cfg = _relay_config(users=[WebUser(name="andrew", role="ops")])
    ident = resolve_web_identity(_req_with_user("andrew"), cfg)
    assert ident is not None
    assert ident.role == "ops"


def test_resolve_web_identity_relay_missing_user_fails_closed_and_logs() -> None:
    cfg = _relay_config()
    with structlog.testing.capture_logs() as captured:
        ident = resolve_web_identity(_req_with_user(None), cfg)
    assert ident is None
    events = [c["event"] for c in captured]
    assert "web.auth.relay_user_missing" in events


def test_resolve_web_identity_relay_empty_user_fails_closed() -> None:
    cfg = _relay_config()
    assert resolve_web_identity(_req_with_user("   "), cfg) is None


def test_resolve_web_identity_relay_unknown_user_fails_closed_and_logs() -> None:
    cfg = _relay_config()
    with structlog.testing.capture_logs() as captured:
        ident = resolve_web_identity(_req_with_user("stranger"), cfg)
    assert ident is None
    events = [c["event"] for c in captured]
    assert "web.auth.relay_user_unknown" in events


def test_resolve_web_identity_relay_needs_no_signing_secret() -> None:
    # The relay path must never touch resolve_signing_secret — a relay
    # instance with an empty secret still authenticates a known user.
    cfg = _relay_config()  # session_secret=""
    ident = resolve_web_identity(_req_with_user("andrew"), cfg)
    assert ident is not None


def test_resolve_web_identity_relay_wrong_peer_fails_closed_and_logs() -> None:
    # WARN-1 regression pin: a request that cleared Layer 1 as the
    # ``web_ingest`` peer (e.g. the ingest token + X-Alfred-Client: web)
    # must NOT drive a chat turn even with a valid known X-Alfred-User —
    # the peer-pin rejects it before identity resolution.
    cfg = _relay_config()
    with structlog.testing.capture_logs() as captured:
        ident = resolve_web_identity(
            _req_with_user("andrew", peer="web_ingest"), cfg
        )
    assert ident is None
    events = [c["event"] for c in captured]
    assert "web.auth.relay_wrong_peer" in events


def test_resolve_web_identity_relay_missing_peer_fails_closed() -> None:
    # No transport_peer at all (would only happen off the auth_middleware
    # path) → fail-closed.
    cfg = _relay_config()
    assert resolve_web_identity(_req_with_user("andrew", peer=""), cfg) is None
