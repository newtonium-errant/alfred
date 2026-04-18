"""Tests for wk3 commit 5 — /opus and /sonnet commands + run_turn bug fix.

The commit does two things:
    1. Registers ``/opus`` and ``/sonnet`` commands that flip the active
       session's model on the active dict.
    2. Fixes the wk2 bug where ``conversation.run_turn`` read
       ``config.anthropic.model`` instead of ``session.model`` — which
       made router-chosen models and explicit switches silently ignored.

Tests for (1) exercise the ``_switch_model`` helper directly (the PTB
CommandHandler plumbing doesn't need a full fake to verify the
side-effects). Tests for (2) use the ``FakeAnthropicClient`` to capture
the ``model`` kwarg passed to ``messages.create``.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alfred.telegram import bot, conversation
from alfred.telegram.session import Session
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- _switch_model helper ---------------------------------------------------


def test_switch_model_flips_active_dict(state_mgr) -> None:
    """Happy path: active session on Sonnet → switch to Opus flips the key."""
    state_mgr.set_active(1, {
        "session_id": "s",
        "chat_id": 1,
        "started_at": "2026-04-18T12:00:00+00:00",
        "last_message_at": "2026-04-18T12:00:00+00:00",
        "model": bot._SONNET_MODEL,
        "transcript": [],
        "vault_ops": [],
    })
    state_mgr.save()

    reply = bot._switch_model(state_mgr, 1, bot._OPUS_MODEL, "Opus")
    assert reply == "switched to Opus."
    active = state_mgr.get_active(1)
    assert active is not None
    assert active["model"] == bot._OPUS_MODEL


def test_switch_model_idempotent_when_already_on_target(state_mgr) -> None:
    """Flipping to the model you're already on reports but doesn't error."""
    state_mgr.set_active(2, {
        "session_id": "s",
        "chat_id": 2,
        "started_at": "2026-04-18T12:00:00+00:00",
        "last_message_at": "2026-04-18T12:00:00+00:00",
        "model": bot._OPUS_MODEL,
        "transcript": [],
        "vault_ops": [],
    })
    state_mgr.save()

    reply = bot._switch_model(state_mgr, 2, bot._OPUS_MODEL, "Opus")
    assert reply == "already on Opus."


def test_switch_model_returns_none_when_no_active_session(state_mgr) -> None:
    """No active session → helper returns None (caller renders terse reply)."""
    assert bot._switch_model(state_mgr, 99, bot._OPUS_MODEL, "Opus") is None


def test_switch_model_persists_across_rehydrate(state_mgr) -> None:
    """The flip survives a state reload — not just an in-memory update."""
    state_mgr.set_active(3, {
        "session_id": "s",
        "chat_id": 3,
        "started_at": "2026-04-18T12:00:00+00:00",
        "last_message_at": "2026-04-18T12:00:00+00:00",
        "model": bot._SONNET_MODEL,
        "transcript": [],
        "vault_ops": [],
    })
    state_mgr.save()

    bot._switch_model(state_mgr, 3, bot._OPUS_MODEL, "Opus")

    # New manager reading the same file sees the new model.
    from alfred.telegram.state import StateManager
    fresh = StateManager(state_mgr.path)
    fresh.load()
    active = fresh.get_active(3)
    assert active is not None
    assert active["model"] == bot._OPUS_MODEL


# --- run_turn reads session.model ------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_uses_session_model_not_config_model(
    state_mgr, talker_config
) -> None:
    """REGRESSION: ``run_turn`` routes to ``session.model`` even if config differs.

    Wk2 accidentally used ``config.anthropic.model`` for every turn's API
    call. That made the router's per-session model choice and the
    explicit ``/opus`` / ``/sonnet`` flip silently ineffective on every
    turn after session open. The fix flips this to ``session.model``.
    """
    # Config says Sonnet; session says Opus. The API call must see Opus.
    sess = Session(
        session_id="model-test",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model=bot._OPUS_MODEL,
    )
    state_mgr.set_active(1, sess.to_dict())

    assert talker_config.anthropic.model == bot._SONNET_MODEL, (
        "This test's premise requires config != session.model"
    )

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok")]),
    ])

    await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="hi",
        config=talker_config,
        vault_context_str="",
        system_prompt="sys",
    )

    assert len(client.messages.calls) == 1
    assert client.messages.calls[0]["model"] == bot._OPUS_MODEL


@pytest.mark.asyncio
async def test_run_turn_model_follows_session_across_multiple_turns(
    state_mgr, talker_config
) -> None:
    """Two turns, session.model flipped between them → second turn uses new model."""
    sess = Session(
        session_id="model-test-2",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model=bot._SONNET_MODEL,
    )
    state_mgr.set_active(1, sess.to_dict())

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok")]),
        FakeResponse(content=[FakeBlock(type="text", text="ok2")]),
    ])

    await conversation.run_turn(
        client=client, state=state_mgr, session=sess,
        user_message="first", config=talker_config,
        vault_context_str="", system_prompt="sys",
    )
    # Simulate /opus: flip session.model in place.
    sess.model = bot._OPUS_MODEL
    state_mgr.set_active(1, sess.to_dict())

    await conversation.run_turn(
        client=client, state=state_mgr, session=sess,
        user_message="second", config=talker_config,
        vault_context_str="", system_prompt="sys",
    )

    assert client.messages.calls[0]["model"] == bot._SONNET_MODEL
    assert client.messages.calls[1]["model"] == bot._OPUS_MODEL


# --- Command registration --------------------------------------------------


def test_build_app_registers_opus_and_sonnet_handlers(talker_config) -> None:
    """``/opus`` and ``/sonnet`` land on the :class:`Application` as CommandHandlers."""
    from alfred.telegram import state as state_mod
    from pathlib import Path
    import tempfile

    # Use a throwaway state file — Application.builder needs a valid bot
    # token but doesn't touch Telegram on its own.
    with tempfile.TemporaryDirectory() as tmp:
        mgr = state_mod.StateManager(Path(tmp) / "s.json")
        mgr.load()
        app = bot.build_app(
            config=talker_config,
            state_mgr=mgr,
            anthropic_client=None,
            system_prompt="",
            vault_context_str="",
        )
        # Collect command strings from the Application's handler registry.
        commands = set()
        for group in app.handlers.values():
            for h in group:
                cmds = getattr(h, "commands", None)
                if cmds:
                    commands.update(cmds)
        assert "opus" in commands
        assert "sonnet" in commands
