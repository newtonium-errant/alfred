"""aiohttp server hosted inside the talker daemon.

The server is the user-facing side of the outbound-push transport.
Other tools POST to it; it forwards the payload to a registered send
callable that delivers via Telegram. In v1 the only registered
callable is the talker's telegram-send path (wired up in commit 6) —
until then the server returns ``503 telegram_not_configured`` so the
scheduler and brief clients get a well-defined error instead of a
hang.

Route surface (v1):

    POST /outbound/send          — immediate or scheduled send
    POST /outbound/send_batch    — multi-chunk send (used by brief)
    GET  /outbound/status/{id}   — lookup a recorded send
    GET  /health                 — liveness / auth / queue depth probe

Stage 3.5 stubs (501 today, real handlers later):

    POST /peer/send              — forward to another Alfred instance
    POST /peer/query             — cross-instance query
    POST /peer/handshake         — peer bootstrap
    GET  /canonical/{type}/{name} — SALEM-owned canonical record fetch

**Route namespace registry** — the registry keyed by namespace is
load-bearing for the Stage 3.5 dovetail. Replacing the 501 stub
registrar with a real one is a one-line diff at the top of this
module; no caller elsewhere needs to change.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web

from .config import (
    DEFAULT_BATCH_MAX_IMAGE_BYTES,
    DEFAULT_BATCH_MAX_IMAGES,
    DEFAULT_BATCH_MAX_INSTRUCTION_CHARS,
    DEFAULT_BATCH_MAX_TOTAL_BYTES,
    DEFAULT_BUGREPORT_MAX_DESCRIPTION_CHARS,
    DEFAULT_BUGREPORT_MAX_SCREENSHOT_BYTES,
    DEFAULT_INGEST_MAX_BODY_CHARS,
    DEFAULT_JEEVES_MAX_TRANSCRIPT_CHARS,
    DEFAULT_TRANSPORT_CLIENT_MAX_BYTES,
    TransportConfig,
    host_is_loopback,
)
from .exceptions import TELEGRAM_UNAVAILABLE_REASON, TelegramUnavailable
from .state import TransportState
from .utils import get_logger

log = get_logger(__name__)


# Type alias for the send callable the talker registers. Contract:
# raises on transport error, returns a list of Telegram message IDs on
# success — and raises :class:`TelegramUnavailable` when the instance has
# no bot at all, which is a NON-DELIVERY and never an empty success.
# ``dedupe_key`` is passed through so the callable can stamp
# it onto Telegram metadata if useful; today it's ignored at the
# Telegram layer.
SendCallable = Callable[
    ...,  # (user_id: int, text: str, dedupe_key: str | None) -> Awaitable[list[int]]
    Awaitable[list[int]],
]


# Application storage keys. aiohttp apps use dict-like storage so
# handlers can reach shared state without module-level globals.
# Prefixing with the module name keeps us from colliding with talker
# keys when the same Application runs both.
_KEY_CONFIG = "transport.config"
_KEY_STATE = "transport.state"
_KEY_SEND_FN = "transport.send_fn"
# Whether a Telegram BOT exists on this instance — a bool or a zero-arg
# predicate, wired by the daemon. Distinct from ``_KEY_SEND_FN``, which
# only says a callable was registered: on every instance since
# 2026-08-14/15 a callable IS registered and it has no bot behind it, so
# /health answered ``telegram_connected: true`` for a dark channel.
_KEY_TELEGRAM_AVAILABLE = "transport.telegram_available"


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


def _resolve_peer_for_auth(
    tokens: dict, client_name: str, presented_token: str,
) -> tuple[str | None, str]:
    """Resolve which peer entry authenticates ``(client_name, token)``.

    Two-phase claim-first-then-verify match (refactor 2026-05-09 Batch B,
    closes the fragility flagged 2026-05-01 in
    ``feedback_per_peer_token_uniqueness.md``):

    Phase 1 — direct claim-first lookup. If ``client_name`` is itself a
    peer key in ``tokens`` (the Stage 3.5 cross-instance peer-auth
    shape — KAL-LE's transport client identifies as ``X-Alfred-Client:
    kal-le`` and the Salem server's ``tokens`` dict has a ``kal-le``
    entry), look up that entry directly and verify the presented token
    matches. This eliminates the iter()-order dependency that the
    previous "first matching token wins" loop had: if two peer pairs
    accidentally shared a token, iter() picked one arbitrarily and the
    other client got rejected with ``client_not_allowed`` based on the
    wrong peer's allowlist.

    Phase 2 — fall through to legacy iter+token-match. v1 single-tenant
    Salem has ``tokens = {"local": ...}`` with internal clients
    (``scheduler`` / ``brief`` / ``janitor`` / ``curator`` / ``talker``)
    that identify themselves by their tool name, NOT by ``local``. Their
    ``client_name`` doesn't match any peer key, so phase 1 misses and we
    fall through to the legacy match. With only one entry in ``tokens``
    iter() picks ``local`` deterministically — no fragility — and the
    existing ``allowed_clients`` check downstream confirms the client
    was authorized.

    Returns ``(peer_name, reject_reason)``:
        peer_name == None       — auth failed; reject_reason names which
                                   gate ("invalid_token" or "unknown").
        peer_name == "local"... — peer that claims this auth pair.

    All token comparisons use ``secrets.compare_digest`` for timing-
    attack safety. Empty tokens (entries with ``token=""``) are skipped
    in both phases — an empty token pair is misconfiguration, never a
    valid match.
    """
    if not presented_token:
        return None, "invalid_token"

    # Phase 1 — claim-first direct lookup.
    direct_entry = tokens.get(client_name)
    if direct_entry is not None and direct_entry.token:
        if secrets.compare_digest(direct_entry.token, presented_token):
            return client_name, ""
        # Direct-match peer exists but token mismatched. Fall through
        # to phase 2 — this lets a misconfigured-client scenario where
        # client_name happens to collide with a peer key still resolve
        # via the legacy path. The defensive ``compare_digest`` keeps
        # phase 1's negative path timing-safe.

    # Phase 2 — legacy iter+token-match.
    for peer_name, entry in tokens.items():
        if not entry.token:
            continue
        if secrets.compare_digest(entry.token, presented_token):
            return peer_name, ""

    return None, "invalid_token"


@web.middleware
async def auth_middleware(
    request: web.Request,
    handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
) -> web.StreamResponse:
    """Bearer-token auth + ``X-Alfred-Client`` allowlist enforcement.

    Two-phase claim-first-then-verify match (see
    ``_resolve_peer_for_auth``):
      1. If the ``X-Alfred-Client`` header value is itself a peer key
         in ``auth.tokens``, verify that peer's token directly. Stage
         3.5 cross-instance peer-auth lives on this path.
      2. Else fall through to legacy iter+token-match. v1 internal
         clients (scheduler / brief / janitor / curator / talker) live
         here — their ``X-Alfred-Client`` value isn't a peer key, but
         it's in the ``allowed_clients`` list of the matched peer.

    The post-match ``allowed_clients`` check is the final gate for
    BOTH phases, so the existing v1 client-allowlist semantic is
    preserved unchanged.

    Never logs token contents — only a prefix + length for audit
    correlation, per builder.md's secret-logging rule. All token
    comparisons use ``secrets.compare_digest`` for timing-attack
    safety.

    The ``/health`` route is unauthenticated on purpose: it's the only
    probe a caller can hit before they've loaded the token, and the
    response body carries no privileged information.
    """
    # /health is always public — it's the bootstrap probe.
    if request.path == "/health":
        return await handler(request)

    config: TransportConfig = request.app[_KEY_CONFIG]

    auth_header = request.headers.get("Authorization", "")
    client_name = request.headers.get("X-Alfred-Client", "")

    if not auth_header.startswith("Bearer "):
        log.warning(
            "transport.server.auth_missing",
            path=request.path,
            client=client_name or "(missing)",
        )
        return web.json_response(
            {"error": "missing_bearer"},
            status=401,
        )

    token = auth_header.removeprefix("Bearer ").strip()

    matching_peer, _reject_reason = _resolve_peer_for_auth(
        config.auth.tokens, client_name, token,
    )

    if matching_peer is None:
        log.warning(
            "transport.server.auth_rejected",
            path=request.path,
            client=client_name or "(missing)",
            token_length=len(token),
            token_prefix=token[:4] if token else "",
        )
        return web.json_response(
            {"error": "invalid_token"},
            status=401,
        )

    entry = config.auth.tokens[matching_peer]
    if entry.allowed_clients and client_name not in entry.allowed_clients:
        log.warning(
            "transport.server.client_rejected",
            path=request.path,
            peer=matching_peer,
            client=client_name or "(missing)",
            allowed_clients=list(entry.allowed_clients),
        )
        return web.json_response(
            {
                "error": "client_not_allowed",
                "peer": matching_peer,
            },
            status=401,
        )

    log.info(
        "transport.server.auth_ok",
        path=request.path,
        peer=matching_peer,
        client=client_name,
    )
    # Stash the peer name so handlers can record it in audit entries.
    request["transport_peer"] = matching_peer
    request["transport_client"] = client_name
    return await handler(request)


# ---------------------------------------------------------------------------
# Outbound handlers
# ---------------------------------------------------------------------------


async def _handle_send(request: web.Request) -> web.StreamResponse:
    """POST /outbound/send — immediate or scheduled single-message send.

    Body shape (JSON):
        {
          "user_id": <int>,
          "text": <str>,
          "scheduled_at": <ISO 8601, optional>,
          "dedupe_key": <str, optional>
        }

    Returns:
        200 {"id": <str>, "status": "scheduled"|"sent", "telegram_message_id": <int?>}
        503 {"reason": "telegram_not_configured"} when no send callable is registered
        503 {"status": "skipped", "reason": "telegram_unavailable"} when the
            instance has no Telegram bot — NOTHING was delivered, and no
            dedupe record is written (see the handler's skip branch)
        400 on schema errors
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id = body.get("user_id")
    text = body.get("text")
    if not isinstance(user_id, int) or not isinstance(text, str) or not text:
        return web.json_response(
            {"error": "user_id_and_text_required"},
            status=400,
        )
    dedupe_key = str(body.get("dedupe_key") or "")
    scheduled_at = body.get("scheduled_at")

    state: TransportState = request.app[_KEY_STATE]
    config: TransportConfig = request.app[_KEY_CONFIG]

    # Dedupe: a send with the same key in the 24h window is idempotent —
    # return the recorded response instead of re-dispatching. Clears
    # the brief-daemon-restart race and the scheduler-restart race in
    # one rule.
    if dedupe_key:
        match = state.find_recent_send(dedupe_key)
        if match is not None:
            return web.json_response({
                "id": match.get("id", ""),
                "status": "duplicate",
                "telegram_message_id": (
                    (match.get("telegram_message_ids") or [None])[0]
                ),
            })

    new_id = uuid.uuid4().hex[:16]

    # Scheduled-for-the-future → park in the queue; the scheduler
    # drains it on its next tick.
    if scheduled_at:
        entry = {
            "id": new_id,
            "user_id": user_id,
            "text": text,
            "dedupe_key": dedupe_key,
            "scheduled_at": scheduled_at,
            "peer": request.get("transport_peer"),
            "client": request.get("transport_client"),
        }
        state.enqueue(entry)
        try:
            state.save()
        except OSError:
            log.exception("transport.server.state_save_failed")
        return web.json_response({"id": new_id, "status": "scheduled"})

    # Immediate send — require the talker's send callable.
    send_fn: SendCallable | None = request.app.get(_KEY_SEND_FN)
    if send_fn is None:
        return web.json_response(
            {"reason": "telegram_not_configured"},
            status=503,
        )

    try:
        msg_ids = await send_fn(
            user_id=user_id,
            text=text,
            dedupe_key=dedupe_key or None,
        )
    except TelegramUnavailable as exc:
        # THE SKIP. Answer "skipped", never "sent", and — the load-bearing
        # half — record NO dedupe row. A 24h dedupe entry for a message that
        # was never delivered would suppress the very retry that could
        # deliver it once the channel came back, which is the original sting
        # relocated one layer up. Nothing before this point has written to
        # state, so the early return IS the guarantee.
        log.warning(
            "transport.server.send_skipped_no_telegram",
            user_id=user_id,
            dedupe_key=dedupe_key,
            client=request.get("transport_client"),
            peer=request.get("transport_peer"),
            reason=TELEGRAM_UNAVAILABLE_REASON,
            detail="nothing was delivered; no dedupe record written",
        )
        return web.json_response(
            {
                "id": new_id,
                "status": "skipped",
                "delivered": False,
                "reason": TELEGRAM_UNAVAILABLE_REASON,
                "detail": str(exc),
            },
            status=503,
        )
    except Exception as exc:  # noqa: BLE001 — surface upstream
        log.warning(
            "transport.server.send_failed",
            user_id=user_id,
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return web.json_response(
            {"error": "send_failed", "detail": str(exc)},
            status=502,
        )

    sent_at = datetime.now(timezone.utc).isoformat()
    state.record_send({
        "id": new_id,
        "user_id": user_id,
        "text": text,
        "dedupe_key": dedupe_key,
        "sent_at": sent_at,
        "telegram_message_ids": list(msg_ids or []),
        "peer": request.get("transport_peer"),
        "client": request.get("transport_client"),
    })
    try:
        state.save()
    except OSError:
        log.exception("transport.server.state_save_failed")

    primary_msg_id: int | None = None
    if msg_ids:
        primary_msg_id = msg_ids[0]
    return web.json_response({
        "id": new_id,
        "status": "sent",
        "telegram_message_id": primary_msg_id,
    })


async def _handle_send_batch(request: web.Request) -> web.StreamResponse:
    """POST /outbound/send_batch — multi-chunk send.

    Body:
        {
          "user_id": <int>,
          "chunks": [<str>, ...],
          "dedupe_key": <str, optional>
        }

    Used by the brief daemon when a rendered brief overflows Telegram's
    single-message limit. The server sends each chunk in order and
    returns the list of Telegram message IDs.

    The dedupe key applies to the whole batch — the first chunk that
    matches the key wins. Subsequent chunks sharing the same key are
    treated as part of the same logical send.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"error": "invalid_json"}, status=400)

    user_id = body.get("user_id")
    chunks = body.get("chunks")
    if (
        not isinstance(user_id, int)
        or not isinstance(chunks, list)
        or not chunks
        or not all(isinstance(c, str) for c in chunks)
    ):
        return web.json_response(
            {"error": "user_id_and_chunks_required"},
            status=400,
        )
    dedupe_key = str(body.get("dedupe_key") or "")

    state: TransportState = request.app[_KEY_STATE]

    # Batch-level dedupe.
    if dedupe_key:
        match = state.find_recent_send(dedupe_key)
        if match is not None:
            return web.json_response({
                "id": match.get("id", ""),
                "status": "duplicate",
                "sent_count": len(match.get("telegram_message_ids") or []),
                "telegram_message_ids": list(match.get("telegram_message_ids") or []),
            })

    send_fn: SendCallable | None = request.app.get(_KEY_SEND_FN)
    if send_fn is None:
        return web.json_response(
            {"reason": "telegram_not_configured"},
            status=503,
        )

    new_id = uuid.uuid4().hex[:16]
    all_msg_ids: list[int] = []
    try:
        for chunk in chunks:
            ids = await send_fn(
                user_id=user_id,
                text=chunk,
                dedupe_key=dedupe_key or None,
            )
            all_msg_ids.extend(ids or [])
    except TelegramUnavailable as exc:
        # Same rule as the single-send skip: "skipped", never a sent_count,
        # and NO dedupe row. ``sent_before_skip`` is 0 on a web-only instance
        # (the condition is fixed for the process lifetime, so the first chunk
        # raises) — it is reported anyway because a nonzero value would mean a
        # partially-delivered batch, and a dedupe row would then strand the
        # undelivered remainder behind an idempotent "duplicate".
        log.warning(
            "transport.server.batch_send_skipped_no_telegram",
            user_id=user_id,
            dedupe_key=dedupe_key,
            chunks=len(chunks),
            sent_before_skip=len(all_msg_ids),
            client=request.get("transport_client"),
            peer=request.get("transport_peer"),
            reason=TELEGRAM_UNAVAILABLE_REASON,
            detail="nothing was delivered; no dedupe record written",
        )
        return web.json_response(
            {
                "id": new_id,
                "status": "skipped",
                "delivered": False,
                "reason": TELEGRAM_UNAVAILABLE_REASON,
                "sent_before_skip": len(all_msg_ids),
                "detail": str(exc),
            },
            status=503,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "transport.server.batch_send_failed",
            user_id=user_id,
            sent_so_far=len(all_msg_ids),
            error=str(exc),
        )
        return web.json_response(
            {"error": "send_failed", "detail": str(exc)},
            status=502,
        )

    sent_at = datetime.now(timezone.utc).isoformat()
    state.record_send({
        "id": new_id,
        "user_id": user_id,
        "dedupe_key": dedupe_key,
        "sent_at": sent_at,
        "telegram_message_ids": all_msg_ids,
        "batch_size": len(chunks),
        "peer": request.get("transport_peer"),
        "client": request.get("transport_client"),
    })
    try:
        state.save()
    except OSError:
        log.exception("transport.server.state_save_failed")

    return web.json_response({
        "id": new_id,
        "sent_count": len(all_msg_ids),
        "telegram_message_ids": all_msg_ids,
    })


async def _handle_status(request: web.Request) -> web.StreamResponse:
    """GET /outbound/status/{id} — look up a recorded send by ID.

    Looks in both ``send_log`` (delivered) and ``pending_queue``
    (scheduled-but-not-yet-sent). 404 when the ID is unknown.
    """
    entry_id = request.match_info.get("id", "")
    state: TransportState = request.app[_KEY_STATE]

    for entry in state.send_log:
        if entry.get("id") == entry_id:
            return web.json_response({
                "id": entry_id,
                "status": "sent",
                "sent_at": entry.get("sent_at"),
                "telegram_message_ids": list(entry.get("telegram_message_ids") or []),
            })
    for entry in state.pending_queue:
        if entry.get("id") == entry_id:
            return web.json_response({
                "id": entry_id,
                "status": "scheduled",
                "scheduled_at": entry.get("scheduled_at"),
            })
    for entry in state.dead_letter:
        if entry.get("id") == entry_id:
            return web.json_response({
                "id": entry_id,
                "status": "dead_letter",
                "reason": entry.get("dead_letter_reason"),
                "dead_lettered_at": entry.get("dead_lettered_at"),
            })

    return web.json_response({"error": "not_found"}, status=404)


async def _handle_health(request: web.Request) -> web.StreamResponse:
    """GET /health — liveness + basic metrics.

    Unauthenticated (see ``auth_middleware``). Returns:
        {
          "status": "ok",
          "telegram_connected": <bool>,
          "telegram_connected_source": "bot" | "send_fn_registered",
          "send_fn_registered": <bool>,
          "queue_depth": <int>,
          "dead_letter_depth": <int>
        }

    ``telegram_connected`` used to mean "a send callable is registered",
    which on every instance since 2026-08-14/15 was true of a channel that
    delivered nothing — the probe most likely to be consulted about exactly
    this failure was the one asserting it could not happen. It now reports
    the BOT's existence when the daemon wired that fact, and says which of
    the two it is via ``telegram_connected_source`` so a reader can tell a
    measured value from an inferred one. ``send_fn_registered`` is kept as
    its own field: an unregistered callable is a different fault (503
    ``telegram_not_configured``) and stayed diagnosable.
    """
    state: TransportState = request.app[_KEY_STATE]
    send_fn = request.app.get(_KEY_SEND_FN)
    available = request.app.get(_KEY_TELEGRAM_AVAILABLE)
    if available is None:
        # Not wired — a test app, or a caller that builds the app directly.
        # Fall back to the old inference rather than fail the probe, and
        # label it so nobody reads a guess as a measurement.
        connected = send_fn is not None
        source = "send_fn_registered"
    else:
        connected = bool(available() if callable(available) else available)
        source = "bot"
    return web.json_response({
        "status": "ok",
        "telegram_connected": connected,
        "telegram_connected_source": source,
        "send_fn_registered": send_fn is not None,
        "queue_depth": len(state.pending_queue),
        "dead_letter_depth": len(state.dead_letter),
    })


# ---------------------------------------------------------------------------
# Peer + canonical stubs — Stage 3.5 pre-commits (501 today)
# ---------------------------------------------------------------------------


async def _peer_not_implemented(_request: web.Request) -> web.StreamResponse:
    """All /peer/* routes return 501 until Stage 3.5 replaces the registrar."""
    return web.json_response(
        {"reason": "peer_not_implemented"},
        status=501,
    )


async def _canonical_not_implemented(_request: web.Request) -> web.StreamResponse:
    """All /canonical/* routes return 501 until Stage 3.5 replaces the registrar."""
    return web.json_response(
        {"reason": "peer_not_implemented"},
        status=501,
    )


# ---------------------------------------------------------------------------
# Route registry — swap a namespace's registrar in one line for Stage 3.5
# ---------------------------------------------------------------------------


def _register_outbound_routes(app: web.Application) -> None:
    app.router.add_post("/outbound/send", _handle_send)
    app.router.add_post("/outbound/send_batch", _handle_send_batch)
    app.router.add_get("/outbound/status/{id}", _handle_status)


def _register_peer_stub(app: web.Application) -> None:
    app.router.add_post("/peer/send", _peer_not_implemented)
    app.router.add_post("/peer/query", _peer_not_implemented)
    app.router.add_post("/peer/handshake", _peer_not_implemented)
    app.router.add_post("/peer/brief_digest", _peer_not_implemented)


def _register_canonical_stub(app: web.Application) -> None:
    # Dynamic path — Stage 3.5 registrar will read {type}/{name} to
    # look up canonical record permissions.
    app.router.add_get("/canonical/{type}/{name}", _canonical_not_implemented)


def _register_health(app: web.Application) -> None:
    app.router.add_get("/health", _handle_health)


# The registry — swapping a stub for a real handler is a one-line diff.
# Keys intentionally chosen to match the route prefix for readability.
#
# Stage 3.5 c3: the ``peer`` + ``canonical`` entries now point at the
# real registrars in :mod:`peer_handlers`. The stub functions above are
# retained for unit-test rollback scenarios — if a regression breaks
# the real handlers, swapping two lines here reverts to 501s.
from .peer_handlers import (
    register_canonical_routes as _register_canonical_routes,
    register_peer_routes as _register_peer_routes,
)

ROUTE_NAMESPACES: dict[str, Callable[[web.Application], None]] = {
    "outbound": _register_outbound_routes,
    "peer": _register_peer_routes,
    "canonical": _register_canonical_routes,
    "health": _register_health,
}


# ---------------------------------------------------------------------------
# App factory + runtime helpers
# ---------------------------------------------------------------------------


def build_app(
    config: TransportConfig,
    state: TransportState,
    send_fn: SendCallable | None = None,
) -> web.Application:
    """Build an aiohttp ``Application`` with all routes registered.

    ``send_fn`` is optional — when ``None`` (the default), the server
    returns ``503 telegram_not_configured`` for send routes. The
    talker daemon registers a real callable at startup in commit 6;
    unit tests pass in stubs via :func:`register_send_callable`.
    """
    # ``client_max_size`` is raised from aiohttp's 1 MiB default so a base64'd
    # PDF (4/3 inflation over the 10 MiB byte cap) reaches the handler and is
    # refused by the ingest route's OWN error taxonomy. aiohttp enforces this
    # ceiling before any handler runs and answers with a bare HTML 413, so a
    # value at or below the route's cap would mean the operator's oversize
    # upload never gets a sentence explaining which limit it hit. See
    # ``DEFAULT_TRANSPORT_CLIENT_MAX_BYTES`` for the full rationale and the
    # stated residual above that number.
    app = web.Application(
        middlewares=[auth_middleware],
        client_max_size=DEFAULT_TRANSPORT_CLIENT_MAX_BYTES,
    )
    app[_KEY_CONFIG] = config
    app[_KEY_STATE] = state
    if send_fn is not None:
        app[_KEY_SEND_FN] = send_fn
    # Background-task retention set (peer-precedence async query broker).
    # Initialized HERE at build time — while the app is still mutable —
    # NOT lazily in the handler: ``app[<new-key>] = ...`` on a STARTED app
    # raises ``DeprecationWarning: Changing state of started or joined
    # application`` (hard-errors in a future aiohttp). The handler only
    # MUTATES this set (``.add`` / ``.discard``), which is allowed
    # post-start. Holds a strong ref to each detached query_result reply
    # task so it isn't GC'd mid-flight; the done-callback discards it.
    app["_bg_tasks"] = set()

    for register in ROUTE_NAMESPACES.values():
        register(app)

    return app


def register_send_callable(
    app: web.Application,
    send_fn: SendCallable,
) -> None:
    """Wire a send callable onto an already-built app.

    Separate from :func:`build_app` so the talker can first construct
    the app, then register the callable as a closure over the PTB
    ``Bot`` instance, then hand the app to :func:`run_server`.
    """
    app[_KEY_SEND_FN] = send_fn


def register_telegram_available(
    app: web.Application,
    available: bool | Callable[[], bool],
) -> None:
    """Wire whether this instance actually HAS a Telegram bot.

    Registering a send callable is not evidence of a bot — since
    2026-08-14/15 every instance registers one over ``app is None``. This
    is the separate fact /health reports. Must be called before the app
    starts (aiohttp forbids new keys on a started application);
    :func:`wire_transport_app` runs well before ``run_server``.
    """
    app[_KEY_TELEGRAM_AVAILABLE] = available


# ---------------------------------------------------------------------------
# Centralized wiring — `wire_transport_app`
# ---------------------------------------------------------------------------
#
# Background: each new transport-app dependency historically shipped as
# its own ``register_*`` helper that the daemon had to remember to call
# at startup. By 2026-05-01 there were 6 such helpers and the daemon was
# missing one (``register_vault_path``), causing every /canonical/* and
# /peer/brief_digest request to 500 with ``vault_not_configured`` for
# the entire lifetime of the talker daemon. Hotfix in commit f0f8a03.
#
# ``wire_transport_app`` consolidates every register call behind one
# function. The signature lists every wireable resource as an explicit
# kwarg — the daemon must opt OUT by omission, which surfaces "do I
# need this?" at every call site rather than letting a silent miss ship.
#
# Why not move wiring INTO ``build_app``? Because send_fn / pending
# callables / instance identity all need closures over runtime objects
# (PTB Bot, executor module, config dataclass) that exist after the
# daemon has done its own setup. ``build_app`` runs early; ``wire_*``
# runs after the daemon has constructed those closures.
#
# The individual ``register_*`` helpers stay public because tests use
# them directly to wire only the surface they're exercising. This is a
# convenience-and-discipline layer ON TOP, not a replacement.

# Type alias for the pending-items resolver callable — kept here so the
# wiring function can be type-checked without importing peer_handlers
# eagerly (avoids a circular import at module load).
_PendingItemsResolveCallable = Callable[..., Awaitable[dict[str, Any]]]
_PeerInboxCallable = Callable[..., Awaitable[dict[str, Any]]]
_TicketOutcomeResolveCallable = Callable[..., Awaitable[dict[str, Any]]]


def wire_transport_app(
    app: web.Application,
    config: TransportConfig,  # noqa: ARG001 — present for forward-compat
    *,
    instance_name: str,
    instance_alias: str = "",
    vault_path: Path | None = None,
    send_fn: SendCallable | None = None,
    telegram_available: bool | Callable[[], bool] | None = None,
    pending_items_aggregate_path: str | Path | None = None,
    pending_items_resolve_callable: _PendingItemsResolveCallable | None = None,
    peer_inbox_callable: _PeerInboxCallable | None = None,
    gcal_client: Any | None = None,
    gcal_config: Any | None = None,
    gcal_intended_on: bool = False,
    nl_llm_callable: Any | None = None,
    nl_llm_model_label: str = "",
    ticket_intake_config: Any | None = None,
    ticket_intake_github_client: Any | None = None,
    ticket_outcome_resolve_callable: _TicketOutcomeResolveCallable | None = None,
    ingest_enabled: bool = False,
    ingest_config: Any | None = None,
    jeeves_enabled: bool = False,
    jeeves_config: Any | None = None,
    jeeves_raw_config: "dict[str, Any] | None" = None,
    batch_enabled: bool = False,
    batch_config: Any | None = None,
    batch_data_dir: str = "",
    rrts_export_enabled: bool = False,
    rrts_export_config: Any | None = None,
    rrts_data_dir: str = "",
    batch_instance: str = "",
    batch_config_path: str = "",
    batch_kick_enabled: bool = False,
    bugreport_enabled: bool = False,
    bugreport_config: Any | None = None,
    recall_enabled: bool = False,
    feed_enabled: bool = False,
    feed_store: Any | None = None,
    feed_daily_sync_config: Any | None = None,
    feed_instance_scope: str = "",
    feed_raw_config: "dict[str, Any] | None" = None,
) -> None:
    """Wire all transport-app dependencies in one place.

    Daemon startup invokes this once instead of orchestrating N
    separate ``register_*`` calls. Each kwarg corresponds to one
    registrar; passing ``None`` (the default) skips that registrar.

    The function is non-magical on purpose: a daemon that doesn't pass
    ``vault_path`` will still see canonical handlers 500 with
    ``vault_not_configured``. The opt-out is explicit-by-omission,
    which surfaces "do I need this?" at every call site.

    Args:
        app: The aiohttp application returned by :func:`build_app`.
        config: The transport config (reserved for future wiring needs;
            unused today but kept in the signature so daemons don't
            have to refactor their call site when a future helper
            needs config-derived state).
        instance_name: This instance's persona name
            ("Salem", "KAL-LE", ...). Wired via
            :func:`peer_handlers.register_instance_identity` so
            ``/peer/handshake`` responses identify correctly. Required
            because every instance has one — no sensible default.
        instance_alias: Casual / display alias for the instance.
            Defaults to empty string, matching the helper's default.
        vault_path: Filesystem path to the instance's vault. Required
            for every ``/canonical/*`` handler, ``/peer/brief_digest``,
            and the pending-items resolvers. Pass ``None`` only if the
            instance is genuinely vault-less.
        send_fn: Outbound send callable (talker → Telegram). Required
            for ``/outbound/send`` to do anything other than 503.
        pending_items_aggregate_path: Salem's aggregate JSONL path.
            Required only on the instance that aggregates peer pushes
            (Salem). KAL-LE / Hypatia leave this ``None``.
        pending_items_resolve_callable: Per-instance resolver callable
            for Salem→peer dispatch. Required on every instance with a
            ``pending_items`` config block.
        peer_inbox_callable: Talker-side handler for inbound /peer/send
            relays. Wired via :func:`peer_handlers.register_peer_inbox`.
        gcal_client: Constructed
            :class:`alfred.integrations.gcal.GCalClient`. Required only on
            instances that opt into the Phase A+ GCal integration (Salem;
            future V.E.R.A. for RRTS). Pass with ``gcal_config`` together
            or omit both — the conflict-check / sync paths short-circuit
            when either is missing.
        gcal_config: Typed
            :class:`alfred.integrations.gcal_config.GCalConfig`. Carries
            the Alfred + primary calendar IDs the handler scans.
        gcal_intended_on: P2-4 sentinel. ``True`` iff ``gcal.enabled``
            was set in config but client construction failed at daemon
            startup. Causes the conflict-check + sync skip sites to
            log at ``warning`` instead of ``debug`` so the operator
            spots the silent feature-degradation. Default ``False``
            for instances that legitimately disabled GCal.
        nl_llm_callable: The NL-broker LLM callable (LLM lane,
            2026-06-10). Required only on instances that enable
            ``transport.canonical.nl_broker`` — the talker daemon builds
            an AsyncAnthropic-backed closure and passes it here. Wired
            via :func:`peer_handlers.register_nl_llm`. Absent →
            ``kind=query_nl`` replies ``nl_broker_unavailable``
            (fail-closed).
        nl_llm_model_label: Resolved model id carried alongside the
            callable for the ``kind:"nl_query"`` audit row.
        ticket_intake_config: Typed
            :class:`alfred.transport.ticket_intake.TicketIntakeConfig`.
            Required only on the instance that receives VERA's
            ``kind=ticket`` pushes (KAL-LE). Pass with
            ``ticket_intake_github_client`` together or omit both —
            ``kind=ticket`` answers 501 when either is missing.
        ticket_intake_github_client: Built
            :class:`alfred.integrations.github_ops.GitHubOpsClient`.
            The daemon constructs it via the fail-loud
            ``build_github_client`` factory; a failed build means the
            daemon logs ``transport.ticket_intake.disabled`` and
            passes ``None`` here (the daemon must still start).
        ingest_enabled: Mount the cross-instance ``POST /vault/ingest``
            route (2026-06-29). Default ``False`` — an un-opted-in
            instance never mounts the route (opt-in inertness, same as
            the web-chat surface). Wired via
            :func:`routes_ingest.register_ingest_routes`; the route
            inherits ``auth_middleware`` peer-gating automatically.
        ingest_config: The typed
            :class:`alfred.transport.config.IngestConfig` carrying
            ``max_body_chars`` + the optional per-instance ``types``
            narrowing list. Read only when ``ingest_enabled`` is True.
        batch_enabled: Mount the bulk scan intake route
            (``POST /vault/batch``, #83). Default ``False`` — an
            un-opted-in instance never mounts it (opt-in inertness).
            Gated by the dedicated ``web_batch`` peer pin, so it stays
            dark until the operator adds that token.
        batch_config: The typed
            :class:`alfred.transport.config.BatchConfig` carrying the
            three upload caps. Read only when ``batch_enabled`` is True.
        bugreport_enabled: Mount the in-app bug-report route
            (``POST /vault/bugreport``, #95). Default ``False`` — an
            un-opted-in instance never mounts it (opt-in inertness).
            Gated by the dedicated ``web_bugreport`` peer pin, so it
            stays dark until the operator adds that token.
        bugreport_config: The typed
            :class:`alfred.transport.config.BugReportConfig` carrying the
            description + screenshot caps. Read only when
            ``bugreport_enabled`` is True.
        batch_data_dir: The DRIP config's resolved ``data_dir``. Passed
            in rather than re-derived from ``logging.dir`` here because
            the route WRITES to ``<data_dir>/batch/<instance>/<id>/`` and
            the ``batch_image`` campaign READS from the path
            ``build_campaign`` derives from ``DripConfig.data_dir``. Two
            independent resolutions of the same value would eventually
            disagree, and the failure is silent and total: submissions
            land where nothing looks for them. One value, one source.
        batch_instance: The DRIP config's instance name, passed for the
            same reason as ``batch_data_dir`` — it is the other half of
            the batch path. Falls back to ``instance_name`` (both read
            ``telegram.instance.name``, so they agree today; the explicit
            kwarg is what keeps them agreeing if either moves).
        batch_config_path: This instance's config file, handed to the
            kicked drip run as ``--config``. Required for the kick: a run
            spawned without it reads ``config.yaml`` and would drain a
            DIFFERENT instance's campaigns.
        batch_kick_enabled: True exactly when the ``batch_image`` drip
            campaign is enabled here. Not an operator switch — it is how
            the route knows whether anything will process a submission,
            and therefore whether to answer "queued" or "saved".
        recall_enabled: Mount the cross-instance recall answer route
            (``POST /peer/recall``, #20 S1). Default ``False`` — an
            un-opted-in instance never mounts the route (opt-in inertness;
            ``/peer/recall`` → 404). Wired via
            :func:`routes_recall.register_recall_routes`, which also carries
            the STAY-C fence (never mounts on STAY-C; raises if enabled on
            one). The allowlist / peer-pin / bounded-projection policy is
            read by the handler from ``transport.recall`` at request time.
        feed_enabled: Mount the feed surface (``GET /feed/items`` +
            ``POST /feed/act``, Feed Phase B). Default ``False`` here; the
            daemon passes ``feed.enabled`` (default true). The routes are
            gated by the dedicated ``web_feed`` peer pin — dark until the
            operator adds that token. Wired via
            :func:`routes_feed.register_feed_routes`.
        feed_store: The constructed :class:`alfred.feed.FeedStore` the feed
            routes read + mutate. Read only when ``feed_enabled`` is True.
        feed_daily_sync_config: The instance's
            :class:`alfred.daily_sync.config.DailySyncConfig`, used by the
            action router to load ``last_batch`` and resolve corpus/queue
            paths. Read only when ``feed_enabled`` is True.
        feed_instance_scope: The running instance's scope name
            (``config.instance.tool_set``: ``"talker"`` / ``"kalle"`` /
            ``"hypatia"``) — forwarded to the proposal resolver on a
            proposal-confirm act. Read only when ``feed_enabled`` is True.
        feed_raw_config: The pre-loaded unified config dict — forwarded to
            the pending-item resolver so it skips per-call config.yaml reads.
            Read only when ``feed_enabled`` is True.

    Logging: emits one info event per registered resource so a
    misconfigured instance has a single grep target
    (``transport.wire_transport_app.*``) rather than spelunking
    through six different daemon paths.
    """
    # Late imports break the circular: peer_handlers imports from
    # server (config storage key constants) at module-load time.
    from .peer_handlers import (
        register_gcal_client,
        register_gcal_intended_on,
        register_instance_identity,
        register_nl_llm,
        register_peer_inbox,
        register_pending_items_aggregate_path,
        register_pending_items_resolve_callable,
        register_ticket_intake,
        register_ticket_outcome_resolver_callable,
        register_vault_path,
    )
    from .routes_batch import register_batch_routes
    from .routes_bugreport import register_bugreport_routes
    from .routes_feed import register_feed_routes
    from .routes_ingest import register_ingest_routes
    from .routes_jeeves import register_jeeves_routes
    from .routes_recall import register_recall_routes

    # Identity is unconditional — every instance has a name.
    register_instance_identity(app, name=instance_name, alias=instance_alias)
    log.info(
        "transport.wire_transport_app.instance_identity_registered",
        name=instance_name,
        alias=instance_alias,
    )

    # Per ``feedback_intentionally_left_blank.md``: a feature kwarg
    # passed as None must emit an explicit debug-level skip log so an
    # operator audit can distinguish "feature X intentionally not
    # wired (KAL-LE has no GCal)" from "feature X forgotten (developer
    # missed the kwarg)". Without these logs, both states look
    # identical in production.

    if vault_path is not None:
        register_vault_path(app, Path(vault_path))
        log.info(
            "transport.wire_transport_app.vault_path_registered",
            vault_path=str(vault_path),
        )
    else:
        log.debug(
            "transport.wire_transport_app.vault_path_skipped",
            reason="no vault_path passed (instance is vault-less or "
                   "caller forgot kwarg)",
        )

    if send_fn is not None:
        register_send_callable(app, send_fn)
        log.info("transport.wire_transport_app.send_fn_registered")
    else:
        log.debug(
            "transport.wire_transport_app.send_fn_skipped",
            reason="no send_fn passed (outbound/send will return 503)",
        )

    if telegram_available is not None:
        register_telegram_available(app, telegram_available)
        # At INFO, not DEBUG: on a web-only instance this line is the
        # startup-time statement that the outbound Telegram leg is dark,
        # and a startup honesty signal below the production log level is
        # silence wearing a log statement.
        log.info(
            "transport.wire_transport_app.telegram_available_registered",
            telegram_available=bool(
                telegram_available()
                if callable(telegram_available)
                else telegram_available
            ),
        )
    else:
        log.info(
            "transport.wire_transport_app.telegram_available_unwired",
            detail=(
                "/health will infer telegram_connected from send_fn "
                "registration, which is NOT evidence of a bot"
            ),
        )

    if pending_items_aggregate_path is not None:
        register_pending_items_aggregate_path(
            app, pending_items_aggregate_path,
        )
        log.info(
            "transport.wire_transport_app.pending_items_aggregate_registered",
            path=str(pending_items_aggregate_path),
        )
    else:
        log.debug(
            "transport.wire_transport_app.pending_items_aggregate_skipped",
            reason="no aggregate path passed (instance does not aggregate "
                   "peer pending-items pushes; KAL-LE / Hypatia leave None)",
        )

    if pending_items_resolve_callable is not None:
        register_pending_items_resolve_callable(
            app, pending_items_resolve_callable,
        )
        log.info(
            "transport.wire_transport_app.pending_items_resolver_registered",
        )
    else:
        log.debug(
            "transport.wire_transport_app.pending_items_resolver_skipped",
            reason="no resolver callable passed (instance has no "
                   "pending_items config block)",
        )

    if peer_inbox_callable is not None:
        register_peer_inbox(app, peer_inbox_callable)
        log.info("transport.wire_transport_app.peer_inbox_registered")
    else:
        log.debug(
            "transport.wire_transport_app.peer_inbox_skipped",
            reason="no peer_inbox callable passed (/peer/send will "
                   "return 501 peer_inbox_not_configured)",
        )

    if nl_llm_callable is not None:
        register_nl_llm(app, nl_llm_callable, model_label=nl_llm_model_label)
        log.info(
            "transport.wire_transport_app.nl_llm_registered",
            model=nl_llm_model_label,
        )
    else:
        log.debug(
            "transport.wire_transport_app.nl_llm_skipped",
            reason="no nl_llm callable passed (instance did not enable "
                   "transport.canonical.nl_broker, OR enabled it and the "
                   "daemon's Anthropic client construction failed — "
                   "kind=query_nl replies nl_broker_unavailable)",
        )

    # GCal: client + config must be paired. Either-but-not-both is a
    # configuration error — log + skip rather than crash, but the
    # explicit warning surfaces the half-wired state.
    if gcal_client is not None and gcal_config is not None:
        register_gcal_client(app, gcal_client, gcal_config)
        log.info(
            "transport.wire_transport_app.gcal_registered",
            alfred_calendar_id=getattr(gcal_config, "alfred_calendar_id", ""),
            primary_calendar_id_set=bool(
                getattr(gcal_config, "primary_calendar_id", "")
            ),
            calendar_label=getattr(gcal_config, "alfred_calendar_label", ""),
            time_zone=getattr(gcal_config, "default_time_zone", ""),
        )
    elif gcal_client is not None or gcal_config is not None:
        log.warning(
            "transport.wire_transport_app.gcal_partial_wiring",
            client_set=gcal_client is not None,
            config_set=gcal_config is not None,
            detail=(
                "GCal client and config must be wired together; "
                "skipping GCal registration"
            ),
        )
    else:
        log.debug(
            "transport.wire_transport_app.gcal_skipped",
            reason="no gcal_client + gcal_config passed (instance "
                   "did not opt into GCal integration; KAL-LE / "
                   "Hypatia / non-GCal Salem all leave None)",
        )

    # Ticket intake (pipeline c3): config + github client must be
    # paired — same posture as GCal above. Either-but-not-both is a
    # wiring error; log + skip rather than crash, with the explicit
    # warning surfacing the half-wired state (kind=ticket 501s).
    if ticket_intake_config is not None and ticket_intake_github_client is not None:
        register_ticket_intake(
            app,
            intake_config=ticket_intake_config,
            github_client=ticket_intake_github_client,
        )
        log.info(
            "transport.wire_transport_app.ticket_intake_registered",
            repo=getattr(
                getattr(ticket_intake_github_client, "config", None),
                "repo", "",
            ),
            state_path=getattr(ticket_intake_config, "state_path", ""),
        )
    elif ticket_intake_config is not None or ticket_intake_github_client is not None:
        log.warning(
            "transport.wire_transport_app.ticket_intake_partial_wiring",
            config_set=ticket_intake_config is not None,
            client_set=ticket_intake_github_client is not None,
            detail=(
                "ticket intake config and github client must be wired "
                "together; skipping registration — kind=ticket will 501"
            ),
        )
    else:
        log.debug(
            "transport.wire_transport_app.ticket_intake_skipped",
            reason="no ticket_intake config + github client passed "
                   "(instance is not the pipeline's intake — only "
                   "KAL-LE registers this)",
        )

    # Ticket-outcome resolver (pipeline c7): the VERA-side receiver for
    # the KAL-LE→VERA outcome write-back. Single callable, registered
    # only on the ticket origin instance (VERA in MVP). Absent →
    # POST /peer/ticket_outcome 501s (the capability isn't advertised
    # in the handshake either) — explicit-by-omission, not silent.
    if ticket_outcome_resolve_callable is not None:
        register_ticket_outcome_resolver_callable(
            app, ticket_outcome_resolve_callable,
        )
        log.info(
            "transport.wire_transport_app.ticket_outcome_resolver_registered",
        )
    else:
        log.debug(
            "transport.wire_transport_app.ticket_outcome_resolver_skipped",
            reason="no ticket_outcome resolver callable passed (instance "
                   "is not a ticket-pipeline origin — only VERA registers "
                   "this; KAL-LE/Salem/Hypatia leave None)",
        )

    # P2-4 sentinel: ``gcal_intended_on=True`` flags the
    # "operator wanted GCal but setup failed" state so the conflict-
    # check + sync skip sites log at warning level instead of debug.
    # Wiring this is independent of (gcal_client, gcal_config) — the
    # daemon sets the flag BEFORE attempting client construction so
    # the sentinel survives a setup failure that left the client unset.
    if gcal_intended_on:
        register_gcal_intended_on(app)
        log.info("transport.wire_transport_app.gcal_intended_on_registered")
    else:
        log.debug(
            "transport.wire_transport_app.gcal_intended_on_skipped",
            reason="gcal_intended_on flag not set (instance did not "
                   "opt into GCal, OR opted in and client constructed "
                   "successfully so no sentinel needed)",
        )

    # Cross-instance verbatim ingest route (2026-06-29). Opt-in via
    # ``transport.ingest.enabled``; register_ingest_routes emits its own
    # explicit disabled-skip log (intentionally-left-blank) when off, so
    # the wire-level branch only needs to surface the wired case.
    if ingest_enabled:
        register_ingest_routes(
            app,
            enabled=True,
            instance_name=instance_name,
            max_body_chars=getattr(
                ingest_config, "max_body_chars", DEFAULT_INGEST_MAX_BODY_CHARS,
            ),
            types=list(getattr(ingest_config, "types", []) or []),
        )
        log.info(
            "transport.wire_transport_app.ingest_registered",
            max_body_chars=getattr(
                ingest_config, "max_body_chars", DEFAULT_INGEST_MAX_BODY_CHARS,
            ),
        )
    else:
        # Still call the registrar so its own disabled-skip log fires —
        # keeps the "ran, did not mount" signal greppable + symmetric.
        register_ingest_routes(
            app, enabled=False, instance_name=instance_name,
        )
        log.debug(
            "transport.wire_transport_app.ingest_skipped",
            reason="transport.ingest.enabled is false / absent (instance "
                   "did not opt into the cross-instance ingest route)",
        )

    # RRTS invoices-export receiver. Opt-in via
    # ``transport.rrts_export.enabled``; register_rrts_routes emits its own
    # disabled-skip log, so the wire-level branch only surfaces the wired
    # case — same symmetry as ingest below it.
    from .routes_rrts import (
        DEFAULT_RRTS_MAX_BYTES as _RRTS_MAX,
        export_dir_for as _rrts_export_dir_for,
        register_rrts_routes as _register_rrts_routes,
    )

    if rrts_export_enabled:
        # The export directory is RESOLVED HERE from the value passed in,
        # never re-derived and never a literal. Same reasoning as
        # ``batch_data_dir`` above: the receiver WRITES this path and the
        # reconciler READS it, so two independent resolutions would
        # eventually disagree — and that failure is silent and total, the
        # snapshot landing where nothing looks for it. An explicitly
        # configured ``export_dir`` wins; otherwise it is the instance's
        # own data dir, which is what keeps two instances on one box from
        # sharing one invoices.json.
        configured_dir = str(
            getattr(rrts_export_config, "export_dir", "") or ""
        ).strip()
        # The segment is NOT spelled here. `export_dir_for` is the one
        # derivation, and the P2 reader calls the same helper — a second
        # spelling is how a writer and a reader land on different files
        # while both look correct.
        resolved_dir = configured_dir or _rrts_export_dir_for(rrts_data_dir)
        _register_rrts_routes(
            app,
            enabled=True,
            export_dir=resolved_dir,
            max_bytes=int(
                getattr(rrts_export_config, "max_bytes", _RRTS_MAX) or _RRTS_MAX
            ),
        )
        log.info(
            "transport.wire_transport_app.rrts_export_registered",
            export_dir=resolved_dir or "(unresolved — writes will 503)",
            source="config" if configured_dir else "instance data dir",
        )
    else:
        # Still call the registrar so its disabled-skip log fires — keeps
        # "ran, did not mount" greppable and symmetric with every other
        # opt-in route.
        _register_rrts_routes(app, enabled=False)
        # INFO, not DEBUG. This log exists to distinguish "not wired" from
        # "forgotten" — and it just failed to do that: the route was never
        # threaded at the production call site, the skip fired on every
        # startup, and nobody saw it because production runs at INFO. A
        # signal emitted below the level anyone reads is the silence it was
        # written to prevent. It fires once per startup, so there is no spam
        # case to weigh against being visible.
        log.info(
            "transport.wire_transport_app.rrts_export_skipped",
            reason="transport.rrts_export.enabled is false / absent",
        )

    # In-app bug reporting (#95). Opt-in via ``transport.bugreport.enabled``;
    # register_bugreport_routes emits its own explicit disabled-skip log
    # (intentionally-left-blank) when off, so the wire-level branch only needs
    # to surface the wired case.
    _bugreport_max_desc = getattr(
        bugreport_config, "max_description_chars",
        DEFAULT_BUGREPORT_MAX_DESCRIPTION_CHARS,
    )
    _bugreport_max_shot = getattr(
        bugreport_config, "max_screenshot_bytes",
        DEFAULT_BUGREPORT_MAX_SCREENSHOT_BYTES,
    )
    if bugreport_enabled:
        register_bugreport_routes(
            app,
            enabled=True,
            instance_name=instance_name,
            max_description_chars=_bugreport_max_desc,
            max_screenshot_bytes=_bugreport_max_shot,
        )
        log.info(
            "transport.wire_transport_app.bugreport_registered",
            max_description_chars=_bugreport_max_desc,
            max_screenshot_bytes=_bugreport_max_shot,
        )
    else:
        # Still call the registrar so its own disabled-skip log fires — keeps
        # the "ran, did not mount" signal greppable + symmetric with ingest.
        register_bugreport_routes(
            app, enabled=False, instance_name=instance_name,
        )
        log.debug(
            "transport.wire_transport_app.bugreport_skipped",
            reason="transport.bugreport.enabled is false / absent (instance "
                   "did not opt into the in-app bug-report route)",
        )

    # Jeeves capture intake (#81). Opt-in via ``transport.jeeves.enabled``;
    # register_jeeves_routes emits its own disabled-skip log when off. The
    # RAW config is threaded through because the route's fail-closed mode
    # gate builds the typed jeeves config itself — an instance that never
    # receives a capture never imports the jeeves package at all.
    if jeeves_enabled:
        register_jeeves_routes(
            app,
            enabled=True,
            instance_name=instance_name,
            max_transcript_chars=getattr(
                jeeves_config, "max_transcript_chars",
                DEFAULT_JEEVES_MAX_TRANSCRIPT_CHARS,
            ),
            types=list(getattr(jeeves_config, "types", []) or []),
            jeeves_raw_config=jeeves_raw_config or {},
        )
        log.info(
            "transport.wire_transport_app.jeeves_registered",
            max_transcript_chars=getattr(
                jeeves_config, "max_transcript_chars",
                DEFAULT_JEEVES_MAX_TRANSCRIPT_CHARS,
            ),
        )
    else:
        register_jeeves_routes(app, enabled=False)
        log.debug(
            "transport.wire_transport_app.jeeves_skipped",
            reason="transport.jeeves.enabled is false / absent (instance has "
                   "no garage capture device pointed at it)",
        )

    # Bulk scan intake (#83). Opt-in via ``transport.batch.enabled``;
    # register_batch_routes emits its own disabled-skip log when off.
    if batch_enabled:
        register_batch_routes(
            app,
            enabled=True,
            instance_name=batch_instance or instance_name,
            data_dir=batch_data_dir,
            max_images=getattr(
                batch_config, "max_images", DEFAULT_BATCH_MAX_IMAGES,
            ),
            max_image_bytes=getattr(
                batch_config, "max_image_bytes", DEFAULT_BATCH_MAX_IMAGE_BYTES,
            ),
            max_total_bytes=getattr(
                batch_config, "max_total_bytes", DEFAULT_BATCH_MAX_TOTAL_BYTES,
            ),
            max_instruction_chars=getattr(
                batch_config, "max_instruction_chars",
                DEFAULT_BATCH_MAX_INSTRUCTION_CHARS,
            ),
            config_path=batch_config_path,
            kick_enabled=batch_kick_enabled,
        )
        log.info(
            "transport.wire_transport_app.batch_registered",
            instance=batch_instance or instance_name,
            data_dir=batch_data_dir or "(unset)",
            kick_enabled=bool(batch_kick_enabled and batch_config_path),
        )
    else:
        # Symmetric with ingest: still call the registrar so its own
        # "ran, did not mount" signal fires and stays greppable.
        register_batch_routes(
            app, enabled=False, instance_name=batch_instance or instance_name,
        )
        log.debug(
            "transport.wire_transport_app.batch_skipped",
            reason="transport.batch.enabled is false / absent (instance did "
                   "not opt into the bulk scan intake route)",
        )

    # Cross-instance recall answer route (#20 S1). Opt-in via
    # ``transport.recall.enabled``; register_recall_routes emits its own
    # explicit disabled-skip log (intentionally-left-blank) when off, and
    # carries the STAY-C fence (never mounts on STAY-C).
    if recall_enabled:
        register_recall_routes(
            app, enabled=True, instance_name=instance_name,
        )
        log.info(
            "transport.wire_transport_app.recall_registered",
            instance=instance_name,
        )
    else:
        # Still call the registrar so its own disabled-skip log fires —
        # keeps the "ran, did not mount" signal greppable + symmetric.
        register_recall_routes(
            app, enabled=False, instance_name=instance_name,
        )
        log.info(
            "transport.wire_transport_app.recall_skipped",
            reason="transport.recall.enabled is false / absent (instance "
                   "did not opt into the cross-instance recall route)",
        )

    # Feed surface (Feed Phase B) — GET /feed/items + POST /feed/act, gated by
    # the dedicated ``web_feed`` peer pin. Mounted when the feed is enabled
    # (default true); dark until the operator adds the ``web_feed`` token
    # (fail-closed 401 until then). register_feed_routes emits its own explicit
    # disabled-skip log (intentionally-left-blank) when off.
    if feed_enabled:
        register_feed_routes(
            app,
            enabled=True,
            feed_store=feed_store,
            daily_sync_config=feed_daily_sync_config,
            instance_name=instance_name,
            instance_scope=feed_instance_scope,
            raw_config=feed_raw_config,
        )
        log.info(
            "transport.wire_transport_app.feed_registered",
            scope=feed_instance_scope,
        )
    else:
        # Still call the registrar so its own disabled-skip log fires —
        # keeps the "ran, did not mount" signal greppable + symmetric.
        register_feed_routes(app, enabled=False)
        log.debug(
            "transport.wire_transport_app.feed_skipped",
            reason="feed.enabled is false / absent (instance did not opt "
                   "into the feed surface)",
        )


async def run_server(
    app: web.Application,
    config: TransportConfig,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Run the aiohttp server until ``shutdown_event`` is set.

    Used by the talker daemon in commit 6 as a sibling asyncio task.
    Tests spin up their own client on top of the ``app`` directly and
    never call this function.
    """
    runner = web.AppRunner(app)
    # setup() + the bind loop live INSIDE the try so runner.cleanup() ALWAYS
    # runs on the failure path (a partial-bind exception must not leak the
    # AppRunner's sockets).
    try:
        await runner.setup()
        # One TCPSite per bind address (a TCPSite binds a single host). The
        # validated host_list() is single-element for a string ``host``
        # (byte-identical to the pre-Stage-3.5 single-bind path) and N-element
        # for the multi-bind list form; every site shares the one AppRunner +
        # the one port. Bind exactly the named addresses — never 0.0.0.0.
        #
        # Bind LOOPBACK FIRST so a misordered config (e.g.
        # ``[10.99.0.1, 127.0.0.1]``) can't starve loopback — the co-located
        # lifeline (health probe + orchestrator env-inject target) must bind.
        hosts = sorted(
            config.server.host_list(),
            key=lambda h: 0 if host_is_loopback(h) else 1,
        )
        bound: list[str] = []
        loopback_failed = False
        for host in hosts:
            # Failure-isolate each bind: a non-assignable address (e.g. the
            # WireGuard overlay IP when wg0 is down → OSError EADDRNOTAVAIL)
            # must NOT abort the whole transport. Warn + continue; hard-fail
            # only on the genuinely-fatal conditions below.
            try:
                site = web.TCPSite(runner, host=host, port=config.server.port)
                await site.start()
            except OSError as exc:
                log.warning(
                    "transport.server.bind_failed",
                    host=host,
                    port=config.server.port,
                    error=str(exc),
                )
                if host_is_loopback(host):
                    loopback_failed = True
                continue
            bound.append(host)
            log.info(
                "transport.server.listening",
                host=host,
                port=config.server.port,
            )
        # Hard-fail ONLY when nothing bound OR loopback specifically failed —
        # loopback is the co-located lifeline, so its absence is fatal even if
        # an overlay address bound. Raising propagates to the daemon's
        # transport-task supervisor (telegram/daemon.py), which logs
        # ``transport.server.task_died`` + triggers graceful shutdown so
        # systemd restarts and re-attempts. Otherwise: proceed degraded-but-up.
        if not bound or loopback_failed:
            raise RuntimeError(
                "transport bind failed (fatal): "
                f"bound={bound} loopback_failed={loopback_failed} "
                f"requested={hosts}"
            )
        # Loopback OMITTED from config (e.g. host: [10.99.0.1]) → we bound
        # overlay-only and the co-located web BFF / health probe / client are
        # unserved. That's the operator's explicit choice (NOT fatal), but it
        # must not be SILENT (intentionally-left-blank) — emit a distinct loud
        # WARN so a missing loopback is visible, not a mystery 'connection
        # refused' downstream.
        if not any(host_is_loopback(h) for h in bound):
            log.warning(
                "transport.server.no_loopback_bound",
                bound=bound,
                detail=(
                    "no loopback address among the bound hosts — co-located "
                    "callers (web BFF, health probe, transport client) cannot "
                    "reach the transport over loopback"
                ),
            )
        # Truthful summary — the addresses that ACTUALLY bound + how many were
        # dropped/failed (not a claim that everything in the list bound).
        log.info(
            "transport.server.bound",
            hosts=bound,
            host_count=len(bound),
            failed=len(hosts) - len(bound),
            port=config.server.port,
        )
        if shutdown_event is None:
            # Park forever — caller will cancel the task.
            while True:
                await asyncio.sleep(3600)
        else:
            await shutdown_event.wait()
    finally:
        log.info("transport.server.stopping")
        await runner.cleanup()
