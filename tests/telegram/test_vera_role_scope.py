"""VERA MVP — role-aware allowlist + scope routing (2026-06-09).

VERA is the first multi-user instance. This file pins the Layer 1 (role
gate) + keystone (role-aware ``resolve_scope``) contract:

    * ``AllowedUser`` + the union allowlist loader — bare ints (flat-list
      instances) normalize to role ``owner``; dict entries carry their
      role; back-compat for Salem / KAL-LE / Hypatia is preserved.
    * ``resolve_scope(tool_set, role)`` — vera-only branch; every other
      tool_set is role-independent and unchanged.
    * ``_role_for`` / ``_require_owner`` — role resolution + owner gate.
    * The ``_execute_tool`` dispatcher end-to-end: a ``vera`` tool_set
      with role=owner routes to the ``vera`` scope (ticket + note OK);
      role=ops routes to ``vera_ops`` (ticket OK, note denied).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from alfred.telegram import bot, conversation
from alfred.telegram.config import (
    AllowedUser,
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
    load_from_unified,
)
from alfred.telegram.session import Session
from alfred.telegram.state import StateManager
from alfred.vault.scope import SCOPE_RULES


# ---------------------------------------------------------------------------
# Union allowlist loader — AllowedUser
# ---------------------------------------------------------------------------


def _base_unified(allowed_users: list) -> dict:
    """Minimal unified config dict with the given allowed_users shape."""
    return {
        "telegram": {
            "bot_token": "test-token",
            "allowed_users": allowed_users,
            "instance": {"name": "TestInstance"},
        },
    }


def test_loader_bare_ints_normalize_to_owner():
    """Flat-list instances (Salem/KAL-LE/Hypatia) stay back-compat."""
    cfg = load_from_unified(_base_unified([111, 222]))
    assert all(isinstance(u, AllowedUser) for u in cfg.allowed_users)
    assert [(u.id, u.role) for u in cfg.allowed_users] == [
        (111, "owner"), (222, "owner"),
    ]


def test_loader_role_dicts_carry_role():
    """VERA-shaped role-bearing entries are parsed with their roles."""
    cfg = load_from_unified(_base_unified([
        {"id": 111, "role": "owner"},
        {"id": 222, "role": "ops"},
    ]))
    assert [(u.id, u.role) for u in cfg.allowed_users] == [
        (111, "owner"), (222, "ops"),
    ]


def test_loader_mixed_shapes_in_one_list():
    """Bare int + role dict in the same list both normalize correctly."""
    cfg = load_from_unified(_base_unified([
        111,
        {"id": 222, "role": "ops"},
    ]))
    assert [(u.id, u.role) for u in cfg.allowed_users] == [
        (111, "owner"), (222, "ops"),
    ]


def test_loader_dict_without_role_defaults_to_owner():
    cfg = load_from_unified(_base_unified([{"id": 333}]))
    assert cfg.allowed_users == [AllowedUser(id=333, role="owner")]


def test_loader_drops_malformed_entries():
    """Bool, missing-id dict, and non-int id are dropped, not crashed."""
    cfg = load_from_unified(_base_unified([
        True,                       # YAML true → bool → dropped
        {"role": "ops"},            # no id → dropped
        {"id": "abc", "role": "ops"},  # non-int id → dropped
        444,                        # valid
    ]))
    assert cfg.allowed_users == [AllowedUser(id=444, role="owner")]


def test_loader_empty_allowed_users():
    cfg = load_from_unified(_base_unified([]))
    assert cfg.allowed_users == []


# ---------------------------------------------------------------------------
# resolve_scope — the keystone
# ---------------------------------------------------------------------------


def test_resolve_scope_vera_owner():
    assert conversation.resolve_scope("vera", "owner") == "vera"


def test_resolve_scope_vera_ops():
    assert conversation.resolve_scope("vera", "ops") == "vera_ops"


def test_resolve_scope_vera_unknown_role_treated_as_non_owner():
    # Any non-owner role on the vera tool_set → vera_ops (fail-narrow).
    assert conversation.resolve_scope("vera", "ops") == "vera_ops"
    assert conversation.resolve_scope("vera", "stranger") == "vera_ops"


def test_resolve_scope_vera_empty_role_defaults_owner():
    # Empty role on vera → owner (back-compat default).
    assert conversation.resolve_scope("vera", "") == "vera"


def test_resolve_scope_other_tool_sets_role_independent():
    """Every non-vera instance ignores role — scope == tool_set."""
    for ts in ("talker", "kalle", "hypatia"):
        assert conversation.resolve_scope(ts, "owner") == ts
        assert conversation.resolve_scope(ts, "ops") == ts
        assert conversation.resolve_scope(ts, "anything") == ts


def test_resolve_scope_empty_tool_set_defaults_talker():
    # Pre-VERA inline default preserved: empty tool_set → talker.
    assert conversation.resolve_scope("", "owner") == "talker"
    assert conversation.resolve_scope("", "ops") == "talker"


def test_vera_scopes_exist_in_scope_rules():
    assert "vera" in SCOPE_RULES
    assert "vera_ops" in SCOPE_RULES


# ---------------------------------------------------------------------------
# _role_for / _require_owner
# ---------------------------------------------------------------------------


def _update_from_user(user_id: int):
    """Minimal Update stand-in carrying an effective_user with an id."""
    return SimpleNamespace(effective_user=SimpleNamespace(id=user_id))


def _vera_config() -> TalkerConfig:
    return load_from_unified(_base_unified([
        {"id": 111, "role": "owner"},
        {"id": 222, "role": "ops"},
    ]))


def test_role_for_owner():
    cfg = _vera_config()
    assert bot._role_for(_update_from_user(111), cfg) == "owner"


def test_role_for_ops():
    cfg = _vera_config()
    assert bot._role_for(_update_from_user(222), cfg) == "ops"


def test_role_for_unmatched_defaults_owner():
    # An allowed-but-unmatched (or absent) user defaults to owner —
    # back-compat for flat-list instances.
    cfg = _vera_config()
    assert bot._role_for(_update_from_user(999), cfg) == "owner"


def test_role_for_bare_int_entry_is_owner():
    # Direct-construct fixture with a bare int still resolves to owner.
    # ``instance=InstanceConfig(name=...)`` is required — TalkerConfig's
    # default InstanceConfig has no default ``name`` (fail-loud-on-
    # missing-name guarantee, feedback_hardcoding_and_alfred_naming.md).
    cfg = TalkerConfig(
        allowed_users=[111],
        instance=InstanceConfig(name="V.E.R.A."),
    )
    assert bot._role_for(_update_from_user(111), cfg) == "owner"


def test_require_owner_true_for_owner():
    cfg = _vera_config()
    assert bot._require_owner(_update_from_user(111), cfg, "/status") is True


def test_require_owner_false_for_ops():
    cfg = _vera_config()
    assert bot._require_owner(_update_from_user(222), cfg, "/status") is False


def test_is_allowed_matches_both_shapes():
    # AllowedUser entries.
    cfg = _vera_config()
    assert bot._is_allowed(_update_from_user(111), cfg) is True
    assert bot._is_allowed(_update_from_user(222), cfg) is True
    assert bot._is_allowed(_update_from_user(999), cfg) is False
    # Bare-int direct-construct fixture still matches. ``instance`` is
    # required (no default name — see test_role_for_bare_int_entry_is_owner).
    flat = TalkerConfig(
        allowed_users=[111],
        instance=InstanceConfig(name="V.E.R.A."),
    )
    assert bot._is_allowed(_update_from_user(111), flat) is True
    assert bot._is_allowed(_update_from_user(222), flat) is False


# ---------------------------------------------------------------------------
# Dispatcher end-to-end — vera owner vs vera_ops
# ---------------------------------------------------------------------------


def _make_vera_vault(tmp_path: Path) -> Path:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    for sub in ("ticket", "note"):
        (vault_dir / sub).mkdir(exist_ok=True)
    return vault_dir


def _make_vera_config(tmp_path: Path) -> TalkerConfig:
    vault_dir = _make_vera_vault(tmp_path)
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[
            AllowedUser(id=111, role="owner"),
            AllowedUser(id=222, role="ops"),
        ],
        anthropic=AnthropicConfig(api_key="test-key"),
        stt=STTConfig(api_key="test-stt-key"),
        session=SessionConfig(state_path=str(tmp_path / "talker_state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="V.E.R.A.", tool_set="vera"),
    )


def _make_session(session_id: str = "vera-test-session") -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        session_id=session_id,
        chat_id=1,
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
    )


_TICKET_FIELDS = {
    "title": "Login button broken on checkout",
    "ticket_type": "bug",
    "reporter": "Ben",
    "area": "checkout",
}


@pytest.mark.asyncio
async def test_vera_ops_dispatcher_creates_ticket(tmp_path):
    """ops role → vera_ops scope → ticket create succeeds."""
    config = _make_vera_config(tmp_path)
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "ticket",
            "name": _TICKET_FIELDS["title"],
            "set_fields": _TICKET_FIELDS,
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
        user_role="ops",
    )
    parsed = json.loads(result)
    assert "error" not in parsed, parsed
    assert parsed["path"].startswith("ticket/"), parsed


@pytest.mark.asyncio
async def test_vera_ops_dispatcher_rejects_note(tmp_path):
    """ops role → vera_ops scope → note create denied (ticket-only)."""
    config = _make_vera_config(tmp_path)
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={"type": "note", "name": "Ops scratch", "set_fields": {}},
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
        user_role="ops",
    )
    parsed = json.loads(result)
    assert "error" in parsed, parsed
    assert "scope denied" in parsed["error"].lower()
    assert "vera-ops" in parsed["error"].lower()


@pytest.mark.asyncio
async def test_vera_owner_dispatcher_creates_ticket_and_note(tmp_path):
    """owner role → vera scope → ticket AND note creates both succeed."""
    config = _make_vera_config(tmp_path)
    sess = _make_session()
    state = StateManager(config.session.state_path)

    ticket_result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "ticket",
            "name": _TICKET_FIELDS["title"],
            "set_fields": _TICKET_FIELDS,
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
        user_role="owner",
    )
    assert "error" not in json.loads(ticket_result), ticket_result

    note_result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={"type": "note", "name": "Owner review note", "set_fields": {}},
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
        user_role="owner",
    )
    parsed = json.loads(note_result)
    assert "error" not in parsed, parsed
    assert parsed["path"].startswith("note/"), parsed


@pytest.mark.asyncio
async def test_vera_default_role_is_owner_when_unset(tmp_path):
    """No user_role passed → defaults to owner → vera scope (note OK)."""
    config = _make_vera_config(tmp_path)
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={"type": "note", "name": "Default-role note", "set_fields": {}},
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
        # user_role omitted → default "owner"
    )
    parsed = json.loads(result)
    assert "error" not in parsed, parsed
    assert parsed["path"].startswith("note/"), parsed
