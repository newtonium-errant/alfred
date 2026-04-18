"""Tests for the wk2 ``session_type`` / ``continues_from`` frontmatter plumbing.

Exercises :func:`alfred.telegram.session._build_session_frontmatter` and
:func:`alfred.telegram.session.close_session` via the vault — the fields
must land on the record and in the ``closed_sessions`` state summary.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from alfred.telegram import session as talker_session
from alfred.telegram.session import Session


def _make_session(**overrides) -> Session:
    now = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
    defaults = {
        "session_id": "abcdef12-0000-0000-0000-000000000000",
        "chat_id": 1,
        "started_at": now,
        "last_message_at": now,
        "model": "claude-sonnet-4-6",
        "transcript": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        "vault_ops": [],
    }
    defaults.update(overrides)
    return Session(**defaults)  # type: ignore[arg-type]


def test_frontmatter_includes_session_type_and_continues_from() -> None:
    """``_build_session_frontmatter`` emits both wk2 top-level fields."""
    sess = _make_session()
    ended = datetime(2026, 4, 18, 12, 15, tzinfo=timezone.utc)

    fm = talker_session._build_session_frontmatter(
        sess,
        ended_at=ended,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="article",
        continues_from="[[session/Voice Session — 2026-04-17 0900 abc123]]",
    )

    assert fm["session_type"] == "article"
    assert fm["continues_from"] == (
        "[[session/Voice Session — 2026-04-17 0900 abc123]]"
    )
    # Existing wk1 fields must still be present (backwards-compat guard).
    assert fm["telegram"]["model"] == "claude-sonnet-4-6"
    assert fm["type"] == "session"
    assert fm["status"] == "completed"


def test_close_session_threads_fields_into_record_and_state(
    state_mgr, talker_config
) -> None:
    """``close_session`` writes wk2 fields to both the vault record and state.

    Uses the pytest ``state_mgr`` + ``talker_config`` fixtures (tmp_path
    backed), so this round-trips through :mod:`alfred.vault.ops` and touches
    the real YAML dump path.
    """
    chat_id = 42
    now = datetime(2026, 4, 18, 13, 30, tzinfo=timezone.utc)

    # Seed an active session manually — simulate the bot's
    # ``_open_session_with_stash`` having stashed the wk2 fields.
    active = {
        "session_id": "deadbeef-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-opus-4-7",
        "transcript": [{"role": "user", "content": "let's continue the draft"}],
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "article",
        "_continues_from": "[[session/Voice Session — prior]]",
    }
    state_mgr.set_active(chat_id, active)
    state_mgr.save()

    rel_path = talker_session.close_session(
        state_mgr,
        vault_path_root=talker_config.vault.path,
        chat_id=chat_id,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="article",
        continues_from="[[session/Voice Session — prior]]",
    )

    record = Path(talker_config.vault.path) / rel_path
    post = frontmatter.load(str(record))
    assert post["session_type"] == "article"
    assert post["continues_from"] == "[[session/Voice Session — prior]]"
    assert post["telegram"]["model"] == "claude-opus-4-7"

    # ``closed_sessions`` summary carries the same two fields so the router
    # can consult state-only in wk2.
    closed = state_mgr.state["closed_sessions"][-1]
    assert closed["session_type"] == "article"
    assert closed["continues_from"] == "[[session/Voice Session — prior]]"
    assert closed["record_path"] == rel_path
