"""Regression pins for the daemon shutdown-close sweep (2026-06-12 fix).

Defect: the shutdown sweep in ``run()``'s finally block called
``session.close_session(...)`` WITHOUT ``pushback_level=``, so
shutdown-closed records got ``telegram.pushback_level: null`` while the
other three close paths (bot ``/end``, timeout sweeper, startup sweep)
all passed ``raw.get("_pushback_level")``. Example record:
``vault/session/voice-2026-06-11-okay-confirmed-the-t1-t2-4dc0c94e.md``.

The sweep is now extracted to the module-level
:func:`alfred.telegram.daemon._close_open_sessions_on_shutdown` so this
call-site contract is pinnable without driving the full PTB lifecycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from alfred.telegram.daemon import _close_open_sessions_on_shutdown


def _seed_active_session(
    state_mgr,
    talker_config,
    chat_id: int,
    **stash_overrides,
) -> dict:
    """Seed an active session dict modeled on the bot's open-time stash."""
    now = datetime(2026, 6, 11, 13, 30, tzinfo=timezone.utc)
    active = {
        "session_id": "deadbeef-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-sonnet-4-6",
        "transcript": [{"role": "user", "content": "okay confirmed the T1 T2"}],
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "note",
    }
    active.update(stash_overrides)
    state_mgr.set_active(chat_id, active)
    state_mgr.save()
    return active


async def test_shutdown_close_populates_pushback_level(
    state_mgr, talker_config
) -> None:
    """Regression pin: shutdown-closed records carry the stashed dial.

    Pre-fix, the shutdown path omitted ``pushback_level=`` and every
    shutdown-closed record landed with ``telegram.pushback_level: null``
    even when the session had stashed ``_pushback_level``.
    """
    chat_id = 42
    _seed_active_session(state_mgr, talker_config, chat_id, _pushback_level=1)

    closed = await _close_open_sessions_on_shutdown(
        state_mgr, talker_config, client=None
    )

    assert len(closed) == 1
    record = Path(talker_config.vault.path) / closed[0]
    post = frontmatter.load(str(record))
    assert post["telegram"]["pushback_level"] == 1
    assert post["telegram"]["close_reason"] == "shutdown"
    # The session was popped from active state.
    assert state_mgr.get_active(chat_id) is None


async def test_shutdown_close_without_stash_defaults_to_none(
    state_mgr, talker_config
) -> None:
    """A pre-wk3 session (no ``_pushback_level`` stash) still closes cleanly.

    Pins the ``get()``-default parity with the other close paths: absent
    stash → ``pushback_level: None``, no KeyError, record written.
    """
    chat_id = 7
    _seed_active_session(state_mgr, talker_config, chat_id)

    closed = await _close_open_sessions_on_shutdown(
        state_mgr, talker_config, client=None
    )

    assert len(closed) == 1
    record = Path(talker_config.vault.path) / closed[0]
    post = frontmatter.load(str(record))
    assert post["telegram"]["pushback_level"] is None
    assert post["telegram"]["close_reason"] == "shutdown"
    assert state_mgr.get_active(chat_id) is None
