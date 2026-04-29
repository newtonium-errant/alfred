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
    POST /peer/brief_digest
                           — accept a brief-section digest from a
                              named peer and stash it in the principal's
                              vault for the brief renderer to pick up.
                              V.E.R.A. content arc: specialist instance
                              produces its own slide, principal renders
                              the pushed slide alongside its own
                              sections.
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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import yaml
from aiohttp import web

from .canonical import apply_field_permissions
from .canonical_audit import append_audit
from .canonical_proposals import (
    Proposal,
    append_proposal,
    find_proposal,
    _now_iso,
)
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
# /canonical/{type}/propose — subordinate proposes a canonical record
# ---------------------------------------------------------------------------
#
# Design ratified in ``project_kalle_propose_person.md`` (2026-04-23).
# When KAL-LE (or STAY-C / V.E.R.A. later) queries SALEM's canonical
# person and hits 404 ``record_not_found``, it escalates to this route
# rather than silently fall back to a bare name string. SALEM queues
# the proposal; Andrew confirms or rejects in the Daily Sync.
#
# Error taxonomy:
#   202 Accepted — proposal queued; {"status": "pending", "correlation_id"}.
#   409 already_exists — race: the record was created between the
#        proposer's 404 read and its propose call. Returns the canonical
#        path so the proposer can re-fetch via GET /canonical.
#   403 not_allowed — reserved for a future per-peer allowlist. All
#        authenticated peers are currently allowed; the 403 path is
#        defined here so operators can tighten later without schema
#        churn (TODO: per-peer allowlist).
#   400 schema_error / invalid_json — body validation failures.
#   404 canonical_not_owned — this instance doesn't hold canonical
#        records (KAL-LE, STAY-C). Same shape as the GET handler for
#        consistency.


# Record types this route accepts. Person is the Q1 use case; we leave
# org/location in the validator because extending later is a one-line
# config change, but the handler still stops any other value short.
_PROPOSE_ALLOWED_TYPES = {"person", "org", "location"}


async def _handle_canonical_propose(request: web.Request) -> web.StreamResponse:
    """POST /canonical/<type>/propose — accept a creation proposal.

    Body::

        {
          "name": "Alex Newton",
          "proposed_fields": {...},
          "source": "KAL-LE observed in commit X during session Y",
          "correlation_id": "kal-le-propose-person-1234"
        }

    See module-level docstring for the full error taxonomy.
    """
    record_type = request.match_info.get("type", "")
    correlation_id = _ensure_correlation_id(request, None)
    peer = request.get("transport_peer", "")
    config = _get_config(request)

    # 404 canonical_not_owned mirrors the GET handler's contract so a
    # subordinate's proposer can treat "not a canonical-owner" the same
    # way on GET and POST.
    if not config.canonical.owner:
        return _json_error(
            404, "canonical_not_owned",
            detail="this instance does not host canonical records",
            correlation_id=correlation_id,
        )

    if record_type not in _PROPOSE_ALLOWED_TYPES:
        return _json_error(
            400, "schema_error",
            detail=(
                f"record_type '{record_type}' is not proposable; "
                f"allowed: {sorted(_PROPOSE_ALLOWED_TYPES)}"
            ),
            correlation_id=correlation_id,
        )

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _json_error(400, "invalid_json", correlation_id=correlation_id)
    if not isinstance(body, dict):
        return _json_error(
            400, "schema_error",
            detail="body must be a JSON object",
            correlation_id=correlation_id,
        )

    # Re-derive correlation id with the body in hand so a caller-supplied
    # ``correlation_id`` field is honoured over a fresh mint.
    correlation_id = _ensure_correlation_id(request, body)

    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return _json_error(
            400, "schema_error",
            detail="name must be a non-empty string",
            correlation_id=correlation_id,
        )
    name = name.strip()

    proposed_fields_raw = body.get("proposed_fields") or {}
    if not isinstance(proposed_fields_raw, dict):
        return _json_error(
            400, "schema_error",
            detail="proposed_fields must be an object",
            correlation_id=correlation_id,
        )
    source = body.get("source") or ""
    if not isinstance(source, str):
        return _json_error(
            400, "schema_error",
            detail="source must be a string",
            correlation_id=correlation_id,
        )

    # TODO: per-peer allowlist. All authenticated peers are currently
    # allowed to propose. When we grow a ``canonical.propose_allowlist``
    # config block, this is where the 403 ``not_allowed`` would land.
    # Leaving the error shape defined (in the module docstring) so the
    # proposer-side client can handle it without a redeploy.

    # 409 early-return: the record was created between the proposer's
    # 404 read and this call. Walk the vault the same way the GET
    # handler does so the race check matches what a fresh GET would
    # see.
    vault_path = _get_vault_path(request)
    if vault_path is not None:
        record_path = vault_path / record_type / f"{name}.md"
        if record_path.exists():
            rel = f"{record_type}/{name}.md"
            log.info(
                "transport.canonical.propose_409_already_exists",
                peer=peer,
                record_type=record_type,
                name=name,
                correlation_id=correlation_id,
                path=rel,
            )
            return web.json_response(
                {
                    "status": "exists",
                    "path": rel,
                    "correlation_id": correlation_id,
                },
                status=409,
            )

    # Idempotency: if the same correlation_id was already queued, don't
    # double-write. Return the original 202 shape so a proposer retry
    # after a torn network response is safe.
    queue_path = config.canonical.proposals_path
    existing = find_proposal(queue_path, correlation_id)
    if existing is not None:
        log.info(
            "transport.canonical.propose_idempotent_hit",
            peer=peer,
            correlation_id=correlation_id,
            state=existing.state,
        )
        return web.json_response(
            {
                "status": existing.state,
                "correlation_id": correlation_id,
            },
            status=202,
        )

    proposal = Proposal(
        correlation_id=correlation_id,
        ts=_now_iso(),
        state="pending",
        proposer=peer,
        record_type=record_type,
        name=name,
        proposed_fields=dict(proposed_fields_raw),
        source=source.strip(),
    )
    append_proposal(queue_path, proposal)

    log.info(
        "transport.canonical.propose_queued",
        peer=peer,
        record_type=record_type,
        name=name,
        correlation_id=correlation_id,
        source_len=len(proposal.source),
        fields=sorted(proposal.proposed_fields.keys()),
    )

    return web.json_response(
        {
            "status": "pending",
            "correlation_id": correlation_id,
        },
        status=202,
    )


# ---------------------------------------------------------------------------
# /peer/brief_digest — accept a one-slide brief section from a peer
# ---------------------------------------------------------------------------


# Hard cap on the digest body — protects the vault from runaway peers.
# A normal "one-slide" digest is ~200-400 words; 50 KB is two orders of
# magnitude larger, well beyond any plausible legitimate value.
_MAX_DIGEST_BYTES = 50_000


def _safe_peer_filename_part(value: str) -> str:
    """Strip path-traversal characters from a peer-supplied string.

    The peer name + date go into a vault filename (``Peer Digest {peer}
    {date}.md``). Replace anything that could escape the directory or
    produce a malformed name with ``-``. Belt-and-braces — auth already
    pins the peer to the authenticated identity, but the date string is
    operator-supplied and worth sanitising.
    """
    out_chars: list[str] = []
    for ch in value:
        if ch.isalnum() or ch in {"-", "_", "."}:
            out_chars.append(ch)
        else:
            out_chars.append("-")
    return "".join(out_chars).strip("-") or "unknown"


async def _handle_peer_brief_digest(request: web.Request) -> web.StreamResponse:
    """POST /peer/brief_digest — accept a brief section from a peer.

    Body:
        {
          "peer":             "kal-le",
          "date":             "2026-04-23",
          "digest_markdown":  "...one-slide markdown...",
          "correlation_id":   "<optional>"
        }

    On success, writes the digest as a vault record at
    ``<vault_path>/run/Peer Digest {peer} {date}.md`` with frontmatter
    capturing source + receipt metadata, and returns 202 Accepted with
    the relative vault path.

    Auth invariants (enforced by the middleware before we get here):
      - Bearer token must match an entry in ``transport.auth.tokens``.
      - ``X-Alfred-Client`` must be in that entry's ``allowed_clients``.
      - The body's ``peer`` field must match the authenticated peer
        (same anti-spoof rule as ``/peer/send``).
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        correlation_id = _ensure_correlation_id(request, None)
        return _json_error(400, "invalid_json", correlation_id=correlation_id)

    correlation_id = _ensure_correlation_id(request, body)

    peer_claim = body.get("peer")
    date_str = body.get("date")
    digest_md = body.get("digest_markdown")

    if not isinstance(peer_claim, str) or not peer_claim:
        return _json_error(
            400, "schema_error",
            detail="peer must be a non-empty string",
            correlation_id=correlation_id,
        )
    if not isinstance(date_str, str) or not date_str:
        return _json_error(
            400, "schema_error",
            detail="date must be a non-empty ISO date string",
            correlation_id=correlation_id,
        )
    if not isinstance(digest_md, str) or not digest_md:
        return _json_error(
            400, "schema_error",
            detail="digest_markdown must be a non-empty string",
            correlation_id=correlation_id,
        )

    encoded_len = len(digest_md.encode("utf-8"))
    if encoded_len > _MAX_DIGEST_BYTES:
        return _json_error(
            400, "schema_error",
            detail=(
                f"digest_markdown exceeds {_MAX_DIGEST_BYTES} byte cap "
                f"(got {encoded_len})"
            ),
            correlation_id=correlation_id,
        )

    auth_peer = request.get("transport_peer", "")
    if peer_claim != auth_peer:
        log.warning(
            "transport.peer.brief_digest_spoofed",
            auth_peer=auth_peer,
            claimed=peer_claim,
            correlation_id=correlation_id,
        )
        return _json_error(
            403, "from_mismatch",
            detail="body.peer must equal authenticated peer",
            correlation_id=correlation_id,
        )

    vault_path = _get_vault_path(request)
    if vault_path is None:
        return _json_error(
            500, "vault_not_configured",
            detail="vault path not registered on transport server",
            correlation_id=correlation_id,
        )

    safe_peer = _safe_peer_filename_part(peer_claim)
    safe_date = _safe_peer_filename_part(date_str)
    received_at = datetime.now(timezone.utc).isoformat()

    # Frontmatter — the brief renderer keys off ``type: run`` + ``source:
    # peer`` + ``peer`` + ``created`` to find today's digests.
    frontmatter = {
        "type": "run",
        "name": f"Peer Digest {safe_peer} {safe_date}",
        "source": "peer",
        "peer": peer_claim,
        "received_at": received_at,
        "created": date_str,
        "correlation_id": correlation_id,
        "content_length": encoded_len,
        "tags": ["peer-digest", safe_peer],
    }

    fm_str = yaml.dump(
        frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    file_text = f"---\n{fm_str}---\n\n{digest_md.rstrip()}\n"

    rel_path = f"run/Peer Digest {safe_peer} {safe_date}.md"
    file_path = vault_path / "run" / f"Peer Digest {safe_peer} {safe_date}.md"
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(file_text, encoding="utf-8")
    except OSError as exc:
        log.warning(
            "transport.peer.brief_digest_write_failed",
            peer=peer_claim,
            path=str(file_path),
            error=str(exc),
            correlation_id=correlation_id,
        )
        return _json_error(
            500, "write_failed",
            detail=str(exc),
            correlation_id=correlation_id,
        )

    log.info(
        "transport.peer.brief_digest_received",
        peer=peer_claim,
        date=date_str,
        path=rel_path,
        bytes=encoded_len,
        correlation_id=correlation_id,
    )

    return web.json_response(
        {
            "status": "accepted",
            "path": rel_path,
            "correlation_id": correlation_id,
        },
        status=202,
    )


# ---------------------------------------------------------------------------
# /peer/pending_items_push — peer → Salem (Pending Items Queue Phase 1)
# ---------------------------------------------------------------------------
#
# Mirror of /peer/brief_digest. Each peer flushes its local
# pending-items JSONL to Salem; Salem appends to its aggregate file
# so the Daily Sync section provider has a single source. Idempotent
# by ``item.id`` — a re-push of the same item is a no-op.
#
# Storage: ``transport.pending_items.aggregate_path`` app key (set by
# the talker daemon at startup). Defaults to
# ``./data/pending_items_aggregate.jsonl`` when unset so unit tests
# don't need to wire up a separate file.

# Hard cap on items per push — protects Salem from a runaway peer.
# A normal flush is ~1-3 items; 100 is two orders of magnitude
# larger, well beyond any plausible legitimate value.
_MAX_PENDING_ITEMS_PER_PUSH = 100

_KEY_PENDING_AGGREGATE_PATH = "transport.pending_items.aggregate_path"


def _get_pending_aggregate_path(request: web.Request) -> Path:
    """Return the aggregate JSONL path. Defaults when unset."""
    raw = request.app.get(_KEY_PENDING_AGGREGATE_PATH)
    if raw:
        return Path(str(raw))
    return Path("./data/pending_items_aggregate.jsonl")


def _existing_aggregate_ids(path: Path) -> set[str]:
    """Return the set of item ids already present in the aggregate file."""
    if not path.exists():
        return set()
    seen: set[str] = set()
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    import json as _json
                    data = _json.loads(line)
                except ValueError:
                    continue
                if not isinstance(data, dict):
                    continue
                item_id = data.get("id")
                if isinstance(item_id, str) and item_id:
                    seen.add(item_id)
    except OSError:
        pass
    return seen


def _append_aggregate_items(
    path: Path,
    items: list[dict[str, Any]],
) -> tuple[int, list[dict[str, Any]]]:
    """Append items to the aggregate JSONL, skipping duplicates by id.

    Returns ``(received_count, errors)``. ``received_count`` includes
    duplicates that were idempotently skipped — the caller advertises
    the count of items the server "received" (not "stored").
    """
    import json as _json

    received = 0
    errors: list[dict[str, Any]] = []
    existing = _existing_aggregate_ids(path)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        for item in items:
            errors.append({
                "id": item.get("id"),
                "reason": "mkdir_failed",
                "detail": str(exc),
            })
        return 0, errors

    try:
        with open(path, "a", encoding="utf-8") as f:
            for item in items:
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    errors.append({
                        "id": None,
                        "reason": "missing_id",
                    })
                    continue
                if item_id in existing:
                    received += 1  # idempotent — count as received
                    continue
                f.write(_json.dumps(item, default=str) + "\n")
                existing.add(item_id)
                received += 1
    except OSError as exc:
        errors.append({
            "id": None,
            "reason": "write_failed",
            "detail": str(exc),
        })

    return received, errors


async def _handle_peer_pending_items_push(
    request: web.Request,
) -> web.StreamResponse:
    """POST /peer/pending_items_push — accept a peer's queue flush.

    Body::

        {
          "from_instance": "hypatia",
          "items": [<queue entry>, ...],
          "correlation_id": "<optional>"
        }

    Salem appends new items to its aggregate JSONL (idempotent by
    item.id) and returns ``{"received": <count>, "errors": [...]}``.

    Auth invariants (enforced by the middleware before we get here):
      * Bearer token must match an entry in ``transport.auth.tokens``.
      * ``X-Alfred-Client`` must be in that entry's ``allowed_clients``.
      * The body's ``from_instance`` must match the authenticated peer.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        correlation_id = _ensure_correlation_id(request, None)
        return _json_error(400, "invalid_json", correlation_id=correlation_id)

    correlation_id = _ensure_correlation_id(request, body)

    from_instance = body.get("from_instance")
    items = body.get("items")
    if not isinstance(from_instance, str) or not from_instance:
        return _json_error(
            400, "schema_error",
            detail="from_instance must be a non-empty string",
            correlation_id=correlation_id,
        )
    if not isinstance(items, list):
        return _json_error(
            400, "schema_error",
            detail="items must be a list",
            correlation_id=correlation_id,
        )
    if len(items) > _MAX_PENDING_ITEMS_PER_PUSH:
        return _json_error(
            400, "schema_error",
            detail=(
                f"items exceeds {_MAX_PENDING_ITEMS_PER_PUSH} per-push cap "
                f"(got {len(items)})"
            ),
            correlation_id=correlation_id,
        )

    auth_peer = request.get("transport_peer", "")
    if from_instance != auth_peer:
        log.warning(
            "transport.peer.pending_items_push_spoofed",
            auth_peer=auth_peer,
            claimed=from_instance,
            correlation_id=correlation_id,
        )
        return _json_error(
            403, "from_mismatch",
            detail="body.from_instance must equal authenticated peer",
            correlation_id=correlation_id,
        )

    # Stamp ``created_by_instance`` from the authenticated peer so a
    # buggy peer can't pretend an item came from a different instance.
    # Items already carrying the same value pass through; mismatches
    # get rewritten with a debug log so the audit trail is honest.
    normalized_items: list[dict[str, Any]] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        item_copy = dict(raw)
        original = str(item_copy.get("created_by_instance") or "")
        if original and original != from_instance:
            log.info(
                "transport.peer.pending_items_push_normalized",
                claimed=original,
                normalized=from_instance,
                item_id=item_copy.get("id"),
            )
        item_copy["created_by_instance"] = from_instance
        normalized_items.append(item_copy)

    aggregate_path = _get_pending_aggregate_path(request)
    received, errors = _append_aggregate_items(aggregate_path, normalized_items)

    log.info(
        "transport.peer.pending_items_push_received",
        from_instance=from_instance,
        received=received,
        sent=len(items),
        errors=len(errors),
        path=str(aggregate_path),
        correlation_id=correlation_id,
    )

    return web.json_response({
        "received": received,
        "errors": errors,
        "correlation_id": correlation_id,
    })


# ---------------------------------------------------------------------------
# /peer/pending_items_resolve — Salem → peer (NEW direction)
# ---------------------------------------------------------------------------
#
# First Salem→peer consumer on the transport substrate. The peer
# (originating instance) receives a resolution dispatch from Salem,
# looks up the item in its local JSONL queue, runs the action plan,
# and acknowledges. Same auth scheme as every other peer route — the
# peer identity in ``X-Alfred-Client`` must match an
# ``auth.tokens.<peer>`` entry on this side.
#
# The per-instance handler depends on the local queue path + vault
# path + telegram user_id, so the talker daemon registers a
# resolver-callable at startup the same way the peer-inbox is wired.
# When the callable isn't registered, return 501 so the operator
# sees the gap rather than a silent no-op.

_KEY_PENDING_RESOLVE_CALLABLE = "transport.pending_items.resolve_callable"

PendingResolveCallable = Callable[..., Awaitable[dict[str, Any]]]


def register_pending_items_resolve_callable(
    app: web.Application,
    callable_: PendingResolveCallable,
) -> None:
    """Wire a pending-items resolver callable onto an already-built app.

    Mirrors :func:`register_peer_inbox`. The callable shape is
    ``(item_id, resolution, resolved_at, correlation_id)
        -> awaitable[dict]``. The talker daemon registers it at
    startup as a closure over the running PendingItemsConfig + the
    vault path + the configured telegram user.
    """
    app[_KEY_PENDING_RESOLVE_CALLABLE] = callable_


def register_pending_items_aggregate_path(
    app: web.Application,
    path: str | Path,
) -> None:
    """Tell the inbound push handler where to aggregate items.

    Salem registers a path under ``./data/`` at startup so peer
    pushes don't fall back to the localhost default.
    """
    app[_KEY_PENDING_AGGREGATE_PATH] = str(path)


async def _handle_peer_pending_items_resolve(
    request: web.Request,
) -> web.StreamResponse:
    """POST /peer/pending_items_resolve — accept a Salem resolution dispatch.

    Body::

        {
          "item_id": "<uuid>",
          "resolution": "<resolution_option_id>",
          "resolved_at": "<iso8601, optional>",
          "correlation_id": "<optional>"
        }

    Response::

        {
          "executed": <bool>,
          "summary": "<user-facing text>",
          "error": "<str or null>",
          "correlation_id": "<echo>"
        }

    501 ``pending_resolver_not_configured`` when the resolver callable
    isn't registered (e.g. the peer doesn't have a ``pending_items``
    block enabled). 401/403 from the auth middleware as usual.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        correlation_id = _ensure_correlation_id(request, None)
        return _json_error(400, "invalid_json", correlation_id=correlation_id)

    correlation_id = _ensure_correlation_id(request, body)

    item_id = body.get("item_id")
    resolution = body.get("resolution")
    resolved_at = body.get("resolved_at")

    if not isinstance(item_id, str) or not item_id:
        return _json_error(
            400, "schema_error",
            detail="item_id must be a non-empty string",
            correlation_id=correlation_id,
        )
    if not isinstance(resolution, str) or not resolution:
        return _json_error(
            400, "schema_error",
            detail="resolution must be a non-empty string",
            correlation_id=correlation_id,
        )
    if resolved_at is not None and not isinstance(resolved_at, str):
        return _json_error(
            400, "schema_error",
            detail="resolved_at must be an ISO 8601 string when present",
            correlation_id=correlation_id,
        )

    resolver: PendingResolveCallable | None = request.app.get(
        _KEY_PENDING_RESOLVE_CALLABLE,
    )
    if resolver is None:
        return _json_error(
            501, "pending_resolver_not_configured",
            detail=(
                "this instance has no Pending Items resolver registered "
                "(likely no pending_items config block)"
            ),
            correlation_id=correlation_id,
        )

    try:
        result = await resolver(
            item_id=item_id,
            resolution=resolution,
            resolved_at=resolved_at,
            correlation_id=correlation_id,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "transport.peer.pending_items_resolver_error",
            item_id=item_id,
            resolution=resolution,
            error=str(exc),
            error_type=exc.__class__.__name__,
            correlation_id=correlation_id,
        )
        return _json_error(
            502, "pending_resolver_error",
            detail=str(exc),
            correlation_id=correlation_id,
        )

    response: dict[str, Any] = {
        "executed": bool(result.get("executed", False)) if isinstance(result, dict) else False,
        "summary": str(result.get("summary", "")) if isinstance(result, dict) else "",
        "error": (
            result.get("error")
            if isinstance(result, dict) and result.get("error") is not None
            else None
        ),
        "correlation_id": correlation_id,
    }
    log.info(
        "transport.peer.pending_items_resolved",
        item_id=item_id,
        resolution=resolution,
        executed=response["executed"],
        correlation_id=correlation_id,
    )
    return web.json_response(response)


# ---------------------------------------------------------------------------
# Registrars — consumed by ROUTE_NAMESPACES in server.py
# ---------------------------------------------------------------------------


def register_peer_routes(app: web.Application) -> None:
    """Swap in real /peer/* handlers (replaces _register_peer_stub)."""
    app.router.add_post("/peer/send", _handle_peer_send)
    app.router.add_post("/peer/query", _handle_peer_query)
    app.router.add_post("/peer/handshake", _handle_peer_handshake)
    app.router.add_post("/peer/brief_digest", _handle_peer_brief_digest)
    app.router.add_post("/peer/pending_items_push", _handle_peer_pending_items_push)
    app.router.add_post("/peer/pending_items_resolve", _handle_peer_pending_items_resolve)


def register_canonical_routes(app: web.Application) -> None:
    """Swap in real /canonical/* handlers (replaces _register_canonical_stub).

    The POST propose route and the GET fetch route don't share a method,
    so aiohttp routes them unambiguously by (method, path) even though
    the URL shapes overlap (``/canonical/person/propose`` is a valid
    ``/canonical/{type}/{name}`` by GET semantics, but we never serve
    a GET for ``name=propose`` — the literal name is reserved).
    """
    app.router.add_post("/canonical/{type}/propose", _handle_canonical_propose)
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
