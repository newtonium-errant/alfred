"""Tests for the c4 client peer dispatch + correlation-id inbox."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from alfred.transport import peers as peers_module
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    PeerEntry,
    ServerConfig,
    SchedulerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.exceptions import TransportError


DUMMY_KALLE_PEER_TOKEN = "DUMMY_KALLE_PEER_TEST_TOKEN_PLACEHOLDER_0123456789"


def _config_with_kalle() -> TransportConfig:
    return TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={}),
        state=StateConfig(),
        peers={
            "kal-le": PeerEntry(
                base_url="http://127.0.0.1:8892",
                token=DUMMY_KALLE_PEER_TOKEN,
            ),
        },
    )


# ---------------------------------------------------------------------------
# _resolve_peer
# ---------------------------------------------------------------------------


def test_resolve_peer_returns_base_and_token():
    config = _config_with_kalle()
    base, token = peers_module._resolve_peer(config, "kal-le")
    assert base == "http://127.0.0.1:8892"
    assert token == DUMMY_KALLE_PEER_TOKEN


def test_resolve_peer_unknown_raises():
    config = _config_with_kalle()
    with pytest.raises(TransportError) as exc_info:
        peers_module._resolve_peer(config, "stay-c")
    assert "unknown peer" in str(exc_info.value)


def test_resolve_peer_empty_token_raises():
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={}),
        state=StateConfig(),
        peers={
            "kal-le": PeerEntry(
                base_url="http://127.0.0.1:8892",
                token="",
            ),
        },
    )
    with pytest.raises(TransportError) as exc_info:
        peers_module._resolve_peer(config, "kal-le")
    assert "no token configured" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Response-inbox + correlation-id round-trip
# ---------------------------------------------------------------------------


def _clear_inbox() -> None:
    """Reset inbox globals between tests. The module-level dicts leak
    across tests otherwise, so each test zeroes them first.
    """
    peers_module._INBOX.clear()
    peers_module._ORPHANS.clear()


async def test_register_response_delivers_to_waiter():
    _clear_inbox()

    async def _waiter() -> dict[str, Any]:
        return await peers_module.await_response("cid-1", timeout=2.0)

    waiter_task = asyncio.create_task(_waiter())
    # Let waiter register.
    await asyncio.sleep(0.05)
    delivered = peers_module.register_response(
        "cid-1", {"status": "ok", "reply": "hello"},
    )
    assert delivered is True

    reply = await waiter_task
    assert reply["status"] == "ok"
    assert reply["reply"] == "hello"


async def test_await_response_times_out():
    _clear_inbox()
    with pytest.raises(asyncio.TimeoutError):
        await peers_module.await_response("never-arrives", timeout=0.1)


async def test_orphan_reply_collected_by_late_waiter():
    """A reply that arrives BEFORE anyone waits is picked up by a later waiter."""
    _clear_inbox()
    # Reply shows up first.
    peers_module.register_response("cid-early", {"ok": True})

    # Waiter arrives later — should still get the reply via the orphan
    # buffer without hitting timeout.
    reply = await peers_module.await_response("cid-early", timeout=0.5)
    assert reply == {"ok": True}


async def test_orphan_buffer_prunes_old_entries(monkeypatch):
    """Orphans older than TTL get dropped on the next prune pass."""
    _clear_inbox()
    # Park an orphan.
    peers_module.register_response("cid-stale", {"value": 1})
    assert "cid-stale" in peers_module._ORPHANS

    # Fast-forward time past the TTL.
    import time as real_time
    base = real_time.monotonic()
    monkeypatch.setattr(
        peers_module.time,
        "monotonic",
        lambda: base + peers_module._ORPHAN_TTL_SECONDS + 1,
    )
    peers_module._prune_orphans()
    assert "cid-stale" not in peers_module._ORPHANS


def test_inbox_stats_shape():
    _clear_inbox()
    stats = peers_module.inbox_stats()
    assert stats == {"pending_waiters": 0, "orphan_replies": 0}

    peers_module.register_response("cid-a", {"x": 1})
    stats = peers_module.inbox_stats()
    assert stats["orphan_replies"] == 1


# ---------------------------------------------------------------------------
# peer_send end-to-end against a live test server
# ---------------------------------------------------------------------------


@pytest.fixture
async def kalle_stub(aiohttp_server, tmp_path):  # type: ignore[no-untyped-def]
    """Spin up a minimal KAL-LE-shaped server on an ephemeral port.

    Only implements the /peer/* endpoints the client exercises — no
    permission checks, no audit, just happy-path echoes. Returns a
    tuple of (base_url, received_list) so tests can assert on what
    was received.
    """
    from aiohttp import web

    received: list[dict[str, Any]] = []

    async def _send(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return web.json_response({"reason": "missing_bearer"}, status=401)
        body = await request.json()
        received.append({"path": "/peer/send", "body": body,
                         "headers": dict(request.headers)})
        return web.json_response({
            "status": "accepted",
            "correlation_id": body.get("correlation_id"),
            "echo_kind": body.get("kind"),
        })

    async def _handshake(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        received.append({"path": "/peer/handshake", "body": body,
                         "headers": dict(request.headers)})
        return web.json_response({
            "instance": "KAL-LE-stub",
            "protocol_version": 1,
            "capabilities": ["bash_exec"],
            "peers": [],
            "correlation_id": request.headers.get("X-Correlation-Id", ""),
        })

    async def _query(request: web.Request) -> web.Response:
        body = await request.json()
        received.append({"path": "/peer/query", "body": body,
                         "headers": dict(request.headers)})
        return web.json_response({
            "type": body["record_type"],
            "name": body["name"],
            "frontmatter": {"name": "Andrew Newton"},
            "granted": ["name"],
            "correlation_id": body.get("correlation_id"),
        })

    app = web.Application()
    app.router.add_post("/peer/send", _send)
    app.router.add_post("/peer/handshake", _handshake)
    app.router.add_post("/peer/query", _query)
    server = await aiohttp_server(app)
    return f"http://{server.host}:{server.port}", received


async def test_peer_send_dispatches_with_correct_headers(kalle_stub):  # type: ignore[no-untyped-def]
    from alfred.transport.client import peer_send

    base_url, received = kalle_stub
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={}),
        state=StateConfig(),
        peers={
            "kal-le": PeerEntry(
                base_url=base_url,
                token=DUMMY_KALLE_PEER_TOKEN,
            ),
        },
    )

    resp = await peer_send(
        "kal-le",
        kind="message",
        payload={"text": "hello from salem"},
        config=config,
        self_name="salem",
        correlation_id="cid-xyz-42",
    )
    assert resp["status"] == "accepted"
    assert resp["correlation_id"] == "cid-xyz-42"
    assert resp["echo_kind"] == "message"

    # Server saw the right headers + body.
    assert len(received) == 1
    entry = received[0]
    assert entry["path"] == "/peer/send"
    assert entry["headers"]["Authorization"] == f"Bearer {DUMMY_KALLE_PEER_TOKEN}"
    assert entry["headers"]["X-Alfred-Client"] == "salem"
    assert entry["headers"]["X-Correlation-Id"] == "cid-xyz-42"
    assert entry["body"]["kind"] == "message"
    assert entry["body"]["from"] == "salem"
    assert entry["body"]["correlation_id"] == "cid-xyz-42"


async def test_peer_send_generates_correlation_id_when_absent(kalle_stub):  # type: ignore[no-untyped-def]
    from alfred.transport.client import peer_send

    base_url, received = kalle_stub
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={}),
        state=StateConfig(),
        peers={
            "kal-le": PeerEntry(base_url=base_url, token=DUMMY_KALLE_PEER_TOKEN),
        },
    )
    resp = await peer_send(
        "kal-le", kind="message", payload={"text": "yo"},
        config=config, self_name="salem",
    )
    cid = resp["correlation_id"]
    # 16 hex chars per the plan.
    assert len(cid) == 16
    assert all(c in "0123456789abcdef" for c in cid)


async def test_peer_handshake_round_trip(kalle_stub):  # type: ignore[no-untyped-def]
    from alfred.transport.client import peer_handshake

    base_url, received = kalle_stub
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={}),
        state=StateConfig(),
        peers={
            "kal-le": PeerEntry(base_url=base_url, token=DUMMY_KALLE_PEER_TOKEN),
        },
    )
    resp = await peer_handshake("kal-le", config=config, self_name="salem")
    assert resp["instance"] == "KAL-LE-stub"
    assert "bash_exec" in resp["capabilities"]


async def test_peer_query_passes_fields(kalle_stub):  # type: ignore[no-untyped-def]
    from alfred.transport.client import peer_query

    base_url, received = kalle_stub
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={}),
        state=StateConfig(),
        peers={
            "kal-le": PeerEntry(base_url=base_url, token=DUMMY_KALLE_PEER_TOKEN),
        },
    )
    resp = await peer_query(
        "kal-le",
        record_type="person",
        name="Andrew Newton",
        fields=["name", "email"],
        config=config,
        self_name="salem",
    )
    assert resp["frontmatter"]["name"] == "Andrew Newton"
    # Body carried the requested fields.
    assert received[-1]["body"]["fields"] == ["name", "email"]


# ---------------------------------------------------------------------------
# Retry behaviour — 5xx triggers one retry
# ---------------------------------------------------------------------------


async def test_peer_retries_once_on_5xx(aiohttp_server):  # type: ignore[no-untyped-def]
    from aiohttp import web

    from alfred.transport.client import peer_send

    call_count = {"n": 0}

    async def _flaky(request: web.Request) -> web.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return web.json_response({"reason": "server_error"}, status=500)
        body = await request.json()
        return web.json_response({"status": "accepted", "correlation_id": body.get("correlation_id")})

    app = web.Application()
    app.router.add_post("/peer/send", _flaky)
    server = await aiohttp_server(app)

    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={}),
        state=StateConfig(),
        peers={
            "kal-le": PeerEntry(
                base_url=f"http://{server.host}:{server.port}",
                token=DUMMY_KALLE_PEER_TOKEN,
            ),
        },
    )
    resp = await peer_send(
        "kal-le", kind="message", payload={"text": "retry me"},
        config=config, self_name="salem",
    )
    assert resp["status"] == "accepted"
    assert call_count["n"] >= 2, "Expected at least one retry after 5xx"


async def test_peer_does_not_retry_on_4xx(aiohttp_server):  # type: ignore[no-untyped-def]
    from aiohttp import web

    from alfred.transport.client import peer_send
    from alfred.transport.exceptions import TransportRejected

    call_count = {"n": 0}

    async def _bad(request: web.Request) -> web.Response:
        call_count["n"] += 1
        return web.json_response({"reason": "schema_error"}, status=400)

    app = web.Application()
    app.router.add_post("/peer/send", _bad)
    server = await aiohttp_server(app)

    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={}),
        state=StateConfig(),
        peers={
            "kal-le": PeerEntry(
                base_url=f"http://{server.host}:{server.port}",
                token=DUMMY_KALLE_PEER_TOKEN,
            ),
        },
    )
    with pytest.raises(TransportRejected):
        await peer_send(
            "kal-le", kind="message", payload={},
            config=config, self_name="salem",
        )
    assert call_count["n"] == 1, "4xx must never retry"
