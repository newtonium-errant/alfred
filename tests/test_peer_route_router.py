"""Tests for the c7 peer_route router extension."""

from __future__ import annotations

from typing import Any

import pytest

from alfred.telegram.router import (
    RouterDecision,
    _decision_from_parsed,
    classify_opening_cue,
)
from alfred.telegram.session_types import defaults_for, known_types


def test_peer_route_in_known_types():
    """peer_route is now a registered session type."""
    assert "peer_route" in known_types()


def test_peer_route_defaults():
    d = defaults_for("peer_route")
    assert d.session_type == "peer_route"
    assert d.supports_continuation is True
    assert d.pushback_level == 0


# ---------------------------------------------------------------------------
# _decision_from_parsed — peer_route handling
# ---------------------------------------------------------------------------


def test_peer_route_with_valid_target():
    parsed = {
        "session_type": "peer_route",
        "target": "kal-le",
        "peer_route_hint": "coding bug report",
        "reasoning": "refactor request",
    }
    decision = _decision_from_parsed(parsed, recent=[])
    assert decision.session_type == "peer_route"
    assert decision.target == "kal-le"
    assert decision.peer_route_hint == "coding bug report"


def test_peer_route_target_lowercased():
    """Uppercase target → lowercased (aliases).

    The router prompt says kal-le but nothing stops the model from
    echoing KAL-LE. Normalise to lowercase.
    """
    parsed = {
        "session_type": "peer_route",
        "target": "KAL-LE",
    }
    decision = _decision_from_parsed(parsed, recent=[])
    assert decision.target == "kal-le"


def test_peer_route_unknown_target_degrades_to_note():
    """Phantom target → fall back to note (safer than forwarding nowhere)."""
    parsed = {
        "session_type": "peer_route",
        "target": "knowledge-alfred-that-doesnt-exist-yet",
    }
    decision = _decision_from_parsed(parsed, recent=[])
    assert decision.session_type == "note"
    assert decision.target is None


def test_peer_route_missing_target_degrades_to_note():
    parsed = {"session_type": "peer_route"}
    decision = _decision_from_parsed(parsed, recent=[])
    assert decision.session_type == "note"
    assert decision.target is None


def test_peer_route_null_target_degrades_to_note():
    parsed = {"session_type": "peer_route", "target": None}
    decision = _decision_from_parsed(parsed, recent=[])
    assert decision.session_type == "note"


def test_non_peer_route_target_is_ignored():
    """Target only populates for peer_route types."""
    parsed = {"session_type": "note", "target": "kal-le"}
    decision = _decision_from_parsed(parsed, recent=[])
    assert decision.target is None


# ---------------------------------------------------------------------------
# classify_opening_cue end-to-end with a fake client
# ---------------------------------------------------------------------------


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeBlock(text)]


class _FakeMessagesClient:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    async def create(self, **kwargs) -> _FakeResponse:
        return _FakeResponse(self._reply)


class _FakeClient:
    def __init__(self, reply: str) -> None:
        self.messages = _FakeMessagesClient(reply)


@pytest.mark.parametrize("cue,target", [
    ("why is the transport scheduler firing twice", "kal-le"),
    ("refactor the janitor fix command", "kal-le"),
    ("promote the retry pattern to canonical aftermath-lab", "kal-le"),
    ("run the tests and tell me what fails", "kal-le"),
    ("review the last three commits", "kal-le"),
])
async def test_classify_peer_route_cues_return_kal_le(cue, target):
    """Coding-intent cues route to kal-le via the real classifier."""
    import json

    reply = json.dumps({
        "session_type": "peer_route",
        "target": target,
        "peer_route_hint": "coding intent",
        "reasoning": cue[:40],
    })
    client = _FakeClient(reply)
    decision = await classify_opening_cue(client, cue, recent_sessions=[])
    assert decision.session_type == "peer_route"
    assert decision.target == target


async def test_classify_phantom_target_falls_back_to_note():
    import json

    reply = json.dumps({
        "session_type": "peer_route",
        "target": "never-existed",
        "reasoning": "wrong target",
    })
    client = _FakeClient(reply)
    decision = await classify_opening_cue(
        client, "forward this please", recent_sessions=[],
    )
    # Router dropped back to note when target failed validation.
    assert decision.session_type == "note"
    assert decision.target is None


async def test_classify_non_coding_cue_stays_note():
    """A note-y opening shouldn't suddenly start routing to kal-le."""
    import json

    reply = json.dumps({
        "session_type": "note",
        "target": None,
        "reasoning": "quick note",
    })
    client = _FakeClient(reply)
    decision = await classify_opening_cue(
        client, "jot this down: dog food supplies low", recent_sessions=[],
    )
    assert decision.session_type == "note"
    assert decision.target is None


# ---------------------------------------------------------------------------
# RouterDecision dataclass surface
# ---------------------------------------------------------------------------


def test_router_decision_new_fields_default_sensibly():
    """New target/peer_route_hint fields must preserve backcompat."""
    # Construct without the new args — should still work.
    d = RouterDecision(
        session_type="note",
        model="claude-sonnet-4-6",
        continues_from=None,
    )
    assert d.target is None
    assert d.peer_route_hint == ""
