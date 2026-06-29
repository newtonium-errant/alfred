"""Tests for ``alfred.web.config`` — the ``web:`` section loader.

Covers: disabled-by-default, ``${VAR}`` substitution, the schema-tolerance
filter (unknown keys dropped), nameless/malformed user dropping, and the
hand-rolled nested ``auth`` / ``email`` construction that deliberately
sidesteps the shared ``_build`` collision footgun.
"""

from __future__ import annotations

import pytest

from alfred.web.config import (
    WebAuthConfig,
    WebConfig,
    WebEmailConfig,
    WebUser,
    _is_unresolved,
    load_from_unified,
    resolve_signing_secret,
)


def test_absent_web_block_is_disabled_default() -> None:
    cfg = load_from_unified({})
    assert isinstance(cfg, WebConfig)
    assert cfg.enabled is False
    assert cfg.users == []
    # Nested blocks default to their own dataclass defaults.
    assert isinstance(cfg.auth, WebAuthConfig)
    assert isinstance(cfg.email, WebEmailConfig)
    assert cfg.auth.session_ttl_hours == 168
    assert cfg.email.provider == "resend"


def test_non_dict_web_section_is_tolerated() -> None:
    # A scalar / list in the ``web`` slot must not crash the loader.
    cfg = load_from_unified({"web": "nonsense"})
    assert cfg.enabled is False
    assert cfg.users == []


def test_basic_users_and_roles() -> None:
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "users": [
                    {"name": "andrew", "role": "owner", "email": "a@example.com"},
                    {"name": "ben", "role": "ops", "email": "b@example.com"},
                ],
            }
        }
    )
    assert cfg.enabled is True
    assert cfg.users == [
        WebUser(name="andrew", role="owner", email="a@example.com"),
        WebUser(name="ben", role="ops", email="b@example.com"),
    ]


def test_role_defaults_to_owner_when_omitted() -> None:
    cfg = load_from_unified(
        {"web": {"enabled": True, "users": [{"name": "andrew"}]}}
    )
    assert cfg.users[0].role == "owner"
    assert cfg.users[0].email == ""


def test_nameless_and_malformed_users_dropped() -> None:
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "users": [
                    {"role": "owner"},          # no name → dropped
                    {"name": "   "},            # blank name → dropped
                    "not-a-dict",               # non-dict → dropped
                    {"name": "real", "role": "ops"},
                ],
            }
        }
    )
    assert [u.name for u in cfg.users] == ["real"]


def test_users_not_a_list_yields_empty() -> None:
    cfg = load_from_unified(
        {"web": {"enabled": True, "users": {"name": "andrew"}}}
    )
    assert cfg.users == []


def test_env_substitution(monkeypatch) -> None:
    monkeypatch.setenv("TEST_WEB_SECRET", "s3cr3t-from-env")
    monkeypatch.setenv("TEST_WEB_BASE", "https://salem.example.com")
    monkeypatch.setenv("TEST_RESEND_KEY", "DUMMY_RESEND_TEST_KEY")
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "users": [{"name": "andrew"}],
                "auth": {
                    "session_secret": "${TEST_WEB_SECRET}",
                    "base_url": "${TEST_WEB_BASE}",
                },
                "email": {"api_key": "${TEST_RESEND_KEY}"},
            }
        }
    )
    assert cfg.auth.session_secret == "s3cr3t-from-env"
    assert cfg.auth.base_url == "https://salem.example.com"
    assert cfg.email.api_key == "DUMMY_RESEND_TEST_KEY"


def test_unset_env_var_left_literal() -> None:
    # An unset ${VAR} stays as its literal text (visible-missing, not blank).
    cfg = load_from_unified(
        {"web": {"enabled": True, "auth": {"session_secret": "${DEFINITELY_UNSET_WEB_VAR}"}}}
    )
    assert cfg.auth.session_secret == "${DEFINITELY_UNSET_WEB_VAR}"


def test_emptied_env_var_resolves_to_literal(monkeypatch) -> None:
    """An env var set to "" coalesces to the literal ${VAR} (canonical _env).

    This is the reconciled semantic the WARN fix brought in: an operator
    who EMPTIES the secret to break auth gets the same fail-loud-able
    literal placeholder as one who never set it (NOT a silent empty string).
    """
    monkeypatch.setenv("TEST_WEB_EMPTY_SECRET", "")
    cfg = load_from_unified(
        {"web": {"enabled": True, "auth": {"session_secret": "${TEST_WEB_EMPTY_SECRET}"}}}
    )
    assert cfg.auth.session_secret == "${TEST_WEB_EMPTY_SECRET}"


def test_is_unresolved_predicate() -> None:
    assert _is_unresolved("") is True
    assert _is_unresolved(None) is True
    assert _is_unresolved("${ALFRED_WEB_SESSION_SECRET}") is True
    assert _is_unresolved("a-real-secret") is False


def test_resolve_signing_secret_returns_valid() -> None:
    auth = WebAuthConfig(session_secret="a-strong-random-secret")
    assert resolve_signing_secret(auth) == "a-strong-random-secret"


def test_resolve_signing_secret_fails_loud_on_empty() -> None:
    with pytest.raises(ValueError, match="session_secret"):
        resolve_signing_secret(WebAuthConfig(session_secret=""))


def test_resolve_signing_secret_fails_loud_on_unresolved_placeholder() -> None:
    # An emptied/absent ALFRED_WEB_SESSION_SECRET arrives here as a literal
    # ${...} placeholder — MUST trip the guard, never HMAC-sign with it.
    with pytest.raises(ValueError, match="unresolved"):
        resolve_signing_secret(
            WebAuthConfig(session_secret="${ALFRED_WEB_SESSION_SECRET}")
        )


def test_auth_email_schema_tolerance_unknown_keys_dropped() -> None:
    # Hand-rolled construction must drop unknown nested keys, not crash.
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "auth": {
                    "session_secret": "x",
                    "session_ttl_hours": 24,
                    "future_unknown_field": "ignored",
                },
                "email": {
                    "provider": "resend",
                    "api_key": "k",
                    "from_address": "f@e.com",
                    "another_future_field": 123,
                },
            }
        }
    )
    assert cfg.auth.session_secret == "x"
    assert cfg.auth.session_ttl_hours == 24
    assert cfg.email.api_key == "k"
    assert cfg.email.from_address == "f@e.com"
    assert not hasattr(cfg.auth, "future_unknown_field")


def test_state_key_in_web_block_does_not_misdispatch() -> None:
    """The ``_build`` collision footgun is sidestepped by hand-rolling.

    ``state`` is a key mapped to other dataclasses in sibling config
    modules' ``_DATACLASS_MAP``. A stray ``state`` key under ``web`` must
    be harmlessly ignored — never built into a foreign dataclass.
    """
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "users": [{"name": "andrew"}],
                "state": {"path": "./data/should_be_ignored.json"},
                "auth": {"session_secret": "x"},
            }
        }
    )
    assert cfg.enabled is True
    assert cfg.auth.session_secret == "x"
    assert not hasattr(cfg, "state")


def test_int_coercion_for_ttl_fields() -> None:
    cfg = load_from_unified(
        {
            "web": {
                "enabled": True,
                "auth": {
                    "session_ttl_hours": "72",        # str → int
                    "magic_link_ttl_minutes": "bad",  # invalid → default
                },
            }
        }
    )
    assert cfg.auth.session_ttl_hours == 72
    assert cfg.auth.magic_link_ttl_minutes == 15  # default fallback
