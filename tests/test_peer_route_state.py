"""Peer-route session-state round-trip pins (``StateManager``).

Formerly ``test_peer_route_bot.py``: the dispatcher-shaped tests that
inspected ``alfred.telegram.bot`` died with the Telegram surface
(2026-08-19). What remains asserts the state layer the peer-route flow
still runs on:
    - Session state persists ``_peer_route_target`` after a peer_route
      classification.
    - Session close clears ``_peer_route_target`` (tested via
      ``state.pop_active`` which close_session invokes).

Full end-to-end (classify → dispatch → inbox → relay) lives in
``tests/integration/test_dual_instance.py`` (c10).
"""

from __future__ import annotations

from pathlib import Path


from alfred.telegram.state import StateManager


def test_active_dict_round_trips_peer_route_target(tmp_path: Path):
    """``_peer_route_target`` survives save/load on the active dict."""
    state_path = tmp_path / "talker_state.json"
    sm = StateManager(str(state_path))
    sm.load()

    sm.set_active(
        chat_id=12345,
        session={
            "chat_id": 12345,
            "session_id": "sess-1",
            "started_at": "2026-04-20T22:00:00+00:00",
            "last_message_at": "2026-04-20T22:00:00+00:00",
            "model": "claude-sonnet-4-6",
            "transcript": [],
            "_peer_route_target": "kal-le",
        },
    )
    sm.save()

    # Reload + check.
    sm2 = StateManager(str(state_path))
    sm2.load()
    active = sm2.get_active(12345)
    assert active is not None
    assert active["_peer_route_target"] == "kal-le"


def test_close_session_clears_peer_route_target(tmp_path: Path):
    """pop_active drops the whole active dict → target goes with it.

    close_session() calls state.pop_active at its tail, so the
    peer_route target never leaks into the next session.
    """
    state_path = tmp_path / "talker_state.json"
    sm = StateManager(str(state_path))
    sm.load()

    sm.set_active(
        chat_id=12345,
        session={
            "chat_id": 12345,
            "session_id": "sess-1",
            "_peer_route_target": "kal-le",
            "started_at": "2026-04-20T22:00:00+00:00",
            "last_message_at": "2026-04-20T22:00:00+00:00",
            "model": "claude-sonnet-4-6",
            "transcript": [],
        },
    )
    sm.save()

    sm.pop_active(12345)
    sm.save()

    assert sm.get_active(12345) is None


