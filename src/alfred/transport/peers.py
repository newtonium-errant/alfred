"""Client-side peer dispatch + correlation-id response inbox.

This module holds:
  - :func:`peer_send`, :func:`peer_query`, :func:`peer_handshake` —
    outbound HTTP calls to another instance. Resolve the base URL +
    token from ``transport.peers[<name>]`` and send via the same
    retry-wrapped HTTP primitive the outbound-push client uses.
  - An in-memory response inbox keyed by correlation_id.
    :func:`register_response` / :func:`await_response` implement the
    async-callback pattern a router uses when it wants to wait for an
    async reply instead of re-issuing a blocking request.

Inbox design (Stage 3.5 D8 decision):
    The router-forwarded flow is fire-and-forget at the HTTP layer —
    Salem POSTs ``/peer/send`` to KAL-LE, KAL-LE queues the work, and
    KAL-LE later POSTs back to Salem with ``kind=query_result`` + the
    same correlation_id. Salem's inbox callable registers the reply
    with :func:`register_response`, unblocking whoever is waiting.

Orphan handling:
    If a reply arrives for a correlation_id nobody's waiting on, we
    stash it in a bounded ring buffer for 5 minutes. Operators can
    inspect via CLI in c9. Silently dropping would make debugging
    race-condition bugs impossible.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any

from .config import TransportConfig
from .utils import get_logger

log = get_logger(__name__)


# --- Message Precedence (Z/O/P/R) ------------------------------------------
#
# USAF Message Precedence (operator framing, 2026-06-09). Four labels;
# MVP implements two delivery LANES (Expedited Z+O / Deferred P+R) — the
# 4-label enum is the schema from day one, split per-tier later as the
# self-observation loop demands.
#
#   Z — Flash      interrupt / highest. MVP: immediate Telegram relay +
#                  a 🚨 Flash marker (NO true turn-preemption yet).
#   O — Immediate  push now (today's relay behavior, made explicit).
#   P — Priority   reply-owed async; placeholder/back-fill via the mailbox.
#   R — Routine    fire-and-forget; batched/scheduled (brief digest /
#                  heartbeat). The DEFAULT when precedence is absent.
#
# Default R (operator Decision A, 2026-06-09): an un-tagged message is
# most likely a report/heartbeat — defaulting to the least-disruptive
# lane (no interrupt, no reply owed) is the fail-safe. Unknown values
# coerce to R + log (forward-compat: "enum, free, never migrate").
PRECEDENCE_FLASH = "Z"
PRECEDENCE_IMMEDIATE = "O"
PRECEDENCE_PRIORITY = "P"
PRECEDENCE_ROUTINE = "R"
PRECEDENCE_VALUES: frozenset[str] = frozenset(
    {PRECEDENCE_FLASH, PRECEDENCE_IMMEDIATE, PRECEDENCE_PRIORITY, PRECEDENCE_ROUTINE}
)
PRECEDENCE_DEFAULT = PRECEDENCE_ROUTINE

# Operator-facing word labels for the Telegram relay prefix (Decision D).
PRECEDENCE_LABELS: dict[str, str] = {
    PRECEDENCE_FLASH: "Flash",
    PRECEDENCE_IMMEDIATE: "Immediate",
    PRECEDENCE_PRIORITY: "Priority",
    PRECEDENCE_ROUTINE: "Routine",
}

# Flash marker — prepended to Z-precedence relays in EVERY label style
# (Decision C: Z = O + a visual Flash marker, no true turn-preemption).
PRECEDENCE_FLASH_MARKER = "🚨"

# Per-instance label style for the Telegram relay prefix (Decision D
# refinement, 2026-06-09). Andrew (ex-USAF) reads Z/O/P/R instantly and
# opts his instances into ``letters``; non-technical users (Ben on VERA)
# need ``words``. Default ``words`` is the safe choice for any new user;
# the operator sets per-instance values in config (Salem/KAL-LE/Hypatia →
# letters, VERA → words). ``both`` shows letter + word.
PRECEDENCE_LABEL_STYLE_LETTERS = "letters"
PRECEDENCE_LABEL_STYLE_WORDS = "words"
PRECEDENCE_LABEL_STYLE_BOTH = "both"
PRECEDENCE_LABEL_STYLES: frozenset[str] = frozenset({
    PRECEDENCE_LABEL_STYLE_LETTERS,
    PRECEDENCE_LABEL_STYLE_WORDS,
    PRECEDENCE_LABEL_STYLE_BOTH,
})
PRECEDENCE_LABEL_STYLE_DEFAULT = PRECEDENCE_LABEL_STYLE_WORDS


def render_precedence_prefix(
    from_peer: str, precedence: str, style: str | None = None,
) -> str:
    """Render the Telegram relay prefix for a precedence-tagged message.

    Returns ``"[<peer> · <label>] "`` (trailing space included) per the
    per-instance ``style``:
        * ``letters`` → ``[KAL-LE · O] ``
        * ``words``   → ``[KAL-LE · Immediate] `` (the default)
        * ``both``    → ``[KAL-LE · O Immediate] ``

    The Flash marker (🚨) is prepended for ``Z`` precedence in EVERY style
    (e.g. ``[VERA · 🚨 Flash] `` / ``[VERA · 🚨 Z] ``). An unknown ``style``
    falls back to ``words`` (the safe default). When ``from_peer`` is
    empty the peer segment is omitted (``[Immediate] ``) so a relay never
    renders an empty ``[ · ...]`` bracket.

    Pure function — no I/O — so the render is unit-testable independent of
    the daemon relay path.
    """
    style = style if style in PRECEDENCE_LABEL_STYLES else PRECEDENCE_LABEL_STYLE_DEFAULT
    letter = precedence if precedence in PRECEDENCE_VALUES else PRECEDENCE_DEFAULT
    word = PRECEDENCE_LABELS[letter]

    if style == PRECEDENCE_LABEL_STYLE_LETTERS:
        label = letter
    elif style == PRECEDENCE_LABEL_STYLE_BOTH:
        label = f"{letter} {word}"
    else:  # words (default)
        label = word

    # Flash marker on Z, in every style.
    if letter == PRECEDENCE_FLASH:
        label = f"{PRECEDENCE_FLASH_MARKER} {label}"

    if from_peer:
        return f"[{from_peer} · {label}] "
    return f"[{label}] "


def normalize_precedence(value: Any) -> tuple[str, bool]:
    """Coerce a precedence value to a valid enum member.

    Returns ``(precedence, was_unknown)``: a valid uppercase Z/O/P/R, and
    a flag set True when the input was missing / unrecognized (coerced to
    the default ``R``). The caller logs ``transport.peer.precedence_unknown``
    on the ``was_unknown`` path so a typo'd or future-tier precedence is
    grep-able without a 400 (per Decision A — don't reject, coerce + log).
    """
    if isinstance(value, str):
        upper = value.strip().upper()
        if upper in PRECEDENCE_VALUES:
            return upper, False
    if value is None:
        # Absent is the common case — default R, NOT "unknown".
        return PRECEDENCE_DEFAULT, False
    # Present but unrecognized — coerce + flag for the log.
    return PRECEDENCE_DEFAULT, True


# --- Response inbox --------------------------------------------------------


# Orphan-reply buffer cap + TTL. 128 entries covers any reasonable
# workload; 300s matches the plan's "log-and-drop" window.
_ORPHAN_BUFFER_MAX = 128
_ORPHAN_TTL_SECONDS = 300

# In-memory inbox. Each correlation_id maps to an (Event, slot) pair.
# The event fires when the reply is delivered; the slot holds the reply
# dict. Orphan replies that arrive before a waiter are parked in
# _ORPHANS until a future ``await_response`` picks them up, or they
# age out.
_INBOX: dict[str, tuple[asyncio.Event, dict[str, Any]]] = {}
_ORPHANS: "OrderedDict[str, tuple[dict[str, Any], float]]" = OrderedDict()


def _prune_orphans(now: float | None = None) -> None:
    """Drop orphan replies older than :data:`_ORPHAN_TTL_SECONDS`."""
    cutoff = (now or time.monotonic()) - _ORPHAN_TTL_SECONDS
    # OrderedDict preserves insertion order; orphans added first are
    # oldest. Pop from the front until we hit a fresh entry.
    while _ORPHANS:
        oldest_cid = next(iter(_ORPHANS))
        _, ts = _ORPHANS[oldest_cid]
        if ts >= cutoff:
            break
        _ORPHANS.pop(oldest_cid, None)
        log.info(
            "transport.peers.orphan_expired",
            correlation_id=oldest_cid,
        )


def register_response(correlation_id: str, reply: dict[str, Any]) -> bool:
    """Deliver a reply to a waiter (if any). Returns True iff delivered.

    When no waiter is registered, park the reply in the orphan buffer
    for later inspection. The router will pick it up if a slightly-
    late ``await_response`` call comes in; otherwise it ages out.
    """
    if not correlation_id:
        return False
    slot = _INBOX.get(correlation_id)
    if slot is not None:
        event, store = slot
        store.update(reply)
        event.set()
        return True

    # Orphan — park it.
    _prune_orphans()
    # Cap the buffer. Evict oldest FIFO if we're at cap.
    if len(_ORPHANS) >= _ORPHAN_BUFFER_MAX:
        evicted_cid, _ = _ORPHANS.popitem(last=False)
        log.info(
            "transport.peers.orphan_evicted",
            correlation_id=evicted_cid,
            reason="buffer_full",
        )
    _ORPHANS[correlation_id] = (dict(reply), time.monotonic())
    log.info(
        "transport.peers.orphan_parked",
        correlation_id=correlation_id,
    )
    return False


# Back-compat alias. The talker daemon's peer-inbox callable imports
# ``deliver_response`` (its domain verb — "deliver this reply to the
# waiter"); the inbox/orphan-buffer verb here is ``register_response``.
# They are the same function. This alias closes a latent ImportError on
# the one path that wires two instances together: when a peer POSTs
# ``kind=query_result`` back to the requester's daemon inbox, the inbox
# handler imports ``deliver_response`` — without this alias that raises
# ``ImportError``, ``register_response`` never runs, ``await_response``
# never wakes, and the requester hangs to its full timeout. Dead on
# master (P1 was sync-only); made live by the async query-broker path.
deliver_response = register_response


async def await_response(
    correlation_id: str,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Block until the reply with ``correlation_id`` arrives, or timeout.

    Raises :class:`asyncio.TimeoutError` when the wait exceeds
    ``timeout`` seconds. Always unregisters the slot before returning
    so a late reply doesn't leak memory.
    """
    # Orphan buffer check — a reply that arrived before we started
    # waiting is perfectly valid and shouldn't require a timeout round-
    # trip to pick up.
    if correlation_id in _ORPHANS:
        reply, _ = _ORPHANS.pop(correlation_id)
        log.info(
            "transport.peers.orphan_collected",
            correlation_id=correlation_id,
        )
        return reply

    event = asyncio.Event()
    store: dict[str, Any] = {}
    _INBOX[correlation_id] = (event, store)
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return dict(store)
    finally:
        _INBOX.pop(correlation_id, None)


def inbox_stats() -> dict[str, Any]:
    """Introspection helper — counts for the CLI's ``tail`` subcommand."""
    _prune_orphans()
    return {
        "pending_waiters": len(_INBOX),
        "orphan_replies": len(_ORPHANS),
    }


# --- Peer HTTP helpers -----------------------------------------------------


def _resolve_peer(config: TransportConfig, peer_name: str) -> tuple[str, str]:
    """Return ``(base_url, token)`` for ``peer_name`` or raise.

    Stage 3.5 D7: discovery is config-driven. Unknown peers surface a
    clear error instead of silently 401-ing on a phantom URL.
    """
    from .exceptions import TransportError

    entry = config.peers.get(peer_name)
    if entry is None or not entry.base_url:
        raise TransportError(
            f"unknown peer '{peer_name}' — add it to transport.peers in config.yaml"
        )
    if not entry.token:
        raise TransportError(
            f"peer '{peer_name}' has no token configured"
        )
    return entry.base_url.rstrip("/"), entry.token
