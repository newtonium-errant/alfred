"""Exception hierarchy for the transport client.

Every failure path raises one of these — callers can either catch the
base :class:`TransportError` or narrow to a specific subclass when a
more targeted recovery is warranted (e.g. the brief daemon catches
only :class:`TransportUnavailable` for "log and continue", and lets
:class:`TransportAuthMissing` propagate so misconfiguration is loud).
"""

from __future__ import annotations


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
