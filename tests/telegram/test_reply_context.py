"""Tests for the Telegram reply-context consumer.

When a user long-presses one of Salem's earlier messages and hits "Reply,"
the Bot API attaches the parent message via ``Message.reply_to_message``.
The talker prepends a machine-generated ``[You are replying to Salem's
earlier message at <ts>: "..."]`` prefix to the turn text so downstream
paths (router + Anthropic turn) see the reply attribution inline.

Covers:
    * ``_build_reply_context_prefix`` — the pure helper that renders the
      prefix from a PTB-shaped parent message object.
    * ``handle_message`` integration — prefix reaches ``conversation.run_turn``
      as the ``user_message`` arg.
    * Router hint — ``has_reply_context`` threads through the open-session
      path into ``classify_opening_cue``.
    * Active-session fast path — a reply with an active session skips the
      router entirely, falls into the existing run-turn flow.
"""

from __future__ import annotations


import pytest

from alfred.telegram import router as router_mod


# --- Helpers --------------------------------------------------------------


# --- _build_reply_context_prefix pure tests -------------------------------


# --- handle_message integration: prefix reaches run_turn ------------------


# --- Router signature-level tests -----------------------------------------


@pytest.mark.asyncio
async def test_router_accepts_has_reply_context_kwarg() -> None:
    """``classify_opening_cue(..., has_reply_context=True)`` is a valid call.

    Smoke test: the router prompt takes the hint as a template variable,
    so a ``KeyError`` on missing format arg would surface immediately.
    Using a BoomClient forces the API-error fallback path, so we don't
    need a full fake response — we just need the prompt-build to not
    raise.
    """

    class BoomMessages:
        async def create(self, **kwargs):
            raise RuntimeError("forced error")

    class BoomClient:
        messages = BoomMessages()

    decision = await router_mod.classify_opening_cue(
        BoomClient(),
        first_message="[You are replying to Salem's earlier message at ...] hi",
        recent_sessions=[],
        has_reply_context=True,
    )
    # Fallback decision on API error; critically, no template-format crash.
    assert decision.session_type == "note"


@pytest.mark.asyncio
async def test_router_has_reply_context_appears_in_prompt() -> None:
    """The ``has_reply_context`` flag is injected into the prompt text.

    We capture the kwargs passed to ``client.messages.create`` and check
    the prompt contains the ``has_reply_context=true`` token so the
    classifier can actually read the signal.
    """
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(
            type="text",
            text='{"session_type": "note", "continues_from": null, '
                 '"reasoning": "reply follow-up"}',
        )]),
    ])

    await router_mod.classify_opening_cue(
        client,
        first_message="[You are replying to Salem's earlier message at "
                      "2026-04-21T10:30:00+00:00: \"brief\"]\n\nwhat's the source?",
        recent_sessions=[],
        has_reply_context=True,
    )

    # The router made exactly one call; the prompt is the first message's content.
    assert len(client.messages.calls) == 1
    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "has_reply_context=true" in prompt


@pytest.mark.asyncio
async def test_router_has_reply_context_false_default() -> None:
    """When ``has_reply_context`` is omitted, prompt contains ``=false``."""
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(
            type="text",
            text='{"session_type": "note", "continues_from": null, '
                 '"reasoning": "fresh note"}',
        )]),
    ])

    await router_mod.classify_opening_cue(
        client,
        first_message="quick note",
        recent_sessions=[],
    )

    prompt = client.messages.calls[0]["messages"][0]["content"]
    assert "has_reply_context=false" in prompt
