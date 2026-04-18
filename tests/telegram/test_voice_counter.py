"""Tests for wk1-polish bug (b): voice / text counting via per-turn ``_kind``.

Wk1 maintained redundant ``voice_messages`` / ``text_messages`` counters on
the active state dict AND derived the same numbers from per-turn ``_kind``
at close time. The two paths could disagree. Wk2 drops the state-dict
counters in favour of per-turn ``_kind``.

These tests assert:
    1. ``run_turn`` threads ``user_kind`` onto the user turn as ``_kind``.
    2. ``_count_message_kinds`` returns the right tallies from a mixed
       transcript.
    3. ``_open_session_with_stash`` no longer initialises the two redundant
       counters on the active dict (they'd be stale the moment a turn ran).
"""

from __future__ import annotations

import pytest

from alfred.telegram import bot, conversation
from alfred.telegram.session import Session
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


def test_count_message_kinds_from_transcript() -> None:
    """Derive voice / text tallies from per-turn ``_kind`` metadata."""
    from datetime import datetime, timezone

    from alfred.telegram import session as talker_session

    sess = Session(
        session_id="abc",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model="claude-sonnet-4-6",
        transcript=[
            {"role": "user", "content": "v1", "_kind": "voice"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "t1", "_kind": "text"},
            {"role": "user", "content": "v2", "_kind": "voice"},
            {"role": "assistant", "content": "ok"},
            # Missing kind falls back to text.
            {"role": "user", "content": "t2"},
        ],
    )
    voice, text = talker_session._count_message_kinds(sess)
    assert voice == 2
    assert text == 2


@pytest.mark.asyncio
async def test_run_turn_stamps_user_kind_on_the_user_turn(
    state_mgr, talker_config
) -> None:
    """``run_turn`` with ``user_kind='voice'`` writes ``_kind=voice`` onto the turn."""
    from datetime import datetime, timezone

    sess = Session(
        session_id="abc",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model="claude-sonnet-4-6",
    )
    state_mgr.set_active(1, sess.to_dict())

    # One end_turn response, so run_turn exits after one API call.
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok")]),
    ])

    await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="hello from voice",
        config=talker_config,
        vault_context_str="",
        system_prompt="sys",
        user_kind="voice",
    )

    user_turns = [t for t in sess.transcript if t["role"] == "user"]
    assert len(user_turns) == 1
    assert user_turns[0]["_kind"] == "voice"
    assert "_ts" in user_turns[0]


def test_open_session_with_stash_does_not_set_state_dict_counters(
    state_mgr, talker_config
) -> None:
    """Active dict must not carry the wk1 ``voice_messages`` / ``text_messages`` keys."""
    bot._open_session_with_stash(state_mgr, chat_id=1, config=talker_config)
    active = state_mgr.get_active(1)
    assert active is not None
    assert "voice_messages" not in active
    assert "text_messages" not in active
    # The wk2 stashed fields MUST still be present — guards against over-trim.
    assert active["_session_type"] == "note"
    assert "_vault_path_root" in active
