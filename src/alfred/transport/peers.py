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
