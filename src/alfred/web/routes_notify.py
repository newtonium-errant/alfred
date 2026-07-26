"""Web notification read routes — KAL-LE ticket → PWA notify (parity #22).

The POLL / READ-ON-REQUEST slice (no push channel — deferred):

    GET  /chat/notifications      → {notifications: [...], unread: int}
    POST /chat/notifications/ack  → {acked: int, unread: int}

serving the bounded per-user store (:mod:`alfred.web.notify_state`) the
transport-level sink fills from ``web_notify``-tagged peer notices.
Mounted by :func:`alfred.web.routes_chat.register_web_routes` on the
transport app, so Layer-1 peer-token auth (``auth_middleware``) already
gates it.

Handler ordering is load-bearing (CLAUDE.md "Relay / asserted-identity
routes — peer-pin requirement"):

1. **Peer-pin FIRST** — ``transport_peer`` must be the dedicated chat
   ``web`` peer or the vouched ``rrts_relay`` peer (the two peers
   ``resolve_web_identity`` itself accepts), checked BEFORE any identity
   resolution or store read. The ``web_ingest`` token shares
   ``allowed_clients: [web]`` — without the pin a deterministic-create-
   only ingest token could read the operator's notifications. Fail-closed
   401 + a logged ``web.notify.wrong_peer``.
2. **Identity** — :func:`resolve_web_identity` (mode-aware, same shape as
   ``_handle_chat_history``), fail-closed 401 ``invalid_session``.
3. **Recipient-pin** — a relay/vouched ``RRTS_INTAKE_ROLE`` identity is
   REFUSED with an explicit 403: a bug-report reporter is authenticated
   enough to chat, but must NEVER read (or ack) the operator's
   notification tray. Explicit 403 (not empty-200) so a misrouted
   reporter client fails loud, not silently blank.
4. **Read/ack** — keyed to the identity's OWN ``synthetic_chat_id``. An
   absent store (notifications on but no ``data_dir`` threaded) and an
   empty tray both serve the intentionally-left-blank
   ``{notifications: [], unread: 0}`` — never a 404/500.
"""

from __future__ import annotations

from aiohttp import web

from alfred.vault.scope import RRTS_INTAKE_ROLE

from .auth import RRTS_RELAY_PEER, WEB_CHAT_PEER, resolve_web_identity
from .config import WebConfig
from .identity import WebIdentity
from .keys import KEY_WEB_CONFIG, KEY_WEB_NOTIFY_STORE
from .utils import get_logger

log = get_logger(__name__)

# Ack-body edge guard: more ids than the store can even hold is malformed.
MAX_ACK_IDS = 200


def _resolve_notify_identity(
    request: web.Request,
) -> tuple[WebIdentity | None, web.Response | None]:
    """Shared auth spine for both routes — ``(identity, error_response)``.

    Exactly one of the pair is non-``None``. See the module docstring for
    the load-bearing ordering (peer-pin → identity → recipient-pin).
    """
    # (a) Peer-pin — BEFORE identity resolution or any read. Allows the
    # same two peers resolve_web_identity accepts; the rrts_relay peer
    # passes HERE but is refused at the recipient-pin below (403, so the
    # reporter-vs-operator distinction is observable, not conflated with
    # the wrong-peer escalation class).
    peer = request.get("transport_peer", "")
    if peer not in (WEB_CHAT_PEER, RRTS_RELAY_PEER):
        log.warning(
            "web.notify.wrong_peer",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=f"{WEB_CHAT_PEER} | {RRTS_RELAY_PEER}",
            detail="notification read/ack requires the dedicated chat "
                   "'web' (or vouched 'rrts_relay') peer token — refusing "
                   "another peer (e.g. web_ingest) — rejecting (401)",
        )
        return None, web.json_response({"error": "wrong_peer"}, status=401)

    # (b) Layer-2 identity — same fail-closed shape as _handle_chat_history.
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return None, web.json_response({"error": "invalid_session"}, status=401)

    # (c) Recipient-pin — a vouched RRTS reporter must not read the
    # operator's tray. Explicit 403 (ratified: 403 over empty-200).
    if identity.role == RRTS_INTAKE_ROLE:
        log.warning(
            "web.notify.reporter_refused",
            reporter=identity.user[:64],
            detail="rrts_intake (vouched reporter) identity may not read "
                   "or ack operator notifications — rejecting (403)",
        )
        return None, web.json_response({"error": "forbidden"}, status=403)

    return identity, None


async def _handle_notifications_list(request: web.Request) -> web.StreamResponse:
    """GET /chat/notifications — the caller's own tray, newest-first."""
    identity, err = _resolve_notify_identity(request)
    if err is not None:
        return err
    assert identity is not None  # narrowed by the shared spine

    store = request.app.get(KEY_WEB_NOTIFY_STORE)
    if store is None:
        # Intentionally-left-blank: notifications enabled but no store
        # (data_dir not threaded) — explicit empty 200, observably logged.
        log.info(
            "web.notify.store_absent",
            user=identity.user,
            reason="no notify store wired (data_dir not threaded)",
        )
        return web.json_response({"notifications": [], "unread": 0})

    items = store.list_for(identity.synthetic_chat_id)
    unread = store.unread_count(identity.synthetic_chat_id)
    if not items:
        # Intentionally-left-blank: an empty tray is "ran, nothing to
        # surface" — distinct from a broken read, never a 404.
        log.info("web.notify.empty", user=identity.user)
    return web.json_response({"notifications": items, "unread": unread})


async def _handle_notifications_ack(request: web.Request) -> web.StreamResponse:
    """POST /chat/notifications/ack {ids: [...]} — mark read, idempotent."""
    identity, err = _resolve_notify_identity(request)
    if err is not None:
        return err
    assert identity is not None  # narrowed by the shared spine

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → 400
        return web.json_response({"error": "invalid_json"}, status=400)
    ids = body.get("ids") if isinstance(body, dict) else None
    if (
        not isinstance(ids, list)
        or len(ids) > MAX_ACK_IDS
        or not all(isinstance(i, str) and i for i in ids)
    ):
        return web.json_response({"error": "invalid_ids"}, status=400)

    store = request.app.get(KEY_WEB_NOTIFY_STORE)
    if store is None:
        log.info(
            "web.notify.store_absent",
            user=identity.user,
            reason="no notify store wired (data_dir not threaded)",
        )
        return web.json_response({"acked": 0, "unread": 0})

    acked = store.ack(identity.synthetic_chat_id, ids)
    unread = store.unread_count(identity.synthetic_chat_id)
    log.info(
        "web.notify.acked",
        user=identity.user,
        requested=len(ids),
        acked=acked,
        unread=unread,
    )
    return web.json_response({"acked": acked, "unread": unread})


def register_notify_routes(app: web.Application) -> None:
    """Mount the notification read/ack routes. Called by
    ``register_web_routes`` (web config + store already stashed)."""
    app.router.add_get("/chat/notifications", _handle_notifications_list)
    app.router.add_post("/chat/notifications/ack", _handle_notifications_ack)
