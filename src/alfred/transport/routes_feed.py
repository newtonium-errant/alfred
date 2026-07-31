"""Feed transport surface — ``GET /feed/items`` + ``POST /feed/act``.

The PWA's Decide deck and Awareness feed read the folded feed store and act on
cards through these two peer-token-gated routes. Acting delegates to
:func:`alfred.daily_sync.action_router.act` — the SAME resolvers the Daily Sync
reply grammar uses. The router's ``(kind, action_id)`` map is the capability
ceiling: this peer can DECIDE feed cards and nothing else (no chat, no ingest,
no vault CRUD outside a mapped resolver).

Auth: Layer 1 (peer token + ``X-Alfred-Client: web``) is enforced by the
transport ``auth_middleware`` for every non-``/health`` route. Both handlers
ALSO peer-pin ``transport_peer == "web_feed"`` (:data:`FEED_PEER_NAME`): the
chat ``web`` token, the ``web_ingest`` token, and this ``web_feed`` token all
carry ``allowed_clients: [web]``, so ``allowed_clients`` alone can't distinguish
them — without the pin a full-chat ``web`` token could drive a feed act. Wrong
peer → fail-closed 401 + a logged ``transport.feed.rejected reason=wrong_peer``.
See CLAUDE.md "Relay / asserted-identity routes — peer-pin requirement".

Deploy posture (all dark): the routes mount whenever the feed is enabled
(default true — a local JSONL, belt-guarded), but the ``web_feed`` token is not
in the box config until the operator adds it, so every request fails auth 401
until then. One box FF + one token line lights the surface.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from .peer_handlers import _get_vault_path
from .utils import get_logger

log = get_logger(__name__)

# The dedicated feed peer NAME (``auth.tokens`` key) whose token authorises feed
# reads + acts. PINNED explicitly — web / web_ingest / web_feed all carry
# ``allowed_clients: [web]``, so without this pin a chat ``web`` token would
# clear Layer 1 as peer ``web`` and drive a feed act.
FEED_PEER_NAME = "web_feed"

# Application-storage keys (stashed by ``register_feed_routes`` so the handlers
# reach them without globals).
_KEY_FEED_STORE = "transport.feed_store"
_KEY_FEED_DS_CONFIG = "transport.feed_ds_config"
_KEY_FEED_INSTANCE = "transport.feed_instance"
_KEY_FEED_SCOPE = "transport.feed_scope"
_KEY_FEED_RAW_CONFIG = "transport.feed_raw_config"

# ActResult.status → HTTP status. Applied paths + the idempotent noop are 200;
# a moved-on item is 409; a bad action is 400; a resolver/domain failure is 422.
# Phase C slice 1 (board DONE path): ``undone`` is an applied path (200);
# ``unsupported_item`` is a lane with no completion writer (422 — understood but
# unprocessable, distinct from a bad action).
_HTTP_STATUS_BY_STATUS: dict[str, int] = {
    "acted": 200,
    "acked": 200,
    "already_acted": 200,
    "undone": 200,
    "stale_item": 409,
    "invalid_action": 400,
    "unsupported_item": 422,
    "error": 422,
}


def _json_error(status: int, error: str, **extra: Any) -> web.Response:
    """Consistent error shape — ``{"error": <code>, ...}`` (mirrors ingest)."""
    payload: dict[str, Any] = {"error": error}
    payload.update(extra)
    return web.json_response(payload, status=status)


def _pin_ok(request: web.Request) -> bool:
    """Peer-pin check; logs + returns False on a wrong-peer request."""
    peer = request.get("transport_peer", "")
    if peer != FEED_PEER_NAME:
        log.warning(
            "transport.feed.rejected",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=FEED_PEER_NAME,
            detail="feed routes require the dedicated 'web_feed' peer token — "
                   "refusing from another peer (e.g. web / web_ingest)",
        )
        return False
    return True


async def _handle_feed_items(request: web.Request) -> web.StreamResponse:
    """GET /feed/items — the folded feed store, optionally filtered.

    Query params (all optional, AND-combined): ``state`` / ``mode`` / ``kind``.
    Returns ``{"items": [<FeedItem dict>...], "count": N}`` sorted by
    ``created_at`` then ``id`` for a stable render order.
    """
    if not _pin_ok(request):
        return _json_error(401, "feed_wrong_peer")

    store = request.app.get(_KEY_FEED_STORE)
    if store is None:
        return _json_error(503, "feed_not_configured")

    q_state = request.query.get("state")
    q_mode = request.query.get("mode")
    q_kind = request.query.get("kind")

    all_items = list(store.load().values())

    def _match(it: Any) -> bool:
        if q_state and it.state != q_state:
            return False
        if q_mode and it.mode != q_mode:
            return False
        if q_kind and it.kind != q_kind:
            return False
        return True

    filtered = sorted(
        (it.to_dict() for it in all_items if _match(it)),
        key=lambda d: (d.get("created_at") or "", d.get("id") or ""),
    )
    # ILB: log every read (even count=0) so "empty feed" is distinguishable
    # from "route silently broke".
    log.info(
        "transport.feed.items",
        peer=request.get("transport_peer", ""),
        count=len(filtered),
        total=len(all_items),
        state=q_state or "",
        mode=q_mode or "",
        kind=q_kind or "",
    )
    return web.json_response({"items": filtered, "count": len(filtered)})


async def _handle_feed_act(request: web.Request) -> web.StreamResponse:
    """POST /feed/act ``{id, action_id}`` — act on one card via the router.

    The router's ``(kind, action_id)`` map is the capability ceiling. The
    synchronous ``act`` runs in a thread executor so a slow pending peer-dispatch
    can't freeze the transport event loop.
    """
    if not _pin_ok(request):
        return _json_error(401, "feed_wrong_peer")

    store = request.app.get(_KEY_FEED_STORE)
    ds_config = request.app.get(_KEY_FEED_DS_CONFIG)
    if store is None or ds_config is None:
        return _json_error(503, "feed_not_configured")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — malformed body → 400
        return _json_error(400, "invalid_json")
    if not isinstance(body, dict):
        return _json_error(400, "invalid_json")

    feed_item_id = body.get("id")
    action_id = body.get("action_id")
    if not isinstance(feed_item_id, str) or not feed_item_id.strip():
        return _json_error(400, "missing_id")
    if not isinstance(action_id, str) or not action_id.strip():
        return _json_error(400, "missing_action_id")

    vault_path = _get_vault_path(request)
    instance_name = request.app.get(_KEY_FEED_INSTANCE, "") or ""
    instance_scope = request.app.get(_KEY_FEED_SCOPE, "") or ""
    raw_config = request.app.get(_KEY_FEED_RAW_CONFIG)

    # Lazy import — action_router pulls the heavy reply_dispatch module only when
    # a request actually fires, keeping this module import-light for tests.
    from alfred.daily_sync import action_router

    def _do_act() -> Any:
        return action_router.act(
            feed_item_id.strip(),
            action_id.strip(),
            feed_store=store,
            config=ds_config,
            vault_path=vault_path,
            instance_name=instance_name,
            instance_scope=instance_scope,
            raw_config=raw_config,
        )

    result = await asyncio.get_running_loop().run_in_executor(None, _do_act)

    log.info(
        "transport.feed.act",
        peer=request.get("transport_peer", ""),
        id=feed_item_id,
        action=action_id,
        status=result.status,
        ok=result.ok,
    )
    status_code = _HTTP_STATUS_BY_STATUS.get(result.status, 200 if result.ok else 400)
    return web.json_response(result.to_dict(), status=status_code)


def register_feed_routes(
    app: web.Application,
    *,
    enabled: bool,
    feed_store: Any = None,
    daily_sync_config: Any = None,
    instance_name: str = "",
    instance_scope: str = "",
    raw_config: "dict[str, Any] | None" = None,
) -> bool:
    """Mount ``GET /feed/items`` + ``POST /feed/act`` — IFF the feed is enabled.

    Returns ``True`` when the routes were mounted, ``False`` when disabled
    (opt-in inertness: nothing registered + the transport server byte-unchanged).
    Must be called BEFORE the app is started (aiohttp forbids route additions on
    a started app); the daemon calls it via
    :func:`alfred.transport.server.wire_transport_app`, the same pre-start window
    as every other ``register_*`` helper.

    The routes inherit the transport ``auth_middleware`` peer-gating
    automatically; the handlers additionally peer-pin :data:`FEED_PEER_NAME`.
    """
    if not enabled:
        # Intentionally-left-blank: disabled is a deliberate state, logged so
        # "no feed routes" is distinguishable from "wiring silently skipped".
        log.info(
            "transport.feed.disabled",
            reason="feed.enabled is false / absent",
        )
        return False

    app[_KEY_FEED_STORE] = feed_store
    app[_KEY_FEED_DS_CONFIG] = daily_sync_config
    app[_KEY_FEED_INSTANCE] = instance_name
    app[_KEY_FEED_SCOPE] = instance_scope
    app[_KEY_FEED_RAW_CONFIG] = raw_config
    app.router.add_get("/feed/items", _handle_feed_items)
    app.router.add_post("/feed/act", _handle_feed_act)
    log.info(
        "transport.feed.registered",
        instance=instance_name,
        scope=instance_scope,
    )
    return True
