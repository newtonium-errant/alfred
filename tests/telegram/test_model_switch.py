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

from alfred.telegram import conversation
from alfred.telegram.session import Session
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- _switch_model helper ---------------------------------------------------


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
        model="claude-opus-4-7",
    )
    state_mgr.set_active(1, sess.to_dict())

    assert talker_config.anthropic.model == "claude-sonnet-4-6", (
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
    assert client.messages.calls[0]["model"] == "claude-opus-4-7"


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
        model="claude-sonnet-4-6",
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
    sess.model = "claude-opus-4-7"
    state_mgr.set_active(1, sess.to_dict())

    await conversation.run_turn(
        client=client, state=state_mgr, session=sess,
        user_message="second", config=talker_config,
        vault_context_str="", system_prompt="sys",
    )

    assert client.messages.calls[0]["model"] == "claude-sonnet-4-6"
    assert client.messages.calls[1]["model"] == "claude-opus-4-7"


# --- Command registration --------------------------------------------------


