"""Tests for :mod:`alfred.telegram.router`.

Uses ``FakeAnthropicClient`` (from conftest) so the router's one Sonnet
call never hits the network. Four scenarios cover the main branches:
happy path, continuation validation, parse fallback, API fallback.
"""

from __future__ import annotations

import pytest

from alfred.telegram import router
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


def _json_response(payload: str) -> FakeResponse:
    """Return a FakeResponse carrying one text block with ``payload``."""
    return FakeResponse(content=[FakeBlock(type="text", text=payload)])


@pytest.mark.asyncio
async def test_classifies_task_from_cue() -> None:
    """A clear 'create a task' cue routes to ``task`` on Sonnet."""
    client = FakeAnthropicClient([
        _json_response(
            '{"session_type": "task", "continues_from": null, '
            '"reasoning": "user asked to create a task"}'
        )
    ])
    decision = await router.classify_opening_cue(
        client,
        first_message="Create a task to call Dr. Bailey tomorrow.",
        recent_sessions=[],
    )
    assert decision.session_type == "task"
    assert decision.model == "claude-sonnet-4-6"
    assert decision.continues_from is None
    # The router should always use the pinned router model for its own call.
    assert client.messages.calls[0]["model"] == router.ROUTER_MODEL


@pytest.mark.asyncio
async def test_continuation_validated_against_recent_sessions() -> None:
    """Router only trusts ``continues_from`` when the path is in recent state."""
    recent = [
        {
            "record_path": "session/Voice Session — 2026-04-17 0900 abc123.md",
            "session_type": "article",
            "started_at": "2026-04-17T09:00:00+00:00",
        },
    ]
    # Case 1: router returns a known path → honoured.
    client_ok = FakeAnthropicClient([
        _json_response(
            '{"session_type": "article", '
            '"continues_from": "session/Voice Session — 2026-04-17 0900 abc123.md", '
            '"reasoning": "continue the article"}'
        )
    ])
    decision_ok = await router.classify_opening_cue(
        client_ok,
        first_message="Let's continue the article.",
        recent_sessions=recent,
    )
    assert decision_ok.session_type == "article"
    assert decision_ok.model == "claude-opus-4-7"
    assert decision_ok.continues_from == (
        "session/Voice Session — 2026-04-17 0900 abc123.md"
    )

    # Case 2: router hallucinates a path → dropped, but type stays article
    # on Opus (plan open question #8 — intent trumps absence).
    client_bad = FakeAnthropicClient([
        _json_response(
            '{"session_type": "article", '
            '"continues_from": "session/Not A Real Session.md", '
            '"reasoning": "hallucinated"}'
        )
    ])
    decision_bad = await router.classify_opening_cue(
        client_bad,
        first_message="Let's continue the article.",
        recent_sessions=recent,
    )
    assert decision_bad.session_type == "article"
    assert decision_bad.model == "claude-opus-4-7"
    assert decision_bad.continues_from is None


@pytest.mark.asyncio
async def test_parse_failure_falls_back_to_note() -> None:
    """Non-JSON output from the router → ``note`` / Sonnet / no continuation."""
    client = FakeAnthropicClient([
        _json_response("Sure! I think this is a journal session.\nGo ahead!"),
    ])
    decision = await router.classify_opening_cue(
        client,
        first_message="I want to reflect on the week.",
        recent_sessions=[],
    )
    assert decision.session_type == "note"
    assert decision.model == "claude-sonnet-4-6"
    assert decision.continues_from is None
    assert "parse" in decision.reasoning.lower()


@pytest.mark.asyncio
async def test_api_error_falls_back_to_note() -> None:
    """A raised exception from the SDK → ``note`` fallback, no crash."""

    class BoomMessages:
        async def create(self, **kwargs):
            raise RuntimeError("429 rate limited")

    class BoomClient:
        messages = BoomMessages()

    decision = await router.classify_opening_cue(
        BoomClient(),
        first_message="quick note — buy milk",
        recent_sessions=[],
    )
    assert decision.session_type == "note"
    assert decision.model == "claude-sonnet-4-6"
    assert decision.continues_from is None
    assert "api" in decision.reasoning.lower()
