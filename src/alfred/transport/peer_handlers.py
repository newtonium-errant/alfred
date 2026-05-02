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
    POST /canonical/{type}/propose
                           — queued shape for ``person`` / ``org`` /
                              ``location``. Salem stores the proposal
                              in the JSONL queue; Andrew confirms via
                              the Daily Sync. Async by design.
    POST /canonical/event/propose-create
                           — synchronous create with conflict-check.
                              The proposing instance is mid-conversation
                              with Andrew and needs immediate response;
                              Salem either creates the record (and
                              returns the path) or returns the
                              conflicting events. NO operator approval
                              gate — Andrew is right there talking to
                              the agent.

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

import re
import uuid
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
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

# Application-storage keys for the GCal integration (Phase A+).
# ``_KEY_GCAL_CLIENT`` holds a constructed
# :class:`alfred.integrations.gcal.GCalClient` — ``None`` / unset means
# GCal is disabled for this instance and conflict-check / sync skip the
# GCal code paths entirely.
# ``_KEY_GCAL_CONFIG`` holds the typed
# :class:`alfred.integrations.gcal_config.GCalConfig` so handlers can
# read the calendar IDs without re-loading config.yaml.
# ``_KEY_GCAL_INTENDED_ON`` is a sentinel: True iff ``gcal.enabled``
# was true in config but client construction failed at daemon startup.
# Distinguishes "gcal intentionally off, skip silently" from "gcal
# meant to be on but setup broke, surface to operator". See
# :func:`_scan_gcal_conflicts` / :func:`_sync_event_to_gcal`.
_KEY_GCAL_CLIENT = "transport.gcal_client"
_KEY_GCAL_CONFIG = "transport.gcal_config"
_KEY_GCAL_INTENDED_ON = "transport.gcal_intended_on"


# ---------------------------------------------------------------------------
# Conflict-source enum (Phase A+)
# ---------------------------------------------------------------------------
#
# The ``source`` field on every conflict-list entry tells the proposing
# instance + Andrew where the conflict came from. Codifying the string
# values here gives consumers (peer_handlers, gcal helpers, future
# downstream renderers) a single source of truth — typo-divergence as
# new sources land (e.g. V.E.R.A. RRTS calendar, STAY-C client calendar)
# can't silently slip in.
#
# Plain class with class-level string constants (not Enum) so the
# values are still raw strings at the JSON boundary — no
# ``.value`` boilerplate at every emit site, no JSON-encoder coupling.


class ConflictSource:
    """String constants for the ``source`` field on conflict entries.

    Each entry in the ``conflicts`` list returned by
    ``/canonical/event/propose-create`` carries one of these values so
    the proposing instance can render appropriately ("you have a
    primary-calendar meeting" vs "you have a vault event"). Stable
    contract — additions (e.g. ``GCAL_RRTS`` when V.E.R.A. ships)
    stay backward-compatible because consumers ignore unknown values
    rather than crash.
    """

    # The proposing instance's local vault has an overlapping
    # ``event/`` record. Source for :func:`_scan_event_conflicts`.
    VAULT = "vault"

    # The instance's writable GCal target (Salem's "Alfred" calendar).
    # Source for the alfred-calendar leg of :func:`_scan_gcal_conflicts`.
    GCAL_ALFRED = "gcal_alfred"

    # The user's primary GCal (read-only by application policy).
    # Source for the primary-calendar leg of :func:`_scan_gcal_conflicts`.
    GCAL_PRIMARY = "gcal_primary"


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
# /canonical/event/propose-create — synchronous create with conflict-check
# ---------------------------------------------------------------------------
#
# Architecturally distinct from the queued ``/canonical/{type}/propose``
# flow. When a proposing instance (Hypatia, KAL-LE) is mid-conversation
# with Andrew and asks to schedule something, Andrew needs the response
# inline — he's right there talking to the agent. There's no operator-
# approval gate; the proposing instance is the operator's surface for
# this turn.
#
# Salem either:
#   * creates the event record and returns ``{status: created, path}``,
#     OR
#   * detects a time conflict against existing vault events and returns
#     ``{status: conflict, conflicts: [...]}`` without creating.
#
# The proposing instance surfaces conflicts inline ("Salem flagged a
# conflict — you have an X at 14:00. Reschedule, or override?"). v1
# does NOT support override; if it surfaces in usage we add an
# ``override_conflict`` flag in v1.1.
#
# Conflict-check semantics: any time-overlap counts. ``[start_a, end_a]``
# overlaps ``[start_b, end_b]`` iff ``start_a < end_b AND end_a >
# start_b`` (half-open ranges, exclusive end). Same-instant boundaries
# (event ends at 14:00, next starts at 14:00) do NOT count as conflicts
# — that's adjacency, not overlap. v1 is vault-only; Phase A+ extends
# to GCal.

# Maximum size of the proposed-event title / summary fields. Belt-and-
# braces — protects the vault from a runaway peer writing megabyte
# strings.
_EVENT_TITLE_MAX_LEN = 240
_EVENT_SUMMARY_MAX_LEN = 4_000
_EVENT_CONTEXT_MAX_LEN = 2_000


def _parse_iso_datetime(value: Any) -> datetime | None:
    """Parse ``value`` into a tz-aware UTC datetime, or return ``None``.

    Accepts:
      * datetime instances (returned as-is, naive datetimes assumed UTC).
      * date instances (converted to midnight UTC start-of-day).
      * ISO 8601 strings (``2026-05-04T14:00:00-03:00``,
        ``2026-05-04T18:00:00Z``, ``2026-05-04`` for date-only).

    Returns ``None`` for unparseable values; the caller's contract is
    "treat as no time" (event with no time can't conflict with anything,
    only the propose path validates that times are present).

    Naive datetimes (no timezone) are interpreted as UTC. The propose
    path requires both start + end with timezone offsets so this is
    primarily for vault-side records that may carry a bare
    ``date: 2026-05-04`` field.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # Z suffix → UTC.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(s)
        except ValueError:
            # Date-only ``2026-05-04`` — fall back to date parsing.
            try:
                d = date.fromisoformat(s[:10])
            except ValueError:
                return None
            return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _event_window(fm: dict[str, Any]) -> tuple[datetime, datetime] | None:
    """Extract the ``[start, end]`` window for an existing event record.

    Field resolution order:
      * ``start`` + ``end`` (preferred — the propose-create flow writes
        both).
      * ``start`` alone (treat ``end`` as ``start + 1h`` so single-time
        events still produce a meaningful window for overlap detection).
      * ``date`` alone — symmetric expansion around UTC midnight to
        cover any plausible local-tz interpretation of "all day on date
        d". A date-only record really means "the whole local day" but
        we don't know which timezone the author meant. Expanding to
        ``[d-1 day @ 12:00 UTC, d+1 day @ 12:00 UTC)`` brackets every
        timezone offset between roughly UTC-12 and UTC+12, so a
        proposed Halifax-evening (UTC-3) event no longer slips past a
        date-only all-day record. The cost is over-conflicting events
        that are legitimately on adjacent days but in different
        timezones — rare in practice for a single-tz vault.

        v2: read ``vault.timezone`` from config and expand the date in
        that timezone explicitly. That removes the over-conflict
        possibility but pulls in config-loading complexity we don't
        need here yet.

    Returns ``None`` when no time fields are parseable. The caller
    skips records with no extractable window.
    """
    from datetime import time, timedelta

    start = _parse_iso_datetime(fm.get("start"))
    end = _parse_iso_datetime(fm.get("end"))
    if start is not None and end is not None:
        return start, end
    if start is not None:
        # No explicit end — assume 1h block. Better than treating as
        # zero-duration (which would never conflict with anything).
        return start, start + timedelta(hours=1)
    # Fallback to date-only: symmetric ±12h expansion around UTC midnight.
    d_dt = _parse_iso_datetime(fm.get("date"))
    if d_dt is not None:
        d = d_dt.date()
        expanded_start = datetime.combine(
            d - timedelta(days=1), time(12, 0), tzinfo=timezone.utc,
        )
        expanded_end = datetime.combine(
            d + timedelta(days=1), time(12, 0), tzinfo=timezone.utc,
        )
        return expanded_start, expanded_end
    return None


def _ranges_overlap(
    a_start: datetime, a_end: datetime,
    b_start: datetime, b_end: datetime,
) -> bool:
    """Half-open range overlap. Adjacency (touch but not cross) is NOT a conflict."""
    return a_start < b_end and a_end > b_start


def _scan_event_conflicts(
    vault_path: Path,
    proposed_start: datetime,
    proposed_end: datetime,
) -> list[dict[str, Any]]:
    """Walk ``vault_path/event/`` and return overlapping records.

    Returns a list of ``{title, start, end, path}`` dicts (one per
    conflicting event), oldest-conflict-first. Empty list ⇒ no conflicts.

    Defensive: skips files that fail frontmatter parse rather than
    raising — the conflict-check path must never crash because one
    record has malformed YAML.
    """
    conflicts: list[dict[str, Any]] = []
    event_dir = vault_path / "event"
    if not event_dir.is_dir():
        return conflicts
    for md_file in sorted(event_dir.glob("*.md")):
        try:
            post = frontmatter.load(str(md_file))
        except Exception:  # noqa: BLE001
            continue
        fm = dict(post.metadata or {})
        window = _event_window(fm)
        if window is None:
            continue
        ev_start, ev_end = window
        if not _ranges_overlap(proposed_start, proposed_end, ev_start, ev_end):
            continue
        title = (
            fm.get("title")
            or fm.get("name")
            or md_file.stem
        )
        rel_path = f"event/{md_file.name}"
        entry: dict[str, Any] = {
            "title": str(title),
            "start": ev_start.isoformat(),
            "end": ev_end.isoformat(),
            "source": ConflictSource.VAULT,
            "path": rel_path,
        }
        # Phase A+: vault records that were synced to GCal carry the
        # remote event ID in frontmatter. Surface it so the merged
        # conflict-list dedup can drop the corresponding gcal_alfred
        # mirror — without this, the same logical event shows up twice.
        gcal_event_id = fm.get("gcal_event_id")
        if isinstance(gcal_event_id, str) and gcal_event_id:
            entry["gcal_event_id"] = gcal_event_id
        conflicts.append(entry)
    return conflicts


def _scan_gcal_conflicts(
    request: web.Request,
    proposed_start: datetime,
    proposed_end: datetime,
    correlation_id: str,
) -> list[dict[str, Any]]:
    """Query Andrew's GCal for events overlapping the proposed window.

    Phase A+ inter-instance comms. Salem maintains a vault-only conflict
    map by default (see :func:`_scan_event_conflicts`). When GCal is
    enabled, we additionally query:

      * **Alfred Calendar** (R/W) — the dedicated calendar Salem writes
        to. Anything Salem has previously synced lands here. Source
        tagged ``gcal_alfred``.
      * **Primary calendar** (R/O by application policy) — Andrew's own
        meetings he scheduled directly. Source tagged ``gcal_primary``.

    The proposing instance + Andrew get the source tag in each conflict
    entry so "you have a primary-calendar meeting at this time" reads
    better than just "you have a meeting".

    Defensive: any GCal API failure is logged + dropped — the conflict
    check falls back to vault-only rather than failing the whole event
    proposal. The trade-off: under a Google outage, Salem might
    successfully create a vault event that conflicts with a real
    primary-calendar meeting. The alternative (fail-closed) would block
    every event proposal during any GCal hiccup. Vault-and-Alfred-cal
    sync still catches the case for next time once GCal recovers.
    Returning the empty-list-with-warning shape lets the operator grep
    ``transport.canonical.event_propose_gcal_failed`` to spot the gap.
    """
    client = request.app.get(_KEY_GCAL_CLIENT)
    config = request.app.get(_KEY_GCAL_CONFIG)
    if client is None or config is None or not config.enabled:
        # GCal not configured for this instance — explicit "did nothing"
        # signal so the caller knows the empty list is "vault-only by
        # design", not "GCal silently misbehaved" (per
        # ``feedback_intentionally_left_blank.md``).
        #
        # Sentinel-aware skip: if the operator INTENDED gcal on but
        # client construction failed at startup, surface the gap at
        # warning level so they spot the silent feature-degradation.
        # The "intended on" gate keeps the log signal clean — instances
        # that legitimately disabled gcal still log at debug.
        if request.app.get(_KEY_GCAL_INTENDED_ON):
            log.warning(
                "transport.canonical.event_propose_gcal_skipped_but_intended_on",
                phase="conflict_check",
                correlation_id=correlation_id,
                hint=(
                    "gcal.enabled is true in config but client setup failed "
                    "at daemon startup. Run `alfred gcal status` and "
                    "check daemon log for talker.daemon.gcal_setup_failed."
                ),
            )
        else:
            log.debug(
                "transport.canonical.event_propose_gcal_skipped",
                reason="not_configured",
                correlation_id=correlation_id,
            )
        return []

    from alfred.integrations.gcal import (
        GCalError,
        event_to_conflict_dict,
    )

    out: list[dict[str, Any]] = []

    # Alfred calendar — Salem's writable target.
    if config.alfred_calendar_id:
        try:
            events = client.list_events(
                config.alfred_calendar_id,
                proposed_start,
                proposed_end,
            )
        except GCalError as exc:
            log.warning(
                "transport.canonical.event_propose_gcal_failed",
                calendar="alfred",
                error=str(exc),
                correlation_id=correlation_id,
            )
            events = []
        for ev in events:
            out.append(
                event_to_conflict_dict(ev, source=ConflictSource.GCAL_ALFRED)
            )

    # Primary calendar — read-only by policy.
    if config.primary_calendar_id:
        try:
            events = client.list_events(
                config.primary_calendar_id,
                proposed_start,
                proposed_end,
            )
        except GCalError as exc:
            log.warning(
                "transport.canonical.event_propose_gcal_failed",
                calendar="primary",
                error=str(exc),
                correlation_id=correlation_id,
            )
            events = []
        for ev in events:
            out.append(
                event_to_conflict_dict(ev, source=ConflictSource.GCAL_PRIMARY)
            )

    return out


# Filename slug builder. Event filenames are the title plus an ISO date
# suffix so the brief renderer (which sorts events by ``date``) and
# Obsidian's filename-based dedup both have something stable to key on.
_FILENAME_BAD_CHARS = re.compile(r'[\\/:*?"<>|\t\n\r]+')


def _safe_event_filename(title: str, start: datetime) -> str:
    """Build a filesystem-safe ``<title> <YYYY-MM-DD>.md`` filename.

    The title is sanitised (path separators, colons, asterisks, etc.
    replaced with spaces; collapsed runs trimmed). Start date is
    formatted as the local-time ISO date so the filename matches the
    user-visible day of the event.
    """
    cleaned = _FILENAME_BAD_CHARS.sub(" ", title).strip()
    cleaned = re.sub(r"\s+", " ", cleaned) or "Event"
    if len(cleaned) > 120:
        cleaned = cleaned[:117].rstrip() + "..."
    # Local-time date for the suffix — reads more naturally than UTC.
    local_date = start.astimezone().date().isoformat()
    return f"{cleaned} {local_date}.md"


async def _handle_canonical_event_propose_create(
    request: web.Request,
) -> web.StreamResponse:
    """POST /canonical/event/propose-create — synchronous, with conflict-check.

    Body::

        {
          "correlation_id": "hypatia-propose-event-<hex6>",
          "start": "2026-05-04T14:00:00-03:00",
          "end":   "2026-05-04T15:00:00-03:00",
          "title": "VAC marketing call follow-up",
          "summary": "Follow-up on Q2 outreach plan",
          "origin_instance": "hypatia",
          "origin_context": "Discussed during marketing strategy session 2026-04-30 17:00"
        }

    Response shapes:
      * 201 Created on a clean create:
        ``{"status": "created", "path": "event/...md", "correlation_id": "..."}``
        Phase A+ sync extensions (added when GCal is configured):
          - on success: ``"gcal_event_id": "<id>", "gcal_calendar": "alfred"``
          - on sync failure: ``"gcal_sync_error": {"code": "<code>",
            "detail": "<msg>"}`` where ``code`` is one of
            ``calendar_id_missing`` / ``auth_failed`` /
            ``missing_dependency`` / ``api_error`` / ``unknown``. Vault
            record IS preserved; the projection failed, not the
            canonical write. Downstream renderers (Hypatia / KAL-LE)
            switch on ``code`` to produce calibrated user-facing
            messages without parsing free-form ``detail`` text.
          - GCal not configured: neither field present.
      * 200 with ``{"status": "conflict", "conflicts": [...]}`` when
        overlap detected. Each conflict carries a ``source`` field
        (``vault`` / ``gcal_alfred`` / ``gcal_primary``) so the
        proposing instance can render appropriately.
      * 404 ``canonical_not_owned`` when this instance isn't a canonical
        owner.
      * 400 ``schema_error`` for malformed bodies (missing fields,
        unparseable times, end <= start).
      * 409 ``already_exists`` when the target file already exists on
        disk (filename collision — different correlation, same title +
        date). Caller should pivot to :func:`vault_edit`.

    Audit: every outcome (created OR conflict) appends one entry to
    ``canonical_audit.jsonl``. Conflicts surface in the audit as
    ``denied=["conflict"]`` so an operator can grep the audit log for
    ``conflict`` to spot proposing-side calibration drift (instance
    keeps trying to schedule into Andrew's busy slots).
    """
    correlation_id = _ensure_correlation_id(request, None)
    peer = request.get("transport_peer", "")
    config = _get_config(request)
    audit_path = config.canonical.audit_log_path

    if not config.canonical.owner:
        append_audit(
            audit_path,
            peer=peer, record_type="event", name="(propose-create)",
            requested=[], granted=[], denied=["canonical_not_owned"],
            correlation_id=correlation_id,
        )
        return _json_error(
            404, "canonical_not_owned",
            detail="this instance does not host canonical records",
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

    correlation_id = _ensure_correlation_id(request, body)

    # --- Schema validation --------------------------------------------------
    title = body.get("title")
    summary = body.get("summary") or ""
    origin_instance = body.get("origin_instance") or peer or ""
    origin_context = body.get("origin_context") or ""
    start_raw = body.get("start")
    end_raw = body.get("end")

    if not isinstance(title, str) or not title.strip():
        return _json_error(
            400, "schema_error",
            detail="title must be a non-empty string",
            correlation_id=correlation_id,
        )
    if len(title) > _EVENT_TITLE_MAX_LEN:
        return _json_error(
            400, "schema_error",
            detail=f"title exceeds {_EVENT_TITLE_MAX_LEN}-char cap",
            correlation_id=correlation_id,
        )
    if not isinstance(summary, str) or len(summary) > _EVENT_SUMMARY_MAX_LEN:
        return _json_error(
            400, "schema_error",
            detail=f"summary must be a string under {_EVENT_SUMMARY_MAX_LEN} chars",
            correlation_id=correlation_id,
        )
    if not isinstance(origin_instance, str):
        return _json_error(
            400, "schema_error",
            detail="origin_instance must be a string",
            correlation_id=correlation_id,
        )
    if not isinstance(origin_context, str) or len(origin_context) > _EVENT_CONTEXT_MAX_LEN:
        return _json_error(
            400, "schema_error",
            detail=f"origin_context must be a string under {_EVENT_CONTEXT_MAX_LEN} chars",
            correlation_id=correlation_id,
        )

    start_dt = _parse_iso_datetime(start_raw)
    end_dt = _parse_iso_datetime(end_raw)
    if start_dt is None or end_dt is None:
        return _json_error(
            400, "schema_error",
            detail="start and end must be ISO 8601 datetime strings with timezone",
            correlation_id=correlation_id,
        )
    if end_dt <= start_dt:
        return _json_error(
            400, "schema_error",
            detail="end must be strictly after start",
            correlation_id=correlation_id,
        )

    # Same anti-spoof rule as /peer/send + /peer/brief_digest: if
    # ``origin_instance`` is supplied, it must match the authenticated
    # peer. Empty value defaults to ``peer`` (already done above) — no
    # mismatch path.
    if origin_instance and peer and origin_instance != peer:
        log.warning(
            "transport.canonical.event_propose_spoofed_origin",
            auth_peer=peer,
            claimed=origin_instance,
            correlation_id=correlation_id,
        )
        return _json_error(
            403, "from_mismatch",
            detail="origin_instance must equal authenticated peer",
            correlation_id=correlation_id,
        )

    vault_path = _get_vault_path(request)
    if vault_path is None:
        return _json_error(
            500, "vault_not_configured",
            detail="vault path not registered on transport server",
            correlation_id=correlation_id,
        )

    # --- Conflict-check -----------------------------------------------------
    # Vault is the local source of truth. GCal (when configured) extends
    # the visibility to Andrew's actual schedule on his phone — without
    # it, Salem can happily schedule an Alfred event into the same hour
    # as a real primary-calendar work meeting. Each entry in the merged
    # list carries a ``source`` field so the caller can render the
    # right context ("you have a primary-calendar meeting" vs "you have
    # a vault event").
    #
    # GCal failures degrade gracefully: a Google API outage falls back
    # to vault-only conflict-check rather than blocking every event
    # proposal. See ``_scan_gcal_conflicts`` for the trade-off note.
    vault_conflicts = _scan_event_conflicts(vault_path, start_dt, end_dt)
    gcal_conflicts = _scan_gcal_conflicts(
        request, start_dt, end_dt, correlation_id,
    )
    # Dedup vault-already-synced-to-gcal-alfred records: when commit 3
    # ships, every vault event has a ``gcal_event_id`` in frontmatter
    # pointing at its mirror on the Alfred calendar. The same logical
    # event would otherwise show up twice (once vault, once gcal_alfred)
    # in the conflict list. Filter gcal_alfred entries whose ID matches
    # any vault entry's gcal_event_id.
    vault_synced_ids = {
        c["gcal_event_id"]
        for c in vault_conflicts
        if c.get("gcal_event_id")
    }
    if vault_synced_ids:
        gcal_conflicts = [
            c for c in gcal_conflicts
            if c.get("gcal_event_id") not in vault_synced_ids
        ]
    conflicts = vault_conflicts + gcal_conflicts
    if conflicts:
        append_audit(
            audit_path,
            peer=peer, record_type="event", name=title.strip(),
            requested=["create"], granted=[], denied=["conflict"],
            correlation_id=correlation_id,
        )
        log.info(
            "transport.canonical.event_propose_conflict",
            peer=peer,
            title=title[:80],
            conflict_count=len(conflicts),
            vault_conflicts=len(vault_conflicts),
            gcal_conflicts=len(gcal_conflicts),
            correlation_id=correlation_id,
        )
        return web.json_response({
            "status": "conflict",
            "conflicts": conflicts,
            "correlation_id": correlation_id,
        })

    # --- Create the record --------------------------------------------------
    safe_filename = _safe_event_filename(title.strip(), start_dt)
    rel_path = f"event/{safe_filename}"
    file_path = vault_path / "event" / safe_filename
    if file_path.exists():
        # Same correlation race window as the queued-propose 409: two
        # propose-creates for the same title + date land back-to-back.
        log.info(
            "transport.canonical.event_propose_409_already_exists",
            peer=peer,
            title=title[:80],
            path=rel_path,
            correlation_id=correlation_id,
        )
        append_audit(
            audit_path,
            peer=peer, record_type="event", name=title.strip(),
            requested=["create"], granted=[], denied=["already_exists"],
            correlation_id=correlation_id,
        )
        return web.json_response(
            {
                "status": "exists",
                "path": rel_path,
                "correlation_id": correlation_id,
            },
            status=409,
        )

    # Build the frontmatter. ``date`` is the local-time ISO date so the
    # brief renderer's existing ``_coerce_date(fm.get("date"))`` lookup
    # surfaces the new event without any brief-side changes (Phase 1
    # SHIPPED 2026-04-21 wires off ``date``). ``start`` + ``end`` carry
    # the precise window for downstream conflict-check.
    today_iso = datetime.now(timezone.utc).date().isoformat()
    local_date_iso = start_dt.astimezone().date().isoformat()
    fm = {
        "type": "event",
        "name": file_path.stem,
        "title": title.strip(),
        "date": local_date_iso,
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "summary": summary.strip(),
        "origin_instance": origin_instance,
        "origin_context": origin_context.strip(),
        "created": today_iso,
        "correlation_id": correlation_id,
        "tags": [],
    }
    fm_str = yaml.dump(
        fm, default_flow_style=False, allow_unicode=True, sort_keys=False,
    )
    body_text = (summary.strip() + "\n") if summary.strip() else ""
    file_text = f"---\n{fm_str}---\n\n{body_text}"

    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(file_text, encoding="utf-8")
    except OSError as exc:
        log.warning(
            "transport.canonical.event_propose_write_failed",
            peer=peer,
            path=str(file_path),
            error=str(exc),
            correlation_id=correlation_id,
        )
        return _json_error(
            500, "write_failed",
            detail=str(exc),
            correlation_id=correlation_id,
        )

    append_audit(
        audit_path,
        peer=peer, record_type="event", name=title.strip(),
        requested=["create"], granted=["create"], denied=[],
        correlation_id=correlation_id,
    )
    log.info(
        "transport.canonical.event_propose_created",
        peer=peer,
        title=title[:80],
        path=rel_path,
        start=start_dt.isoformat(),
        end=end_dt.isoformat(),
        correlation_id=correlation_id,
    )

    # --- Sync to GCal (Phase A+) -------------------------------------------
    # Vault is canonical; the Alfred Calendar entry is a projection so
    # Andrew sees the event on his phone. Sync failure does NOT roll
    # back the vault create — operator can manually re-sync (or a
    # future Phase D sync loop will do it). The response carries a
    # ``gcal_sync_error`` field so the proposing instance can tell
    # Andrew "saved to vault but GCal sync failed".
    response_payload: dict[str, Any] = {
        "status": "created",
        "path": rel_path,
        "correlation_id": correlation_id,
    }
    sync_result = _sync_event_to_gcal(
        request,
        file_path=file_path,
        title=title.strip(),
        description=summary.strip(),
        start_dt=start_dt,
        end_dt=end_dt,
        correlation_id=correlation_id,
    )
    if sync_result.get("event_id"):
        response_payload["gcal_event_id"] = sync_result["event_id"]
        # Calendar-kind label is config-driven (per-instance) — Salem
        # ships ``"alfred"``; V.E.R.A. RRTS would set ``"rrts"``;
        # STAY-C client cal would set ``"stayc"``. Defensive fallback
        # to ``"alfred"`` keeps the field present for callers that
        # still parse it as a fixed string.
        response_payload["gcal_calendar"] = sync_result.get(
            "calendar_label", "alfred",
        )
    elif sync_result.get("error"):
        response_payload["gcal_sync_error"] = sync_result["error"]
    # When sync_result is empty (GCal not configured), neither key is
    # added — the proposing instance's silence on the GCal field is
    # the same signal as "this instance doesn't sync to GCal".

    return web.json_response(response_payload, status=201)


def _classify_gcal_error(exc: BaseException) -> str:
    """Map a GCal-side exception to a stable code for downstream renderers.

    The ``gcal_sync_error`` field on the ``/canonical/event/propose-create``
    response carries a ``code`` so Hypatia / KAL-LE can switch on it
    without parsing free-form ``detail`` text. Codes are intentionally
    coarse for v1 — future refinements (e.g. splitting ``api_error``
    into ``quota_exceeded`` / ``rate_limited`` / ``server_error``) can
    add codes without breaking consumers that already handle the
    coarse value.

    Lazy-imports the ``GCalError`` hierarchy because this module loads
    on instances that don't have the optional ``[gcal]`` deps installed
    — eager import would crash KAL-LE / Hypatia at startup.
    """
    try:
        from alfred.integrations.gcal import (
            GCalAPIError,
            GCalNotAuthorized,
            GCalNotInstalled,
        )
    except ImportError:
        # Optional dep absent — caller hit a non-GCal exception path.
        return "unknown"

    if isinstance(exc, GCalNotAuthorized):
        # Token refresh failed OR no token on disk. Operator-fixable
        # (run ``alfred gcal authorize``); transient network failures
        # also surface here per the GCalClient docstring.
        return "auth_failed"
    if isinstance(exc, GCalNotInstalled):
        # google-* libs missing; operator must ``pip install '.[gcal]'``.
        return "missing_dependency"
    if isinstance(exc, GCalAPIError):
        # Catch-all for HTTP / quota / API surface failures. Future
        # refinement can split on HTTP status if cheaply available.
        return "api_error"
    return "unknown"


def _sync_event_to_gcal(
    request: web.Request,
    *,
    file_path: Path,
    title: str,
    description: str,
    start_dt: datetime,
    end_dt: datetime,
    correlation_id: str,
) -> dict[str, Any]:
    """Mirror a freshly-created vault event to the Alfred Calendar.

    Returns a dict with one of:
      * ``{}`` — GCal not configured for this instance (silent skip).
      * ``{"event_id": "<gcal-id>", "calendar_label": "<label>"}`` — sync
        succeeded; vault frontmatter has been updated to include
        ``gcal_event_id`` + ``gcal_calendar`` (the label is config-driven
        per :attr:`GCalConfig.alfred_calendar_label`).
      * ``{"error": {"code": "<code>", "detail": "<msg>"}}`` — sync
        failed; vault stays intact. The ``code`` is one of
        ``calendar_id_missing`` / ``auth_failed`` / ``missing_dependency``
        / ``api_error`` / ``unknown`` — see :func:`_classify_gcal_error`.
        Downstream renderers (Hypatia / KAL-LE) switch on ``code`` to
        produce calibrated user-facing messages without parsing free-form
        ``detail`` text.

    Per architecture:
      * Salem ONLY writes to the configured Alfred Calendar ID — never
        primary. This function enforces that policy by reading the ID
        from config and refusing to act on anything else.
      * Vault is canonical. We re-write the markdown frontmatter on
        success because the vault record's ``gcal_event_id`` is what
        the next conflict-check cycle uses for dedup. Without that
        write-back, every subsequent propose-create would see the same
        event on both vault and gcal_alfred and double-count.
    """
    client = request.app.get(_KEY_GCAL_CLIENT)
    config = request.app.get(_KEY_GCAL_CONFIG)
    if client is None or config is None or not config.enabled:
        # Sentinel-aware skip: if the operator INTENDED gcal on but
        # client construction failed at startup, surface the gap at
        # warning level so they spot the silent feature-degradation.
        # Without the sentinel, every event proposal would emit a
        # noisy warning even on instances that legitimately disabled
        # gcal — the "intended on" gate keeps the log signal clean.
        if request.app.get(_KEY_GCAL_INTENDED_ON):
            log.warning(
                "transport.canonical.event_propose_gcal_skipped_but_intended_on",
                phase="sync",
                correlation_id=correlation_id,
                hint=(
                    "gcal.enabled is true in config but client setup failed "
                    "at daemon startup. Run `alfred gcal status` and "
                    "check daemon log for talker.daemon.gcal_setup_failed."
                ),
            )
        else:
            log.debug(
                "transport.canonical.event_propose_gcal_sync_skipped",
                reason="not_configured",
                correlation_id=correlation_id,
            )
        return {}
    if not config.alfred_calendar_id:
        log.warning(
            "transport.canonical.event_propose_gcal_sync_skipped",
            reason="alfred_calendar_id_empty",
            correlation_id=correlation_id,
        )
        return {
            "error": {
                "code": "calendar_id_missing",
                "detail": "gcal alfred_calendar_id not configured",
            }
        }

    from alfred.integrations.gcal import GCalError

    # P2-2: pass time_zone through when configured. When empty (default),
    # GCalClient.create_event omits the timeZone field from the body and
    # GCal falls back to the calendar's own default zone for display.
    # The dateTime offset still pins the absolute time unambiguously.
    create_kwargs: dict[str, Any] = {
        "start": start_dt,
        "end": end_dt,
        "title": title,
        "description": description,
    }
    if getattr(config, "default_time_zone", ""):
        create_kwargs["time_zone"] = config.default_time_zone

    try:
        event_id = client.create_event(
            config.alfred_calendar_id,
            **create_kwargs,
        )
    except GCalError as exc:
        # Per spec: do NOT roll back the vault create. Vault is canonical;
        # GCal is the projection. Surface the error so Hypatia / KAL-LE
        # can tell Andrew "saved to vault but GCal sync failed".
        code = _classify_gcal_error(exc)
        log.warning(
            "transport.canonical.event_propose_gcal_sync_failed",
            error=str(exc),
            error_code=code,
            correlation_id=correlation_id,
        )
        return {"error": {"code": code, "detail": str(exc)}}

    # P2-1: calendar-kind label is config-driven so V.E.R.A.'s RRTS
    # calendar can write ``"rrts"``, STAY-C client cal ``"stayc"``, etc.
    # Fallback to ``"alfred"`` covers the default-config case and any
    # operator who omits the field.
    calendar_label = getattr(config, "alfred_calendar_label", "") or "alfred"

    # Success: write gcal_event_id + gcal_calendar back into the vault
    # record's frontmatter. We re-load the file we just wrote rather
    # than reusing the in-memory dict so any changes a parallel writer
    # made between our write and now are preserved (defensive — there
    # shouldn't be a parallel writer but cheap insurance).
    try:
        post = frontmatter.load(str(file_path))
        post["gcal_event_id"] = event_id
        post["gcal_calendar"] = calendar_label
        # Preserve trailing-newline behaviour matching the original write.
        new_text = frontmatter.dumps(post)
        if not new_text.endswith("\n"):
            new_text += "\n"
        file_path.write_text(new_text, encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        # Frontmatter rewrite failure is a soft error — the GCal event
        # exists, but our dedup loses its anchor. Log + keep going so
        # the caller still gets the success path.
        log.warning(
            "transport.canonical.event_propose_gcal_writeback_failed",
            error=str(exc),
            event_id=event_id,
            path=str(file_path),
            correlation_id=correlation_id,
        )

    log.info(
        "transport.canonical.event_propose_gcal_synced",
        event_id=event_id,
        calendar_id=config.alfred_calendar_id,
        calendar_label=calendar_label,
        correlation_id=correlation_id,
    )
    return {"event_id": event_id, "calendar_label": calendar_label}


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

    The ``/canonical/event/propose-create`` route is registered before
    the generic ``{type}/propose`` route so aiohttp's first-match
    semantics route it to the synchronous-create handler. ``event``
    proposes don't go through the queued shape — they're synchronous
    with conflict-check by design.
    """
    app.router.add_post(
        "/canonical/event/propose-create",
        _handle_canonical_event_propose_create,
    )
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


def register_gcal_client(
    app: web.Application,
    client: Any,
    config: Any,
) -> None:
    """Wire a Google Calendar client + config onto the transport app.

    Phase A+ inter-instance comms. The conflict-check + sync-on-create
    paths in :func:`_handle_canonical_event_propose_create` look up
    these two app keys; either being absent / ``None`` makes those
    paths skip the GCal code (vault-only behaviour, same as before
    Phase A+ shipped).

    ``client`` is an :class:`alfred.integrations.gcal.GCalClient` (or
    a stub for tests). ``config`` is the typed
    :class:`alfred.integrations.gcal_config.GCalConfig`.

    Loose typing on the params avoids importing from
    ``alfred.integrations`` at module load — the transport module
    needs to load on instances that didn't pip-install ``[gcal]``.
    """
    app[_KEY_GCAL_CLIENT] = client
    app[_KEY_GCAL_CONFIG] = config


def register_gcal_intended_on(app: web.Application) -> None:
    """Mark the app as "operator wanted GCal on, but client setup failed".

    P2-4 sentinel. Daemon calls this when ``gcal.enabled`` is true in
    config but :class:`GCalClient` construction failed at startup
    (e.g. credentials file missing, scopes malformed). Without the
    sentinel, every conflict-check + sync attempt would emit
    ``transport.canonical.event_propose_gcal_skipped`` at ``debug``
    level — the operator sees nothing, but the system is in
    "intended-on, actually-off" state.

    With the sentinel set, those skip sites log at ``warning`` instead,
    pointing the operator to ``alfred gcal status`` + the
    ``talker.daemon.gcal_setup_failed`` log line. Instances that
    legitimately disabled GCal don't call this helper, so their skips
    stay quiet (debug-level).

    Idempotent: calling multiple times is safe (sets the same flag).
    """
    app[_KEY_GCAL_INTENDED_ON] = True
