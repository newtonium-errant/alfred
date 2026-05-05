"""Tests for wk3 commit 1 — pushback mechanism.

Covers the per-level directive text, the four-block cache-control system
block ordering, and the end-to-end thread from router decision through
``run_turn`` to the Anthropic API call.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from alfred.telegram import bot, conversation, session_types
from alfred.telegram.session import Session
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


def test_pushback_directive_renders_per_level() -> None:
    """Levels 0-5 each produce a distinct directive string."""
    seen: set[str] = set()
    for level in range(6):
        text = conversation._pushback_directive(level)
        assert isinstance(text, str)
        assert text  # non-empty
        # Every level announces itself in the directive so the model can
        # distinguish them at a glance — and so we can assert the dial
        # label landed in the prompt without grepping whole paragraphs.
        assert f"level {level}" in text.lower()
        seen.add(text)
    assert len(seen) == 6  # all six levels distinct


def test_pushback_directive_unknown_level_falls_back_to_three() -> None:
    """Out-of-range levels render the middle (level 3) directive.

    The fallback to a mid-intensity level is deliberate: picking either
    extreme (0 or 5) on a config typo would be a user-visible regression.
    """
    assert (
        conversation._pushback_directive(99)
        == conversation._pushback_directive(3)
    )
    assert (
        conversation._pushback_directive(-1)
        == conversation._pushback_directive(3)
    )


def test_build_system_blocks_pushback_block_second_to_last() -> None:
    """Pushback block is the second-to-last cache-control text block.

    Cache ordering runs most-stable-first: system prompt → vault ctx →
    calibration → pushback → today. This test locks pushback's position
    so the prompt-caching prefix stays stable as calibration lands;
    today's-date block (added 2026-05-06 to close the day-of-week
    date-math gap from conversation ``716f5b24``) sits AFTER pushback
    because today is the most-volatile block (changes daily) and any
    cacheable block placed after it would invalidate the cache prefix
    on every date rollover.
    """
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        pushback_level=4,
    )
    # System + vault + pushback + today = 4 blocks (no calibration here).
    assert len(blocks) == 4
    assert blocks[0]["text"] == "SYS"
    assert blocks[1]["text"] == "VAULT"
    # Pushback is now second-to-last; today-block tails.
    assert "Session pushback directive" in blocks[-2]["text"]
    assert "level 4" in blocks[-2]["text"].lower()
    assert blocks[-1]["text"].startswith("## Today")
    # Cache-control invariant: every block EXCEPT the today-block
    # carries an ephemeral breakpoint. The today-block is intentionally
    # uncached (changes daily; cache TTL is 5min so caching it would
    # churn the cache pointlessly).
    for block in blocks[:-1]:
        assert block["type"] == "text"
        assert block["cache_control"] == {"type": "ephemeral"}
    assert blocks[-1]["type"] == "text"
    assert "cache_control" not in blocks[-1]


def test_build_system_blocks_pushback_none_skips_block() -> None:
    """``pushback_level=None`` omits the directive block entirely.

    This keeps pre-wk3 active sessions (rehydrated from state without a
    stashed level) behaving exactly like wk2 — no new system text injected.
    The today-block ALWAYS tails so the date anchor is unconditional.
    """
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        pushback_level=None,
    )
    # System + vault + today = 3 blocks (today always present).
    assert len(blocks) == 3
    assert all("pushback" not in b["text"].lower() for b in blocks)
    assert blocks[-1]["text"].startswith("## Today")


def test_build_system_blocks_cache_order_with_all_five_blocks() -> None:
    """Full five-block layout in canonical order.

    Order: system prompt → vault context → calibration → pushback →
    today. Locked here so a future addition can't accidentally move
    today off the tail (which would either invalidate the cache prefix
    on every date rollover OR re-cache an ephemeral block that
    legitimately should stay uncached).
    """
    blocks = conversation._build_system_blocks(
        system_prompt="SYS",
        vault_context_str="VAULT",
        calibration_str="CAL",
        pushback_level=2,
    )
    assert len(blocks) == 5
    assert blocks[0]["text"] == "SYS"
    assert blocks[1]["text"] == "VAULT"
    assert blocks[2]["text"].startswith("## Alfred's calibration for this user")
    assert "CAL" in blocks[2]["text"]
    assert blocks[3]["text"].startswith("## Session pushback directive")
    assert blocks[4]["text"].startswith("## Today")


@pytest.mark.asyncio
async def test_run_turn_threads_pushback_level_into_system_blocks(
    state_mgr, talker_config
) -> None:
    """``run_turn`` passes ``pushback_level`` through to the API call.

    Uses the FakeAnthropicClient to capture the ``system`` kwarg and
    asserts the pushback directive landed in the final block.
    """
    sess = Session(
        session_id="abc",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model="claude-sonnet-4-6",
    )
    state_mgr.set_active(1, sess.to_dict())

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok")]),
    ])

    await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=sess,
        user_message="test",
        config=talker_config,
        vault_context_str="VAULT",
        system_prompt="SYS",
        pushback_level=4,
    )

    assert len(client.messages.calls) == 1
    call = client.messages.calls[0]
    system = call["system"]
    assert isinstance(system, list)
    # Pushback is now second-to-last; today's-date block (added
    # 2026-05-06) tails.
    assert "Session pushback directive" in system[-2]["text"]
    assert "level 4" in system[-2]["text"].lower()
    assert system[-1]["text"].startswith("## Today")


@pytest.mark.asyncio
async def test_open_session_with_stash_records_pushback_level(
    state_mgr, talker_config
) -> None:
    """``_open_session_with_stash`` persists ``_pushback_level`` on the active dict."""
    bot._open_session_with_stash(
        state_mgr,
        chat_id=1,
        config=talker_config,
        session_type="journal",
        pushback_level=4,
    )
    active = state_mgr.get_active(1)
    assert active is not None
    assert active["_pushback_level"] == 4


@pytest.mark.asyncio
async def test_routed_open_stashes_type_pushback_level(
    state_mgr, talker_config
) -> None:
    """``_open_routed_session`` pulls pushback from the session-type default."""
    from tests.telegram.conftest import FakeBlock, FakeResponse

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(
            type="text",
            text=(
                '{"session_type": "journal", "continues_from": null, '
                '"reasoning": "reflective"}'
            ),
        )]),
    ])

    await bot._open_routed_session(
        state_mgr,
        talker_config,
        client,
        chat_id=7,
        first_message="I want to think through something.",
    )

    active = state_mgr.get_active(7)
    assert active is not None
    # journal → level 4 per the defaults table.
    assert active["_pushback_level"] == session_types.defaults_for("journal").pushback_level
    assert active["_pushback_level"] == 4
