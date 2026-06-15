"""VERA — role-aware allowlist + scope routing.

VERA is the first multi-user instance. This file pins the Layer 1 (role
gate) + keystone (role-aware ``resolve_scope``) contract:

    * ``AllowedUser`` + the union allowlist loader — bare ints (flat-list
      instances) normalize to role ``owner``; dict entries carry their
      role; back-compat for Salem / KAL-LE / Hypatia is preserved.
    * ``resolve_scope(tool_set, role)`` — vera-only branch; every other
      tool_set is role-independent and unchanged.
    * ``_role_for`` / ``_require_owner`` — role resolution + owner gate.
    * The ``_execute_tool`` dispatcher end-to-end: a ``vera`` tool_set
      with role=owner routes to the ``vera`` scope, role=ops routes to
      ``vera_ops``.

**Capability expansion 2026-06-15 (vera-assistant arc).** BOTH roles
now create+edit the same five business record types (ticket + note +
task + decision + project). The routing layer (resolve_scope / role
gate) is UNCHANGED — only the per-scope create allowlists widened (see
test_vera_scope.py). The dispatcher e2e tests below confirm both roles
create the business types.
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
@pytest.mark.parametrize(
    "rec_type,set_fields",
    [
        ("note", {}),
        ("task", {"status": "todo"}),
        ("decision", {"status": "draft"}),
        ("project", {"status": "active"}),
    ],
)
async def test_vera_ops_dispatcher_creates_business_types(
    tmp_path, rec_type, set_fields,
):
    """Capability expansion 2026-06-15: ops role → vera_ops scope now
    creates note/task/decision/project (was ticket-only). INVERTS the
    prior test_vera_ops_dispatcher_rejects_note."""
    config = _make_vera_config(tmp_path)
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": rec_type, "name": f"Ops {rec_type}",
            "set_fields": set_fields,
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
        user_role="ops",
    )
    parsed = json.loads(result)
    assert "error" not in parsed, parsed
    assert parsed["path"].startswith(f"{rec_type}/"), parsed


@pytest.mark.asyncio
async def test_vera_ops_dispatcher_rejects_learn_type(tmp_path):
    """ops role → vera_ops scope → a learn type (assumption) still denied
    — proves the decision grant didn't open the learn set via the
    dispatcher path."""
    config = _make_vera_config(tmp_path)
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "assumption", "name": "Ops assumption",
            "set_fields": {"status": "active"},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
        user_role="ops",
    )
    parsed = json.loads(result)
    assert "error" in parsed, parsed


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


# ---------------------------------------------------------------------------
# owner_only decorator — end-to-end gate (code-review Nit 2)
# ---------------------------------------------------------------------------
#
# The direct unit tests above cover _require_owner / _role_for / _is_allowed
# in isolation. ``owner_only`` is the actual production gate wrapping the
# owner-only command handlers — it reads config off
# ``ctx.application.bot_data[_KEY_CONFIG]`` and drops the call for a
# non-owner BEFORE the wrapped handler runs. These tests drive that
# decorator path end-to-end (security-relevant: this is what keeps an
# ``ops`` user out of /calibrate, /brief, /status, etc.).


def _fake_ctx(config: TalkerConfig) -> SimpleNamespace:
    """Minimal ContextTypes stand-in carrying config under _KEY_CONFIG.

    Mirrors the shape ``owner_only._wrapped`` reads:
    ``ctx.application.bot_data[_KEY_CONFIG]``.
    """
    return SimpleNamespace(
        application=SimpleNamespace(bot_data={bot._KEY_CONFIG: config}),
    )


@pytest.mark.asyncio
async def test_owner_only_decorator_blocks_ops_user(tmp_path):
    """ops user → decorated handler's inner body is NOT invoked (silent drop)."""
    config = _make_vera_config(tmp_path)  # 111=owner, 222=ops

    calls: list[str] = []

    @bot.owner_only
    async def fake_handler(update, ctx) -> None:
        calls.append("ran")

    await fake_handler(_update_from_user(222), _fake_ctx(config))

    # The inner handler must NOT have run — the decorator dropped it.
    assert calls == [], (
        "owner_only let an ops user through to the wrapped handler"
    )


@pytest.mark.asyncio
async def test_owner_only_decorator_allows_owner_user(tmp_path):
    """owner user → decorated handler's inner body IS invoked."""
    config = _make_vera_config(tmp_path)  # 111=owner, 222=ops

    calls: list[str] = []

    @bot.owner_only
    async def fake_handler(update, ctx) -> None:
        calls.append("ran")

    await fake_handler(_update_from_user(111), _fake_ctx(config))

    # The inner handler ran exactly once for the owner.
    assert calls == ["ran"], (
        "owner_only failed to invoke the wrapped handler for an owner user"
    )


@pytest.mark.asyncio
async def test_owner_only_on_status_blocks_ops_via_decorator(tmp_path):
    """End-to-end through the SHIPPED decorated handler ``on_status``.

    Pins that ``on_status`` is actually wrapped by ``owner_only`` (not
    just that the decorator works on a synthetic handler): an ops user
    hitting /status gets dropped before the handler's body runs. We
    assert via the role-deny log emission rather than a reply, since
    the handler short-circuits before any ``update.message`` access —
    so a bare SimpleNamespace update (no .message) is safe here.
    """
    import structlog

    config = _make_vera_config(tmp_path)  # 222 = ops
    with structlog.testing.capture_logs() as captured:
        await bot.on_status(_update_from_user(222), _fake_ctx(config))

    denied = [
        c for c in captured
        if c.get("event") == "talker.bot.role_denied"
    ]
    assert len(denied) == 1, (
        f"expected on_status to drop an ops user via owner_only "
        f"(role_denied log); captured={[c.get('event') for c in captured]!r}"
    )
    assert denied[0]["role"] == "ops"
    assert denied[0]["command"] == "on_status"


# ---------------------------------------------------------------------------
# AllowedUser.name — loader parse (VERA reporter follow-up)
# ---------------------------------------------------------------------------


def test_loader_parses_name_when_present():
    """Dict entries carry their ``name`` through the loader."""
    cfg = load_from_unified(_base_unified([
        {"id": 111, "role": "owner", "name": "Andrew"},
        {"id": 222, "role": "ops", "name": "Ben"},
    ]))
    assert [(u.id, u.role, u.name) for u in cfg.allowed_users] == [
        (111, "owner", "Andrew"),
        (222, "ops", "Ben"),
    ]


def test_loader_name_absent_defaults_none():
    """A role dict without ``name`` → name=None (back-compat)."""
    cfg = load_from_unified(_base_unified([{"id": 333, "role": "ops"}]))
    assert cfg.allowed_users == [AllowedUser(id=333, role="ops", name=None)]


def test_loader_bare_int_has_no_name():
    """Bare-int entries (flat-list instances) → name=None."""
    cfg = load_from_unified(_base_unified([111, 222]))
    assert all(u.name is None for u in cfg.allowed_users)


def test_loader_empty_name_normalizes_to_none():
    """An empty-string or non-str name → None (not the empty string)."""
    cfg = load_from_unified(_base_unified([
        {"id": 111, "name": ""},
        {"id": 222, "name": 12345},  # non-str
    ]))
    assert [(u.id, u.name) for u in cfg.allowed_users] == [
        (111, None), (222, None),
    ]


def test_allowed_user_name_defaults_none_on_direct_construct():
    """``AllowedUser(id, role)`` without name → name=None (dataclass default)."""
    assert AllowedUser(id=1, role="owner").name is None
    assert AllowedUser(id=1).name is None


# ---------------------------------------------------------------------------
# _name_for — sender display-name resolution
# ---------------------------------------------------------------------------


def _vera_named_config() -> TalkerConfig:
    return load_from_unified(_base_unified([
        {"id": 111, "role": "owner", "name": "Andrew"},
        {"id": 222, "role": "ops", "name": "Ben"},
        {"id": 333, "role": "ops"},  # nameless ops entry
    ]))


def test_name_for_owner():
    cfg = _vera_named_config()
    assert bot._name_for(_update_from_user(111), cfg) == "Andrew"


def test_name_for_ops():
    cfg = _vera_named_config()
    assert bot._name_for(_update_from_user(222), cfg) == "Ben"


def test_name_for_nameless_entry_returns_none():
    """A matched entry WITHOUT a name → None (role-fallback territory)."""
    cfg = _vera_named_config()
    assert bot._name_for(_update_from_user(333), cfg) is None


def test_name_for_unmatched_user_returns_none():
    cfg = _vera_named_config()
    assert bot._name_for(_update_from_user(999), cfg) is None


def test_name_for_bare_int_entry_returns_none():
    """Direct-construct fixture with a bare int → None (no name)."""
    cfg = TalkerConfig(
        allowed_users=[111],
        instance=InstanceConfig(name="V.E.R.A."),
    )
    assert bot._name_for(_update_from_user(111), cfg) is None


def test_name_for_single_user_flat_instance_returns_none():
    """Flat-list (Salem-shape) config → name is None for every user.

    This is the inert/back-compat guarantee at the resolution layer:
    a single-user instance never produces a sender name, so the
    downstream sender-identity block is never injected.
    """
    cfg = load_from_unified(_base_unified([8661018406]))
    assert bot._name_for(_update_from_user(8661018406), cfg) is None


# ---------------------------------------------------------------------------
# _build_sender_identity_text — block rendering
# ---------------------------------------------------------------------------


def test_sender_identity_text_uses_name_when_present():
    text = conversation._build_sender_identity_text("Ben", "ops")
    assert "Ben" in text
    assert "ops" in text
    assert "## Current message sender" in text


def test_sender_identity_text_falls_back_to_role_when_nameless():
    """name=None → the block names 'the <role> user' (role fallback)."""
    text = conversation._build_sender_identity_text(None, "ops")
    assert "the ops user" in text
    assert "ops" in text


# ---------------------------------------------------------------------------
# _build_system_blocks — sender-identity injection + inert default
# ---------------------------------------------------------------------------


def _last_block_text(blocks: list) -> str:
    return blocks[-1]["text"]


def test_system_blocks_omit_sender_block_by_default():
    """No sender_identity_block kwarg → byte-identical to pre-feature.

    The back-compat guarantee for Salem / KAL-LE / Hypatia: the system
    blocks end with the today-block (no sender block appended).
    """
    blocks = conversation._build_system_blocks(
        "SYSTEM", "VAULT CTX",
    )
    # Today-block stays last — no sender block.
    assert all(
        "## Current message sender" not in b.get("text", "")
        for b in blocks
    )


def test_system_blocks_append_sender_block_when_present():
    """A sender_identity_block lands as the LAST (tail, uncached) block."""
    sender = conversation._build_sender_identity_text("Ben", "ops")
    blocks = conversation._build_system_blocks(
        "SYSTEM", "VAULT CTX",
        sender_identity_block=sender,
    )
    last = blocks[-1]
    assert "## Current message sender" in last["text"]
    assert "Ben" in last["text"]
    # Tail position is uncached — most-volatile block.
    assert "cache_control" not in last


def test_system_blocks_sender_block_after_today_block():
    """Sender block sits AFTER the today-block (cache-neutral tail)."""
    sender = conversation._build_sender_identity_text("Andrew", "owner")
    blocks = conversation._build_system_blocks(
        "SYSTEM", "VAULT CTX",
        sender_identity_block=sender,
    )
    texts = [b.get("text", "") for b in blocks]
    today_idx = next(
        i for i, t in enumerate(texts) if "Today" in t or "today" in t
    )
    sender_idx = next(
        i for i, t in enumerate(texts) if "## Current message sender" in t
    )
    assert sender_idx > today_idx, (
        "sender-identity block must come AFTER the today-block so it "
        "doesn't invalidate any cache prefix"
    )


def test_system_blocks_dynamic_per_message():
    """Different senders → different sender blocks (dynamic per-message).

    Pins that the block is rebuilt per call rather than a static var:
    two distinct senders produce two distinct tail blocks.
    """
    blocks_ben = conversation._build_system_blocks(
        "SYSTEM", "VAULT CTX",
        sender_identity_block=conversation._build_sender_identity_text(
            "Ben", "ops",
        ),
    )
    blocks_andrew = conversation._build_system_blocks(
        "SYSTEM", "VAULT CTX",
        sender_identity_block=conversation._build_sender_identity_text(
            "Andrew", "owner",
        ),
    )
    assert "Ben" in _last_block_text(blocks_ben)
    assert "Andrew" in _last_block_text(blocks_andrew)
    assert _last_block_text(blocks_ben) != _last_block_text(blocks_andrew)
