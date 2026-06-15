"""Ticket pipeline c7 — /peer/ticket_outcome handler (VERA-side receiver).

The HTTP boundary of the KAL-LE→VERA outcome write-back. Mirrors the
/peer/pending_items_resolve handler tests: schema gates, 501 when the
resolver isn't wired, the resolver-callable dispatch shape, the 404
not-found contract, and the log-emission pins.
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog
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
    register_ticket_outcome_resolver_callable,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState


DUMMY_VERA_LOCAL_TOKEN = "DUMMY_VERA_LOCAL_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"
DUMMY_KALLE_PEER_TOKEN = "DUMMY_KALLE_PEER_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"


def _build_vera_config() -> TransportConfig:
    """VERA-style config: KAL-LE is an inbound peer (allowed_clients
    [kal-le]) — the auth shape that lets KAL-LE reach /peer/ticket_outcome."""
    tokens = {
        "local": AuthTokenEntry(
            token=DUMMY_VERA_LOCAL_TOKEN,
            allowed_clients=["brief_digest_push"],
        ),
        "kalle": AuthTokenEntry(
            token=DUMMY_KALLE_PEER_TOKEN,
            allowed_clients=["kal-le"],
        ),
    }
    return TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens=tokens),
        state=StateConfig(),
        canonical=CanonicalConfig(owner=False),
        peers={
            "kalle": PeerEntry(
                base_url="http://127.0.0.1:8892",
                token=DUMMY_KALLE_PEER_TOKEN,
            ),
        },
    )


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
        "X-Alfred-Client": "kal-le",
    }


@pytest.fixture
async def vera_outcome_app(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """VERA-side app with a stubbed ticket-outcome resolver callable."""
    config = _build_vera_config()
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)

    calls: list[dict[str, Any]] = []

    async def _resolver(
        *, ticket_uid, status, disposition, pr_number, resolved_at,
        correlation_id,
    ):
        calls.append({
            "ticket_uid": ticket_uid,
            "status": status,
            "disposition": disposition,
            "pr_number": pr_number,
            "resolved_at": resolved_at,
            "correlation_id": correlation_id,
        })
        # Default stub: ticket found + applied.
        return {"found": True, "applied": True, "relpath": "ticket/A.md"}

    register_ticket_outcome_resolver_callable(app, _resolver)
    app["_resolver_calls"] = calls
    tc: TestClient = await aiohttp_client(app)
    return tc


# ---------------------------------------------------------------------------
# Happy path + dispatch shape + log-emission
# ---------------------------------------------------------------------------


async def test_outcome_happy_path_calls_resolver(vera_outcome_app):  # type: ignore[no-untyped-def]
    with structlog.testing.capture_logs() as captured:
        resp = await vera_outcome_app.post(
            "/peer/ticket_outcome",
            json={
                "ticket_uid": "vera-20260613-6ca5b92f",
                "status": "resolved",
                "disposition": "merged",
                "pr_number": 8,
                "resolved_at": "2026-06-15T12:00:00+00:00",
                "correlation_id": "kal-le-ticket-outcome-test",
            },
            headers=_auth_headers(),
        )
    assert resp.status == 200, await resp.text()
    body = await resp.json()
    assert body["applied"] is True
    assert body["relpath"] == "ticket/A.md"
    assert body["error"] is None
    calls = vera_outcome_app.server.app["_resolver_calls"]
    assert len(calls) == 1
    assert calls[0]["ticket_uid"] == "vera-20260613-6ca5b92f"
    assert calls[0]["status"] == "resolved"
    assert calls[0]["disposition"] == "merged"
    assert calls[0]["pr_number"] == 8
    # Log-emission pin.
    applied = [
        c for c in captured
        if c.get("event") == "transport.peer.ticket_outcome_applied"
    ]
    assert len(applied) == 1
    assert applied[0]["status"] == "resolved"
    assert applied[0]["disposition"] == "merged"
    assert applied[0]["applied"] is True


async def test_outcome_optional_fields_omitted(vera_outcome_app):  # type: ignore[no-untyped-def]
    resp = await vera_outcome_app.post(
        "/peer/ticket_outcome",
        json={
            "ticket_uid": "vera-x",
            "status": "closed",
            "disposition": "closed_no_merge",
        },
        headers=_auth_headers(),
    )
    assert resp.status == 200, await resp.text()
    calls = vera_outcome_app.server.app["_resolver_calls"]
    assert calls[0]["pr_number"] is None
    assert calls[0]["resolved_at"] is None


# ---------------------------------------------------------------------------
# 501 — resolver not wired
# ---------------------------------------------------------------------------


async def test_outcome_missing_resolver_501(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """No resolver registered (a non-origin instance) → 501, never 500."""
    config = _build_vera_config()
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)
    # Deliberately do NOT register the resolver.
    tc: TestClient = await aiohttp_client(app)
    resp = await tc.post(
        "/peer/ticket_outcome",
        json={"ticket_uid": "x", "status": "resolved", "disposition": "merged"},
        headers=_auth_headers(),
    )
    assert resp.status == 501
    body = await resp.json()
    assert body["reason"] == "ticket_outcome_resolver_not_configured"


# ---------------------------------------------------------------------------
# 404 — ticket not found
# ---------------------------------------------------------------------------


async def test_outcome_not_found_404(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    config = _build_vera_config()
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)

    async def _resolver(**kwargs):
        return {"found": False}

    register_ticket_outcome_resolver_callable(app, _resolver)
    with structlog.testing.capture_logs() as captured:
        tc: TestClient = await aiohttp_client(app)
        resp = await tc.post(
            "/peer/ticket_outcome",
            json={
                "ticket_uid": "vera-missing", "status": "resolved",
                "disposition": "merged",
            },
            headers=_auth_headers(),
        )
    assert resp.status == 404
    body = await resp.json()
    assert body["reason"] == "ticket_not_found"
    not_found = [
        c for c in captured
        if c.get("event") == "transport.peer.ticket_outcome_not_found"
    ]
    assert len(not_found) == 1


# ---------------------------------------------------------------------------
# 502 — resolver raised
# ---------------------------------------------------------------------------


async def test_outcome_resolver_error_502(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    config = _build_vera_config()
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)

    async def _resolver(**kwargs):
        raise RuntimeError("scope denied")

    register_ticket_outcome_resolver_callable(app, _resolver)
    tc: TestClient = await aiohttp_client(app)
    resp = await tc.post(
        "/peer/ticket_outcome",
        json={
            "ticket_uid": "vera-x", "status": "resolved",
            "disposition": "merged",
        },
        headers=_auth_headers(),
    )
    assert resp.status == 502
    body = await resp.json()
    assert body["reason"] == "ticket_outcome_resolver_error"


# ---------------------------------------------------------------------------
# Schema gates
# ---------------------------------------------------------------------------


async def test_outcome_empty_uid_400(vera_outcome_app):  # type: ignore[no-untyped-def]
    resp = await vera_outcome_app.post(
        "/peer/ticket_outcome",
        json={"ticket_uid": "", "status": "resolved", "disposition": "merged"},
        headers=_auth_headers(),
    )
    assert resp.status == 400
    assert (await resp.json())["reason"] == "schema_error"


async def test_outcome_bad_status_400(vera_outcome_app):  # type: ignore[no-untyped-def]
    """Only resolved | closed are accepted on the wire (in_progress /
    open are not resolution states)."""
    resp = await vera_outcome_app.post(
        "/peer/ticket_outcome",
        json={"ticket_uid": "x", "status": "in_progress", "disposition": "m"},
        headers=_auth_headers(),
    )
    assert resp.status == 400
    assert (await resp.json())["reason"] == "schema_error"
    # The resolver must NOT be called on a schema reject.
    assert vera_outcome_app.server.app["_resolver_calls"] == []


async def test_outcome_missing_disposition_400(vera_outcome_app):  # type: ignore[no-untyped-def]
    resp = await vera_outcome_app.post(
        "/peer/ticket_outcome",
        json={"ticket_uid": "x", "status": "resolved", "disposition": ""},
        headers=_auth_headers(),
    )
    assert resp.status == 400
    assert (await resp.json())["reason"] == "schema_error"


async def test_outcome_bad_pr_number_type_400(vera_outcome_app):  # type: ignore[no-untyped-def]
    resp = await vera_outcome_app.post(
        "/peer/ticket_outcome",
        json={
            "ticket_uid": "x", "status": "resolved", "disposition": "merged",
            "pr_number": "eight",
        },
        headers=_auth_headers(),
    )
    assert resp.status == 400
    assert (await resp.json())["reason"] == "schema_error"


async def test_outcome_missing_bearer_401(vera_outcome_app):  # type: ignore[no-untyped-def]
    resp = await vera_outcome_app.post(
        "/peer/ticket_outcome",
        json={"ticket_uid": "x", "status": "resolved", "disposition": "merged"},
    )
    assert resp.status == 401


# ---------------------------------------------------------------------------
# Handshake capability advertisement
# ---------------------------------------------------------------------------


async def test_handshake_advertises_ticket_outcome_when_wired(
    vera_outcome_app,
):  # type: ignore[no-untyped-def]
    resp = await vera_outcome_app.post(
        "/peer/handshake", json={"from": "kal-le"}, headers=_auth_headers(),
    )
    assert resp.status == 200
    caps = (await resp.json())["capabilities"]
    assert "ticket_outcome" in caps


async def test_handshake_omits_ticket_outcome_when_unwired(
    aiohttp_client, tmp_path,
):  # type: ignore[no-untyped-def]
    config = _build_vera_config()
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)  # no resolver wired
    tc: TestClient = await aiohttp_client(app)
    resp = await tc.post(
        "/peer/handshake", json={"from": "kal-le"}, headers=_auth_headers(),
    )
    assert resp.status == 200
    caps = (await resp.json())["capabilities"]
    assert "ticket_outcome" not in caps
