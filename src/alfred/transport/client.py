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
