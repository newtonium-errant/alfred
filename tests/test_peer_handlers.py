"""End-to-end tests for the c3 /peer/* + /canonical/* handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from alfred.transport.canonical_audit import read_audit
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    PeerEntry,
    PeerFieldRules,
    ServerConfig,
    SchedulerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import (
    register_instance_identity,
    register_peer_inbox,
    register_vault_path,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState


DUMMY_SALEM_PEER_TOKEN = "DUMMY_SALEM_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"
DUMMY_KALLE_PEER_TOKEN = "DUMMY_KALLE_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"


def _build_config(
    *,
    canonical_owner: bool = False,
    peer_permissions: dict[str, dict[str, PeerFieldRules]] | None = None,
    extra_peers: dict[str, PeerEntry] | None = None,
    audit_log_path: str = "",
) -> TransportConfig:
    # Empty audit_log_path → append_audit is a no-op. Tests that assert
    # on audit content override the path to a tmp file.
    tokens: dict[str, AuthTokenEntry] = {
        "local": AuthTokenEntry(
            token=DUMMY_SALEM_PEER_TOKEN,
            allowed_clients=["scheduler", "brief", "kal-le"],
        ),
        "kal-le": AuthTokenEntry(
            token=DUMMY_KALLE_PEER_TOKEN,
            allowed_clients=["kal-le", "salem"],
        ),
    }
    return TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens=tokens),
        state=StateConfig(),
        canonical=CanonicalConfig(
            owner=canonical_owner,
            audit_log_path=audit_log_path,
            peer_permissions=peer_permissions or {},
        ),
        peers=extra_peers or {},
    )


@pytest.fixture
async def salem_app(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Salem-style app: canonical owner, KAL-LE permissioned, vault loaded."""
    audit_path = tmp_path / "canonical_audit.jsonl"
    config = _build_config(
        canonical_owner=True,
        peer_permissions={
            "kal-le": {
                "person": PeerFieldRules(
                    fields=["name", "email", "timezone", "preferences.coding"],
                ),
            },
            "stay-c": {
                "person": PeerFieldRules(fields=["name"]),
            },
        },
        extra_peers={
            "kal-le": PeerEntry(
                base_url="http://127.0.0.1:8892",
                token=DUMMY_KALLE_PEER_TOKEN,
            ),
        },
        audit_log_path=str(audit_path),
    )
    state = TransportState.create(tmp_path / "transport_state.json")

    # Build a tiny vault with one person record.
    vault_root = tmp_path / "vault"
    (vault_root / "person").mkdir(parents=True)
    record = vault_root / "person" / "Andrew Newton.md"
    record.write_text(
        "---\n"
        "name: Andrew Newton\n"
        "email: andrew@example.com\n"
        "phone: +1-555-1234\n"
        "timezone: America/Halifax\n"
        "preferences:\n"
        "  coding: python\n"
        "  writing: voice\n"
        "type: person\n"
        "---\n"
        "Body content should NEVER be exposed.\n",
        encoding="utf-8",
    )

    app = build_app(config, state)
    register_vault_path(app, vault_root)
    register_instance_identity(app, name="S.A.L.E.M.", alias="Salem")
    # Stash audit path on the app BEFORE handing to aiohttp_client —
    # aiohttp raises a DeprecationWarning if you mutate after startup.
    app["_audit_path"] = audit_path

    tc: TestClient = await aiohttp_client(app)
    return tc


# ---------------------------------------------------------------------------
# /peer/handshake
# ---------------------------------------------------------------------------


async def test_handshake_returns_identity_and_capabilities(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/handshake",
        json={"from": "kal-le"},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["instance"] == "S.A.L.E.M."
    assert body["alias"] == "Salem"
    assert body["protocol_version"] == 1
    assert "canonical_owner" in body["capabilities"]
    assert "peer_message" in body["capabilities"]
    # peers list includes kal-le.
    peer_names = {p["name"] for p in body["peers"]}
    assert "kal-le" in peer_names


async def test_handshake_echoes_correlation_id(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/handshake",
        json={},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
            "X-Correlation-Id": "test-cid-12345678",
        },
    )
    body = await resp.json()
    assert body["correlation_id"] == "test-cid-12345678"


async def test_handshake_generates_correlation_id_if_absent(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/handshake",
        json={},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    body = await resp.json()
    assert body["correlation_id"]
    assert len(body["correlation_id"]) == 16  # uuid hex truncated


# ---------------------------------------------------------------------------
# /peer/send
# ---------------------------------------------------------------------------


async def test_peer_send_without_inbox_returns_501(salem_app):  # type: ignore[no-untyped-def]
    """No inbox registered → 501 peer_inbox_not_configured."""
    resp = await salem_app.post(
        "/peer/send",
        json={"kind": "message", "from": "kal-le", "payload": {"text": "hi"}},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 501
    body = await resp.json()
    assert body["reason"] == "peer_inbox_not_configured"


async def _build_salem_with_inbox(
    aiohttp_client, tmp_path, inbox,
):  # type: ignore[no-untyped-def]
    """Factory — salem-style app with a pre-registered peer inbox.

    Registering the inbox *before* ``aiohttp_client(app)`` avoids the
    "mutating started app" DeprecationWarning. Tests that need
    per-case inbox behaviour use this factory instead of the
    session-level ``salem_app`` fixture.
    """
    audit_path = tmp_path / "canonical_audit.jsonl"
    config = _build_config(
        canonical_owner=True,
        peer_permissions={
            "kal-le": {
                "person": PeerFieldRules(
                    fields=["name", "email", "timezone", "preferences.coding"],
                ),
            },
        },
        audit_log_path=str(audit_path),
    )
    state = TransportState.create(tmp_path / "transport_state.json")
    vault_root = tmp_path / "vault"
    (vault_root / "person").mkdir(parents=True)
    app = build_app(config, state)
    register_vault_path(app, vault_root)
    register_instance_identity(app, name="S.A.L.E.M.", alias="Salem")
    register_peer_inbox(app, inbox)
    tc: TestClient = await aiohttp_client(app)
    return tc


async def test_peer_send_delivers_to_inbox(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Happy path: inbox callable gets the payload."""
    received: list[dict[str, Any]] = []

    async def _inbox(**kwargs) -> dict[str, Any]:
        received.append(kwargs)
        return {"delivered_at": "2026-04-20T22:00:00Z"}

    client = await _build_salem_with_inbox(aiohttp_client, tmp_path, _inbox)

    resp = await client.post(
        "/peer/send",
        json={
            "kind": "message",
            "from": "kal-le",
            "payload": {"user_id": 123, "text": "Routed code review complete"},
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
            "X-Correlation-Id": "cid-abc123",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "accepted"
    assert body["correlation_id"] == "cid-abc123"
    assert body["delivered_at"] == "2026-04-20T22:00:00Z"

    assert len(received) == 1
    assert received[0]["kind"] == "message"
    assert received[0]["from_peer"] == "kal-le"
    assert received[0]["correlation_id"] == "cid-abc123"
    assert received[0]["payload"]["text"] == "Routed code review complete"


async def test_peer_send_rejects_spoofed_from(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Body's ``from`` must match the authenticated peer."""
    async def _inbox(**kwargs) -> dict[str, Any]:
        return {}

    client = await _build_salem_with_inbox(aiohttp_client, tmp_path, _inbox)

    resp = await client.post(
        "/peer/send",
        json={
            "kind": "message",
            "from": "stay-c",  # Lying about identity
            "payload": {"text": "malicious"},
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 403
    body = await resp.json()
    assert body["reason"] == "from_mismatch"


async def test_peer_send_schema_error_on_bad_kind(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/send",
        json={"kind": "unknown_kind", "from": "kal-le", "payload": {}},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"


async def test_peer_send_inbox_exception_becomes_502(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    async def _inbox(**kwargs) -> dict[str, Any]:
        raise RuntimeError("telegram outage")

    client = await _build_salem_with_inbox(aiohttp_client, tmp_path, _inbox)

    resp = await client.post(
        "/peer/send",
        json={"kind": "message", "from": "kal-le", "payload": {"text": "hi"}},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 502
    body = await resp.json()
    assert body["reason"] == "peer_inbox_error"


# ---------------------------------------------------------------------------
# /canonical/<type>/<name>
# ---------------------------------------------------------------------------


async def test_canonical_get_filtered_frontmatter(salem_app):  # type: ignore[no-untyped-def]
    """KAL-LE can read name + email + timezone + preferences.coding."""
    resp = await salem_app.get(
        "/canonical/person/Andrew Newton",
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
            "X-Correlation-Id": "cid-canon-1",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["type"] == "person"
    assert body["name"] == "Andrew Newton"
    fm = body["frontmatter"]
    assert fm["name"] == "Andrew Newton"
    assert fm["email"] == "andrew@example.com"
    assert fm["timezone"] == "America/Halifax"
    assert fm["preferences"] == {"coding": "python"}
    # NEVER exposed: phone, preferences.writing, body.
    assert "phone" not in fm
    assert "writing" not in fm["preferences"]
    assert "body" not in body


async def test_canonical_get_audit_log_entry_written(salem_app, tmp_path):  # type: ignore[no-untyped-def]
    await salem_app.get(
        "/canonical/person/Andrew Newton",
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
            "X-Correlation-Id": "cid-audit-1",
        },
    )
    audit_path = salem_app.server.app["_audit_path"]
    entries = read_audit(audit_path)
    assert len(entries) == 1
    e = entries[0]
    assert e["peer"] == "kal-le"
    assert e["type"] == "person"
    assert e["name"] == "Andrew Newton"
    assert "name" in e["granted"]
    assert "phone" in e["denied"]
    assert e["correlation_id"] == "cid-audit-1"


async def test_canonical_get_unknown_peer_returns_403(salem_app):  # type: ignore[no-untyped-def]
    """Local/salem peer has no permissions for person records → 403."""
    resp = await salem_app.get(
        "/canonical/person/Andrew Newton",
        headers={
            "Authorization": f"Bearer {DUMMY_SALEM_PEER_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 403
    body = await resp.json()
    assert body["reason"] == "no_permitted_fields"


async def test_canonical_get_missing_record_returns_404(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.get(
        "/canonical/person/Ghost",
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 404
    body = await resp.json()
    assert body["reason"] == "record_not_found"


async def test_canonical_get_on_non_owner_returns_404_not_owned(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """An instance without canonical ownership returns 404 canonical_not_owned."""
    config = _build_config(canonical_owner=False)
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)
    tc: TestClient = await aiohttp_client(app)

    resp = await tc.get(
        "/canonical/person/Whoever",
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 404
    body = await resp.json()
    assert body["reason"] == "canonical_not_owned"


# ---------------------------------------------------------------------------
# /peer/query — same permission filter via POST
# ---------------------------------------------------------------------------


async def test_peer_query_filters_fields(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/query",
        json={
            "record_type": "person",
            "name": "Andrew Newton",
            "fields": ["name", "email"],
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["frontmatter"]["name"] == "Andrew Newton"
    assert body["frontmatter"]["email"] == "andrew@example.com"
    # timezone is permitted but wasn't requested — the intersection
    # with the ``fields`` argument drops it.
    assert "timezone" not in body["frontmatter"]


async def test_peer_query_schema_error(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/query",
        json={"record_type": 123, "name": "Andrew Newton"},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"


async def test_peer_query_not_owner_returns_404(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    config = _build_config(canonical_owner=False)
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)
    tc: TestClient = await aiohttp_client(app)

    resp = await tc.post(
        "/peer/query",
        json={"record_type": "person", "name": "x"},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 404
    body = await resp.json()
    assert body["reason"] == "canonical_not_owned"
