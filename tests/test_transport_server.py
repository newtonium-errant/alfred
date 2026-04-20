"""Tests for ``alfred.transport.server``.

Uses aiohttp's ``AiohttpClient`` test fixture to spin up the real
``Application`` and exercise it end-to-end — auth middleware, route
dispatch, dedupe, scheduled-vs-immediate branching, 501 stubs, 503
when telegram isn't wired.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    SchedulerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.server import build_app, register_send_callable
from alfred.transport.state import TransportState


# Fake bearer token used across every test — the obviously-fake pattern
# per builder.md's GitGuardian post-mortem. Never a real ``sk-``/``gsk-``
# prefix in test fixtures.
DUMMY_TRANSPORT_TEST_TOKEN = "DUMMY_TRANSPORT_TEST_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234"


def _build_config(
    *,
    allowed_clients: list[str] | None = None,
    extra_peers: dict[str, AuthTokenEntry] | None = None,
) -> TransportConfig:
    tokens: dict[str, AuthTokenEntry] = {
        "local": AuthTokenEntry(
            token=DUMMY_TRANSPORT_TEST_TOKEN,
            allowed_clients=allowed_clients or ["scheduler", "brief", "janitor"],
        ),
    }
    if extra_peers:
        tokens.update(extra_peers)
    return TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens=tokens),
        state=StateConfig(),
    )


def _build_state(tmp_path: Path) -> TransportState:
    return TransportState.create(tmp_path / "transport_state.json")


@pytest.fixture
async def client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Client wired with default config + working send stub."""
    config = _build_config()
    state = _build_state(tmp_path)

    sent: list[dict[str, Any]] = []

    async def _send_stub(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        sent.append({"user_id": user_id, "text": text, "dedupe_key": dedupe_key})
        return [1000 + len(sent)]

    app = build_app(config, state, send_fn=_send_stub)
    app["_test_sent"] = sent
    app["_test_state"] = state
    tc: TestClient = await aiohttp_client(app)
    return tc


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


async def test_health_is_public(client):  # type: ignore[no-untyped-def]
    """/health does not require auth — it's the bootstrap probe."""
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["telegram_connected"] is True
    assert body["queue_depth"] == 0


async def test_send_rejects_missing_auth_header(client):  # type: ignore[no-untyped-def]
    resp = await client.post("/outbound/send", json={"user_id": 1, "text": "hi"})
    assert resp.status == 401


async def test_send_rejects_wrong_token(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": "Bearer wrong-token",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "invalid_token"


async def test_send_rejects_client_not_in_allowlist(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "unregistered_client",
        },
    )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "client_not_allowed"
    assert body["peer"] == "local"


# ---------------------------------------------------------------------------
# Outbound send — immediate
# ---------------------------------------------------------------------------


async def test_send_immediate_delivers_via_send_fn(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={"user_id": 123, "text": "Reminder: call Dr Bailey"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "sent"
    assert body["telegram_message_id"] == 1001

    # The stub recorded the send.
    sent = client.app["_test_sent"]
    assert len(sent) == 1
    assert sent[0]["user_id"] == 123
    assert sent[0]["text"] == "Reminder: call Dr Bailey"

    # State persisted the send_log entry.
    state: TransportState = client.app["_test_state"]
    assert len(state.send_log) == 1
    assert state.send_log[0]["user_id"] == 123
    assert state.send_log[0]["telegram_message_ids"] == [1001]


async def test_send_rejects_missing_fields(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={"user_id": "not-an-int", "text": ""},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert "user_id" in body["error"]


async def test_send_with_scheduled_at_queues_instead_of_dispatching(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={
            "user_id": 123,
            "text": "Future reminder",
            "scheduled_at": "2099-01-01T00:00:00+00:00",
            "dedupe_key": "reminder-future",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "scheduled"
    assert body["id"]
    # No immediate dispatch happened.
    assert client.app["_test_sent"] == []
    # Entry now lives in pending_queue.
    state: TransportState = client.app["_test_state"]
    assert len(state.pending_queue) == 1


async def test_send_dedupe_returns_same_entry(client):  # type: ignore[no-untyped-def]
    """A send with a matching dedupe_key in the 24h window returns ``duplicate``."""
    first = await client.post(
        "/outbound/send",
        json={
            "user_id": 123,
            "text": "Only once",
            "dedupe_key": "brief-2026-04-20",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "brief",
        },
    )
    assert first.status == 200

    second = await client.post(
        "/outbound/send",
        json={
            "user_id": 123,
            "text": "Only once (retry)",
            "dedupe_key": "brief-2026-04-20",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "brief",
        },
    )
    assert second.status == 200
    body = await second.json()
    assert body["status"] == "duplicate"

    # send_fn was only called once.
    assert len(client.app["_test_sent"]) == 1


# ---------------------------------------------------------------------------
# Outbound send_batch
# ---------------------------------------------------------------------------


async def test_send_batch_delivers_each_chunk_in_order(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send_batch",
        json={
            "user_id": 123,
            "chunks": ["part 1", "part 2", "part 3"],
            "dedupe_key": "brief-2026-04-20",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "brief",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["sent_count"] == 3
    assert len(body["telegram_message_ids"]) == 3
    # send_fn called once per chunk, in order.
    sent = client.app["_test_sent"]
    assert [s["text"] for s in sent] == ["part 1", "part 2", "part 3"]


async def test_send_batch_rejects_empty_chunks(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send_batch",
        json={"user_id": 123, "chunks": []},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "brief",
        },
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# Status lookup
# ---------------------------------------------------------------------------


async def test_status_returns_sent_entry(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={"user_id": 123, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    body = await resp.json()
    entry_id = body["id"]

    lookup = await client.get(
        f"/outbound/status/{entry_id}",
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert lookup.status == 200
    lb = await lookup.json()
    assert lb["status"] == "sent"
    assert lb["telegram_message_ids"]


async def test_status_unknown_returns_404(client):  # type: ignore[no-untyped-def]
    resp = await client.get(
        "/outbound/status/never-existed",
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 404


# ---------------------------------------------------------------------------
# 501 stubs — Stage 3.5 pre-commit
# ---------------------------------------------------------------------------


async def test_peer_routes_return_501(client):  # type: ignore[no-untyped-def]
    """The /peer/* routes are registered but return 501 today.

    This is load-bearing for the Stage 3.5 dovetail — swapping the
    stub registrar for a real handler is a one-line change.
    """
    for path in ("/peer/send", "/peer/query", "/peer/handshake"):
        resp = await client.post(
            path,
            json={},
            headers={
                "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
                "X-Alfred-Client": "scheduler",
            },
        )
        assert resp.status == 501, f"{path} should be 501"
        body = await resp.json()
        assert body["reason"] == "peer_not_implemented"


async def test_canonical_routes_return_501(client):  # type: ignore[no-untyped-def]
    resp = await client.get(
        "/canonical/person/Andrew Newton",
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 501
    body = await resp.json()
    assert body["reason"] == "peer_not_implemented"


# ---------------------------------------------------------------------------
# 503 when Telegram isn't configured
# ---------------------------------------------------------------------------


async def test_send_returns_503_when_no_callable_registered(
    aiohttp_client, tmp_path,  # type: ignore[no-untyped-def]
):
    """An unregistered send callable yields 503 telegram_not_configured."""
    config = _build_config()
    state = _build_state(tmp_path)
    app = build_app(config, state, send_fn=None)
    tc: TestClient = await aiohttp_client(app)

    resp = await tc.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 503
    body = await resp.json()
    assert body["reason"] == "telegram_not_configured"

    # /health still works and reports telegram_connected: False.
    health = await tc.get("/health")
    hb = await health.json()
    assert hb["telegram_connected"] is False


async def test_register_send_callable_enables_delivery(
    aiohttp_client, tmp_path,  # type: ignore[no-untyped-def]
):
    """``register_send_callable`` swaps a live callable onto a built app."""
    config = _build_config()
    state = _build_state(tmp_path)
    app = build_app(config, state, send_fn=None)
    # Register after build — same path the talker daemon uses.
    async def _stub(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        return [555]

    register_send_callable(app, _stub)
    tc: TestClient = await aiohttp_client(app)
    resp = await tc.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Multi-peer token dict — Stage 3.5 D2/D7 pre-commit sanity
# ---------------------------------------------------------------------------


async def test_multiple_peer_tokens_each_authenticate_independently(
    aiohttp_client, tmp_path,  # type: ignore[no-untyped-def]
):
    """Two entries in auth.tokens each authenticate with their own secret.

    This is the Stage 3.5 D2 pre-commit contract: the schema already
    supports per-peer tokens; adding a second peer (``kal-le``) is a
    config-only change.
    """
    kal_token = "DUMMY_KAL_LE_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_01234567890123"
    config = _build_config(
        extra_peers={
            "kal-le": AuthTokenEntry(
                token=kal_token,
                allowed_clients=["kal-le-router"],
            ),
        },
    )
    state = _build_state(tmp_path)

    async def _stub(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        return [777]

    app = build_app(config, state, send_fn=_stub)
    tc: TestClient = await aiohttp_client(app)

    # Local token with local client works.
    ok_local = await tc.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert ok_local.status == 200

    # Kal-le's token is accepted — its allowed_clients includes
    # kal-le-router.
    ok_kal = await tc.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi-kal"},
        headers={
            "Authorization": f"Bearer {kal_token}",
            "X-Alfred-Client": "kal-le-router",
        },
    )
    assert ok_kal.status == 200

    # Kal-le's token with a local client fails (client not allowed
    # under that peer's allowlist).
    bad = await tc.post(
        "/outbound/send",
        json={"user_id": 1, "text": "mismatch"},
        headers={
            "Authorization": f"Bearer {kal_token}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert bad.status == 401
