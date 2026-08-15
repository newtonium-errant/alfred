"""Exception hierarchy for the transport client.

Every failure path raises one of these — callers can either catch the
base :class:`TransportError` or narrow to a specific subclass when a
more targeted recovery is warranted (e.g. the brief daemon catches
only :class:`TransportUnavailable` for "log and continue", and lets
:class:`TransportAuthMissing` propagate so misconfiguration is loud).

One member, :class:`TelegramUnavailable`, is dual-role: it is raised
IN-PROCESS by the talker's send callable (``alfred.telegram.send``) and
again CLIENT-SIDE when the server reports the same condition over HTTP.
One definition, so the in-process consumers (transport server handlers,
scheduler, peer relay) and the over-the-wire consumers (pending items,
email classifier, brief, CLI) spell the same condition the same way.
"""

from __future__ import annotations


# The single spelling of "this instance has no Telegram bot". Shared by the
# exception below, the server's 503 body, the client's no-retry set, the
# scheduler's dead-letter reason, and the peer relay's ACK — lifted to one
# constant BEFORE the second consumer existed, because there are six.
TELEGRAM_UNAVAILABLE_REASON = "telegram_unavailable"


class TransportError(Exception):
    """Base class for every client-side failure."""


class TransportAuthMissing(TransportError):
    """``ALFRED_TRANSPORT_TOKEN`` not in environment.

    The orchestrator is expected to inject this env var into every
    tool subprocess. Raising here makes a mis-configured deploy loud
    at first send attempt instead of silently 401-looping forever.
    """


class TransportServerDown(TransportError):
    """Connection refused or DNS failure — the server isn't up.

    Distinct from :class:`TransportUnavailable` so the brief daemon
    and scheduler can log-and-continue without confusing
    "talker daemon isn't running" with "upstream Anthropic API is
    timing out".
    """


class TransportRejected(TransportError):
    """The server returned a 4xx — do NOT retry.

    Includes 401 (bad token), 400 (payload schema error), 404
    (status id not found), and any other client-fault 4xx. Caller
    must fix the request before retrying — the retry wrapper never
    retries a 4xx.
    """

    def __init__(self, message: str, status_code: int, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class TransportUnavailable(TransportError):
    """The server returned a 5xx or a 503 ``telegram_not_configured``.

    The retry wrapper retries once on this before giving up. Brief
    dispatch catches this category to log-and-continue — the brief is
    still in the vault, it just didn't push out.
    """


class TelegramUnavailable(TransportUnavailable):
    """There is no Telegram bot on this instance — NOTHING was delivered.

    Raised by the talker's send callable when the instance runs web-only
    (``bot_token: ""``, which is every instance since 2026-08-14/15), and
    raised again client-side when the transport server answers
    ``503 {"status": "skipped", "reason": "telegram_unavailable"}``.

    WHY IT IS AN EXCEPTION AND NOT A RETURN VALUE. The send path used to
    answer ``[]`` for this case, which is indistinguishable from a
    successful send with zero recipients — so seven consumers read a
    non-delivery as a delivery, and one of them destroyed the content it
    was recovering. A value that means "nothing happened" will eventually
    be read as "nothing went wrong"; a raise cannot be.

    WHY IT SUBCLASSES ``TransportUnavailable``. Every consumer that
    already caught ``TransportError`` fails CLOSED the day this ships —
    it records a non-delivery without knowing this class exists. The
    narrow type is then how a consumer that CARES tells a CONFIGURED-DARK
    channel from a broken one, and the difference is load-bearing: dark
    is permanent for the process lifetime, so retrying cannot help and
    re-parking forever is worse than dead-lettering honestly.
    """

    #: Machine-readable discriminator; mirrored in the server's 503 body,
    #: the scheduler's dead-letter reason, and the relay ACK.
    reason = TELEGRAM_UNAVAILABLE_REASON
