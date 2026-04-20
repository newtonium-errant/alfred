"""Tests for wk2b commit 2 — silent capture behaviour.

Covers:
    * ``run_turn`` short-circuits when ``session_type == "capture"``:
      transcript is appended, NO LLM call is made, sentinel is returned.
    * Non-capture sessions still run the LLM path (regression guard).
    * ``handle_message`` posts a reaction emoji when ``run_turn`` returns
      the capture sentinel, and does NOT send a text reply.
    * Inline commands (/end, /opus) still fire during a capture session.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import bot, conversation
from alfred.telegram.session import Session
from tests.telegram.conftest import FakeAnthropicClient


# --- run_turn silent short-circuit ----------------------------------------


def _new_session(state_mgr, chat_id: int = 1) -> Session:
    now = datetime.now(timezone.utc)
    sess = Session(
        session_id=f"cap-{chat_id}",
        chat_id=chat_id,
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
        opening_model="claude-sonnet-4-6",
    )
    state_mgr.set_active(chat_id, sess.to_dict())
    state_mgr.save()
    return sess


@pytest.mark.asyncio
async def test_capture_run_turn_skips_llm_and_appends_transcript(
    state_mgr, talker_config,
) -> None:
    """Capture session: transcript grows, sentinel returned, LLM never called."""
    sess = _new_session(state_mgr, chat_id=1)
    client = FakeAnthropicClient([])  # empty queue — assert .calls == []

    result = await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="rambling about the new plan",
        config=talker_config,
        vault_context_str="",
        system_prompt="sys",
        user_kind="voice",
        session_type="capture",
    )

    assert result == conversation.CAPTURE_SENTINEL
    # User turn WAS appended so /extract and /brief can see it later.
    assert len(sess.transcript) == 1
    assert sess.transcript[0]["role"] == "user"
    assert sess.transcript[0]["content"] == "rambling about the new plan"
    assert sess.transcript[0]["_kind"] == "voice"
    # Zero LLM calls — the short-circuit fires before ``messages.create``.
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_non_capture_run_turn_still_calls_llm(
    state_mgr, talker_config,
) -> None:
    """Regression: journal/note/task sessions still hit the LLM path."""
    sess = _new_session(state_mgr, chat_id=2)
    # Seed one response so run_turn completes end_turn on the first pass.
    from tests.telegram.conftest import FakeBlock, FakeResponse
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ack")]),
    ])

    result = await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="hi",
        config=talker_config,
        vault_context_str="",
        system_prompt="sys",
        user_kind="text",
        session_type="note",  # NOT capture
    )

    assert result == "ack"
    # Exactly one LLM call.
    assert len(client.messages.calls) == 1


@pytest.mark.asyncio
async def test_capture_skips_llm_even_with_canned_responses(
    state_mgr, talker_config,
) -> None:
    """Even if the fake client would HAPPILY answer, capture must not call it."""
    sess = _new_session(state_mgr, chat_id=3)
    from tests.telegram.conftest import FakeBlock, FakeResponse
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="would-be reply")]),
    ])

    result = await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="more rambling",
        config=talker_config,
        vault_context_str="",
        system_prompt="sys",
        session_type="capture",
    )

    assert result == conversation.CAPTURE_SENTINEL
    assert client.messages.calls == []


# --- handle_message reaction-emoji integration ----------------------------


def _seed_capture_session(state_mgr, chat_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    state_mgr.set_active(chat_id, {
        "session_id": f"cap-{chat_id}",
        "chat_id": chat_id,
        "started_at": now,
        "last_message_at": now,
        "model": bot._SONNET_MODEL,
        "opening_model": bot._SONNET_MODEL,
        "transcript": [],
        "vault_ops": [],
        "_vault_path_root": "",
        "_user_vault_path": "person/Test",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "capture",
        "_continues_from": None,
        "_pushback_level": 0,
    })
    state_mgr.save()


def _make_update(text: str, chat_id: int = 1, user_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.voice = None
    update.message.message_id = 42
    update.message.reply_text = AsyncMock()
    return update


def _make_ctx(config, state_mgr, client) -> MagicMock:
    ctx = MagicMock()
    ctx.application.bot_data = {
        "config": config,
        "state_mgr": state_mgr,
        "anthropic_client": client,
        "system_prompt": "sys",
        "vault_context_str": "",
        "chat_locks": {},
    }
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.set_message_reaction = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_handle_message_posts_reaction_on_capture_sentinel(
    state_mgr, talker_config, fake_client,
) -> None:
    """Capture session: reaction emoji is posted; no text reply is sent."""
    _seed_capture_session(state_mgr, chat_id=1)
    update = _make_update("rambling thought", chat_id=1)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    await bot.handle_message(update, ctx, text="rambling thought", voice=False)

    # Reaction was posted on the inbound message.
    ctx.bot.set_message_reaction.assert_called_once()
    call_kwargs = ctx.bot.set_message_reaction.call_args.kwargs
    assert call_kwargs["chat_id"] == 1
    assert call_kwargs["message_id"] == 42
    # Heavy check mark emoji (load-bearing constant — see _CAPTURE_REACTION_EMOJI).
    from telegram import ReactionTypeEmoji
    assert isinstance(call_kwargs["reaction"][0], ReactionTypeEmoji)
    assert call_kwargs["reaction"][0].emoji == "\N{HEAVY CHECK MARK}"

    # No text reply was sent.
    update.message.reply_text.assert_not_called()

    # Transcript carries the user turn so /extract and /brief can work.
    active = state_mgr.get_active(1)
    assert len(active["transcript"]) == 1
    assert active["transcript"][0]["content"] == "rambling thought"


@pytest.mark.asyncio
async def test_handle_message_reaction_fallback_text_reply(
    state_mgr, talker_config, fake_client,
) -> None:
    """If set_message_reaction fails, fall back to a minimal text reply."""
    _seed_capture_session(state_mgr, chat_id=2)
    update = _make_update("thought", chat_id=2)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)
    ctx.bot.set_message_reaction.side_effect = RuntimeError("reaction api down")

    await bot.handle_message(update, ctx, text="thought", voice=False)

    # Reaction was attempted.
    ctx.bot.set_message_reaction.assert_called_once()
    # Fallback text reply was sent (the minimal "." dot).
    update.message.reply_text.assert_called_once()
    assert update.message.reply_text.call_args.args[0] == "."


@pytest.mark.asyncio
async def test_inline_end_still_fires_during_capture(
    state_mgr, talker_config, fake_client, tmp_path,
) -> None:
    """``/end`` inline during a capture session must still close normally."""
    _seed_capture_session(state_mgr, chat_id=3)
    # /end needs a writeable vault path.
    active = state_mgr.get_active(3)
    active["_vault_path_root"] = talker_config.vault.path
    state_mgr.set_active(3, active)
    state_mgr.save()

    update = _make_update("ok /end", chat_id=3)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    await bot.handle_message(update, ctx, text="ok /end", voice=False)

    # Session closed — active slot cleared.
    assert state_mgr.get_active(3) is None
    # /end path replies with "session closed. ...".
    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args.args[0]
    assert reply.startswith("session closed.")
    # No reaction was posted — /end is a command, not a conversational turn.
    ctx.bot.set_message_reaction.assert_not_called()
