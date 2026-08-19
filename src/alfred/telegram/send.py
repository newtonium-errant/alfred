"""The Telegram send leg — and the typed signal for when there isn't one.

WHY THIS MODULE EXISTS. The talker's send callable used to be a closure inside
``run()`` that answered ``[]`` when the instance had no bot. ``[]`` is exactly
what a successful send with zero recipients would produce, so every consumer
read it as SUCCESS. On 2026-08-15 that cost five operator verdicts: a
batch-authority write recorded ``message_ids`` it never got and the retries
409'd. A discovery sweep found the same read in six more places — the
pending-items executor marked an item RESOLVED and destroyed it, the email
classifier recorded ``pushed_to_telegram=True`` for mail nobody received, the
scheduler stamped ``sent_at`` rows and dead reminders, the peer relay ACKed
``relayed: True`` to the sending instance, and ``brief.pushed`` and
``alfred transport send-test`` both claimed success on a dead channel.

The old guard's comment said the branch was "not reached in practice." Every
instance has been web-only since 2026-08-14/15 (``bot_token: ""`` +
``web.web_only: true``), so it is not a guard at all — it is the hot path.

TELEGRAM RETIREMENT (2026-08-18; delivery machinery removed 2026-08-19, T4
C4). Every BotFather token is revoked and ``alfred.telegram.bot`` is deleted,
so the DELIVER arm of the contract is no longer reachable: there is no PTB
``Application`` to send through, on any instance, by construction. The
delivering branch this module used to carry (per-chat locks, the 250ms
rate-limit floor, ``app.bot.send_message``) is gone with it — the callable
now raises on every call. The CONTRACT below is unchanged and the raise is
the same raise consumers were already built against; what changed is that
the other branch stopped existing. ``relay_to_operator`` keeps its three ACK
shapes (its ``send_fn`` parameter accepts any conforming callable, which is
also how the delivered shape stays testable).

THE CONTRACT, stated once so consumers can rely on it:

    A send either DELIVERS — returning a non-empty list of Telegram message
    ids — or it RAISES. There is no third answer. In particular there is no
    return value that means "nothing was delivered": that state is
    :class:`~alfred.transport.exceptions.TelegramUnavailable`.

Because that exception subclasses ``TransportUnavailable`` ⊂ ``TransportError``,
a consumer that already catches transport failures fails CLOSED without knowing
this class exists; a consumer that wants to tell a CONFIGURED-DARK channel from
a broken one narrows to the type. Both readings are honest. The one reading
that is no longer expressible is the one that caused the incident.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from alfred.transport.exceptions import (
    TELEGRAM_UNAVAILABLE_REASON,
    TelegramUnavailable,
)

from .utils import get_logger

log = get_logger(__name__)


# (user_id: int, text: str, dedupe_key: str | None) -> Awaitable[list[int]]
TelegramSendCallable = Callable[..., Awaitable[list[int]]]


def build_send_via_telegram() -> TelegramSendCallable:
    """Build the talker's send callable — which, post-retirement, always raises.

    Telegram is retired: no bot is ever built, so the returned callable
    raises :class:`~alfred.transport.exceptions.TelegramUnavailable` on every
    call rather than answering ``[]`` — see the module docstring for the
    seven consumers that read that ``[]`` as a delivery.

    Still a module-level factory (not an inline closure, not a bare function)
    for the same reason as before: the contract has a home and a test that
    does not need a daemon, and the daemon-side wiring pin
    (``test_talker_transport_wiring``) keeps asserting the send leg comes
    from here.
    """

    async def _send_via_telegram(
        user_id: int, text: str, dedupe_key: str | None = None,
    ) -> list[int]:
        """Refuse one Telegram send, loudly and typed.

        Raises :class:`TelegramUnavailable` unconditionally: the Telegram
        surface is retired (tokens revoked 2026-08-18, bot deleted
        2026-08-19). Never returns.
        """
        # THE SKIP. Loud per send, deliberately: this is a non-delivery,
        # and ``feedback_intentionally_left_blank`` says a non-event needs
        # a signature of its own. The consumers each log their own
        # disposition on top of this line — what they did about it is the
        # part an operator greps for. Event name + detail phrasing are a
        # STABLE grep surface (daily_sync's authority-write comment and
        # operator runbooks reference them) — do not reword casually.
        log.warning(
            "talker.daemon.telegram_send_skipped",
            detail=(
                "web-only mode (no bot_token) — NOTHING WAS DELIVERED. "
                "This is a non-delivery, never a send with zero "
                "recipients."
            ),
            user_id=user_id,
            dedupe_key=dedupe_key or "",
            text_len=len(text),
        )
        raise TelegramUnavailable(
            "no Telegram bot on this instance (web-only): nothing was "
            f"delivered to user_id={user_id}",
        )

    return _send_via_telegram


async def relay_to_operator(
    send_fn: TelegramSendCallable,
    user_id: int,
    text: str,
    *,
    kind: str,
    precedence: str,
    from_peer: str,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Relay one peer-inbox message to the operator and build the peer's ACK.

    The ACK is the SENDING instance's only evidence of what happened to its
    message, so it must never claim a relay that did not occur. Three
    outcomes, three shapes:

      * delivered → ``relayed: True`` plus the Telegram ``message_ids``
      * dark      → ``relayed: False`` plus ``reason: "telegram_unavailable"``
      * failed    → ``relayed: False`` plus ``error``

    Before the skip was typed, the dark case produced ``{"relayed": True,
    "message_ids": []}`` — a delivery recorded, on the peer, for a message the
    operator never saw. The dark and failed shapes stay distinguishable
    because only one of them is worth retrying: a peer that reads ``reason ==
    "telegram_unavailable"`` knows that instance has no Telegram at all.

    Lives here rather than inline in the daemon's peer-inbox closure so the
    ACK contract is testable without standing up a daemon.
    """
    ack: dict[str, Any] = {"kind": kind, "precedence": precedence}
    try:
        msg_ids = await send_fn(user_id, text)
    except TelegramUnavailable as exc:
        log.warning(
            "talker.daemon.peer_inbox_relay_skipped_no_telegram",
            kind=kind,
            precedence=precedence,
            from_peer=from_peer,
            correlation_id=correlation_id,
            reason=TELEGRAM_UNAVAILABLE_REASON,
            detail=(
                "no Telegram bot on this instance — the peer message was NOT "
                "relayed to the operator."
            ),
        )
        return {
            **ack,
            "relayed": False,
            "reason": TELEGRAM_UNAVAILABLE_REASON,
            "error": str(exc),
        }
    except Exception as exc:  # noqa: BLE001 — a relay failure still ACKs the sender
        log.warning(
            "talker.daemon.peer_inbox_relay_failed",
            kind=kind,
            precedence=precedence,
            from_peer=from_peer,
            error=str(exc),
            correlation_id=correlation_id,
        )
        return {**ack, "relayed": False, "error": str(exc)}
    return {**ack, "relayed": True, "message_ids": msg_ids}


__all__ = [
    "TelegramSendCallable",
    "build_send_via_telegram",
    "relay_to_operator",
]
