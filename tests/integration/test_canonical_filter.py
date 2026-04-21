"""Canonical filter integration — real record, real permission check, audit entry verified.

One focused scenario: SALEM owns a canonical person record, KAL-LE
queries it, only permitted fields come back, and the audit log has a
matching entry with granted + denied fields correctly classified.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient

from alfred.transport.canonical_audit import read_audit
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    PeerFieldRules,
    ServerConfig,
    SchedulerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import (
    register_instance_identity,
    register_vault_path,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState


DUMMY_KALLE_PEER_TOKEN = "DUMMY_KALLE_PEER_E2E_PLACEHOLDER_01234567"


async def test_canonical_read_filters_and_audits(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Full path: Salem hosts vault → KAL-LE reads → audit logged."""
    audit_path = tmp_path / "canonical_audit.jsonl"

    # --- Vault ---
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    (vault / "person" / "Andrew Newton.md").write_text(
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
        "SENSITIVE body content — must never be returned.\n",
        encoding="utf-8",
    )

    # --- Salem config ---
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={
            "kal-le": AuthTokenEntry(
                token=DUMMY_KALLE_PEER_TOKEN,
                allowed_clients=["kal-le"],
            ),
        }),
        state=StateConfig(),
        canonical=CanonicalConfig(
            owner=True,
            audit_log_path=str(audit_path),
            peer_permissions={
                "kal-le": {
                    "person": PeerFieldRules(
                        fields=["name", "email", "preferences.coding"],
                    ),
                },
            },
        ),
    )
    state = TransportState.create(tmp_path / "state.json")
    app = build_app(config, state)
    register_vault_path(app, vault)
    register_instance_identity(app, name="S.A.L.E.M.", alias="Salem")

    client: TestClient = await aiohttp_client(app)

    # --- KAL-LE reads the record ---
    resp = await client.get(
        "/canonical/person/Andrew Newton",
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
            "X-Correlation-Id": "e2e-cid-001",
        },
    )
    assert resp.status == 200
    body = await resp.json()

    # --- Filter check: permitted fields present ---
    fm = body["frontmatter"]
    assert fm["name"] == "Andrew Newton"
    assert fm["email"] == "andrew@example.com"
    assert fm["preferences"] == {"coding": "python"}

    # --- Filter check: denied fields absent ---
    assert "phone" not in fm
    assert "timezone" not in fm  # Not in the permission list
    assert "writing" not in fm["preferences"]

    # --- Filter check: body never exposed ---
    assert "body" not in body
    assert "SENSITIVE" not in str(body)

    # --- Audit entry present ---
    entries = read_audit(str(audit_path))
    assert len(entries) == 1
    e = entries[0]
    assert e["peer"] == "kal-le"
    assert e["type"] == "person"
    assert e["name"] == "Andrew Newton"
    assert e["correlation_id"] == "e2e-cid-001"
    # Granted set covers name + email + preferences.coding.
    assert "name" in e["granted"]
    assert "email" in e["granted"]
    assert "preferences.coding" in e["granted"]
    # Denied set covers phone + timezone.
    assert "phone" in e["denied"]
    assert "timezone" in e["denied"]


async def test_canonical_read_denied_peer_still_audits(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """A peer with no permissions for a type still appears in the audit log.

    Even a 403 no_permitted_fields outcome must be logged — that's how
    operators detect an unauthorized peer trying to read things.
    """
    audit_path = tmp_path / "canonical_audit.jsonl"

    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    (vault / "person" / "Andrew Newton.md").write_text(
        "---\nname: Andrew Newton\ntype: person\n---\n",
        encoding="utf-8",
    )

    unprivileged_token = "DUMMY_UNPRIVILEGED_PEER_TEST_TOKEN_01234567"
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={
            "stay-c": AuthTokenEntry(
                token=unprivileged_token,
                allowed_clients=["stay-c"],
            ),
        }),
        state=StateConfig(),
        canonical=CanonicalConfig(
            owner=True,
            audit_log_path=str(audit_path),
            # stay-c has NO permissions for person records.
            peer_permissions={},
        ),
    )
    state = TransportState.create(tmp_path / "state.json")
    app = build_app(config, state)
    register_vault_path(app, vault)

    client: TestClient = await aiohttp_client(app)
    resp = await client.get(
        "/canonical/person/Andrew Newton",
        headers={
            "Authorization": f"Bearer {unprivileged_token}",
            "X-Alfred-Client": "stay-c",
        },
    )
    assert resp.status == 403
    body = await resp.json()
    assert body["reason"] == "no_permitted_fields"

    # Audit entry written.
    entries = read_audit(str(audit_path))
    assert len(entries) == 1
    assert entries[0]["peer"] == "stay-c"
    assert entries[0]["granted"] == []
