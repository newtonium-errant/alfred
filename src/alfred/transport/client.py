"""Client helper for the outbound-push transport.

Every tool calls one of these three coroutines:

- :func:`send_outbound`     — single message, optionally scheduled
- :func:`send_outbound_batch` — multi-chunk send (brief auto-push)
- :func:`get_status`        — inspect an entry by ID

The client reads config from the environment:

- ``ALFRED_TRANSPORT_HOST`` / ``ALFRED_TRANSPORT_PORT`` — bind address
  (defaults to ``127.0.0.1:8891``, matching the server default)
- ``ALFRED_TRANSPORT_TOKEN`` — bearer token. Raises
  :class:`TransportAuthMissing` if unset; the orchestrator injects
  this into every tool's subprocess env.

``X-Alfred-Client`` is auto-detected from ``sys.argv[0]`` so
``alfred-brief`` → ``brief``, ``alfred-scheduler`` → ``scheduler``.
Callers can override when the detection doesn't match the allowlist.

Retry policy: one retry on 5xx/timeout/ConnectionRefused with
0.5s → 2s backoff. 4xx never retries (client error — re-sending
won't help). Every failure path adheres to builder.md's
subprocess-failure-contract shape: a ``response_summary`` field with
status + truncated body so grep-by-error works.
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any

import httpx

from .exceptions import (
    TransportAuthMissing,
    TransportError,
    TransportRejected,
    TransportServerDown,
    TransportUnavailable,
)
from .utils import get_logger

log = get_logger(__name__)


# --- Defaults + env resolution ---------------------------------------------


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8891

# Retry shape: 0.5s then 2s. The first retry covers server restarts
# (the talker comes back up in under 2s); the second buys one more
# chance for a transient aiohttp hiccup.
_RETRY_BACKOFFS: tuple[float, ...] = (0.5, 2.0)
_REQUEST_TIMEOUT = 15.0


def _resolve_base_url() -> str:
    host = os.environ.get("ALFRED_TRANSPORT_HOST", DEFAULT_HOST)
    port = os.environ.get("ALFRED_TRANSPORT_PORT", str(DEFAULT_PORT))
    return f"http://{host}:{port}"


def _resolve_token() -> str:
    token = os.environ.get("ALFRED_TRANSPORT_TOKEN", "")
    if not token:
        raise TransportAuthMissing(
            "ALFRED_TRANSPORT_TOKEN is not set. The orchestrator injects "
            "this from config.yaml's transport.auth.tokens.local.token. "
            "For manual runs, export it before calling the client."
        )
    return token


def _detect_client_name() -> str:
    """Best-effort identification for the ``X-Alfred-Client`` header.

    Looks at ``sys.argv[0]`` — ``alfred-brief`` becomes ``brief``,
    ``alfred-scheduler`` becomes ``scheduler``. Falls back to
    ``cli`` which must then be in the peer's ``allowed_clients``
    list (the default ``local`` config lists scheduler/brief/janitor/
    curator/talker but not ``cli`` — callers invoking from an ad-hoc
    script should pass ``client_name`` explicitly).
    """
    argv0 = os.path.basename(sys.argv[0] or "")
    if argv0.startswith("alfred-"):
        return argv0.removeprefix("alfred-")
    # The talker daemon runs as ``alfred`` (no dash), scheduler runs
    # inside the talker process. Default to "talker" as the least
    # surprising "I came from inside the big process" value.
    if argv0 in {"alfred", "python", "python3"}:
        return "talker"
    return argv0 or "cli"


# --- Low-level request wrapper ---------------------------------------------


async def _request(
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    client_name: str | None = None,
) -> dict[str, Any]:
    """Issue a single request with retry on 5xx / connection errors.

    Returns the parsed JSON body on success. Raises one of the
    :class:`TransportError` subclasses on every failure path.
    """
    base_url = _resolve_base_url()
    token = _resolve_token()
    client_header = client_name or _detect_client_name()

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Alfred-Client": client_header,
    }

    url = f"{base_url}{path}"
    last_exc: Exception | None = None

    # One initial attempt + one retry on 5xx/connection issues.
    for attempt_num, backoff in enumerate([0.0, *_RETRY_BACKOFFS], start=0):
        if backoff:
            await asyncio.sleep(backoff)
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.request(
                    method, url, json=json, headers=headers,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_exc = exc
            log.warning(
                "transport.client.connect_failed",
                url=url,
                attempt=attempt_num,
                error=str(exc),
                response_summary=f"Connect failure: {exc.__class__.__name__}",
            )
            continue
        except httpx.RequestError as exc:
            last_exc = exc
            log.warning(
                "transport.client.request_error",
                url=url,
                attempt=attempt_num,
                error=str(exc),
                response_summary=f"Request error: {exc.__class__.__name__}: {exc}",
            )
            continue

        # 4xx — no retry.
        if 400 <= resp.status_code < 500:
            body_text = resp.text[:500]
            log.warning(
                "transport.client.nonzero_response",
                code=resp.status_code,
                body=body_text,
                response_summary=(
                    f"Status {resp.status_code}: {body_text[:200] or '(no body)'}"
                ),
            )
            raise TransportRejected(
                f"HTTP {resp.status_code} from {path}: {body_text[:200]}",
                status_code=resp.status_code,
                body=body_text,
            )

        # 5xx — one retry only.
        if 500 <= resp.status_code < 600:
            body_text = resp.text[:500]
            log.warning(
                "transport.client.nonzero_response",
                code=resp.status_code,
                body=body_text,
                attempt=attempt_num,
                response_summary=(
                    f"Status {resp.status_code}: {body_text[:200] or '(no body)'}"
                ),
            )
            last_exc = TransportUnavailable(
                f"HTTP {resp.status_code} from {path}: {body_text[:200]}"
            )
            continue

        # Success.
        try:
            return resp.json()
        except ValueError as exc:
            raise TransportError(
                f"non-JSON response from {path}: {resp.text[:200]}"
            ) from exc

    # Exhausted retries.
    if isinstance(last_exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        raise TransportServerDown(
            f"Could not reach {url} after {len(_RETRY_BACKOFFS) + 1} attempt(s): "
            f"{last_exc}"
        ) from last_exc
    if isinstance(last_exc, TransportUnavailable):
        raise last_exc
    if last_exc is not None:
        raise TransportUnavailable(f"{url}: {last_exc}") from last_exc
    raise TransportUnavailable(f"{url}: no response after retries")


# --- Public API ------------------------------------------------------------


async def send_outbound(
    user_id: int,
    text: str,
    *,
    scheduled_at: str | None = None,
    dedupe_key: str | None = None,
    client_name: str | None = None,
) -> dict[str, Any]:
    """Send a single outbound message.

    Args:
        user_id: Telegram user_id / chat_id to deliver to.
        text: Message body. The server enforces Telegram's 4096-char
            limit; prefer :func:`send_outbound_batch` with chunker
            output for anything that might overflow.
        scheduled_at: Optional ISO 8601 timestamp (UTC-aware). When
            supplied and in the future, the server queues the send
            instead of dispatching immediately.
        dedupe_key: Optional idempotency key. A second send with the
            same key inside the 24h window returns the recorded
            entry instead of re-dispatching.
        client_name: Optional override for the ``X-Alfred-Client``
            header. Detected from ``sys.argv[0]`` by default.

    Returns:
        The server's JSON response dict — ``{"id", "status", ...}``.

    Raises:
        :class:`TransportAuthMissing`: ``ALFRED_TRANSPORT_TOKEN`` unset.
        :class:`TransportServerDown`: server unreachable.
        :class:`TransportRejected`: 4xx — caller must fix request.
        :class:`TransportUnavailable`: 5xx / telegram_not_configured.
    """
    body: dict[str, Any] = {"user_id": user_id, "text": text}
    if scheduled_at is not None:
        body["scheduled_at"] = scheduled_at
    if dedupe_key:
        body["dedupe_key"] = dedupe_key
    return await _request(
        "POST", "/outbound/send",
        json=body, client_name=client_name,
    )


async def send_outbound_batch(
    user_id: int,
    chunks: list[str],
    *,
    dedupe_key: str | None = None,
    client_name: str | None = None,
) -> dict[str, Any]:
    """Send a sequence of Telegram-safe chunks as one logical batch.

    Used by the brief daemon — a rendered brief that exceeds
    Telegram's single-message limit goes through
    :func:`alfred.transport.utils.chunk_for_telegram` and is
    dispatched via this call so the server can preserve ordering and
    apply per-chat rate-limit backoff across the sequence.
    """
    if not chunks:
        raise TransportRejected(
            "send_outbound_batch: chunks must be non-empty",
            status_code=400,
        )
    body: dict[str, Any] = {"user_id": user_id, "chunks": list(chunks)}
    if dedupe_key:
        body["dedupe_key"] = dedupe_key
    return await _request(
        "POST", "/outbound/send_batch",
        json=body, client_name=client_name,
    )


async def get_status(
    entry_id: str,
    *,
    client_name: str | None = None,
) -> dict[str, Any]:
    """Fetch the status of a previously-submitted send by id."""
    return await _request(
        "GET", f"/outbound/status/{entry_id}",
        client_name=client_name,
    )


# ---------------------------------------------------------------------------
# Stage 3.5 peer dispatch — talks to another Alfred instance's transport
# ---------------------------------------------------------------------------


async def _peer_request(
    *,
    base_url: str,
    token: str,
    method: str,
    path: str,
    self_name: str,
    correlation_id: str | None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Peer-variant of :func:`_request`.

    Same retry shape (0.5s → 2s on 5xx/connection), same error
    taxonomy. Key differences:
      - base URL + token come from the caller (peer-specific, not
        read from env).
      - ``X-Alfred-Client`` is the instance name (``salem``,
        ``kal-le``) because the remote's ``allowed_clients`` is keyed
        on peer names, not tool names.
      - Correlation id travels via ``X-Correlation-Id`` when provided.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Alfred-Client": self_name,
    }
    if correlation_id:
        headers["X-Correlation-Id"] = correlation_id

    url = f"{base_url.rstrip('/')}{path}"
    last_exc: Exception | None = None

    for attempt_num, backoff in enumerate([0.0, *_RETRY_BACKOFFS], start=0):
        if backoff:
            await asyncio.sleep(backoff)
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
                resp = await client.request(
                    method, url, json=json_body, headers=headers,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_exc = exc
            log.warning(
                "transport.client.peer_connect_failed",
                url=url,
                attempt=attempt_num,
                error=str(exc),
                response_summary=f"Connect failure: {exc.__class__.__name__}",
            )
            continue
        except httpx.RequestError as exc:
            last_exc = exc
            log.warning(
                "transport.client.peer_request_error",
                url=url,
                attempt=attempt_num,
                error=str(exc),
                response_summary=f"Request error: {exc.__class__.__name__}: {exc}",
            )
            continue

        if 400 <= resp.status_code < 500:
            body_text = resp.text[:500]
            log.warning(
                "transport.client.peer_nonzero_response",
                code=resp.status_code,
                body=body_text,
                response_summary=(
                    f"Status {resp.status_code}: {body_text[:200] or '(no body)'}"
                ),
            )
            raise TransportRejected(
                f"HTTP {resp.status_code} from {path}: {body_text[:200]}",
                status_code=resp.status_code,
                body=body_text,
            )

        if 500 <= resp.status_code < 600:
            body_text = resp.text[:500]
            log.warning(
                "transport.client.peer_nonzero_response",
                code=resp.status_code,
                body=body_text,
                attempt=attempt_num,
                response_summary=(
                    f"Status {resp.status_code}: {body_text[:200] or '(no body)'}"
                ),
            )
            last_exc = TransportUnavailable(
                f"HTTP {resp.status_code} from {path}: {body_text[:200]}"
            )
            continue

        try:
            return resp.json()
        except ValueError as exc:
            raise TransportError(
                f"non-JSON response from {path}: {resp.text[:200]}"
            ) from exc

    if isinstance(last_exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        raise TransportServerDown(
            f"Could not reach {url} after {len(_RETRY_BACKOFFS) + 1} attempt(s): "
            f"{last_exc}"
        ) from last_exc
    if isinstance(last_exc, TransportUnavailable):
        raise last_exc
    if last_exc is not None:
        raise TransportUnavailable(f"{url}: {last_exc}") from last_exc
    raise TransportUnavailable(f"{url}: no response after retries")


def _new_correlation_id() -> str:
    """16 hex chars — same width the server mints when none is supplied."""
    import uuid
    return uuid.uuid4().hex[:16]


async def peer_send(
    peer_name: str,
    kind: str,
    payload: dict[str, Any],
    *,
    config: "TransportConfig | None" = None,
    self_name: str = "salem",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """POST /peer/send on the named peer's transport.

    ``config`` is usually built from ``config.yaml`` via
    ``load_from_unified``. Accepts ``None`` only in tests that monkey-
    patch the request function — production callers must supply it
    because the peer URL + token live in the caller's config.
    """
    from .config import TransportConfig, load_config
    from .peers import _resolve_peer

    if config is None:
        config = load_config()
    base_url, token = _resolve_peer(config, peer_name)
    cid = correlation_id or _new_correlation_id()

    body = {
        "kind": kind,
        "from": self_name,
        "payload": payload,
        "correlation_id": cid,
    }
    return await _peer_request(
        base_url=base_url,
        token=token,
        method="POST",
        path="/peer/send",
        self_name=self_name,
        correlation_id=cid,
        json_body=body,
    )


async def peer_query(
    peer_name: str,
    record_type: str,
    name: str,
    *,
    fields: list[str] | None = None,
    filter: dict[str, Any] | None = None,  # noqa: A002
    config: "TransportConfig | None" = None,
    self_name: str = "salem",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """POST /peer/query on the named peer — typically SALEM asking SALEM.

    In a peer-federated deployment KAL-LE might call this to fetch
    Andrew's canonical contact fields. The server applies its field-
    level permission filter and audit-logs the call.
    """
    from .config import TransportConfig, load_config
    from .peers import _resolve_peer

    if config is None:
        config = load_config()
    base_url, token = _resolve_peer(config, peer_name)
    cid = correlation_id or _new_correlation_id()

    body: dict[str, Any] = {"record_type": record_type, "name": name}
    if fields:
        body["fields"] = list(fields)
    if filter:
        body["filter"] = dict(filter)

    return await _peer_request(
        base_url=base_url,
        token=token,
        method="POST",
        path="/peer/query",
        self_name=self_name,
        correlation_id=cid,
        json_body=body,
    )


async def peer_handshake(
    peer_name: str,
    *,
    config: "TransportConfig | None" = None,
    self_name: str = "salem",
) -> dict[str, Any]:
    """POST /peer/handshake on the named peer — capability discovery."""
    from .config import TransportConfig, load_config
    from .peers import _resolve_peer

    if config is None:
        config = load_config()
    base_url, token = _resolve_peer(config, peer_name)
    cid = _new_correlation_id()
    return await _peer_request(
        base_url=base_url,
        token=token,
        method="POST",
        path="/peer/handshake",
        self_name=self_name,
        correlation_id=cid,
        json_body={"from": self_name, "protocol_version": 1},
    )


async def peer_get_canonical_person(
    peer_name: str,
    name: str,
    *,
    config: "TransportConfig | None" = None,
    self_name: str = "kal-le",
    correlation_id: str | None = None,
) -> dict[str, Any] | None:
    """GET /canonical/person/{name} on the named peer (typically Salem).

    Returns the server's response dict on 200, or ``None`` on
    404 ``record_not_found`` / ``canonical_not_owned``. Every other
    error propagates as a :class:`TransportError` subclass.

    The ``None`` return is the signal the caller uses to decide whether
    to escalate via :func:`peer_propose_canonical_person`. Propose-
    person c3 shipped the pair so a subordinate instance never silently
    fails on an unknown person.
    """
    from urllib.parse import quote

    from .config import TransportConfig, load_config
    from .exceptions import TransportRejected
    from .peers import _resolve_peer

    if config is None:
        config = load_config()
    base_url, token = _resolve_peer(config, peer_name)
    cid = correlation_id or _new_correlation_id()

    path = f"/canonical/person/{quote(name, safe='')}"
    try:
        return await _peer_request(
            base_url=base_url,
            token=token,
            method="GET",
            path=path,
            self_name=self_name,
            correlation_id=cid,
            json_body=None,
        )
    except TransportRejected as exc:
        # 404 is a legitimate outcome — the person doesn't exist on
        # the canonical owner (yet). The caller decides whether to
        # propose. Other 4xx (401, 403) still raise so misconfig is
        # loud.
        if exc.status_code == 404:
            log.info(
                "transport.client.canonical_person_not_found",
                peer=peer_name,
                person=name,
                correlation_id=cid,
            )
            return None
        raise


async def peer_get_canonical_record(
    peer_name: str,
    record_type: str,
    name: str,
    *,
    config: "TransportConfig | None" = None,
    self_name: str = "kal-le",
    correlation_id: str | None = None,
) -> dict[str, Any] | None:
    """GET /canonical/{type}/{name} on the named peer (typically Salem).

    Generic counterpart to :func:`peer_get_canonical_person` — works for
    any canonical record type the peer permits. Returns the server's
    response dict on 200, or ``None`` on 404 (``record_not_found`` /
    ``canonical_not_owned``). Other 4xx (401, 403) propagate as
    :class:`TransportRejected` so misconfig is loud.

    The 403 ``no_permitted_fields`` case (peer has no allowlist for this
    type) propagates rather than collapsing to ``None`` because the
    caller's UX is different — "you're not allowed to see this" vs
    "it doesn't exist".
    """
    from urllib.parse import quote

    from .config import TransportConfig, load_config
    from .exceptions import TransportRejected
    from .peers import _resolve_peer

    if config is None:
        config = load_config()
    base_url, token = _resolve_peer(config, peer_name)
    cid = correlation_id or _new_correlation_id()

    path = f"/canonical/{quote(record_type, safe='')}/{quote(name, safe='')}"
    try:
        return await _peer_request(
            base_url=base_url,
            token=token,
            method="GET",
            path=path,
            self_name=self_name,
            correlation_id=cid,
            json_body=None,
        )
    except TransportRejected as exc:
        if exc.status_code == 404:
            log.info(
                "transport.client.canonical_record_not_found",
                peer=peer_name,
                record_type=record_type,
                name=name,
                correlation_id=cid,
            )
            return None
        raise


async def peer_propose_canonical_record(
    peer_name: str,
    record_type: str,
    name: str,
    *,
    proposed_fields: dict[str, Any] | None = None,
    source: str = "",
    config: "TransportConfig | None" = None,
    self_name: str = "kal-le",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """POST /canonical/{type}/propose on the named peer (queued shape).

    Generalization of the original ``peer_propose_canonical_person``
    contract to ``person`` / ``org`` / ``location`` — types whose
    creation needs operator judgment about identity / duplication,
    queued via the Daily Sync. Returns the server's response dict.
    Caller inspects ``status`` — ``"pending"`` (HTTP 202) means queued;
    ``"exists"`` (HTTP 409) means a record landed between the
    proposer's 404 read and this call.

    Note: ``event`` is not eligible — events go through the synchronous
    ``POST /canonical/event/propose-create`` route with conflict-check.
    See :func:`peer_propose_event`.

    ``correlation_id`` defaults to ``{self_name}-propose-{type}-<hex6>``
    so the audit trail is greppable by proposer + type.
    """
    import secrets

    from .config import TransportConfig, load_config
    from .exceptions import TransportRejected
    from .peers import _resolve_peer

    if config is None:
        config = load_config()
    base_url, token = _resolve_peer(config, peer_name)
    cid = (
        correlation_id
        or f"{self_name}-propose-{record_type}-{secrets.token_hex(3)}"
    )

    body: dict[str, Any] = {
        "name": name,
        "correlation_id": cid,
    }
    if proposed_fields:
        body["proposed_fields"] = dict(proposed_fields)
    if source:
        body["source"] = source

    try:
        response = await _peer_request(
            base_url=base_url,
            token=token,
            method="POST",
            path=f"/canonical/{record_type}/propose",
            self_name=self_name,
            correlation_id=cid,
            json_body=body,
        )
    except TransportRejected as exc:
        # 409 already_exists — collapse into a structured return so the
        # caller (re-GET on the canonical record) has one less branch.
        if exc.status_code == 409:
            import json as _json
            try:
                parsed = _json.loads(exc.body) if exc.body else {}
            except ValueError:
                parsed = {}
            if not isinstance(parsed, dict):
                parsed = {}
            parsed.setdefault("status", "exists")
            parsed.setdefault("correlation_id", cid)
            log.info(
                "transport.client.canonical_propose_409_already_exists",
                peer=peer_name,
                record_type=record_type,
                name=name,
                correlation_id=cid,
            )
            return parsed
        raise

    log.info(
        "transport.client.canonical_propose_sent",
        peer=peer_name,
        record_type=record_type,
        name=name,
        correlation_id=cid,
        status=response.get("status") if isinstance(response, dict) else None,
    )
    return response if isinstance(response, dict) else {"correlation_id": cid}


async def peer_propose_canonical_person(
    peer_name: str,
    name: str,
    *,
    proposed_fields: dict[str, Any] | None = None,
    source: str = "",
    config: "TransportConfig | None" = None,
    self_name: str = "kal-le",
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """POST /canonical/person/propose on the named peer.

    Backwards-compat wrapper around
    :func:`peer_propose_canonical_record` pinned to ``record_type =
    "person"``. Existing callers (KAL-LE propose-person, the
    ``alfred transport propose-person`` CLI) are unaffected by the
    generalization to org / location.
    """
    return await peer_propose_canonical_record(
        peer_name,
        "person",
        name,
        proposed_fields=proposed_fields,
        source=source,
        config=config,
        self_name=self_name,
        correlation_id=correlation_id,
    )


async def resolve_or_propose_canonical_record(
    peer_name: str,
    record_type: str,
    name: str,
    *,
    proposed_fields: dict[str, Any] | None = None,
    source: str = "",
    config: "TransportConfig | None" = None,
    self_name: str = "kal-le",
) -> dict[str, Any]:
    """Generic high-level helper — GET, propose on 404, handle 409 race.

    Generalization of :func:`resolve_or_propose_canonical_person` to
    ``person`` / ``org`` / ``location``. Returns one of:

      * ``{"status": "found", "frontmatter": {...}, ...}`` — record
        exists; ``frontmatter`` carries the peer-visible subset.
      * ``{"status": "pending", "correlation_id": "..."}`` — record
        didn't exist; proposal queued for Andrew's Daily Sync review.
      * ``{"status": "found", ...}`` — also the 409 race outcome,
        collapsed via a follow-up GET so callers don't have to branch.

    NOT eligible for ``record_type="event"`` — events go through the
    synchronous ``propose-create`` flow with conflict-check.
    """
    record = await peer_get_canonical_record(
        peer_name, record_type, name,
        config=config, self_name=self_name,
    )
    if record is not None:
        return {"status": "found", **record}

    proposal = await peer_propose_canonical_record(
        peer_name, record_type, name,
        proposed_fields=proposed_fields,
        source=source,
        config=config,
        self_name=self_name,
    )
    status = proposal.get("status") if isinstance(proposal, dict) else None

    # 409 race — re-GET and return the fresh record.
    if status == "exists":
        fresh = await peer_get_canonical_record(
            peer_name, record_type, name,
            config=config, self_name=self_name,
            correlation_id=proposal.get("correlation_id"),
        )
        if fresh is not None:
            return {"status": "found", **fresh}
        # The 409 said "exists" but the re-GET 404'd — fall through to
        # pending so the caller doesn't hang on a phantom record.

    log.info(
        "transport.client.canonical_propose_pending",
        peer=peer_name,
        record_type=record_type,
        name=name,
        correlation_id=proposal.get("correlation_id") if isinstance(proposal, dict) else None,
    )
    return {
        "status": "pending",
        "correlation_id": proposal.get("correlation_id") if isinstance(proposal, dict) else None,
    }


async def resolve_or_propose_canonical_person(
    peer_name: str,
    name: str,
    *,
    proposed_fields: dict[str, Any] | None = None,
    source: str = "",
    config: "TransportConfig | None" = None,
    self_name: str = "kal-le",
) -> dict[str, Any]:
    """High-level helper — GET, then propose on 404, handle the 409 race.

    Backwards-compat wrapper around
    :func:`resolve_or_propose_canonical_record` pinned to
    ``record_type="person"``. The ``alfred transport propose-person``
    CLI subcommand still calls this directly; KAL-LE's interactive
    propose-person flow is unchanged.
    """
    return await resolve_or_propose_canonical_record(
        peer_name,
        "person",
        name,
        proposed_fields=proposed_fields,
        source=source,
        config=config,
        self_name=self_name,
    )


async def peer_send_brief_digest(
    peer_name: str,
    *,
    digest_markdown: str,
    digest_date: str,
    self_name: str,
    config: "TransportConfig | None" = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """POST /peer/brief_digest on the named peer (V.E.R.A. content arc).

    Args:
        peer_name: Outbound peer key (typically ``"salem"`` from
            KAL-LE's perspective). Used to look up base_url + token in
            the caller's ``transport.peers`` config block.
        digest_markdown: The one-slide rendered digest body. The
            principal's brief renderer uses this verbatim under
            ``### {Sender} Update``.
        digest_date: ISO date string (typically today's local date on
            the sender). The principal stores the digest under that
            date and matches it when rendering today's brief.
        self_name: This instance's identity (``"kal-le"``,
            ``"stay-c"``, etc.). Goes into the body's ``peer`` field
            so the principal's anti-spoof check passes.
        config: Pre-loaded TransportConfig. Production callers supply
            it; tests can monkey-patch the request layer instead.
        correlation_id: Optional caller-supplied id for tracing. The
            sender passes ``"{self_name}-brief-{date}"`` by convention
            so a re-fire on the same day is observable as a retry.

    Returns:
        Server's response dict — ``{"status": "accepted", "path": str,
        "correlation_id": str}`` on the happy path. Caller logs and
        moves on — failures should NOT be retried inline (the
        principal's brief tolerates a missing digest via the
        intentionally-left-blank fallback).
    """
    from .config import TransportConfig, load_config
    from .peers import _resolve_peer

    if config is None:
        config = load_config()
    base_url, token = _resolve_peer(config, peer_name)
    cid = correlation_id or f"{self_name}-brief-{digest_date}"

    body: dict[str, Any] = {
        "peer": self_name,
        "date": digest_date,
        "digest_markdown": digest_markdown,
        "correlation_id": cid,
    }
    return await _peer_request(
        base_url=base_url,
        token=token,
        method="POST",
        path="/peer/brief_digest",
        self_name=self_name,
        correlation_id=cid,
        json_body=body,
    )


# ---------------------------------------------------------------------------
# Pending Items Queue — peer ↔ Salem (Phase 1)
# ---------------------------------------------------------------------------


async def peer_push_pending_items(
    peer_name: str,
    *,
    items: list[dict[str, Any]],
    self_name: str,
    config: "TransportConfig | None" = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """POST /peer/pending_items_push on the named peer (peer → Salem).

    First-direction call for the Pending Items Queue Phase 1. Each
    non-Salem instance flushes its local queue to Salem so Salem's
    Daily Sync can surface a cross-instance aggregate.

    Args:
        peer_name: Outbound peer key (typically ``"salem"``).
        items: List of pending-item dicts (already serialized via
            :meth:`PendingItem.to_dict`). The Salem handler appends
            new ids and silently drops duplicates by id (idempotent).
        self_name: This instance's identity. Goes into the body's
            ``from_instance`` field so Salem's anti-spoof check passes.
        config: Pre-loaded TransportConfig.
        correlation_id: Optional tracing id. Defaults to a
            ``"{self_name}-pending-{count}-{hex}"`` form.

    Returns:
        Server's response dict —
        ``{"received": <count>, "errors": [...]}`` on the happy path.
    """
    import secrets

    from .config import TransportConfig, load_config
    from .peers import _resolve_peer

    if config is None:
        config = load_config()
    base_url, token = _resolve_peer(config, peer_name)
    cid = (
        correlation_id
        or f"{self_name}-pending-{len(items)}-{secrets.token_hex(3)}"
    )

    body: dict[str, Any] = {
        "from_instance": self_name,
        "items": list(items),
        "correlation_id": cid,
    }
    return await _peer_request(
        base_url=base_url,
        token=token,
        method="POST",
        path="/peer/pending_items_push",
        self_name=self_name,
        correlation_id=cid,
        json_body=body,
    )


async def peer_resolve_pending_item(
    peer_name: str,
    *,
    item_id: str,
    resolution: str,
    self_name: str = "salem",
    resolved_at: str | None = None,
    config: "TransportConfig | None" = None,
    correlation_id: str | None = None,
) -> dict[str, Any]:
    """POST /peer/pending_items_resolve on the named peer (Salem → peer).

    First Salem→peer consumer on the transport substrate. Existing
    consumers (brief_digest_push, propose-person) are all peer→Salem.

    Args:
        peer_name: Outbound peer key (the originating instance from
            the Pending Item's ``created_by_instance`` field).
        item_id: The Pending Item's UUID — looked up in the peer's
            local JSONL queue.
        resolution: The chosen resolution_option's id (``"noted"``,
            ``"show_me"``, etc.).
        self_name: Salem's identity (default ``"salem"``).
        resolved_at: Optional caller-supplied iso8601 timestamp;
            defaults to "now" on the receiving side.
        config: Pre-loaded TransportConfig.
        correlation_id: Optional tracing id.

    Returns:
        Server's response dict — ``{"executed": <bool>, "summary":
        "...", "error": "..."}``. The caller (Salem's Daily Sync
        dispatcher) surfaces the summary text back to Andrew.
    """
    import secrets

    from .config import TransportConfig, load_config
    from .peers import _resolve_peer

    if config is None:
        config = load_config()
    base_url, token = _resolve_peer(config, peer_name)
    cid = (
        correlation_id
        or f"salem-resolve-pending-{item_id[:8]}-{secrets.token_hex(2)}"
    )

    body: dict[str, Any] = {
        "item_id": item_id,
        "resolution": resolution,
        "correlation_id": cid,
    }
    if resolved_at:
        body["resolved_at"] = resolved_at
    return await _peer_request(
        base_url=base_url,
        token=token,
        method="POST",
        path="/peer/pending_items_resolve",
        self_name=self_name,
        correlation_id=cid,
        json_body=body,
    )
