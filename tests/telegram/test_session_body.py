"""Tests for wk1-polish bug (a): per-turn ``_ts`` on every ``append_turn``.

The session body renderer (``_build_session_body``) pulls ``_ts`` off each
turn to render real HH:MM timestamps. Wk1 forgot to stamp ``_ts`` on most
turns, so long sessions rendered as if every turn happened in one minute.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from alfred.telegram import session as talker_session
from alfred.telegram.session import Session


def _make_session() -> Session:
    now = datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc)
    return Session(
        session_id="abc",
        chat_id=1,
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
    )


def test_append_turn_stamps_ts_on_every_turn(state_mgr) -> None:
    """Every turn appended via ``append_turn`` carries a parseable ``_ts``."""
    sess = _make_session()
    state_mgr.set_active(1, sess.to_dict())

    talker_session.append_turn(state_mgr, sess, "user", "hi", kind="voice")
    # Tiny sleep to guarantee monotonic ``_ts`` on the next turn, so the
    # test is specific about each turn getting its own stamp.
    time.sleep(0.01)
    talker_session.append_turn(state_mgr, sess, "assistant", "hello")
    time.sleep(0.01)
    talker_session.append_turn(state_mgr, sess, "user", "again", kind="text")

    assert len(sess.transcript) == 3
    stamps = [t["_ts"] for t in sess.transcript]
    # All present, all ISO-parseable, all distinct.
    for s in stamps:
        datetime.fromisoformat(s)
    assert len(set(stamps)) == 3

    # Only user turns carry ``_kind``; assistant has no input modality.
    assert sess.transcript[0]["_kind"] == "voice"
    assert "_kind" not in sess.transcript[1]
    assert sess.transcript[2]["_kind"] == "text"


def test_body_renders_distinct_per_turn_timestamps(state_mgr) -> None:
    """``_build_session_body`` uses per-turn ``_ts`` to render HH:MM.

    We stub ``_now_utc`` via monkeypatch-free staging: craft the transcript
    by hand with known ``_ts`` values and confirm the rendered body
    contains the expected timestamps on distinct lines.
    """
    sess = _make_session()
    sess.transcript = [
        {
            "role": "user",
            "content": "First at 10:05",
            "_ts": "2026-04-18T10:05:00+00:00",
            "_kind": "text",
        },
        {
            "role": "assistant",
            "content": "Reply at 10:06",
            "_ts": "2026-04-18T10:06:00+00:00",
        },
        {
            "role": "user",
            "content": "Voice at 10:30",
            "_ts": "2026-04-18T10:30:00+00:00",
            "_kind": "voice",
        },
    ]

    body = talker_session._build_session_body(sess)

    assert "**Andrew** (10:05):" in body
    assert "**Alfred** (10:06):" in body
    assert "**Andrew** (10:30 · voice):" in body
    # Not all turns collapsed to one timestamp.
    assert body.count("(10:05") == 1
    assert body.count("(10:06") == 1
    assert body.count("(10:30") == 1


# --- Tool-result speaker attribution (2026-06-12 fix) ----------------------
#
# Anthropic delivers tool results as USER-role messages carrying
# ``tool_result`` content blocks. The renderer used to attribute every
# user-role turn to **Andrew**, so machine output read as operator speech
# and misled the distiller. Tool results now render under a **Tool**
# speaker; ``tool_use`` stays under **Alfred** (the assistant invoked it).


def test_pure_tool_result_turn_renders_under_tool_speaker() -> None:
    """A user-role turn that is ONLY a tool_result gets no Andrew header."""
    sess = _make_session()
    sess.transcript = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_0123456789",
                    "content": "3 records found",
                },
            ],
            "_ts": "2026-04-18T10:07:00+00:00",
        },
    ]

    body = talker_session._build_session_body(sess)

    assert "**Tool** (10:07): [tool_result: toolu_01…]" in body
    assert "**Andrew** (10:07" not in body


def test_mixed_turn_splits_tool_and_andrew_lines() -> None:
    """tool_result + text in one turn → Tool line AND Andrew line."""
    sess = _make_session()
    sess.transcript = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_0123456789",
                    "content": "ok",
                },
                {"type": "text", "text": "also, remind me about the optometrist"},
            ],
            "_ts": "2026-04-18T10:08:00+00:00",
        },
    ]

    body = talker_session._build_session_body(sess)

    assert "**Tool** (10:08): [tool_result: toolu_01…]" in body
    assert "**Andrew** (10:08): also, remind me about the optometrist" in body
    # The marker must not bleed onto the Andrew line.
    andrew_line = next(
        ln for ln in body.splitlines() if ln.startswith("**Andrew**")
    )
    assert "tool_result" not in andrew_line


def test_error_tool_result_keeps_error_marker_under_tool() -> None:
    """``is_error`` keeps the `` error`` marker, attributed to Tool."""
    sess = _make_session()
    sess.transcript = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_0123456789",
                    "content": "boom",
                    "is_error": True,
                },
            ],
            "_ts": "2026-04-18T10:09:00+00:00",
        },
    ]

    body = talker_session._build_session_body(sess)

    assert "**Tool** (10:09): [tool_result error: toolu_01…]" in body
    assert "**Andrew**" not in body


def test_plain_string_user_turn_still_renders_as_andrew() -> None:
    """Regression pin: ordinary user text keeps the Andrew header."""
    sess = _make_session()
    sess.transcript = [
        {
            "role": "user",
            "content": "plain message",
            "_ts": "2026-04-18T10:10:00+00:00",
            "_kind": "text",
        },
    ]

    body = talker_session._build_session_body(sess)

    assert "**Andrew** (10:10): plain message" in body
    assert "**Tool**" not in body


def test_assistant_tool_use_still_renders_under_alfred() -> None:
    """Regression pin: tool_use stays under Alfred — he invoked it."""
    sess = _make_session()
    sess.transcript = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "name": "vault_search",
                    "input": {"glob": "project/*.md"},
                },
            ],
            "_ts": "2026-04-18T10:11:00+00:00",
        },
    ]

    body = talker_session._build_session_body(sess)

    assert "**Alfred** (10:11): [tool_use: vault_search" in body
    assert "**Tool**" not in body
