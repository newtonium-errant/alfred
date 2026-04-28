"""Integration tests for the talker outbound transport (chunking + failure surfacing).

Triggered by the 2026-04-28 Hypatia silent-drop incident: a 4852-char reply
hit Telegram's 4096-char limit, the bot logged a warning, persisted the
response to the session as if delivered, and the user saw nothing for 73
minutes.

These tests cover the three layers:

1. **Chunking (L1)** — long replies split on paragraph / sentence boundaries
   so each ``sendMessage`` call lands under Telegram's per-message cap.
2. **User-visible alert (L2)** — when any chunk fails, a short alert is
   posted explaining the drop and pointing the user at the eventual
   session record.
3. **Session annotation (L3)** — the active session gains an
   ``outbound_failures`` entry tying the failure to its assistant
   ``turn_index``; successful sends leave the session clean.

All three layers exercised against ``_send_outbound_chunked`` with a
mock ``reply_text`` so no Telegram I/O happens.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import bot
from alfred.telegram.session import Session


def _make_session(chat_id: int = 1, transcript: list | None = None) -> Session:
    now = datetime.now(timezone.utc)
    return Session(
        session_id="abc12345-test-session",
        chat_id=chat_id,
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
        opening_model="claude-sonnet-4-6",
        transcript=transcript or [],
        vault_ops=[],
    )


def _seed_active(state_mgr, session: Session) -> None:
    state_mgr.set_active(session.chat_id, session.to_dict())
    state_mgr.save()


def _make_update(reply_mock) -> MagicMock:
    update = MagicMock()
    update.message.reply_text = reply_mock
    return update


# --- L1: chunking --------------------------------------------------------


@pytest.mark.asyncio
async def test_short_response_sends_in_one_chunk(state_mgr) -> None:
    """A reply under the threshold goes through a single sendMessage call."""
    session = _make_session()
    session.transcript.append({
        "role": "assistant",
        "content": "short text",
        "_ts": session.started_at.isoformat(),
    })
    _seed_active(state_mgr, session)

    reply_mock = AsyncMock()
    update = _make_update(reply_mock)

    await bot._send_outbound_chunked(
        update=update,
        state_mgr=state_mgr,
        session=session,
        chat_id=session.chat_id,
        response_text="hello world",
    )

    assert reply_mock.call_count == 1
    assert reply_mock.call_args.args[0] == "hello world"
    # No outbound_failures stored.
    assert session.outbound_failures == []
    assert state_mgr.get_active(session.chat_id).get("outbound_failures") == []


@pytest.mark.asyncio
async def test_hypatia_incident_4852_chars_chunks_and_succeeds(state_mgr) -> None:
    """4852-char reply (the real Hypatia incident size) splits and delivers.

    Real-world shape: multiple paragraphs (Hypatia's responses are
    structured prose). Two paragraphs of ~2400 chars each → 2 sends, no
    failure, no session annotation.
    """
    session = _make_session()
    session.transcript.append({
        "role": "assistant",
        "content": "(huge response)",
        "_ts": session.started_at.isoformat(),
    })
    _seed_active(state_mgr, session)

    para_a = "A" * 2400
    para_b = "B" * 2400  # together 4803 + 2 separator = 4805
    response_text = f"{para_a}\n\n{para_b}"
    assert len(response_text) > 4096  # confirms we'd hit Telegram's cap

    reply_mock = AsyncMock()
    update = _make_update(reply_mock)

    await bot._send_outbound_chunked(
        update=update,
        state_mgr=state_mgr,
        session=session,
        chat_id=session.chat_id,
        response_text=response_text,
    )

    # 2 sends instead of 1; both succeeded.
    assert reply_mock.call_count == 2
    # Each chunk under the cap (with headroom).
    for call in reply_mock.call_args_list:
        assert len(call.args[0]) <= bot._OUTBOUND_CHUNK_LIMIT
    # No failure entry stored.
    assert session.outbound_failures == []


# --- L2 + L3: user-visible alert + session annotation --------------------


@pytest.mark.asyncio
async def test_first_chunk_failure_triggers_alert_and_session_annotation(
    state_mgr,
) -> None:
    """Mock Telegram returns 400 on the first send → alert posted, session annotated."""
    session = _make_session(chat_id=42)
    # Three turns: user → assistant → user → assistant (failure attaches
    # to the last assistant turn at index 3).
    session.transcript = [
        {"role": "user", "content": "hi", "_kind": "text"},
        {"role": "assistant", "content": "hello"},
        {"role": "user", "content": "more", "_kind": "text"},
        {"role": "assistant", "content": "(this one fails)"},
    ]
    _seed_active(state_mgr, session)

    # First call (the chunked send) raises; second call (the alert) succeeds.
    reply_mock = AsyncMock(
        side_effect=[Exception("Message is too long"), None]
    )
    update = _make_update(reply_mock)

    response_text = "x" * 100  # short — won't chunk; tests pure send-failure path

    await bot._send_outbound_chunked(
        update=update,
        state_mgr=state_mgr,
        session=session,
        chat_id=session.chat_id,
        response_text=response_text,
    )

    # Two reply calls: chunk (failed) + alert (succeeded).
    assert reply_mock.call_count == 2
    alert_text = reply_mock.call_args_list[1].args[0]
    assert alert_text.startswith("⚠️ Reply failed to deliver via Telegram.")
    assert "abc12345" in alert_text  # short session id reference
    assert "Message is too long" in alert_text  # error inline

    # Session annotation: one outbound_failures entry pointing at the
    # last assistant turn (index 3), zero chunks delivered.
    assert len(session.outbound_failures) == 1
    entry = session.outbound_failures[0]
    assert entry["turn_index"] == 3
    assert entry["chunks_attempted"] == 1
    assert entry["chunks_sent"] == 0
    assert entry["error"] == "Message is too long"
    assert entry["delivered"] is False
    assert entry["length"] == len(response_text)
    # Persisted to state too.
    persisted = state_mgr.get_active(session.chat_id)
    assert persisted["outbound_failures"][0]["turn_index"] == 3


@pytest.mark.asyncio
async def test_alert_failure_logs_and_gives_up(state_mgr) -> None:
    """If the alert ALSO fails, we log and give up — no infinite loop."""
    session = _make_session()
    session.transcript.append({
        "role": "assistant",
        "content": "(fails)",
    })
    _seed_active(state_mgr, session)

    # Both calls raise — chunk send and alert send.
    reply_mock = AsyncMock(side_effect=Exception("network down"))
    update = _make_update(reply_mock)

    # Should NOT raise — must swallow the alert failure.
    await bot._send_outbound_chunked(
        update=update,
        state_mgr=state_mgr,
        session=session,
        chat_id=session.chat_id,
        response_text="anything",
    )

    # Two attempts (chunk + alert), no third (no infinite loop).
    assert reply_mock.call_count == 2
    # Session annotation still persisted — the failure record survives
    # even when the alert can't reach the user.
    assert len(session.outbound_failures) == 1


@pytest.mark.asyncio
async def test_successful_chunked_send_leaves_session_clean(state_mgr) -> None:
    """Chunked send that fully succeeds writes no outbound_failures entry."""
    session = _make_session()
    session.transcript.append({
        "role": "assistant",
        "content": "(big but ok)",
    })
    _seed_active(state_mgr, session)

    reply_mock = AsyncMock()
    update = _make_update(reply_mock)

    para_a = "A" * 2400
    para_b = "B" * 2400
    response_text = f"{para_a}\n\n{para_b}"

    await bot._send_outbound_chunked(
        update=update,
        state_mgr=state_mgr,
        session=session,
        chat_id=session.chat_id,
        response_text=response_text,
    )

    assert reply_mock.call_count == 2
    assert session.outbound_failures == []
    # Persisted state mirrors the dataclass.
    assert state_mgr.get_active(session.chat_id)["outbound_failures"] == []


# --- L3 frontmatter integration ------------------------------------------


def test_session_frontmatter_omits_outbound_failures_when_empty() -> None:
    """No failures → field is absent from frontmatter (existing-shape consumers)."""
    from alfred.telegram.session import _build_session_frontmatter
    session = _make_session()
    fm = _build_session_frontmatter(
        session,
        ended_at=session.started_at,
        reason="manual",
    )
    assert "outbound_failures" not in fm


def test_session_frontmatter_includes_outbound_failures_when_present() -> None:
    """Failures present → field round-trips into frontmatter for surfacing tools."""
    from alfred.telegram.session import _build_session_frontmatter
    session = _make_session()
    session.outbound_failures.append({
        "turn_index": 0,
        "timestamp": "2026-04-28T16:00:57.512717+00:00",
        "error": "Message is too long",
        "length": 4852,
        "chunks_attempted": 1,
        "chunks_sent": 0,
        "delivered": False,
    })
    fm = _build_session_frontmatter(
        session,
        ended_at=session.started_at,
        reason="manual",
    )
    assert "outbound_failures" in fm
    assert fm["outbound_failures"][0]["error"] == "Message is too long"
    assert fm["outbound_failures"][0]["delivered"] is False
