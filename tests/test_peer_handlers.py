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


# ---------------------------------------------------------------------------
# /canonical/{type}/propose — generalized to person / org / location
# ---------------------------------------------------------------------------


@pytest.fixture
async def salem_propose_app(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Salem-style app with proposals queue path wired up.

    Same shape as ``salem_app`` but configured with a ``proposals_path``
    so the propose route can persist queued entries.
    """
    audit_path = tmp_path / "canonical_audit.jsonl"
    proposals_path = tmp_path / "canonical_proposals.jsonl"
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={
            "local": AuthTokenEntry(
                token=DUMMY_SALEM_PEER_TOKEN,
                allowed_clients=["scheduler", "brief", "kal-le"],
            ),
            "kal-le": AuthTokenEntry(
                token=DUMMY_KALLE_PEER_TOKEN,
                allowed_clients=["kal-le", "salem"],
            ),
        }),
        state=StateConfig(),
        canonical=CanonicalConfig(
            owner=True,
            audit_log_path=str(audit_path),
            proposals_path=str(proposals_path),
            peer_permissions={
                "kal-le": {
                    "person": PeerFieldRules(fields=["name", "email"]),
                    "org": PeerFieldRules(fields=["name", "type"]),
                    "location": PeerFieldRules(fields=["name", "address"]),
                },
            },
        ),
        peers={},
    )
    state = TransportState.create(tmp_path / "transport_state.json")

    vault_root = tmp_path / "vault"
    (vault_root / "person").mkdir(parents=True)
    (vault_root / "org").mkdir()
    (vault_root / "location").mkdir()
    (vault_root / "event").mkdir()

    app = build_app(config, state)
    register_vault_path(app, vault_root)
    register_instance_identity(app, name="S.A.L.E.M.", alias="Salem")
    app["_proposals_path"] = proposals_path
    app["_vault_root"] = vault_root
    app["_audit_path"] = audit_path

    tc: TestClient = await aiohttp_client(app)
    return tc


async def test_canonical_propose_person_queues(salem_propose_app):  # type: ignore[no-untyped-def]
    """Backwards-compat: person propose still queues as before."""
    resp = await salem_propose_app.post(
        "/canonical/person/propose",
        json={
            "name": "Elena Brighton",
            "proposed_fields": {"description": "NP colleague"},
            "source": "KAL-LE coding session 2026-04-30",
            "correlation_id": "kal-le-propose-person-test1",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 202
    body = await resp.json()
    assert body["status"] == "pending"
    assert body["correlation_id"] == "kal-le-propose-person-test1"


async def test_canonical_propose_org_queues(salem_propose_app):  # type: ignore[no-untyped-def]
    """org is in _PROPOSE_ALLOWED_TYPES — the propose route accepts it."""
    from alfred.transport.canonical_proposals import iter_proposals

    resp = await salem_propose_app.post(
        "/canonical/org/propose",
        json={
            "name": "Aftermath Labs Inc",
            "proposed_fields": {
                "type": "company",
                "description": "Andrew's consulting practice",
            },
            "source": "KAL-LE observed in commit message 2026-04-30",
            "correlation_id": "kal-le-propose-org-abc123",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 202
    body = await resp.json()
    assert body["status"] == "pending"
    assert body["correlation_id"] == "kal-le-propose-org-abc123"

    # Verify the proposal landed in the queue with record_type="org".
    proposals = iter_proposals(salem_propose_app.server.app["_proposals_path"])
    matching = [p for p in proposals if p.correlation_id == "kal-le-propose-org-abc123"]
    assert len(matching) == 1
    assert matching[0].record_type == "org"
    assert matching[0].name == "Aftermath Labs Inc"
    assert matching[0].proposed_fields["type"] == "company"


async def test_canonical_propose_location_queues(salem_propose_app):  # type: ignore[no-untyped-def]
    """location is in _PROPOSE_ALLOWED_TYPES — the propose route accepts it."""
    from alfred.transport.canonical_proposals import iter_proposals

    resp = await salem_propose_app.post(
        "/canonical/location/propose",
        json={
            "name": "Halifax Convention Centre",
            "proposed_fields": {
                "address": "1650 Argyle St, Halifax NS",
            },
            "source": "Hypatia conversation 2026-05-01",
            "correlation_id": "hypatia-propose-location-zzz999",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 202
    body = await resp.json()
    assert body["status"] == "pending"

    proposals = iter_proposals(salem_propose_app.server.app["_proposals_path"])
    matching = [p for p in proposals if p.correlation_id == "hypatia-propose-location-zzz999"]
    assert len(matching) == 1
    assert matching[0].record_type == "location"


async def test_canonical_propose_unknown_type_400(salem_propose_app):  # type: ignore[no-untyped-def]
    """Types not in _PROPOSE_ALLOWED_TYPES return 400 schema_error.

    ``event`` is intentionally NOT proposable via the queued shape —
    events go through ``/canonical/event/propose-create`` (synchronous
    with conflict-check). A queued propose for ``event`` rejects.
    """
    resp = await salem_propose_app.post(
        "/canonical/event/propose",
        json={"name": "x", "correlation_id": "test-event-bad"},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"


async def test_canonical_propose_org_409_when_record_exists(salem_propose_app):  # type: ignore[no-untyped-def]
    """409 race: org record was created between the proposer's 404 + propose."""
    vault_root = salem_propose_app.server.app["_vault_root"]
    (vault_root / "org" / "Existing Org.md").write_text(
        "---\ntype: org\nname: Existing Org\n---\nbody\n",
        encoding="utf-8",
    )

    resp = await salem_propose_app.post(
        "/canonical/org/propose",
        json={
            "name": "Existing Org",
            "correlation_id": "test-org-409",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 409
    body = await resp.json()
    assert body["status"] == "exists"
    assert body["path"] == "org/Existing Org.md"


# ---------------------------------------------------------------------------
# /canonical/event/propose-create — synchronous create with conflict-check
# ---------------------------------------------------------------------------
#
# Architecturally distinct from /canonical/{type}/propose: events are
# synchronous (Andrew is mid-conversation with the proposing instance).
# Salem either creates the record + returns 201, or detects a time
# overlap with existing vault events + returns 200 with conflict list.


@pytest.fixture
async def salem_event_app(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Salem-style app with vault root primed for event propose-create tests."""
    audit_path = tmp_path / "canonical_audit.jsonl"
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={
            "kal-le": AuthTokenEntry(
                token=DUMMY_KALLE_PEER_TOKEN,
                allowed_clients=["kal-le", "salem"],
            ),
        }),
        state=StateConfig(),
        canonical=CanonicalConfig(
            owner=True,
            audit_log_path=str(audit_path),
            peer_permissions={
                "kal-le": {
                    "event": PeerFieldRules(fields=["name", "title", "start", "end"]),
                },
            },
        ),
        peers={},
    )
    state = TransportState.create(tmp_path / "transport_state.json")

    vault_root = tmp_path / "vault"
    (vault_root / "event").mkdir(parents=True)

    app = build_app(config, state)
    register_vault_path(app, vault_root)
    register_instance_identity(app, name="S.A.L.E.M.", alias="Salem")
    app["_vault_root"] = vault_root
    app["_audit_path"] = audit_path

    tc: TestClient = await aiohttp_client(app)
    return tc


def _seed_event(vault_root, *, filename: str, fields: dict) -> None:  # type: ignore[no-untyped-def]
    """Helper: write a vault event record with the given frontmatter."""
    import yaml as _yaml
    fm = {"type": "event", "name": filename.removesuffix(".md")}
    fm.update(fields)
    text = "---\n" + _yaml.dump(fm, default_flow_style=False) + "---\n\nbody\n"
    (vault_root / "event" / filename).write_text(text, encoding="utf-8")


async def test_event_propose_create_happy_path(salem_event_app):  # type: ignore[no-untyped-def]
    """No conflict → record created, 201, path returned, file on disk."""
    resp = await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "kal-le-propose-event-happy1",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "VAC marketing call follow-up",
            "summary": "Follow-up on Q2 outreach plan",
            "origin_instance": "kal-le",
            "origin_context": "marketing strategy session 2026-04-30",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["status"] == "created"
    assert body["path"].startswith("event/VAC marketing call follow-up ")
    assert body["path"].endswith(".md")
    assert body["correlation_id"] == "kal-le-propose-event-happy1"

    vault_root = salem_event_app.server.app["_vault_root"]
    written = vault_root / body["path"]
    assert written.exists()
    text = written.read_text(encoding="utf-8")
    assert "type: event" in text
    assert "title: VAC marketing call follow-up" in text
    assert "start: '2026-05-04T17:00:00+00:00'" in text  # converted to UTC
    assert "origin_instance: kal-le" in text
    assert "origin_context: marketing strategy session 2026-04-30" in text


async def test_event_propose_create_conflict_with_start_end(salem_event_app):  # type: ignore[no-untyped-def]
    """Existing event with start+end overlapping → 200 conflict, no create."""
    vault_root = salem_event_app.server.app["_vault_root"]
    _seed_event(
        vault_root,
        filename="EI Call 2026-05-04.md",
        fields={
            "title": "EI Call",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T14:30:00-03:00",
            "date": "2026-05-04",
        },
    )

    resp = await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "test-conflict-1",
            "start": "2026-05-04T14:15:00-03:00",  # overlaps EI call
            "end": "2026-05-04T15:00:00-03:00",
            "title": "VAC marketing call",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "conflict"
    assert len(body["conflicts"]) == 1
    c = body["conflicts"][0]
    assert c["title"] == "EI Call"
    assert c["path"] == "event/EI Call 2026-05-04.md"
    # No new file should have landed.
    files = list((vault_root / "event").glob("VAC marketing*.md"))
    assert files == []


async def test_event_propose_create_conflict_multiple(salem_event_app):  # type: ignore[no-untyped-def]
    """Proposed window overlaps multiple existing events → all returned."""
    vault_root = salem_event_app.server.app["_vault_root"]
    _seed_event(
        vault_root, filename="Morning Standup.md",
        fields={
            "title": "Morning Standup",
            "start": "2026-05-04T13:00:00-03:00",
            "end": "2026-05-04T14:30:00-03:00",
        },
    )
    _seed_event(
        vault_root, filename="Lunch.md",
        fields={
            "title": "Lunch",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
        },
    )
    _seed_event(
        vault_root, filename="Far Future.md",
        fields={
            "title": "Far Future",
            "start": "2026-12-04T14:00:00-03:00",
            "end": "2026-12-04T15:00:00-03:00",
        },
    )

    resp = await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "test-multi-conflict",
            "start": "2026-05-04T13:30:00-03:00",
            "end": "2026-05-04T14:45:00-03:00",
            "title": "Big meeting",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "conflict"
    titles = {c["title"] for c in body["conflicts"]}
    assert titles == {"Morning Standup", "Lunch"}


async def test_event_propose_create_adjacent_no_conflict(salem_event_app):  # type: ignore[no-untyped-def]
    """Same-instant boundary (event_end == proposed_start) is NOT a conflict."""
    vault_root = salem_event_app.server.app["_vault_root"]
    _seed_event(
        vault_root, filename="Coffee.md",
        fields={
            "title": "Coffee",
            "start": "2026-05-04T13:00:00-03:00",
            "end": "2026-05-04T14:00:00-03:00",
        },
    )

    resp = await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "test-adjacent",
            "start": "2026-05-04T14:00:00-03:00",  # touches but doesn't overlap
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Next meeting",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["status"] == "created"


async def test_event_propose_create_conflict_with_date_only_event(salem_event_app):  # type: ignore[no-untyped-def]
    """Existing event with only ``date`` (no start/end) → treated as full-day window."""
    vault_root = salem_event_app.server.app["_vault_root"]
    _seed_event(
        vault_root, filename="All Day Conf 2026-05-04.md",
        fields={
            "title": "All Day Conf",
            "date": "2026-05-04",
        },
    )

    resp = await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "test-dateonly-conflict",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Sub meeting",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "conflict"
    assert body["conflicts"][0]["title"] == "All Day Conf"


async def test_event_propose_create_schema_error_missing_times(salem_event_app):  # type: ignore[no-untyped-def]
    resp = await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "test-bad-1",
            "title": "no times",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"


async def test_event_propose_create_schema_error_end_before_start(salem_event_app):  # type: ignore[no-untyped-def]
    resp = await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "test-bad-2",
            "start": "2026-05-04T15:00:00-03:00",
            "end": "2026-05-04T14:00:00-03:00",  # before start
            "title": "reversed",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"


async def test_event_propose_create_spoofed_origin_403(salem_event_app):  # type: ignore[no-untyped-def]
    """origin_instance must match authenticated peer (anti-spoof)."""
    resp = await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "test-spoof",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "spoofed",
            "origin_instance": "stay-c",  # lying — auth is kal-le
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 403
    body = await resp.json()
    assert body["reason"] == "from_mismatch"


async def test_event_propose_create_not_owner_404(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Non-canonical-owner returns 404 canonical_not_owned (KAL-LE/Hypatia)."""
    config = _build_config(canonical_owner=False)
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)
    tc: TestClient = await aiohttp_client(app)

    resp = await tc.post(
        "/canonical/event/propose-create",
        json={
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "x",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 404
    body = await resp.json()
    assert body["reason"] == "canonical_not_owned"


async def test_event_propose_create_audit_records_outcome(salem_event_app):  # type: ignore[no-untyped-def]
    """Created + conflict outcomes both append audit entries."""
    from alfred.transport.canonical_audit import read_audit

    audit_path = salem_event_app.server.app["_audit_path"]

    # Happy path → granted=["create"]
    await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "audit-test-1",
            "start": "2026-06-01T10:00:00-03:00",
            "end": "2026-06-01T11:00:00-03:00",
            "title": "Audit happy",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )

    # Same window again → conflict; denied=["conflict"].
    await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "audit-test-2",
            "start": "2026-06-01T10:30:00-03:00",
            "end": "2026-06-01T11:30:00-03:00",
            "title": "Audit clash",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )

    entries = read_audit(audit_path)
    by_cid = {e["correlation_id"]: e for e in entries}
    assert "audit-test-1" in by_cid
    assert by_cid["audit-test-1"]["granted"] == ["create"]
    assert by_cid["audit-test-1"]["denied"] == []
    assert "audit-test-2" in by_cid
    assert by_cid["audit-test-2"]["denied"] == ["conflict"]


async def test_event_propose_create_409_when_filename_collides(salem_event_app):  # type: ignore[no-untyped-def]
    """Same title + same date but different correlation → 409 already_exists."""
    resp1 = await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "first",
            "start": "2026-07-01T10:00:00-03:00",
            "end": "2026-07-01T11:00:00-03:00",
            "title": "Recurring Standup",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp1.status == 201

    # Same title + same date, but a non-overlapping (later) time slot —
    # the conflict-check passes (no overlap with the first event's
    # 10:00-11:00 window), so we reach the filename collision branch
    # downstream of the conflict-scan and the route returns 409.
    resp2 = await salem_event_app.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "second",
            "start": "2026-07-01T13:00:00-03:00",
            "end": "2026-07-01T14:00:00-03:00",
            "title": "Recurring Standup",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp2.status == 409
    body = await resp2.json()
    assert body["status"] == "exists"
    assert body["path"] == "event/Recurring Standup 2026-07-01.md"
