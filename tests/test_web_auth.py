"""Tests for ``alfred.web.auth`` — token codec + require_web_session."""

from __future__ import annotations

import structlog
from aiohttp.test_utils import make_mocked_request

from alfred.web.auth import (
    SESSION_HEADER,
    make_magic_token,
    make_session_token,
    require_web_session,
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
