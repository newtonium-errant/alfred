"""Tests for GCal-aware conflict-check in /canonical/event/propose-create.

Phase A+ inter-instance comms commit 2 — the handler now merges vault
events with Google Calendar events (Alfred + primary calendars) when
GCal is wired into the transport app.

Mocks the GCal client so the tests don't need real credentials and
never touch the network.

Coverage:
  * GCal disabled (no client / no config / config.enabled False) →
    handler behaves identically to vault-only flow (regression check)
  * GCal enabled + alfred-cal conflict → conflict response includes
    gcal_alfred entry
  * GCal enabled + primary-cal conflict → conflict response includes
    gcal_primary entry
  * Vault + GCal conflict simultaneously → both surface
  * Vault record with gcal_event_id matches GCal event → dedup drops
    the gcal_alfred mirror
  * GCal API failure → graceful degradation to vault-only (warning
    logged, no 500 to caller)
  * Vault conflict shape now includes ``source: "vault"``
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient

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
from alfred.transport.server import build_app
from alfred.transport.peer_handlers import (
    register_gcal_client,
    register_instance_identity,
    register_vault_path,
)
from alfred.transport.state import TransportState


DUMMY_KALLE_PEER_TOKEN = "DUMMY_KALLE_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_transport_config(audit_path) -> TransportConfig:
    return TransportConfig(
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


def _make_gcal_config(
    *,
    alfred_id: str = "alfred-cal@group.calendar.google.com",
    primary_id: str = "andrew@example.com",
    enabled: bool = True,
) -> object:
    """Build a GCalConfig matching the typed dataclass."""
    from alfred.integrations.gcal_config import GCalConfig
    return GCalConfig(
        enabled=enabled,
        alfred_calendar_id=alfred_id,
        primary_calendar_id=primary_id,
    )


def _make_gcal_client_mock(
    *,
    alfred_events: list | None = None,
    primary_events: list | None = None,
    raise_on_alfred: bool = False,
    raise_on_primary: bool = False,
) -> MagicMock:
    """Mock GCalClient.list_events keyed on calendar_id."""
    from alfred.integrations.gcal import GCalError

    alfred_events = alfred_events or []
    primary_events = primary_events or []

    client = MagicMock()

    def _list(calendar_id, time_min, time_max, **kwargs):
        if calendar_id == "alfred-cal@group.calendar.google.com":
            if raise_on_alfred:
                raise GCalError("simulated alfred-cal failure")
            return alfred_events
        if calendar_id == "andrew@example.com":
            if raise_on_primary:
                raise GCalError("simulated primary-cal failure")
            return primary_events
        return []

    client.list_events.side_effect = _list
    return client


def _make_gcal_event(
    *,
    event_id: str,
    title: str,
    start_iso: str,
    end_iso: str,
    calendar_id: str = "alfred-cal@group.calendar.google.com",
) -> object:
    from alfred.integrations.gcal import GCalEvent
    return GCalEvent(
        id=event_id,
        calendar_id=calendar_id,
        title=title,
        start=datetime.fromisoformat(start_iso),
        end=datetime.fromisoformat(end_iso),
        description="",
    )


@pytest.fixture
async def app_factory(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Factory that builds a Salem-style app with optional GCal wiring."""
    audit_path = tmp_path / "canonical_audit.jsonl"
    config = _make_transport_config(audit_path)
    state = TransportState.create(tmp_path / "transport_state.json")
    vault_root = tmp_path / "vault"
    (vault_root / "event").mkdir(parents=True)

    async def _factory(*, gcal_client=None, gcal_config=None) -> TestClient:
        app = build_app(config, state)
        register_vault_path(app, vault_root)
        register_instance_identity(app, name="S.A.L.E.M.", alias="Salem")
        if gcal_client is not None and gcal_config is not None:
            register_gcal_client(app, gcal_client, gcal_config)
        app["_vault_root"] = vault_root
        app["_audit_path"] = audit_path
        return await aiohttp_client(app)

    return _factory


def _seed_event(vault_root, *, filename: str, fields: dict) -> None:
    import yaml as _yaml
    fm = {"type": "event", "name": filename.removesuffix(".md")}
    fm.update(fields)
    text = "---\n" + _yaml.dump(fm, default_flow_style=False) + "---\n\nbody\n"
    (vault_root / "event" / filename).write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# GCal disabled / unconfigured → vault-only behaviour preserved
# ---------------------------------------------------------------------------


async def test_no_gcal_wiring_works_vault_only(app_factory):  # type: ignore[no-untyped-def]
    """Regression: handler must still work when no GCal client is registered."""
    client = await app_factory()  # no gcal wiring
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "vault-only-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Solo event",
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


async def test_vault_conflict_now_includes_source_field(app_factory):  # type: ignore[no-untyped-def]
    """The vault-conflict shape gains a ``source: "vault"`` field in Phase A+."""
    client = await app_factory()
    vault_root = client.server.app["_vault_root"]
    _seed_event(
        vault_root, filename="EI Call.md",
        fields={
            "title": "EI Call",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T14:30:00-03:00",
        },
    )
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "vault-source-test",
            "start": "2026-05-04T14:15:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "VAC call",
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
    assert body["conflicts"][0]["source"] == "vault"


# ---------------------------------------------------------------------------
# GCal enabled — single-source conflicts
# ---------------------------------------------------------------------------


async def test_alfred_calendar_conflict_surfaces(app_factory):  # type: ignore[no-untyped-def]
    """Event on Alfred calendar overlaps proposed window → gcal_alfred conflict."""
    gcal_event = _make_gcal_event(
        event_id="ev-1",
        title="Coaching session",
        start_iso="2026-05-04T14:00:00-03:00",
        end_iso="2026-05-04T15:00:00-03:00",
        calendar_id="alfred-cal@group.calendar.google.com",
    )
    gcal_client = _make_gcal_client_mock(alfred_events=[gcal_event])
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "alfred-conflict-test",
            "start": "2026-05-04T14:30:00-03:00",
            "end": "2026-05-04T15:30:00-03:00",
            "title": "VAC call",
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
    assert c["source"] == "gcal_alfred"
    assert c["title"] == "Coaching session"
    assert c["gcal_event_id"] == "ev-1"


async def test_primary_calendar_conflict_surfaces(app_factory):  # type: ignore[no-untyped-def]
    """Real meeting on Andrew's primary calendar blocks the proposal."""
    gcal_event = _make_gcal_event(
        event_id="primary-ev-1",
        title="VAC marketing meeting",
        start_iso="2026-05-04T14:00:00-03:00",
        end_iso="2026-05-04T15:00:00-03:00",
        calendar_id="andrew@example.com",
    )
    gcal_client = _make_gcal_client_mock(primary_events=[gcal_event])
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "primary-conflict-test",
            "start": "2026-05-04T14:30:00-03:00",
            "end": "2026-05-04T15:30:00-03:00",
            "title": "Hypatia thinks Andrew is free",
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
    assert c["source"] == "gcal_primary"
    assert c["gcal_event_id"] == "primary-ev-1"


# ---------------------------------------------------------------------------
# Combined sources — vault + GCal at the same time
# ---------------------------------------------------------------------------


async def test_vault_and_gcal_conflicts_both_surface(app_factory):  # type: ignore[no-untyped-def]
    gcal_event = _make_gcal_event(
        event_id="prim-ev-1",
        title="Real meeting",
        start_iso="2026-05-04T14:00:00-03:00",
        end_iso="2026-05-04T15:00:00-03:00",
        calendar_id="andrew@example.com",
    )
    gcal_client = _make_gcal_client_mock(primary_events=[gcal_event])
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    vault_root = client.server.app["_vault_root"]
    _seed_event(
        vault_root, filename="Vault-only event.md",
        fields={
            "title": "Vault-only event",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T14:30:00-03:00",
        },
    )

    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "combined-test",
            "start": "2026-05-04T14:15:00-03:00",
            "end": "2026-05-04T15:30:00-03:00",
            "title": "VAC call",
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
    sources = sorted(c["source"] for c in body["conflicts"])
    assert sources == ["gcal_primary", "vault"]


# ---------------------------------------------------------------------------
# Dedup: vault record synced to gcal_alfred → only vault entry surfaces
# ---------------------------------------------------------------------------


async def test_dedup_when_vault_record_already_synced_to_gcal(app_factory):  # type: ignore[no-untyped-def]
    """Vault has gcal_event_id=X; GCal returns the same X event → only one entry."""
    gcal_event = _make_gcal_event(
        event_id="mirror-id-7",
        title="Coaching",
        start_iso="2026-05-04T14:00:00-03:00",
        end_iso="2026-05-04T15:00:00-03:00",
    )
    gcal_client = _make_gcal_client_mock(alfred_events=[gcal_event])
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    vault_root = client.server.app["_vault_root"]
    _seed_event(
        vault_root, filename="Coaching.md",
        fields={
            "title": "Coaching",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "gcal_event_id": "mirror-id-7",
            "gcal_calendar": "alfred",
        },
    )
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "dedup-test",
            "start": "2026-05-04T14:30:00-03:00",
            "end": "2026-05-04T15:30:00-03:00",
            "title": "Conflicting proposal",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    body = await resp.json()
    assert body["status"] == "conflict"
    # Should be one entry — the vault one — gcal_alfred mirror dropped.
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["source"] == "vault"
    assert body["conflicts"][0]["gcal_event_id"] == "mirror-id-7"


# ---------------------------------------------------------------------------
# Graceful degradation on GCal API failure
# ---------------------------------------------------------------------------


async def test_gcal_api_failure_falls_back_to_vault(app_factory):  # type: ignore[no-untyped-def]
    """GCal API explosion must not crash the request — vault-only result stands."""
    gcal_client = _make_gcal_client_mock(
        raise_on_alfred=True, raise_on_primary=True,
    )
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "gcal-fail-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Robust under GCal outage",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    # No conflicts in vault, GCal blew up but we degraded gracefully → 201.
    assert resp.status == 201
    body = await resp.json()
    assert body["status"] == "created"


async def test_gcal_disabled_in_config_skips_calls(app_factory):  # type: ignore[no-untyped-def]
    """``enabled: false`` → conflict-check skips GCal entirely (no list_events)."""
    gcal_client = _make_gcal_client_mock()
    gcal_config = _make_gcal_config(enabled=False)

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "disabled-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "GCal off",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 201
    # Critically: no GCal call attempted.
    assert gcal_client.list_events.called is False
