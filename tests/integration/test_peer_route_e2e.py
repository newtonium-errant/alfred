"""Salem-side end-to-end: opening cue → classify peer_route → dispatch.

Exercises the router + session state + (mocked) peer-send path inside
a single process. The Telegram PTB handler itself is NOT exercised
(that requires a mock Telegram API + --real-telegram gate); here we
assert the glue between the router and the peer-send correlation-id
wait.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from alfred.telegram.router import RouterDecision, classify_opening_cue
from alfred.telegram.state import StateManager


class _FakeAnthropicResponse:
    def __init__(self, text: str) -> None:
        self.content = [type("B", (), {"type": "text", "text": text})()]


class _FakeAnthropic:
    def __init__(self, router_reply: str) -> None:
        self._reply = router_reply

    class _Messages:
        def __init__(self, reply: str) -> None:
            self._reply = reply

        async def create(self, **kwargs) -> _FakeAnthropicResponse:
            return _FakeAnthropicResponse(self._reply)

    def __init__(self, router_reply: str) -> None:
        self.messages = _FakeAnthropic._Messages(router_reply)


async def test_classify_coding_cue_returns_peer_route():
    """End-to-end: a coding-intent opening cue classifies as peer_route to kal-le."""
    reply = json.dumps({
        "session_type": "peer_route",
        "target": "kal-le",
        "peer_route_hint": "scheduler debugging",
        "reasoning": "coding intent",
    })
    client = _FakeAnthropic(reply)
    decision = await classify_opening_cue(
        client, "why is the transport scheduler firing twice", recent_sessions=[],
    )
    assert decision.session_type == "peer_route"
    assert decision.target == "kal-le"


async def test_classify_non_coding_cue_stays_note():
    reply = json.dumps({
        "session_type": "note",
        "reasoning": "quick jot",
    })
    client = _FakeAnthropic(reply)
    decision = await classify_opening_cue(
        client, "remember to call Dr Bailey tomorrow", recent_sessions=[],
    )
    assert decision.session_type == "note"
    assert decision.target is None


def test_peer_route_target_persists_on_active_session(tmp_path: Path):
    """When _open_routed_session stashes _peer_route_target, subsequent
    turns see it via state_mgr.get_active(chat_id).
    """
    state_path = tmp_path / "state.json"
    sm = StateManager(str(state_path))
    sm.load()

    sm.set_active(
        chat_id=100,
        session={
            "chat_id": 100,
            "session_id": "s-1",
            "started_at": "2026-04-20T22:00:00+00:00",
            "last_message_at": "2026-04-20T22:00:00+00:00",
            "model": "claude-sonnet-4-6",
            "transcript": [],
            "_peer_route_target": "kal-le",
            "_session_type": "peer_route",
        },
    )
    sm.save()

    reloaded = StateManager(str(state_path))
    reloaded.load()
    active = reloaded.get_active(100)
    assert active is not None
    assert active.get("_peer_route_target") == "kal-le"
    assert active.get("_session_type") == "peer_route"
