"""Tests for wk2 routed-session opening + continuation pre-seeding.

Exercises ``_open_routed_session`` in :mod:`alfred.telegram.bot` with a
fake Anthropic client so the router's classification is deterministic.

Three scenarios:
    1. Happy-path article continuation — session opens on Opus, transcript
       primed with a context turn, ``_continues_from`` stashed.
    2. Unknown continuation path from router → dropped, session opens fresh.
    3. Fresh ``note`` cue with no recent sessions → Sonnet, no primer.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alfred.telegram import bot
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


def _json_response(payload: str) -> FakeResponse:
    return FakeResponse(content=[FakeBlock(type="text", text=payload)])


def _seed_closed_session(state_mgr, record_path: str, stype: str = "article") -> None:
    """Append one closed-session entry so the router can find it."""
    state_mgr.state.setdefault("closed_sessions", []).append({
        "session_id": "prior-00000000-0000-0000-0000-000000000000",
        "chat_id": 1,
        "started_at": "2026-04-17T09:00:00+00:00",
        "ended_at": "2026-04-17T09:45:00+00:00",
        "reason": "explicit",
        "record_path": record_path,
        "message_count": 12,
        "vault_ops": 0,
        "session_type": stype,
        "continues_from": None,
    })
    state_mgr.save()


@pytest.mark.asyncio
async def test_article_continuation_seeds_primer_and_opus(
    state_mgr, talker_config
) -> None:
    """Router flags article continuation → Opus, primer, ``_continues_from``."""
    record_path = "session/Voice Session — 2026-04-17 0900 prior123.md"
    _seed_closed_session(state_mgr, record_path, stype="article")

    client = FakeAnthropicClient([
        _json_response(
            '{"session_type": "article", '
            f'"continues_from": "{record_path}", '
            '"reasoning": "continue the article"}'
        )
    ])

    sess = await bot._open_routed_session(
        state_mgr,
        talker_config,
        client,
        chat_id=1,
        first_message="Let's continue the article.",
    )

    assert sess.model == "claude-opus-4-7"

    # Primer landed as an assistant turn.
    assert len(sess.transcript) == 1
    primer = sess.transcript[0]
    assert primer["role"] == "assistant"
    assert "continuing from" in primer["content"]
    assert "12 turns" in primer["content"]

    # Active-dict contract fields stashed correctly.
    active = state_mgr.get_active(1)
    assert active is not None
    assert active["_session_type"] == "article"
    assert active["_continues_from"] == f"[[{record_path}]]"


@pytest.mark.asyncio
async def test_router_continuation_path_not_in_state_is_dropped(
    state_mgr, talker_config
) -> None:
    """Hallucinated continuation path → no primer, ``_continues_from`` None.

    Type still flips to ``article`` / Opus (intent trumps absence).
    """
    client = FakeAnthropicClient([
        _json_response(
            '{"session_type": "article", '
            '"continues_from": "session/Does Not Exist.md", '
            '"reasoning": "hallucinated"}'
        )
    ])

    sess = await bot._open_routed_session(
        state_mgr,
        talker_config,
        client,
        chat_id=2,
        first_message="Let's continue the article.",
    )

    assert sess.model == "claude-opus-4-7"
    assert sess.transcript == []  # No primer.
    active = state_mgr.get_active(2)
    assert active is not None
    assert active["_session_type"] == "article"
    assert active["_continues_from"] is None


@pytest.mark.asyncio
async def test_fresh_note_cue_opens_on_sonnet_without_primer(
    state_mgr, talker_config
) -> None:
    """Plain note cue → Sonnet, ``note`` session type, empty transcript."""
    client = FakeAnthropicClient([
        _json_response(
            '{"session_type": "note", "continues_from": null, '
            '"reasoning": "quick reminder"}'
        )
    ])

    sess = await bot._open_routed_session(
        state_mgr,
        talker_config,
        client,
        chat_id=3,
        first_message="Remind me to water the plants.",
    )

    assert sess.model == "claude-sonnet-4-6"
    assert sess.transcript == []
    active = state_mgr.get_active(3)
    assert active is not None
    assert active["_session_type"] == "note"
    assert active["_continues_from"] is None
