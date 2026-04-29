"""Tests for the Pending Items Queue peer endpoints.

Covers:
* ``POST /peer/pending_items_push`` (peer → Salem) — accepts a flush,
  appends to the aggregate JSONL, idempotent by item.id.
* ``POST /peer/pending_items_resolve`` (Salem → peer) — dispatches a
  resolution to the registered resolver callable, surfaces the
  result.
* Auth + anti-spoof: bearer required, ``from_instance`` must match
  authenticated peer, missing resolver → 501.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    PeerEntry,
    SchedulerConfig,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import (
    register_pending_items_aggregate_path,
    register_pending_items_resolve_callable,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState


DUMMY_SALEM_LOCAL_TOKEN = "DUMMY_SALEM_LOCAL_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"
DUMMY_HYPATIA_PEER_TOKEN = "DUMMY_HYPATIA_PEER_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"


def _build_salem_config() -> TransportConfig:
    tokens = {
        "local": AuthTokenEntry(
            token=DUMMY_SALEM_LOCAL_TOKEN,
            allowed_clients=["scheduler", "brief", "talker"],
        ),
        "hypatia": AuthTokenEntry(
            token=DUMMY_HYPATIA_PEER_TOKEN,
            allowed_clients=["hypatia"],
        ),
    }
    return TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens=tokens),
        state=StateConfig(),
        canonical=CanonicalConfig(owner=True),
        peers={
            "hypatia": PeerEntry(
                base_url="http://127.0.0.1:8893",
                token=DUMMY_HYPATIA_PEER_TOKEN,
            ),
        },
    )


@pytest.fixture
async def salem_push_app(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Salem-style app with the pending-items aggregate path registered."""
    aggregate_path = tmp_path / "pending_items_aggregate.jsonl"
    config = _build_salem_config()
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)
    register_pending_items_aggregate_path(app, str(aggregate_path))
    app["_aggregate_path"] = aggregate_path
    tc: TestClient = await aiohttp_client(app)
    return tc


@pytest.fixture
async def hypatia_resolve_app(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Hypatia-side app with a stubbed resolver callable.

    Salem dispatches to /peer/pending_items_resolve; Hypatia's app
    runs the resolver against its local queue. The fixture stubs the
    resolver so we can assert dispatch shape without setting up a
    full pending_items config.
    """
    config = _build_salem_config()  # symmetric auth in tests
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)

    calls: list[dict[str, Any]] = []

    async def _resolver(*, item_id, resolution, resolved_at, correlation_id):
        calls.append({
            "item_id": item_id,
            "resolution": resolution,
            "resolved_at": resolved_at,
            "correlation_id": correlation_id,
        })
        return {
            "ok": True,
            "executed": True,
            "summary": "stub-noop-resolved",
            "error": None,
            "item_id": item_id,
            "resolution": resolution,
        }

    register_pending_items_resolve_callable(app, _resolver)
    app["_resolver_calls"] = calls
    tc: TestClient = await aiohttp_client(app)
    return tc


# ---------------------------------------------------------------------------
# /peer/pending_items_push
# ---------------------------------------------------------------------------


async def test_push_happy_path_appends_to_aggregate(salem_push_app):  # type: ignore[no-untyped-def]
    items = [
        {
            "id": "item-1",
            "category": "outbound_failure",
            "created_at": "2026-04-28T16:00:00+00:00",
            "created_by_instance": "hypatia",
            "session_id": "abc",
            "context": "first failure",
            "resolution_options": [],
            "status": "pending",
        },
        {
            "id": "item-2",
            "category": "outbound_failure",
            "created_at": "2026-04-28T16:01:00+00:00",
            "created_by_instance": "hypatia",
            "session_id": "abc",
            "context": "second failure",
            "resolution_options": [],
            "status": "pending",
        },
    ]
    resp = await salem_push_app.post(
        "/peer/pending_items_push",
        json={
            "from_instance": "hypatia",
            "items": items,
            "correlation_id": "hypatia-pending-2-test",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_HYPATIA_PEER_TOKEN}",
            "X-Alfred-Client": "hypatia",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["received"] == 2
    assert body["errors"] == []

    aggregate_path: Path = salem_push_app.server.app["_aggregate_path"]
    rows = aggregate_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(rows) == 2
    parsed = [json.loads(r) for r in rows]
    assert {p["id"] for p in parsed} == {"item-1", "item-2"}


async def test_push_idempotent_on_repeat_id(salem_push_app):  # type: ignore[no-untyped-def]
    """A second push with the same id is dropped silently — count still received."""
    item = {
        "id": "dup-id",
        "category": "outbound_failure",
        "created_at": "2026-04-28T16:00:00+00:00",
        "created_by_instance": "hypatia",
        "context": "dup test",
        "resolution_options": [],
        "status": "pending",
    }
    headers = {
        "Authorization": f"Bearer {DUMMY_HYPATIA_PEER_TOKEN}",
        "X-Alfred-Client": "hypatia",
    }
    r1 = await salem_push_app.post(
        "/peer/pending_items_push",
        json={"from_instance": "hypatia", "items": [item]},
        headers=headers,
    )
    assert r1.status == 200
    r2 = await salem_push_app.post(
        "/peer/pending_items_push",
        json={"from_instance": "hypatia", "items": [item]},
        headers=headers,
    )
    assert r2.status == 200
    body = await r2.json()
    # Idempotent — Salem says "received" but doesn't write a duplicate.
    assert body["received"] == 1

    aggregate_path: Path = salem_push_app.server.app["_aggregate_path"]
    rows = aggregate_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(rows) == 1


async def test_push_anti_spoof_rejects_403(salem_push_app):  # type: ignore[no-untyped-def]
    """body.from_instance must equal authenticated peer."""
    resp = await salem_push_app.post(
        "/peer/pending_items_push",
        json={
            "from_instance": "kal-le",  # Lying — auth is hypatia
            "items": [{"id": "x", "category": "outbound_failure"}],
        },
        headers={
            "Authorization": f"Bearer {DUMMY_HYPATIA_PEER_TOKEN}",
            "X-Alfred-Client": "hypatia",
        },
    )
    assert resp.status == 403
    body = await resp.json()
    assert body["reason"] == "from_mismatch"


async def test_push_normalizes_created_by_instance(salem_push_app):  # type: ignore[no-untyped-def]
    """Auth-trusted peer overrides item.created_by_instance for tamper-proof attribution."""
    item = {
        "id": "normalize-test",
        "category": "outbound_failure",
        "created_by_instance": "kal-le",  # buggy or hostile
        "created_at": "2026-04-28T16:00:00+00:00",
        "context": "normalize",
        "resolution_options": [],
        "status": "pending",
    }
    resp = await salem_push_app.post(
        "/peer/pending_items_push",
        json={"from_instance": "hypatia", "items": [item]},
        headers={
            "Authorization": f"Bearer {DUMMY_HYPATIA_PEER_TOKEN}",
            "X-Alfred-Client": "hypatia",
        },
    )
    assert resp.status == 200
    aggregate_path: Path = salem_push_app.server.app["_aggregate_path"]
    rows = aggregate_path.read_text(encoding="utf-8").strip().split("\n")
    parsed = json.loads(rows[0])
    # Stamped with the authenticated peer, not the body's claim.
    assert parsed["created_by_instance"] == "hypatia"


async def test_push_missing_bearer_401(salem_push_app):  # type: ignore[no-untyped-def]
    resp = await salem_push_app.post(
        "/peer/pending_items_push",
        json={"from_instance": "hypatia", "items": []},
        headers={"X-Alfred-Client": "hypatia"},
    )
    assert resp.status == 401


async def test_push_oversize_400(salem_push_app):  # type: ignore[no-untyped-def]
    """A push of >100 items is rejected to protect Salem."""
    items = [{"id": f"id-{i}"} for i in range(150)]
    resp = await salem_push_app.post(
        "/peer/pending_items_push",
        json={"from_instance": "hypatia", "items": items},
        headers={
            "Authorization": f"Bearer {DUMMY_HYPATIA_PEER_TOKEN}",
            "X-Alfred-Client": "hypatia",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"
    assert "per-push cap" in body["detail"]


# ---------------------------------------------------------------------------
# /peer/pending_items_resolve
# ---------------------------------------------------------------------------


async def test_resolve_happy_path_calls_resolver(hypatia_resolve_app):  # type: ignore[no-untyped-def]
    resp = await hypatia_resolve_app.post(
        "/peer/pending_items_resolve",
        json={
            "item_id": "item-uuid",
            "resolution": "noted",
            "correlation_id": "salem-resolve-test",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_HYPATIA_PEER_TOKEN}",
            "X-Alfred-Client": "hypatia",
        },
    )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["executed"] is True
    assert body["summary"] == "stub-noop-resolved"
    calls = hypatia_resolve_app.server.app["_resolver_calls"]
    assert len(calls) == 1
    assert calls[0]["item_id"] == "item-uuid"
    assert calls[0]["resolution"] == "noted"


async def test_resolve_missing_resolver_returns_501(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """When no resolver callable is registered, the endpoint returns 501."""
    config = _build_salem_config()
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)
    # Deliberately do NOT register the resolver.
    tc: TestClient = await aiohttp_client(app)

    resp = await tc.post(
        "/peer/pending_items_resolve",
        json={"item_id": "x", "resolution": "noted"},
        headers={
            "Authorization": f"Bearer {DUMMY_HYPATIA_PEER_TOKEN}",
            "X-Alfred-Client": "hypatia",
        },
    )
    assert resp.status == 501
    body = await resp.json()
    assert body["reason"] == "pending_resolver_not_configured"


async def test_resolve_schema_validation(hypatia_resolve_app):  # type: ignore[no-untyped-def]
    resp = await hypatia_resolve_app.post(
        "/peer/pending_items_resolve",
        json={"item_id": "", "resolution": "noted"},
        headers={
            "Authorization": f"Bearer {DUMMY_HYPATIA_PEER_TOKEN}",
            "X-Alfred-Client": "hypatia",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"
