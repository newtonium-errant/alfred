"""Tests for ``alfred.web.identity`` — synthetic session id + collision guard.

Covers: the reserved-band bounds (above Telegram's 2**52 ceiling, below
the 2**53 safe-int ceiling), determinism + case-insensitivity, allowlist
resolution, and the fail-loud wire-time collision guard (pairwise +
Telegram-disjoint).
"""

from __future__ import annotations

import pytest

from alfred.web import identity as identity_mod
from alfred.web.config import WebConfig, WebUser
from alfred.web.identity import (
    WEB_USER_ID_BASE,
    WEB_USER_ID_SPAN,
    WebIdentity,
    check_synthetic_id_collisions,
    resolve_identity_from_name,
    synthetic_chat_id,
)

TELEGRAM_ID_CEILING = 2 ** 52  # Bot API: chat ids fit in <= 52 significant bits
SAFE_INT_CEILING = 2 ** 53     # IEEE-754 safe-integer ceiling


def test_band_constants_sit_above_telegram_below_safe_int() -> None:
    assert WEB_USER_ID_BASE > TELEGRAM_ID_CEILING
    assert WEB_USER_ID_BASE + WEB_USER_ID_SPAN < SAFE_INT_CEILING


@pytest.mark.parametrize(
    "name", ["andrew", "ben", "pat", "Some Very Long Name", "x", "用户"]
)
def test_synthetic_id_in_reserved_band(name: str) -> None:
    sid = synthetic_chat_id(name)
    assert TELEGRAM_ID_CEILING < sid < SAFE_INT_CEILING
    assert WEB_USER_ID_BASE <= sid < WEB_USER_ID_BASE + WEB_USER_ID_SPAN


def test_synthetic_id_is_deterministic() -> None:
    assert synthetic_chat_id("andrew") == synthetic_chat_id("andrew")


def test_synthetic_id_case_and_whitespace_insensitive() -> None:
    assert synthetic_chat_id("Andrew") == synthetic_chat_id("andrew")
    assert synthetic_chat_id("  andrew  ") == synthetic_chat_id("andrew")


def test_distinct_names_distinct_ids() -> None:
    ids = {synthetic_chat_id(n) for n in ("andrew", "ben", "pat")}
    assert len(ids) == 3


def test_resolve_identity_hit() -> None:
    cfg = WebConfig(
        enabled=True,
        users=[WebUser(name="Andrew", role="owner", email="a@e.com")],
    )
    # Case-insensitive match against the allowlist.
    ident = resolve_identity_from_name(cfg, "andrew")
    assert ident == WebIdentity(
        user="Andrew",
        role="owner",
        synthetic_chat_id=synthetic_chat_id("Andrew"),
    )


def test_resolve_identity_miss_and_empty() -> None:
    cfg = WebConfig(enabled=True, users=[WebUser(name="andrew", role="owner")])
    assert resolve_identity_from_name(cfg, "nobody") is None
    assert resolve_identity_from_name(cfg, None) is None
    assert resolve_identity_from_name(cfg, "   ") is None


def test_collision_guard_clean_returns_mapping() -> None:
    users = [WebUser(name="andrew"), WebUser(name="ben"), WebUser(name="pat")]
    mapping = check_synthetic_id_collisions(users, allowed_user_ids=[1, 2, 3])
    assert set(mapping) == {"andrew", "ben", "pat"}
    assert all(TELEGRAM_ID_CEILING < v < SAFE_INT_CEILING for v in mapping.values())


def test_collision_guard_raises_on_telegram_overlap() -> None:
    users = [WebUser(name="andrew")]
    sid = synthetic_chat_id("andrew")
    with pytest.raises(ValueError, match="allowed_users id"):
        check_synthetic_id_collisions(users, allowed_user_ids=[sid])


def test_collision_guard_raises_on_pairwise(monkeypatch) -> None:
    # Force a hash collision by stubbing synthetic_chat_id to a constant —
    # the (astronomically unlikely) pairwise case must fail loud.
    monkeypatch.setattr(identity_mod, "synthetic_chat_id", lambda _name: 42)
    users = [WebUser(name="andrew"), WebUser(name="ben")]
    with pytest.raises(ValueError, match="collision"):
        check_synthetic_id_collisions(users, allowed_user_ids=[])


def test_collision_guard_empty_users_is_clean() -> None:
    assert check_synthetic_id_collisions([], allowed_user_ids=[1, 2]) == {}
