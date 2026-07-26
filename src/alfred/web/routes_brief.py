"""Web outbound-read routes — brief + Daily Sync READ-ON-OPEN (#30).

One route:

    GET /web/outbound/{kind}/latest → {kind, date, markdown}

serving the latest daemon-spooled outbound artifact (see
:mod:`alfred.web.outbound_store` — the brief daemon spools ``brief``, the
daily_sync daemon spools ``daily_sync``). Mounted by
:func:`alfred.web.routes_chat.register_web_routes` on the transport app,
so Layer-1 peer-token auth (``auth_middleware``) already gates it.

Handler ordering is load-bearing (CLAUDE.md "Relay / asserted-identity
routes — peer-pin requirement"):

1. **Peer-pin FIRST** — ``transport_peer`` must be the dedicated chat
   ``web`` peer (:data:`alfred.web.auth.WEB_CHAT_PEER`), checked BEFORE
   any identity resolution or spool read. The ``web_ingest`` token shares
   ``allowed_clients: [web]``, so it clears Layer 1 as peer
   ``web_ingest`` — without the pin a deterministic-create-only ingest
   token could read the operator's brief. Fail-closed 401 + a logged
   ``web.outbound.wrong_peer``.
2. **Identity** — :func:`resolve_web_identity` (mode-aware, same shape as
   ``_handle_chat_history``), fail-closed 401 ``invalid_session``.
3. **Kind allowlist** — 400 ``unknown_kind`` for anything outside
   :data:`OUTBOUND_KINDS`.
4. **Read** — ``read_latest``; an absent spool (including an unthreaded
   ``data_dir``) is the intentionally-left-blank empty 200
   ``{kind, date: null, markdown: null}`` — NEVER a 404, so the FE can
   render "no brief yet today" instead of an error state.
"""

from __future__ import annotations

from aiohttp import web

from .auth import WEB_CHAT_PEER, resolve_web_identity
from .config import WebConfig
from .keys import KEY_WEB_CONFIG, KEY_WEB_DATA_DIR
from .outbound_store import read_latest
from .utils import get_logger

log = get_logger(__name__)

# The outbound artifact kinds the route serves. Anything else → 400
# unknown_kind (the kind is a path segment — allowlist, never interpolate
# it into a filesystem path unchecked).
OUTBOUND_KINDS: frozenset[str] = frozenset({"brief", "daily_sync"})


async def _handle_outbound_latest(request: web.Request) -> web.StreamResponse:
    """GET /web/outbound/{kind}/latest — the latest spooled artifact."""
    # (a) Peer-pin — BEFORE identity resolution or any read (see module
    # docstring / CLAUDE.md peer-pin requirement). ``transport_peer`` is
    # the matched ``auth.tokens`` key stamped by ``auth_middleware``.
    peer = request.get("transport_peer", "")
    if peer != WEB_CHAT_PEER:
        log.warning(
            "web.outbound.wrong_peer",
            reason="wrong_peer",
            peer=peer or "(none)",
            expected=WEB_CHAT_PEER,
            detail="outbound read requires the dedicated chat 'web' peer "
                   "token — refusing a read from another peer "
                   "(e.g. web_ingest) — rejecting (401)",
        )
        return web.json_response({"error": "wrong_peer"}, status=401)

    # (b) Layer-2 identity — same fail-closed shape as _handle_chat_history.
    web_config: WebConfig = request.app[KEY_WEB_CONFIG]
    identity = resolve_web_identity(request, web_config)
    if identity is None:
        return web.json_response({"error": "invalid_session"}, status=401)

    # (c) Kind allowlist.
    kind = request.match_info.get("kind", "")
    if kind not in OUTBOUND_KINDS:
        return web.json_response({"error": "unknown_kind"}, status=400)

    # (d) Read. ``data_dir`` may be None when the mount site didn't thread
    # it — that reads as "nothing spooled" (never a crash).
    data_dir = request.app.get(KEY_WEB_DATA_DIR)
    latest = read_latest(data_dir, kind)
    if latest is None:
        # Intentionally-left-blank: an empty spool is "ran, nothing to
        # surface" (daemon hasn't fired yet today / ever) — an explicit
        # 200 with null fields, observably distinct from a broken read
        # and NEVER a 404 (the FE renders "no brief yet today").
        log.info(
            "web.outbound.empty",
            kind=kind,
            user=identity.user,
            data_dir_set=data_dir is not None,
        )
        return web.json_response({"kind": kind, "date": None, "markdown": None})

    return web.json_response(
        {"kind": kind, "date": latest["date"], "markdown": latest["markdown"]}
    )


def register_brief_routes(app: web.Application) -> None:
    """Mount the outbound-read route. Called by ``register_web_routes``
    (web config already stashed on the app; data_dir stashed there too)."""
    app.router.add_get("/web/outbound/{kind}/latest", _handle_outbound_latest)
