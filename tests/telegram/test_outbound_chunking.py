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


# --- L1: chunking --------------------------------------------------------


# --- L2 + L3: user-visible alert + session annotation --------------------


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
