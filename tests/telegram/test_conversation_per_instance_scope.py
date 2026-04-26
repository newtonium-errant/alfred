"""Per-instance scope routing in the talker dispatcher.

Code-reviewer P0: prior to this fix the ``_execute_tool`` dispatcher in
``conversation.py`` hardcoded ``scope.check_scope("talker", ...)``
regardless of which instance was running. Hypatia ``document`` and
KAL-LE ``pattern`` creates were rejected at ``talker_types_only``
*before* the scope-aware ``_validate_type`` gate ever engaged. The bot
path was a release-blocker for Phase 1 Hypatia even though the CLI
agent path was already unblocked by commit b0217c2.

This file pins the new contract: the dispatcher reads
``config.instance.tool_set`` and routes ``check_scope`` + the
``vault_create`` / ``vault_edit`` ``scope=`` kwarg to the matching
scope key. The two-gate contract (``_validate_type`` + ``check_scope``
allowlist) propagates correctly on the bot path.

Coverage:
    * Salem (``tool_set="talker"``) → ``check_scope("talker", ...)``
      and ``ops.vault_create(scope="talker")`` — note creates work,
      pattern creates rejected at the talker allowlist.
    * KAL-LE (``tool_set="kalle"``) → ``check_scope("kalle", ...)`` —
      pattern creates work; task creates rejected (operational types
      are Salem's territory).
    * Hypatia (``tool_set="hypatia"``) → ``check_scope("hypatia", ...)``
      — document creates work; pattern creates rejected (kalle-only).
    * No config (``config=None``) → defaults to ``"talker"`` for
      backwards compatibility with legacy callers + tests.
    * Every ``InstanceConfig.tool_set`` value used in shipped configs
      is a valid scope key — guards against future config typos
      silently falling through to talker.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.telegram import conversation
from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
from alfred.telegram.session import Session
from alfred.telegram.state import StateManager
from alfred.vault.scope import SCOPE_RULES


# --- Fixtures --------------------------------------------------------------


def _make_vault(tmp_path: Path) -> Path:
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    # Mirror enough of the scaffold tree that vault_create can land
    # records of every type we exercise.
    for sub in (
        "session", "task", "note", "project",
        "pattern", "principle",
        "document", "concept", "source",
    ):
        (vault_dir / sub).mkdir(exist_ok=True)
    return vault_dir


def _make_config(
    tmp_path: Path,
    *,
    instance_name: str,
    tool_set: str,
) -> TalkerConfig:
    vault_dir = _make_vault(tmp_path)
    return TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        anthropic=AnthropicConfig(api_key="test-key"),
        stt=STTConfig(api_key="test-stt"),
        session=SessionConfig(state_path=str(tmp_path / "talker_state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name=instance_name, tool_set=tool_set),
    )


def _make_session(session_id: str = "scope-test-session") -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        session_id=session_id,
        chat_id=1,
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
    )


# --- Salem (talker) --------------------------------------------------------


@pytest.mark.asyncio
async def test_salem_dispatcher_routes_to_talker_scope_for_note(tmp_path):
    """Salem (tool_set=talker) → note create succeeds via talker_types_only."""
    config = _make_config(tmp_path, instance_name="Salem", tool_set="talker")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "note",
            "name": "Salem Test Note",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "path" in parsed, parsed
    assert parsed["path"].startswith("note/")


@pytest.mark.asyncio
async def test_salem_dispatcher_rejects_pattern_create(tmp_path):
    """Salem must NOT be able to create kalle-only types."""
    config = _make_config(tmp_path, instance_name="Salem", tool_set="talker")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "pattern",
            "name": "Bad Pattern",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    # Talker scope's allowlist message — proves we routed to "talker".
    assert "scope denied" in parsed["error"].lower()
    assert "talker" in parsed["error"].lower()


# --- KAL-LE (kalle) --------------------------------------------------------


@pytest.mark.asyncio
async def test_kalle_dispatcher_routes_to_kalle_scope_for_pattern(tmp_path):
    """KAL-LE (tool_set=kalle) → pattern create succeeds via kalle_types_only."""
    config = _make_config(tmp_path, instance_name="KAL-LE", tool_set="kalle")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "pattern",
            "name": "Test Pattern",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "path" in parsed, parsed
    assert parsed["path"].startswith("pattern/")


@pytest.mark.asyncio
async def test_kalle_dispatcher_rejects_task_create(tmp_path):
    """KAL-LE has no operational types — task creates must be denied."""
    config = _make_config(tmp_path, instance_name="KAL-LE", tool_set="kalle")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "task",
            "name": "Bad Task",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    # The kalle allowlist message — proves we routed to "kalle", NOT
    # talker (which would have allowed task).
    assert "scope denied" in parsed["error"].lower()
    assert "kalle" in parsed["error"].lower()


# --- Hypatia (hypatia) -----------------------------------------------------


@pytest.mark.asyncio
async def test_hypatia_dispatcher_routes_to_hypatia_scope_for_document(tmp_path):
    """Hypatia (tool_set=hypatia) → document create succeeds via the
    hypatia scope's create allowlist + the scope-aware _validate_type."""
    config = _make_config(tmp_path, instance_name="Hypatia", tool_set="hypatia")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "document",
            "name": "Test Document",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "path" in parsed, parsed
    assert parsed["path"].startswith("document/")


@pytest.mark.asyncio
async def test_hypatia_dispatcher_rejects_pattern_create(tmp_path):
    """Hypatia must NOT be able to create kalle-only types."""
    config = _make_config(tmp_path, instance_name="Hypatia", tool_set="hypatia")
    sess = _make_session()
    state = StateManager(config.session.state_path)

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "pattern",
            "name": "Bad Pattern",
            "set_fields": {},
        },
        vault_path=config.vault.path,
        state=state,
        session=sess,
        config=config,
    )
    parsed = json.loads(result)
    assert "error" in parsed
    assert "scope denied" in parsed["error"].lower()
    assert "hypatia" in parsed["error"].lower()


# --- Default / fallback ----------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_defaults_to_talker_when_config_is_none(tmp_path):
    """Legacy callers that pass ``config=None`` must keep getting talker scope.

    A handful of older test paths and the early ``_execute_tool`` callers
    didn't plumb ``config`` through. They must continue to work as
    Salem-shaped — the fallback string ``"talker"`` is what makes that
    contract explicit.
    """
    vault_dir = _make_vault(tmp_path)
    sess = _make_session()
    state = StateManager(str(tmp_path / "state.json"))

    result = await conversation._execute_tool(
        tool_name="vault_create",
        tool_input={
            "type": "note",
            "name": "No Config Note",
            "set_fields": {},
        },
        vault_path=str(vault_dir),
        state=state,
        session=sess,
        config=None,
    )
    parsed = json.loads(result)
    assert "path" in parsed, parsed


# --- Config typo guard (code-reviewer P2) ---------------------------------


def test_every_shipped_tool_set_is_a_valid_scope_key():
    """Every ``tool_set`` value used in a shipped config maps to a real
    ``SCOPE_RULES`` entry.

    Without this assertion a typo in ``config.<instance>.yaml`` (e.g.
    ``tool_set: hypatya``) would silently fall through to the
    dispatcher's ``"talker"`` fallback — exactly the silent-misroute
    failure mode that motivated this fix in the first place.

    The list below mirrors the live shipped configs and the
    ``tools_for_set`` / ``VAULT_TOOLS_BY_SET`` registry. Adding a new
    instance? Add its tool_set string here AND add it to
    ``SCOPE_RULES`` — bouncing this test fails loud at CI time.
    """
    shipped_tool_sets = {"talker", "kalle", "hypatia"}
    for tool_set in shipped_tool_sets:
        assert tool_set in SCOPE_RULES, (
            f"tool_set {tool_set!r} has no SCOPE_RULES entry — the "
            f"dispatcher's check_scope({tool_set!r}, ...) call would "
            f"raise ScopeError on every tool invocation."
        )
