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

import pytest

from alfred.telegram import conversation
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


