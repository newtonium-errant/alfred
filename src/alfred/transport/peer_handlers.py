"""Real ``/peer/*`` and ``/canonical/*`` handlers for Stage 3.5.

Replaces the 501 stubs registered by ``_register_peer_stub`` +
``_register_canonical_stub`` in ``server.py``. Installed by flipping
two entries in ``ROUTE_NAMESPACES``.

Handlers:
    POST /peer/send        — forward an inbound peer message (kind =
                              message | query_result | notice). The
                              talker registers a peer-inbox callable
                              at startup.
    POST /peer/query       — SALEM-only canonical queries with field-
                              level permission filter + audit trail.
    POST /peer/handshake   — bootstrap: returns our identity, version,
                              capability list, and known peers. No
                              additional authz beyond bearer auth.
    GET  /canonical/{type}/{name}
                           — SALEM-only canonical record fetch. Peers
                              that don't hold canonical records return
                              404 ``canonical_not_owned``.

Error taxonomy (aligned with the outbound contract):
    401 missing_bearer / invalid_token / client_not_allowed
    403 no_permitted_fields / peer_not_canonical_owner
    404 record_not_found / canonical_not_owned / unknown_peer
    400 schema_error
    501 peer_inbox_not_configured
    502 peer_inbox_error
    503 peer_unavailable

Correlation IDs:
    Every inbound request may carry an ``X-Correlation-Id`` header. If
    present, it's echoed in the response body + any audit entry so
    callers can correlate retries. Absent → we generate one server-side.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import frontmatter
from aiohttp import web

from .canonical import apply_field_permissions
from .canonical_audit import append_audit
from .config import TransportConfig
from .utils import get_logger

log = get_logger(__name__)


# Application-storage key for the peer-inbox callable. The talker
# registers it at startup — a coroutine with shape
# ``(kind, payload, from_peer, correlation_id) -> dict`` that enqueues
# the message for bot delivery + returns an ack dict.
_KEY_PEER_INBOX = "transport.peer_inbox"

# Application-storage key for the vault path (needed for /canonical).
_KEY_VAULT_PATH = "transport.vault_path"


PeerInboxCallable = Callable[..., Awaitable[dict[str, Any]]]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_config(request: web.Request) -> TransportConfig:
    # Same key the outbound handlers use — keeps config access symmetric.
    return request.app["transport.config"]


def _get_vault_path(request: web.Request) -> Path | None:
    raw = request.app.get(_KEY_VAULT_PATH)
    if raw is None:
        return None
    return Path(str(raw))


def _ensure_correlation_id(request: web.Request, body: dict[str, Any] | None = None) -> str:
    """Echo an incoming correlation id if supplied, else mint a fresh one."""
    header = request.headers.get("X-Correlation-Id") or ""
    if header:
        return header[:64]
    if isinstance(body, dict):
        raw = body.get("correlation_id")
        if isinstance(raw, str) and raw:
            return raw[:64]
    return uuid.uuid4().hex[:16]


def _json_error(
    status: int,
    reason: str,
    *,
    correlation_id: str = "",
    **extra: Any,
) -> web.Response:
    """Consistent error shape — ``{reason, correlation_id, ...}``."""
    payload: dict[str, Any] = {"reason": reason}
    if correlation_id:
        payload["correlation_id"] = correlation_id
    payload.update(extra)
    return web.json_response(payload, status=status)


# ---------------------------------------------------------------------------
# /peer/send — inbound relay from another instance
# ---------------------------------------------------------------------------


async def _handle_peer_send(request: web.Request) -> web.StreamResponse:
    """POST /peer/send — accept a message routed from another instance.

    Body:
        {
          "kind":    "message" | "query_result" | "notice",
          "from":    "<peer-name>",          # must match auth peer
          "payload": {...},                   # kind-specific
          "correlation_id": "<optional>",
        }

    The talker (on Salem) registers a peer-inbox callable that picks
    this up and relays to the user via Telegram with the ``[KAL-LE]``
    prefix. If the callable isn't registered, we return 501 — the
    server came up but nobody's listening for peer messages.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        correlation_id = _ensure_correlation_id(request, None)
        return _json_error(
            400, "invalid_json", correlation_id=correlation_id,
        )

    correlation_id = _ensure_correlation_id(request, body)

    kind = body.get("kind")
    from_peer_claim = body.get("from")
    payload = body.get("payload")
    if kind not in {"message", "query_result", "notice"}:
        return _json_error(
            400, "schema_error",
            detail="kind must be message | query_result | notice",
            correlation_id=correlation_id,
        )
    if not isinstance(payload, dict):
        return _json_error(
            400, "schema_error",
            detail="payload must be an object",
            correlation_id=correlation_id,
        )

    # Bearer auth already set ``transport_peer`` on the request. The
    # ``from`` field in the body must match — callers can't spoof a
    # different peer identity even under a valid bearer. The payload
    # body is tamper-evident modulo the auth token.
    auth_peer = request.get("transport_peer", "")
    if from_peer_claim and from_peer_claim != auth_peer:
        log.warning(
            "transport.peer.spoofed_from",
            auth_peer=auth_peer,
            claimed=from_peer_claim,
            correlation_id=correlation_id,
        )
        return _json_error(
            403, "from_mismatch",
            detail="body.from must equal authenticated peer",
            correlation_id=correlation_id,
        )

    inbox: PeerInboxCallable | None = request.app.get(_KEY_PEER_INBOX)
    if inbox is None:
        return _json_error(
            501, "peer_inbox_not_configured", correlation_id=correlation_id,
        )

    try:
        result = await inbox(
            kind=kind,
            payload=payload,
            from_peer=auth_peer,
            correlation_id=correlation_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "transport.peer.inbox_error",
            error=str(exc),
            error_type=exc.__class__.__name__,
            correlation_id=correlation_id,
        )
        return _json_error(
            502, "peer_inbox_error",
            detail=str(exc),
            correlation_id=correlation_id,
        )

    # Success — merge any inbox-provided fields with our correlation
    # id + status. The inbox callable may return message ids, transcript
    # handles, etc.
    response: dict[str, Any] = {
        "status": "accepted",
        "correlation_id": correlation_id,
    }
    if isinstance(result, dict):
        response.update(result)
    return web.json_response(response)


# ---------------------------------------------------------------------------
# /peer/query — canonical query with permission filter
# ---------------------------------------------------------------------------


async def _handle_peer_query(request: web.Request) -> web.StreamResponse:
    """POST /peer/query — SALEM-only canonical query.

    Body:
        {
          "record_type": "person",
          "name":        "Andrew Newton",
          "fields":      ["name", "email"],     # requested
          "filter":      {...optional extra...}
        }

    Same permission filter as ``GET /canonical/<type>/<name>``; the
    difference is that /peer/query is the pull endpoint a peer hits
    directly to retrieve canonical data, whereas /canonical/... is the
    endpoint a peer can read without even knowing the URL shape in
    advance (browser-style).
    """
    config = _get_config(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        correlation_id = _ensure_correlation_id(request, None)
        return _json_error(400, "invalid_json", correlation_id=correlation_id)

    correlation_id = _ensure_correlation_id(request, body)

    if not config.canonical.owner:
        return _json_error(
            404, "canonical_not_owned",
            detail="this instance does not host canonical records",
            correlation_id=correlation_id,
        )

    record_type = body.get("record_type")
    name = body.get("name")
    requested_fields = body.get("fields") or []
    if not isinstance(record_type, str) or not isinstance(name, str):
        return _json_error(
            400, "schema_error",
            detail="record_type and name must be strings",
            correlation_id=correlation_id,
        )
    if not isinstance(requested_fields, list):
        return _json_error(
            400, "schema_error",
            detail="fields must be a list of strings",
            correlation_id=correlation_id,
        )

    peer = request.get("transport_peer", "")
    return await _serve_canonical(
        request,
        peer=peer,
        record_type=record_type,
        name=name,
        requested=requested_fields,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# /peer/handshake
# ---------------------------------------------------------------------------


# Bumped whenever the peer protocol gains or changes a required field.
# Stage 3.5 first release = 1.
_PEER_PROTOCOL_VERSION = 1


# Capabilities advertised by this build. Peers can check for "bash_exec"
# before routing a coding request to KAL-LE, etc.
_DEFAULT_CAPABILITIES: tuple[str, ...] = (
    "outbound_send",
    "peer_message",
    "peer_query",
)


def _compute_capabilities(config: TransportConfig) -> list[str]:
    caps = list(_DEFAULT_CAPABILITIES)
    if config.canonical.owner:
        caps.append("canonical_owner")
    return caps


async def _handle_peer_handshake(request: web.Request) -> web.StreamResponse:
    """POST /peer/handshake — advertise identity + capabilities.

    Body (optional):
        {"from": "<peer-name>", "protocol_version": <int>}

    Response:
        {
          "instance":          "<this-instance-name>",
          "protocol_version":  <int>,
          "capabilities":      [...],
          "peers":             [{"name": "kal-le", "base_url": "..."}, ...],
          "correlation_id":    "<echo-or-fresh>"
        }

    No scope enforcement beyond bearer auth — a peer that can auth
    already knows enough about us to ask for the full handshake. The
    ``peers`` list is clipped to names + base URLs; tokens never leave
    this process.
    """
    config = _get_config(request)
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        # Empty body is valid.
        body = {}
    correlation_id = _ensure_correlation_id(request, body)

    instance_name = request.app.get("transport.instance_name", "")
    alias = request.app.get("transport.instance_alias", "")

    peers_advertised = [
        {"name": peer_name, "base_url": entry.base_url}
        for peer_name, entry in config.peers.items()
        if entry.base_url
    ]

    return web.json_response({
        "instance": instance_name,
        "alias": alias,
        "protocol_version": _PEER_PROTOCOL_VERSION,
        "capabilities": _compute_capabilities(config),
        "peers": peers_advertised,
        "correlation_id": correlation_id,
    })


# ---------------------------------------------------------------------------
# /canonical/{type}/{name}
# ---------------------------------------------------------------------------


async def _handle_canonical_get(request: web.Request) -> web.StreamResponse:
    """GET /canonical/<type>/<name> — peer fetch of a canonical record.

    * 404 ``canonical_not_owned`` when ``transport.canonical.owner: false``
      (KAL-LE, STAY-C, etc.). SALEM is the only instance that holds
      canonical records in v1.
    * 403 ``no_permitted_fields`` when the peer's allowlist for this
      record type is empty.
    * 404 ``record_not_found`` when the record file doesn't exist.
    * 200 with filtered frontmatter otherwise. Bodies are never
      returned — the response body contains only frontmatter.

    Every outcome (including 403/404) appends an entry to
    ``canonical_audit.jsonl`` so the audit trail is complete.
    """
    record_type = request.match_info.get("type", "")
    name = request.match_info.get("name", "")
    correlation_id = _ensure_correlation_id(request, None)
    peer = request.get("transport_peer", "")

    return await _serve_canonical(
        request,
        peer=peer,
        record_type=record_type,
        name=name,
        requested=[],  # GET form requests all permitted fields
        correlation_id=correlation_id,
    )


async def _serve_canonical(
    request: web.Request,
    *,
    peer: str,
    record_type: str,
    name: str,
    requested: list[str],
    correlation_id: str,
) -> web.StreamResponse:
    """Shared impl for /peer/query + GET /canonical/<type>/<name>.

    Applies the permission filter, audits every outcome, never exposes
    the record body.
    """
    config = _get_config(request)
    audit_path = config.canonical.audit_log_path

    if not config.canonical.owner:
        append_audit(
            audit_path,
            peer=peer, record_type=record_type, name=name,
            requested=requested, granted=[], denied=[],
            correlation_id=correlation_id,
        )
        return _json_error(
            404, "canonical_not_owned",
            correlation_id=correlation_id,
        )

    # Look up the peer's allowlist for this record type.
    perms = config.canonical.peer_permissions
    peer_rules = perms.get(peer, {})
    type_rules = peer_rules.get(record_type)
    if type_rules is None or not getattr(type_rules, "fields", []):
        append_audit(
            audit_path,
            peer=peer, record_type=record_type, name=name,
            requested=requested, granted=[], denied=[],
            correlation_id=correlation_id,
        )
        return _json_error(
            403, "no_permitted_fields",
            detail=f"peer '{peer}' has no permitted fields for type '{record_type}'",
            correlation_id=correlation_id,
        )

    # Load the record from the vault.
    vault_path = _get_vault_path(request)
    if vault_path is None:
        # No vault registered — the talker never wired one up. Treat
        # as a server-side not-found but record it.
        append_audit(
            audit_path,
            peer=peer, record_type=record_type, name=name,
            requested=requested, granted=[], denied=[],
            correlation_id=correlation_id,
        )
        return _json_error(
            404, "record_not_found",
            detail="vault not configured",
            correlation_id=correlation_id,
        )

    record_path = vault_path / record_type / f"{name}.md"
    if not record_path.exists():
        append_audit(
            audit_path,
            peer=peer, record_type=record_type, name=name,
            requested=requested, granted=[], denied=[],
            correlation_id=correlation_id,
        )
        return _json_error(
            404, "record_not_found",
            correlation_id=correlation_id,
        )

    try:
        post = frontmatter.load(str(record_path))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "transport.canonical.parse_failed",
            path=str(record_path),
            error=str(exc),
        )
        return _json_error(
            500, "record_parse_failed",
            detail=str(exc),
            correlation_id=correlation_id,
        )

    filtered, granted, denied = apply_field_permissions(
        peer=peer,
        record_type=record_type,
        frontmatter=dict(post.metadata or {}),
        perms=perms,
    )

    # If the peer asked for specific fields, honour the intersection —
    # but we can't add fields the permission filter already withheld.
    if requested:
        requested_set = {f for f in requested if isinstance(f, str)}
        filtered = {k: v for k, v in filtered.items() if k in requested_set
                    or any(k == rf.split(".", 1)[0] for rf in requested_set)}
        granted = [g for g in granted if g in requested_set
                   or any(g.split(".", 1)[0] == rf.split(".", 1)[0] for rf in requested_set)]

    append_audit(
        audit_path,
        peer=peer, record_type=record_type, name=name,
        requested=requested or granted, granted=granted, denied=denied,
        correlation_id=correlation_id,
    )

    return web.json_response({
        "type": record_type,
        "name": name,
        "frontmatter": filtered,
        "granted": granted,
        "correlation_id": correlation_id,
    })


# ---------------------------------------------------------------------------
# Registrars — consumed by ROUTE_NAMESPACES in server.py
# ---------------------------------------------------------------------------


def register_peer_routes(app: web.Application) -> None:
    """Swap in real /peer/* handlers (replaces _register_peer_stub)."""
    app.router.add_post("/peer/send", _handle_peer_send)
    app.router.add_post("/peer/query", _handle_peer_query)
    app.router.add_post("/peer/handshake", _handle_peer_handshake)


def register_canonical_routes(app: web.Application) -> None:
    """Swap in real /canonical/* handlers (replaces _register_canonical_stub)."""
    app.router.add_get("/canonical/{type}/{name}", _handle_canonical_get)


def register_peer_inbox(
    app: web.Application,
    callable_: PeerInboxCallable,
) -> None:
    """Wire a peer-inbox callable onto an already-built app.

    Mirrors :func:`register_send_callable` — the talker registers this
    at startup. The callable shape is
    ``(kind, payload, from_peer, correlation_id) -> awaitable[dict]``.
    """
    app[_KEY_PEER_INBOX] = callable_


def register_vault_path(app: web.Application, vault_path: Path) -> None:
    """Tell the canonical handler where the vault lives."""
    app[_KEY_VAULT_PATH] = str(vault_path)


def register_instance_identity(
    app: web.Application,
    *,
    name: str,
    alias: str = "",
) -> None:
    """Stash the instance identity for /peer/handshake responses."""
    app["transport.instance_name"] = name
    app["transport.instance_alias"] = alias
