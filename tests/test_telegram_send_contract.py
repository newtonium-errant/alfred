"""PY-A — the send contract: a skipped send is not a send.

THE BUG THIS LANE CLOSES. ``_send_via_telegram`` answered ``[]`` when the
instance had no Telegram bot. ``[]`` is also what a successful send with zero
recipients returns, so ``[]`` meant two things and every consumer picked the
wrong one. On 2026-08-15 that cost five operator verdicts (a batch-authority
write gated on ``message_ids`` met the dark bot and the retries 409'd).

THE FIX IS A CONTRACT, not seven patches: a send DELIVERS (non-empty message
ids) or it RAISES :class:`TelegramUnavailable`. Nothing returnable means
"nothing was delivered".

TELEGRAM RETIREMENT (T4 C4, 2026-08-19). The delivering branch is DELETED —
tokens revoked, ``alfred.telegram.bot`` gone, ``build_send_via_telegram()``
takes no ``app`` and the callable raises on every call. The contract is
unchanged; the DELIVER arm just stopped being reachable through Telegram.
Consequences for this file:

  * The send callable's live-delivery tests are retired WITH the branch —
    there is no live path left to control against. The positive controls
    move down a level: the pins below prove the raise is the TYPED raise
    (not an import error, not a crashed factory), that the skip log fires
    with its fields, and that ``relay_to_operator`` still produces all
    THREE ack shapes (its delivered shape driven by a stub ``send_fn``,
    which is honest — relay accepts any conforming callable).
  * New omitted-behavior pins hold the deletion itself: the module must
    not regrow a delivery arm (no ``send_message`` call, no ``app``
    parameter), and per the deletion-pin rule they sit NEXT TO live-path
    proof so a dead import can't green them vacuously.

The mutation that reds this file is still the shipped behaviour: replace the
``raise`` in ``build_send_via_telegram`` with ``return []``.

The seven downstream consumers are pinned in
``test_telegram_skip_consumers.py`` (over the HTTP client) and in the
transport server / scheduler files (in-process).
"""

from __future__ import annotations

import inspect

import pytest
import structlog

import alfred.telegram.send as send_mod
from alfred.telegram.send import build_send_via_telegram, relay_to_operator
from alfred.transport.exceptions import (
    TELEGRAM_UNAVAILABLE_REASON,
    TelegramUnavailable,
    TransportError,
)


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _dark_send():  # type: ignore[no-untyped-def]
    """The only send there is: Telegram retired, every call raises."""
    return build_send_via_telegram()


async def _stub_delivering_send(
    user_id: int, text: str, dedupe_key: str | None = None,
) -> list[int]:
    """A conforming DELIVERING callable for relay_to_operator's live shape.

    Not a Telegram path — there is none — but the relay contract is defined
    over any ``TelegramSendCallable``, and its delivered ACK shape must stay
    pinned or a regression in relay logic hides behind the dark channel.
    """
    return [701]


def _events(captured: list[dict], name: str) -> list[dict]:
    return [c for c in captured if c.get("event") == name]


# ---------------------------------------------------------------------------
# THE ROOT — the send contract itself
# ---------------------------------------------------------------------------


class TestTheSendContract:
    """Mutation that reds this class: put ``return []`` back in place of the
    ``raise`` in ``build_send_via_telegram``."""

    async def test_every_send_raises_rather_than_answering_empty(self) -> None:
        with pytest.raises(TelegramUnavailable):
            await _dark_send()(42, "the message nobody got")

    async def test_the_raise_is_typed_and_names_the_recipient(self) -> None:
        """POSITIVE CONTROL for the deletion pins below: the factory builds a
        LIVE callable whose refusal carries the user id — proving the path
        executes (a broken import or a crashed factory could not produce
        this), which is what licenses the source-level absence assertions."""
        with pytest.raises(TelegramUnavailable, match="user_id=42"):
            await _dark_send()(42, "hello")

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
        it was for. Event name + detail phrasing are a stable grep surface —
        daily_sync's authority-write comment references them."""
        with structlog.testing.capture_logs() as captured:
            with pytest.raises(TelegramUnavailable):
                await _dark_send()(4242, "hi")
        matches = _events(captured, "talker.daemon.telegram_send_skipped")
        assert len(matches) == 1
        assert matches[0]["user_id"] == 4242
        assert matches[0]["log_level"] == "warning"
        assert "NOTHING WAS DELIVERED" in matches[0]["detail"]


# ---------------------------------------------------------------------------
# THE DELETION — the delivering branch must stay deleted (T4 C4)
# ---------------------------------------------------------------------------


class TestTheDeliveringBranchStaysDeleted:
    """Omitted-behavior pins. Their live-path positive control is
    ``test_the_raise_is_typed_and_names_the_recipient`` above — the module
    imports, the factory runs, the callable executes; these assertions are
    about what that RUNNING module no longer contains."""

    def test_the_factory_takes_no_app(self) -> None:
        """Signature pin: a resurrected ``app`` / lock-map / floor parameter
        is the delivery arm growing back."""
        params = inspect.signature(build_send_via_telegram).parameters
        assert len(params) == 0, (
            f"build_send_via_telegram grew parameters {list(params)} — the "
            "delivering branch was deleted in T4 C4; a send app has no home "
            "here any more."
        )

    def test_the_module_contains_no_delivery_code(self) -> None:
        """AST sweep, not a raw-source grep: the module docstring is ALLOWED
        to mention ``send_message`` as history (it does), so the pin matches
        code referents — attribute accesses and names — never prose."""
        import ast

        tree = ast.parse(inspect.getsource(send_mod))
        forbidden: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in (
                "send_message", "Lock",
            ):
                forbidden.append(f"line {node.lineno}: .{node.attr}")
            if isinstance(node, ast.Name) and node.id == "SEND_FLOOR_SECONDS":
                forbidden.append(f"line {node.lineno}: SEND_FLOOR_SECONDS")
        assert forbidden == [], (
            "delivery machinery reappeared in alfred.telegram.send "
            f"(deleted in T4 C4): {forbidden}"
        )


# ---------------------------------------------------------------------------
# CONSUMER 4 — the peer-inbox relay ACK
# ---------------------------------------------------------------------------


class TestPeerRelayAck:
    """The ACK is the sending instance's only evidence. Three outcomes,
    three shapes — all three still pinned: dark through the real collapsed
    send, delivered through a stub ``send_fn`` (see its docstring), broken
    through a raising stub."""

    async def test_a_dark_relay_acks_not_relayed_with_a_reason(self) -> None:
        ack = await relay_to_operator(
            _dark_send(), 42, "S.A.L.E.M.: the roof is on fire",
            kind="notice", precedence="R", from_peer="salem",
        )
        assert ack["relayed"] is False
        assert ack["reason"] == TELEGRAM_UNAVAILABLE_REASON
        assert "message_ids" not in ack

    async def test_a_delivering_send_fn_still_acks_relayed(self) -> None:
        """POSITIVE CONTROL for the relay's dark shape: the same relay code
        with a delivering callable ACKs ``relayed: True`` + the ids — so the
        dark assertions above cannot be satisfied by a relay that answers
        ``relayed: False`` to everything."""
        ack = await relay_to_operator(
            _stub_delivering_send, 42, "S.A.L.E.M.: the roof is on fire",
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
