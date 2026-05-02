"""Tests for GCal sync-on-create in /canonical/event/propose-create.

Phase A+ inter-instance comms commit 3 — when the handler successfully
creates a vault event and GCal is configured, it pushes the event to
the Alfred Calendar so Andrew sees it on his phone, and writes the
returned event ID back into the vault frontmatter as ``gcal_event_id``
+ ``gcal_calendar``.

Coverage:
  * No GCal config → vault create succeeds, no GCal call attempted,
    response has no ``gcal_event_id`` field
  * GCal enabled + create_event succeeds → vault frontmatter rewritten
    with gcal_event_id + gcal_calendar; response carries both fields
  * GCal enabled + create_event raises → vault file preserved,
    response carries ``gcal_sync_error`` (no rollback)
  * alfred_calendar_id empty → no API call, response carries error
  * Frontmatter writeback failure → soft fail (event_id still in
    response, log warning)
  * GCal enabled but event proposal hits CONFLICT → no create_event
    call (sync only happens on success path)
"""

from __future__ import annotations

import frontmatter
from datetime import datetime
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
    enabled: bool = True,
):
    from alfred.integrations.gcal_config import GCalConfig
    return GCalConfig(
        enabled=enabled,
        alfred_calendar_id=alfred_id,
        primary_calendar_id="andrew@example.com",
    )


@pytest.fixture
async def app_factory(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
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
        return await aiohttp_client(app)

    return _factory


# ---------------------------------------------------------------------------
# No GCal → no sync, no fields in response (silent skip)
# ---------------------------------------------------------------------------


async def test_no_gcal_no_sync_field_in_response(app_factory):  # type: ignore[no-untyped-def]
    client = await app_factory()
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "no-gcal-test",
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
    assert "gcal_event_id" not in body
    assert "gcal_sync_error" not in body
    # Vault file has no gcal_event_id either.
    vault_root = client.server.app["_vault_root"]
    fm = frontmatter.load(str(vault_root / body["path"]))
    assert "gcal_event_id" not in fm.metadata


# ---------------------------------------------------------------------------
# Happy path: GCal create succeeds → frontmatter has gcal_event_id
# ---------------------------------------------------------------------------


async def test_gcal_sync_happy_path_writes_back_event_id(app_factory):  # type: ignore[no-untyped-def]
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []  # no conflicts
    gcal_client.create_event.return_value = "new-gcal-event-id-42"
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "happy-sync-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Coaching session",
            "summary": "Weekly check-in",
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
    assert body["gcal_event_id"] == "new-gcal-event-id-42"
    assert body["gcal_calendar"] == "alfred"

    # Verify create_event was called with the right shape.
    gcal_client.create_event.assert_called_once()
    kwargs = gcal_client.create_event.call_args.kwargs
    args = gcal_client.create_event.call_args.args
    # First positional arg is the calendar ID.
    assert args[0] == "alfred-cal@group.calendar.google.com"
    assert kwargs["title"] == "Coaching session"
    assert kwargs["description"] == "Weekly check-in"
    # start / end passed through as datetime objects.
    assert isinstance(kwargs["start"], datetime)
    assert isinstance(kwargs["end"], datetime)

    # Verify vault frontmatter has the writeback.
    vault_root = client.server.app["_vault_root"]
    fm = frontmatter.load(str(vault_root / body["path"]))
    assert fm["gcal_event_id"] == "new-gcal-event-id-42"
    assert fm["gcal_calendar"] == "alfred"


# ---------------------------------------------------------------------------
# Sync failure: vault preserved, response carries gcal_sync_error
# ---------------------------------------------------------------------------


async def test_gcal_sync_failure_preserves_vault_record(app_factory):  # type: ignore[no-untyped-def]
    from alfred.integrations.gcal import GCalAPIError

    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.side_effect = GCalAPIError(
        "simulated quota exceeded",
    )
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "fail-sync-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Quota-blocked event",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    # Vault create still succeeds — sync failure does not roll back.
    assert resp.status == 201
    body = await resp.json()
    assert body["status"] == "created"
    assert "gcal_event_id" not in body
    # gcal_sync_error is a structured dict so downstream renderers can
    # switch on ``code`` without parsing free-form ``detail`` text.
    assert "gcal_sync_error" in body
    err = body["gcal_sync_error"]
    assert isinstance(err, dict)
    assert err["code"] == "api_error"  # GCalAPIError → api_error
    assert "quota" in err["detail"].lower()

    # Vault file exists, no gcal_event_id field.
    vault_root = client.server.app["_vault_root"]
    fm = frontmatter.load(str(vault_root / body["path"]))
    assert "gcal_event_id" not in fm.metadata


async def test_gcal_sync_failure_classifies_auth_failed(app_factory):  # type: ignore[no-untyped-def]
    """GCalNotAuthorized → ``code: "auth_failed"`` in the error dict."""
    from alfred.integrations.gcal import GCalNotAuthorized

    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.side_effect = GCalNotAuthorized(
        "token expired and refresh failed",
    )
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "auth-fail-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Auth-blocked event",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 201
    body = await resp.json()
    err = body["gcal_sync_error"]
    assert isinstance(err, dict)
    assert err["code"] == "auth_failed"
    assert "token expired" in err["detail"]


async def test_gcal_sync_failure_classifies_missing_dependency(app_factory):  # type: ignore[no-untyped-def]
    """GCalNotInstalled → ``code: "missing_dependency"``."""
    from alfred.integrations.gcal import GCalNotInstalled

    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.side_effect = GCalNotInstalled(
        "google-auth not installed",
    )
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "missing-dep-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Dep-missing event",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 201
    body = await resp.json()
    err = body["gcal_sync_error"]
    assert isinstance(err, dict)
    assert err["code"] == "missing_dependency"


# ---------------------------------------------------------------------------
# alfred_calendar_id empty → sync skipped with error
# ---------------------------------------------------------------------------


async def test_gcal_sync_skipped_when_calendar_id_empty(app_factory):  # type: ignore[no-untyped-def]
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.return_value = "should-not-be-called"
    gcal_config = _make_gcal_config(alfred_id="")  # empty!

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "empty-id-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Half-configured GCal",
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
    # No create_event call attempted.
    assert gcal_client.create_event.called is False
    # Error surfaced in response — structured dict with calendar_id_missing
    # code so a Hypatia/KAL-LE renderer can switch on the code rather
    # than parsing free-form ``detail`` text.
    assert "gcal_sync_error" in body
    err = body["gcal_sync_error"]
    assert isinstance(err, dict)
    assert err["code"] == "calendar_id_missing"
    assert "alfred_calendar_id" in err["detail"]


# ---------------------------------------------------------------------------
# Conflict path → sync NOT attempted (only happens on successful create)
# ---------------------------------------------------------------------------


async def test_gcal_sync_not_attempted_on_conflict(app_factory):  # type: ignore[no-untyped-def]
    """Vault conflict → 200 conflict response, no GCal create attempted."""
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.return_value = "should-not-be-called"
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    vault_root = client.server.app["_vault_root"]
    # Seed a conflicting vault event.
    import yaml as _yaml
    fm = {
        "type": "event", "name": "Existing",
        "title": "Existing", "start": "2026-05-04T14:00:00-03:00",
        "end": "2026-05-04T15:00:00-03:00",
    }
    text = "---\n" + _yaml.dump(fm) + "---\nbody\n"
    (vault_root / "event" / "Existing.md").write_text(text, encoding="utf-8")

    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "conflict-no-sync-test",
            "start": "2026-05-04T14:30:00-03:00",
            "end": "2026-05-04T15:30:00-03:00",
            "title": "Should not sync",
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
    # Critical: no create_event call.
    assert gcal_client.create_event.called is False


# ---------------------------------------------------------------------------
# Frontmatter rewrite failure is a soft error
# ---------------------------------------------------------------------------


async def test_frontmatter_writeback_failure_is_soft(app_factory, monkeypatch):  # type: ignore[no-untyped-def]
    """If the post-sync frontmatter rewrite raises, response still says created."""
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.return_value = "soft-fail-id-99"
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)

    # Force the frontmatter.dumps to raise.
    import alfred.transport.peer_handlers as ph_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated dumps failure")

    monkeypatch.setattr(ph_mod.frontmatter, "dumps", _boom)

    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "soft-fail-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Soft-fail event",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 201
    body = await resp.json()
    # Created + GCal sync succeeded — only the writeback failed.
    assert body["status"] == "created"
    assert body["gcal_event_id"] == "soft-fail-id-99"
    assert body["gcal_calendar"] == "alfred"
