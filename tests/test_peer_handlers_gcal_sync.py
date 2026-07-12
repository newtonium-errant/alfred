"""Tests for GCal sync-on-create in /canonical/event/propose-create.

Phase A+ inter-instance comms commit 3 — when the handler successfully
creates a vault event and GCal is configured, it pushes the event to
Andrew's Calendar (S.A.L.E.M.) so Andrew sees it on his phone, and
writes the returned event ID back into the vault frontmatter as
``gcal_event_id`` + ``gcal_calendar``.

Coverage:
  * No GCal config → vault create succeeds, no GCal call attempted,
    response has no ``gcal_event_id`` field AND no ``gcal_sync`` key
    (absent = "GCal didn't participate")
  * GCal enabled + create_event succeeds → vault frontmatter rewritten
    with gcal_event_id + gcal_calendar; response carries both fields
    PLUS ``gcal_sync: {status: ok}`` (unified-shape contract)
  * GCal enabled + create_event raises → vault file preserved,
    response carries ``gcal_sync: {status: failed, error_code, error}``
    (no rollback). Replaces the older ``gcal_sync_error`` field.
  * alfred_calendar_id empty → no API call, response carries
    ``gcal_sync: {status: failed, error_code: calendar_id_missing}``
  * Frontmatter writeback failure → soft fail (event_id still in
    response, ``gcal_sync.status == "ok"``, log warning)
  * GCal enabled but event proposal hits CONFLICT → no create_event
    call (sync only happens on success path)
  * Legacy-field regression pin: ``gcal_sync_error`` MUST NOT appear
    in any response shape — the unified ``gcal_sync`` field replaces
    it. Same-day convergence with the in-process tool_result contract
    from :func:`alfred.vault.ops.translate_gcal_sync_result`.
"""

from __future__ import annotations

import time

import frontmatter
import structlog
from datetime import datetime
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient

import alfred.transport.peer_handlers as peer_handlers
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
    # No GCal action attempted → ``gcal_sync`` omitted entirely
    # (absent = "GCal didn't participate", NOT "succeeded silently").
    assert "gcal_sync" not in body
    # Legacy-field regression pin (Build #82 unification).
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
    # Unified contract: success carries ``gcal_sync: {status: ok}`` so
    # a partial-failure consumer doesn't have to special-case the
    # success branch (mirrors in-process vault_create tool_result).
    assert body["gcal_sync"] == {"status": "ok"}
    # Legacy-field regression pin.
    assert "gcal_sync_error" not in body

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
# Sync failure: vault preserved, response carries gcal_sync.status=failed
# (replaces the older ``gcal_sync_error`` field — see module docstring).
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
    # Legacy-field regression pin: ``gcal_sync_error`` is replaced by
    # the unified ``gcal_sync`` field (Build #82 convergence).
    assert "gcal_sync_error" not in body
    # ``gcal_sync`` is a structured dict so downstream renderers can
    # switch on ``error_code`` without parsing free-form ``error`` text.
    assert "gcal_sync" in body
    sync = body["gcal_sync"]
    assert isinstance(sync, dict)
    assert sync["status"] == "failed"
    assert sync["error_code"] == "api_error"  # GCalAPIError → api_error
    assert "quota" in sync["error"].lower()

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
    assert "gcal_sync_error" not in body  # legacy-field regression pin
    sync = body["gcal_sync"]
    assert isinstance(sync, dict)
    assert sync["status"] == "failed"
    assert sync["error_code"] == "auth_failed"
    assert "token expired" in sync["error"]


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
    assert "gcal_sync_error" not in body  # legacy-field regression pin
    sync = body["gcal_sync"]
    assert isinstance(sync, dict)
    assert sync["status"] == "failed"
    assert sync["error_code"] == "missing_dependency"


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
    # Legacy-field regression pin.
    assert "gcal_sync_error" not in body
    # Error surfaced in the unified response — structured dict with
    # ``error_code: calendar_id_missing`` so a Hypatia/KAL-LE renderer
    # can switch on the code rather than parsing free-form ``error`` text.
    assert "gcal_sync" in body
    sync = body["gcal_sync"]
    assert isinstance(sync, dict)
    assert sync["status"] == "failed"
    assert sync["error_code"] == "calendar_id_missing"
    assert "alfred_calendar_id" in sync["error"]


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

    # Force the frontmatter.dumps to raise. Post-refactor (vault-ops
    # hook ship), the writeback lives in
    # ``alfred.integrations.gcal_sync`` — patching ``peer_handlers``'s
    # frontmatter no longer affects the path. Patch the actual module
    # that owns the writeback now.
    import alfred.integrations.gcal_sync as sync_mod

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated dumps failure")

    monkeypatch.setattr(sync_mod.frontmatter, "dumps", _boom)

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
    # The sync layer logs + swallows, so the LLM-facing surface
    # still reads "ok" (which is correct — GCal DOES have the event).
    assert body["status"] == "created"
    assert body["gcal_event_id"] == "soft-fail-id-99"
    assert body["gcal_calendar"] == "alfred"
    assert body["gcal_sync"] == {"status": "ok"}
    assert "gcal_sync_error" not in body  # legacy-field regression pin


# ---------------------------------------------------------------------------
# Build #37 — create-succeeded-but-client-saw-error (STT-idempotency class).
#
# Two post-commit failure classes must NEVER surface to the caller as an
# error on a committed create:
#   (1) the GCal sync raises a NON-``GCalError`` after the vault commit
#       (cold discovery build() / OAuth refresh failure) —
#       ``sync_event_create_to_gcal`` only contains ``GCalError``, so
#       anything else would 500 the handler AFTER the vault write.
#   (2) the GCal sync is SLOW past the server-side per-phase deadline —
#       without a bound the peer client read-times-out and retries into the
#       409 already_exists path while the create actually landed (the
#       2026-07-09 Victoria-concert incident).
# In both cases the response is a 201 ``created`` carrying the committed
# ``path`` plus an accurate ``gcal_sync`` status — the caller's FIRST call
# gets the truth, no retry-into-dedup guessing.
# ---------------------------------------------------------------------------


async def test_gcal_sync_non_gcalerror_post_commit_returns_created(app_factory):  # type: ignore[no-untyped-def]
    """Class (1): a raw exception from create_event (e.g. discovery build()
    blowing up) is contained post-commit — the handler returns 201 created
    with ``gcal_sync.status == "failed"`` instead of a 500."""
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []  # no conflicts
    # NOT a GCalError — sync_event_create_to_gcal's inner except only
    # catches GCalError, so this propagates all the way to the handler.
    gcal_client.create_event.side_effect = RuntimeError(
        "Failed to build Calendar service: discovery fetch failed",
    )
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    with structlog.testing.capture_logs() as cap:
        resp = await client.post(
            "/canonical/event/propose-create",
            json={
                "correlation_id": "non-gcalerror-post-commit",
                "start": "2026-05-04T14:00:00-03:00",
                "end": "2026-05-04T15:00:00-03:00",
                "title": "Build service blows up",
                "origin_instance": "kal-le",
            },
            headers={
                "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
                "X-Alfred-Client": "kal-le",
            },
        )
    # The create is committed — a post-commit sync explosion is NOT a 500.
    assert resp.status == 201
    body = await resp.json()
    assert body["status"] == "created"
    assert body["path"]  # caller gets the committed path on the first call
    assert "gcal_event_id" not in body
    sync = body["gcal_sync"]
    assert sync["status"] == "failed"
    # RuntimeError is not a GCalError subclass → classify → "unknown".
    assert sync["error_code"] == "unknown"
    assert "discovery" in sync["error"].lower()

    # Log-emission pin (feedback_log_emission_test_pattern): the contained
    # post-commit failure must be observable, with the exception type.
    matches = [
        c for c in cap
        if c.get("event") == "transport.canonical.event_propose_gcal_sync_failed"
    ]
    assert len(matches) == 1
    assert matches[0]["error_type"] == "RuntimeError"

    # Vault record preserved — the sync failure did not roll it back.
    vault_root = client.server.app["_vault_root"]
    fm = frontmatter.load(str(vault_root / body["path"]))
    assert "gcal_event_id" not in fm.metadata


async def test_gcal_sync_timeout_returns_committed_success(app_factory, monkeypatch):  # type: ignore[no-untyped-def]
    """Class (2), THE incident: a slow-but-successful GCal sync exceeds the
    server-side deadline → the handler still returns the committed 201 with
    ``error_code == "sync_timeout"`` (never a client-visible timeout that
    triggers a retry-into-409)."""
    # Shrink the deadline so the test doesn't have to block for 6 real
    # seconds. The handler reads the module global at call time.
    monkeypatch.setattr(peer_handlers, "_GCAL_PHASE_DEADLINE_S", 0.2)

    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []  # conflict scan is fast

    def _slow_create(*args, **kwargs):
        # Blocks the worker thread past the deadline. The orphaned thread
        # finishes in the background (Python threads aren't cancellable).
        time.sleep(0.8)
        return "eventually-landed-id"

    gcal_client.create_event.side_effect = _slow_create
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    with structlog.testing.capture_logs() as cap:
        resp = await client.post(
            "/canonical/event/propose-create",
            json={
                "correlation_id": "slow-sync-timeout",
                "start": "2026-05-04T14:00:00-03:00",
                "end": "2026-05-04T15:00:00-03:00",
                "title": "Slow sync but committed",
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
    assert body["path"]  # committed create surfaced on the FIRST call
    assert "gcal_event_id" not in body
    sync = body["gcal_sync"]
    assert sync["status"] == "failed"
    assert sync["error_code"] == "sync_timeout"

    # Log-emission pin: the deadline breach is observable with the budget.
    matches = [
        c for c in cap
        if c.get("event") == "transport.canonical.event_propose_gcal_sync_timeout"
    ]
    assert len(matches) == 1
    assert matches[0]["deadline_s"] == 0.2


async def test_gcal_slow_conflict_scan_degrades_and_still_creates(app_factory, monkeypatch):  # type: ignore[no-untyped-def]
    """Companion to the timeout fix: the conflict-scan is ALSO a blocking
    GCal round trip (and carries the cold-start cost). A slow scan degrades
    to vault-only conflicts and the create still proceeds within the client
    deadline, returning a normal committed success."""
    monkeypatch.setattr(peer_handlers, "_GCAL_PHASE_DEADLINE_S", 0.2)

    gcal_client = MagicMock()

    def _slow_list(*args, **kwargs):
        time.sleep(0.8)  # blocks past the deadline
        return []

    gcal_client.list_events.side_effect = _slow_list
    gcal_client.create_event.return_value = "created-after-degrade-id"
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    with structlog.testing.capture_logs() as cap:
        resp = await client.post(
            "/canonical/event/propose-create",
            json={
                "correlation_id": "slow-conflict-scan",
                "start": "2026-05-04T14:00:00-03:00",
                "end": "2026-05-04T15:00:00-03:00",
                "title": "Slow conflict scan",
                "origin_instance": "kal-le",
            },
            headers={
                "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
                "X-Alfred-Client": "kal-le",
            },
        )
    # Conflict scan timed out → degraded to vault-only (no conflict) → the
    # create proceeds and the sync (fast create_event) succeeds normally.
    assert resp.status == 201
    body = await resp.json()
    assert body["status"] == "created"
    assert body["gcal_sync"] == {"status": "ok"}
    assert gcal_client.create_event.called is True

    # Log-emission pin: the conflict-scan deadline breach is observable.
    matches = [
        c for c in cap
        if c.get("event") == "transport.canonical.event_propose_gcal_conflict_timeout"
    ]
    assert len(matches) == 1


async def test_gcal_409_already_exists_returns_before_sync(app_factory):  # type: ignore[no-untyped-def]
    """Regression pin: a filename collision (same title + date) still short-
    circuits to 409 already_exists BEFORE any GCal create is attempted — the
    threading/bounding changes must not move the 409 gate past the sync."""
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []  # no conflicts
    gcal_client.create_event.return_value = "first-create-id"
    gcal_config = _make_gcal_config()

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    headers = {
        "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
        "X-Alfred-Client": "kal-le",
    }
    resp1 = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "collide-first",
            "start": "2026-07-01T10:00:00-03:00",
            "end": "2026-07-01T11:00:00-03:00",
            "title": "Recurring Standup",
            "origin_instance": "kal-le",
        },
        headers=headers,
    )
    assert resp1.status == 201

    # Same title + same date, non-overlapping later slot → conflict-check
    # passes, filename collides → 409, and NO second create_event fires.
    resp2 = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "collide-second",
            "start": "2026-07-01T13:00:00-03:00",
            "end": "2026-07-01T14:00:00-03:00",
            "title": "Recurring Standup",
            "origin_instance": "kal-le",
        },
        headers=headers,
    )
    assert resp2.status == 409
    body = await resp2.json()
    assert body["status"] == "exists"
    assert body["path"] == "event/Recurring Standup 2026-07-01.md"
    # Sync only ran for the first (successful) create — the 409 path never
    # reached the GCal sync.
    assert gcal_client.create_event.call_count == 1
