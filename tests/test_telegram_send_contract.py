"""PY-A — the send contract: a skipped send is not a send.

THE BUG THIS LANE CLOSES. ``_send_via_telegram`` answered ``[]`` when the
instance had no Telegram bot. ``[]`` is also what a successful send with zero
recipients returns, so ``[]`` meant two things and every consumer picked the
wrong one. On 2026-08-15 that cost five operator verdicts (a batch-authority
write gated on ``message_ids`` met the dark bot and the retries 409'd). Every
instance has been web-only since 2026-08-14/15, so the branch the old comment
called "not reached in practice" is the only branch that runs.

THE FIX IS A CONTRACT, not seven patches: a send DELIVERS (non-empty message
ids) or it RAISES :class:`TelegramUnavailable`. Nothing returnable means
"nothing was delivered".

THIS FILE PINS THE CONTRACT ITSELF — the send callable and the peer-inbox
relay that calls it in-process. The seven downstream consumers are pinned in
``test_telegram_skip_consumers.py`` (over the HTTP client) and in the transport
server / scheduler files (in-process). The mutation that reds THIS file is the
shipped behaviour: restore ``return []`` in place of the raise in
``build_send_via_telegram``.

EVERY REFUSAL PIN HERE CARRIES ITS POSITIVE CONTROL. "Dark instance ⇒ no
delivery recorded" passes just as well against a build where nothing works at
all, so each test that asserts a skip also proves its nearest admissible
neighbour — the same call with a live bot — still delivers.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog

from alfred.telegram.send import build_send_via_telegram, relay_to_operator
from alfred.transport.exceptions import (
    TELEGRAM_UNAVAILABLE_REASON,
    TelegramUnavailable,
    TransportError,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


class _FakeMessage:
    def __init__(self, message_id: int) -> None:
        self.message_id = message_id


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_message(self, chat_id: int, text: str) -> _FakeMessage:
        self.sent.append({"chat_id": chat_id, "text": text})
        return _FakeMessage(700 + len(self.sent))


class _FakeApp:
    """Stands in for the PTB ``Application`` — the send path only touches
    ``app.bot.send_message``."""

    def __init__(self) -> None:
        self.bot = _FakeBot()


def _live_send():  # type: ignore[no-untyped-def]
    """The positive control: an instance whose bot exists."""
    return build_send_via_telegram(_FakeApp(), floor_seconds=0.0)


def _dark_send():  # type: ignore[no-untyped-def]
    """The instance under test: ``bot_token: ""`` ⇒ no app was ever built."""
    return build_send_via_telegram(None)


def _events(captured: list[dict], name: str) -> list[dict]:
    return [c for c in captured if c.get("event") == name]


# ---------------------------------------------------------------------------
# THE ROOT — the send contract itself
# ---------------------------------------------------------------------------


class TestTheSendContract:
    """Mutation that reds this class: put ``return []`` back in place of the
    ``raise`` in ``build_send_via_telegram``."""

    async def test_a_dark_instance_raises_rather_than_answering_empty(self) -> None:
        with pytest.raises(TelegramUnavailable):
            await _dark_send()(42, "the message nobody got")

    async def test_a_live_instance_returns_real_message_ids(self) -> None:
        """POSITIVE CONTROL for the whole file. Without this, every assertion
        above is equally true of a build where sending is simply broken."""
        assert await _live_send()(42, "hello") == [701]

    async def test_an_unaware_consumer_still_fails_closed(self) -> None:
        """THE SAFETY PROPERTY behind the type choice. ``TelegramUnavailable``
        subclasses ``TransportUnavailable`` ⊂ ``TransportError``, so a consumer
        written before this lane — one that only ever caught transport errors —
        records a non-delivery without knowing the class exists. The old ``[]``
        had the opposite default: silence read as success."""
        with pytest.raises(TransportError):
            await _dark_send()(42, "hi")

    async def test_the_skip_is_never_silent(self) -> None:
        """``feedback_intentionally_left_blank``: a non-delivery needs a
        signature of its own, at WARNING (production log level), carrying who
        it was for."""
        with structlog.testing.capture_logs() as captured:
            with pytest.raises(TelegramUnavailable):
                await _dark_send()(4242, "hi")
        matches = _events(captured, "talker.daemon.telegram_send_skipped")
        assert len(matches) == 1
        assert matches[0]["user_id"] == 4242
        assert matches[0]["log_level"] == "warning"
        assert "NOTHING WAS DELIVERED" in matches[0]["detail"]

    async def test_a_live_send_does_not_log_a_skip(self) -> None:
        with structlog.testing.capture_logs() as captured:
            await _live_send()(42, "hi")
        assert _events(captured, "talker.daemon.telegram_send_skipped") == []

    async def test_an_api_failure_is_still_its_own_thing(self) -> None:
        """A dark channel and a broken one must stay distinguishable — that is
        the whole reason the skip is a NARROW type rather than a bare raise."""
        app = _FakeApp()

        async def _boom(chat_id: int, text: str):  # type: ignore[no-untyped-def]
            raise RuntimeError("429 Too Many Requests")

        app.bot.send_message = _boom  # type: ignore[assignment]
        send = build_send_via_telegram(app, floor_seconds=0.0)
        with pytest.raises(RuntimeError):
            await send(42, "hi")
        # ...and it is NOT swallowed into the skip type.
        with pytest.raises(RuntimeError):
            await send(42, "hi")


# ---------------------------------------------------------------------------
# CONSUMER 4 — the peer-inbox relay ACK
# ---------------------------------------------------------------------------


class TestPeerRelayAck:
    """The ACK is the sending instance's only evidence. Shipped behaviour:
    ``{"relayed": True, "message_ids": []}`` on a dark bot — a delivery
    recorded for a message the operator never saw."""

    async def test_a_dark_relay_acks_not_relayed_with_a_reason(self) -> None:
        ack = await relay_to_operator(
            _dark_send(), 42, "S.A.L.E.M.: the roof is on fire",
            kind="notice", precedence="R", from_peer="salem",
        )
        assert ack["relayed"] is False
        assert ack["reason"] == TELEGRAM_UNAVAILABLE_REASON
        assert "message_ids" not in ack

    async def test_a_live_relay_still_acks_relayed(self) -> None:
        ack = await relay_to_operator(
            _live_send(), 42, "S.A.L.E.M.: the roof is on fire",
            kind="notice", precedence="R", from_peer="salem",
        )
        assert ack["relayed"] is True
        assert ack["message_ids"] == [701]

    async def test_a_broken_relay_is_distinguishable_from_a_dark_one(self) -> None:
        """Both ack ``relayed: False``; only the dark one carries the reason,
        which is what tells the sending peer "do not retry, that instance has
        no Telegram" rather than "try again later"."""

        async def _boom(user_id: int, text: str, dedupe_key: str | None = None):
            raise RuntimeError("connection reset")

        ack = await relay_to_operator(
            _boom, 42, "x", kind="message", precedence="R", from_peer="salem",
        )
        assert ack["relayed"] is False
        assert ack.get("reason") != TELEGRAM_UNAVAILABLE_REASON
        assert "connection reset" in ack["error"]

    async def test_the_dark_relay_says_so_in_the_log(self) -> None:
        with structlog.testing.capture_logs() as captured:
            await relay_to_operator(
                _dark_send(), 42, "x",
                kind="notice", precedence="R", from_peer="salem",
                correlation_id="corr-1",
            )
        matches = _events(
            captured, "talker.daemon.peer_inbox_relay_skipped_no_telegram",
        )
        assert len(matches) == 1
        assert matches[0]["from_peer"] == "salem"
        assert matches[0]["correlation_id"] == "corr-1"
        assert matches[0]["reason"] == TELEGRAM_UNAVAILABLE_REASON
