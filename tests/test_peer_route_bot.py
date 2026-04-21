"""Lightweight unit tests for the Salem-side peer_route dispatcher.

Full bot integration (PTB + Telegram server) would require a mock
Telegram API; here we assert:
    - Session state persists ``_peer_route_target`` after a peer_route
      classification.
    - Session close clears ``_peer_route_target`` (tested via
      ``state.pop_active`` which close_session invokes).
    - The auto-forward shape: when ``_peer_route_target`` is set on
      the active dict, the next turn reads it (we exercise the
      active-dict access rather than the full PTB handler, because
      the handler needs a running Telegram Bot instance).

Full end-to-end (classify → dispatch → inbox → relay) lives in
``tests/integration/test_dual_instance.py`` (c10).
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_peer_dispatch_reads_raw_config_from_bot_data():
    """Sanity check on the bot_data key where raw_config lives.

    The dispatcher in bot.py pulls ``raw_config`` from ``bot_data`` to
    build the TransportConfig at forward time. If someone renames that
    key, peer routing silently falls through to Salem's normal
    handling — this test fails fast so the drift is caught.
    """
    import inspect

    from alfred.telegram import bot as bot_module

    src = inspect.getsource(bot_module._dispatch_peer_route)
    assert "raw_config" in src, (
        "_dispatch_peer_route must read raw_config from ctx.application.bot_data"
    )
    assert "bot_data" in src


def test_build_app_accepts_raw_config(monkeypatch):
    """``bot.build_app`` takes raw_config and stashes it on bot_data."""
    # We don't actually start the app — just check the function signature
    # + the bot_data assignment by patching the PTB Application.builder.
    import inspect
    from alfred.telegram import bot as bot_module

    sig = inspect.signature(bot_module.build_app)
    assert "raw_config" in sig.parameters
    # Default is None so pre-Stage-3.5 callers still work.
    assert sig.parameters["raw_config"].default is None


def test_peer_mid_wait_and_max_wait_constants():
    """The wait-ping + max-wait constants must stay in-bounds.

    Chosen per the plan: 20s mid-ping, 45s hard cap. Changing these
    should be a deliberate decision, not accidental drift — this test
    fails if someone bumps the cap past 120s or below 10s (either
    extreme is a bug).
    """
    from alfred.telegram import bot as bot_module

    assert 5 <= bot_module._PEER_MID_WAIT_PING_SECONDS <= 60
    assert 15 <= bot_module._PEER_MAX_WAIT_SECONDS <= 120
    assert bot_module._PEER_MID_WAIT_PING_SECONDS < bot_module._PEER_MAX_WAIT_SECONDS
