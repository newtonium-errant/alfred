"""Tests for wk2b commit 1 — new ``capture`` session type + router wiring.

Covers:
    * ``capture`` entry in the session-type defaults (pushback=0, no
      continuation, Sonnet).
    * ``known_types()`` exposes ``capture``.
    * Router prefix short-circuit: ``capture:`` prefix dispatches to
      capture WITHOUT an LLM call.
    * Router LLM path honours a ``capture`` classification result.
"""

from __future__ import annotations

import pytest

from alfred.telegram import router, session_types
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


def test_capture_defaults_match_spec() -> None:
    """Capture session type defaults: Sonnet, pushback=0, no continuation."""
    d = session_types.defaults_for("capture")
    assert d.session_type == "capture"
    assert d.model == "claude-sonnet-4-6"
    assert d.pushback_level == 0
    assert d.supports_continuation is False


def test_capture_in_known_types() -> None:
    """``known_types()`` now includes capture alongside the original 5."""
    types = session_types.known_types()
    assert "capture" in types
    # Ensure we didn't drop the original types.
    assert {"note", "task", "journal", "article", "brainstorm"}.issubset(types)


@pytest.mark.asyncio
async def test_capture_prefix_short_circuits_router_without_llm_call() -> None:
    """``capture: ...`` prefix dispatches capture without hitting the LLM.

    Load-bearing: an explicit ``capture:`` prefix is a user-asserted
    classification. Routing through the LLM would risk a mis-route on a
    borderline phrasing that happens to also mention "remind me" etc.
    """
    client = FakeAnthropicClient([])  # empty queue — if LLM is called the
                                      # fake returns a default response but
                                      # we assert .calls is empty below.
    decision = await router.classify_opening_cue(
        client,
        first_message="capture: thinking out loud about the Q2 plan",
        recent_sessions=[],
    )
    assert decision.session_type == "capture"
    assert decision.model == "claude-sonnet-4-6"
    assert decision.continues_from is None
    # Zero LLM calls — the prefix path is deterministic.
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_capture_prefix_is_case_insensitive_and_whitespace_tolerant() -> None:
    """``  Capture: ...`` and ``CAPTURE:...`` both trip the short-circuit."""
    client = FakeAnthropicClient([])
    for opening in ("Capture: foo", "  capture:  bar", "CAPTURE:baz"):
        d = await router.classify_opening_cue(client, opening, [])
        assert d.session_type == "capture", f"opening={opening!r}"
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_llm_classifier_can_route_to_capture() -> None:
    """LLM returning ``capture`` (no prefix) is honoured and threads defaults."""
    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                FakeBlock(
                    type="text",
                    text=(
                        '{"session_type": "capture", '
                        '"continues_from": null, '
                        '"reasoning": "user wants to ramble"}'
                    ),
                )
            ]
        )
    ])
    decision = await router.classify_opening_cue(
        client,
        first_message="let me just ramble through this idea for a bit",
        recent_sessions=[],
    )
    assert decision.session_type == "capture"
    assert decision.model == "claude-sonnet-4-6"
    assert decision.continues_from is None
    # One LLM call — no prefix match.
    assert len(client.messages.calls) == 1
