"""P2 fixes for the GCal Phase A+ ship.

Coverage:
  P2-1 — ``alfred_calendar_label`` is config-driven (defaults to
         ``"alfred"``; per-instance override flows through to vault
         frontmatter + response payload)
  P2-2 — ``default_time_zone`` propagates to ``GCalClient.create_event``
         when set; omitted when empty (no ``time_zone`` kwarg)
  P2-3 — ``ConflictSource`` constants resolve to the documented
         ``vault`` / ``gcal_alfred`` / ``gcal_primary`` strings (this
         pins the JSON-boundary contract)
  P2-4 — sentinel ``_KEY_GCAL_INTENDED_ON`` flips the skip-site log
         level from debug to warning; daemon-source pin confirms the
         talker daemon sets the flag BEFORE attempting client
         construction so a failure preserves the intent
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import frontmatter
import pytest
from aiohttp.test_utils import TestClient

from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    PeerFieldRules,
    SchedulerConfig,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import (
    ConflictSource,
    _KEY_GCAL_INTENDED_ON,
    register_gcal_client,
    register_gcal_intended_on,
    register_instance_identity,
    register_vault_path,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState


DUMMY_KALLE_PEER_TOKEN = "DUMMY_KALLE_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"


# ---------------------------------------------------------------------------
# P2-3: ConflictSource enum constants pin the JSON-boundary string values
# ---------------------------------------------------------------------------


def test_conflict_source_constants_are_stable_strings():
    """The ``source`` field's possible values are documented contract.

    Downstream renderers (Hypatia / KAL-LE) switch on these strings.
    Renaming a constant value (e.g. ``"vault"`` → ``"vault_record"``)
    would silently break every consumer that expected the old form.
    """
    assert ConflictSource.VAULT == "vault"
    assert ConflictSource.GCAL_ALFRED == "gcal_alfred"
    assert ConflictSource.GCAL_PRIMARY == "gcal_primary"


def test_conflict_source_values_are_distinct():
    """Sanity: no two constants resolve to the same string."""
    values = {
        ConflictSource.VAULT,
        ConflictSource.GCAL_ALFRED,
        ConflictSource.GCAL_PRIMARY,
    }
    assert len(values) == 3


# ---------------------------------------------------------------------------
# P2-1 + P2-2: GCalConfig new fields load + default cleanly
# ---------------------------------------------------------------------------


def test_gcal_config_new_fields_default():
    from alfred.integrations.gcal_config import load_from_unified

    cfg = load_from_unified({})
    # Defaults: label "alfred" so existing Salem-style config keeps
    # producing the same vault frontmatter; tz "" so display falls
    # back to calendar's own zone (no behaviour change).
    assert cfg.alfred_calendar_label == "alfred"
    assert cfg.default_time_zone == ""


def test_gcal_config_new_fields_loaded():
    from alfred.integrations.gcal_config import load_from_unified

    cfg = load_from_unified({
        "gcal": {
            "enabled": True,
            "alfred_calendar_label": "rrts",
            "default_time_zone": "America/Halifax",
        }
    })
    assert cfg.alfred_calendar_label == "rrts"
    assert cfg.default_time_zone == "America/Halifax"


def test_gcal_config_label_null_falls_back_to_alfred():
    """Explicit ``null`` in YAML → "alfred" default (back-compat)."""
    from alfred.integrations.gcal_config import load_from_unified

    cfg = load_from_unified({
        "gcal": {"alfred_calendar_label": None}
    })
    assert cfg.alfred_calendar_label == "alfred"


def test_gcal_config_label_empty_string_preserved():
    """Empty-string label is a valid override (caller wants no label)."""
    from alfred.integrations.gcal_config import load_from_unified

    cfg = load_from_unified({
        "gcal": {"alfred_calendar_label": ""}
    })
    assert cfg.alfred_calendar_label == ""


def test_gcal_config_tz_null_falls_back_to_empty():
    from alfred.integrations.gcal_config import load_from_unified

    cfg = load_from_unified({
        "gcal": {"default_time_zone": None}
    })
    assert cfg.default_time_zone == ""


# ---------------------------------------------------------------------------
# Shared test fixtures (Salem-style app + helpers)
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
                    "event": PeerFieldRules(
                        fields=["name", "title", "start", "end"],
                    ),
                },
            },
        ),
        peers={},
    )


def _make_gcal_config(
    *,
    alfred_id: str = "alfred-cal@group.calendar.google.com",
    label: str = "alfred",
    time_zone: str = "",
    enabled: bool = True,
):
    from alfred.integrations.gcal_config import GCalConfig
    return GCalConfig(
        enabled=enabled,
        alfred_calendar_id=alfred_id,
        primary_calendar_id="andrew@example.com",
        alfred_calendar_label=label,
        default_time_zone=time_zone,
    )


@pytest.fixture
async def app_factory(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    audit_path = tmp_path / "canonical_audit.jsonl"
    config = _make_transport_config(audit_path)
    state = TransportState.create(tmp_path / "transport_state.json")
    vault_root = tmp_path / "vault"
    (vault_root / "event").mkdir(parents=True)

    async def _factory(
        *,
        gcal_client=None,
        gcal_config=None,
        gcal_intended_on: bool = False,
    ) -> TestClient:
        app = build_app(config, state)
        register_vault_path(app, vault_root)
        register_instance_identity(app, name="S.A.L.E.M.", alias="Salem")
        if gcal_client is not None and gcal_config is not None:
            register_gcal_client(app, gcal_client, gcal_config)
        if gcal_intended_on:
            register_gcal_intended_on(app)
        app["_vault_root"] = vault_root
        return await aiohttp_client(app)

    return _factory


# ---------------------------------------------------------------------------
# P2-1: alfred_calendar_label flows to vault frontmatter + response payload
# ---------------------------------------------------------------------------


async def test_custom_calendar_label_writes_to_frontmatter(app_factory):  # type: ignore[no-untyped-def]
    """V.E.R.A.-style ``label: "rrts"`` lands in vault + response."""
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.return_value = "rrts-event-id-7"
    gcal_config = _make_gcal_config(label="rrts")

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "label-rrts-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "RRTS pickup",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 201
    body = await resp.json()
    assert body["gcal_calendar"] == "rrts"

    # Vault frontmatter carries the same label.
    vault_root = client.server.app["_vault_root"]
    fm = frontmatter.load(str(vault_root / body["path"]))
    assert fm["gcal_calendar"] == "rrts"


async def test_default_calendar_label_unchanged(app_factory):  # type: ignore[no-untyped-def]
    """Salem's default ``"alfred"`` label still works (back-compat)."""
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.return_value = "default-id-1"
    gcal_config = _make_gcal_config()  # label defaults to "alfred"

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "label-default-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Default-label event",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    body = await resp.json()
    assert body["gcal_calendar"] == "alfred"


async def test_empty_calendar_label_falls_back_to_alfred(app_factory):  # type: ignore[no-untyped-def]
    """Defensive: empty-string label still emits ``"alfred"`` so the
    response field is never blank for downstream renderers."""
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.return_value = "fallback-id-1"
    gcal_config = _make_gcal_config(label="")  # empty override

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "label-empty-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Empty-label fallback",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    body = await resp.json()
    assert body["gcal_calendar"] == "alfred"


# ---------------------------------------------------------------------------
# P2-2: default_time_zone propagates to create_event
# ---------------------------------------------------------------------------


async def test_time_zone_propagates_to_create_event(app_factory):  # type: ignore[no-untyped-def]
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.return_value = "tz-event-id-1"
    gcal_config = _make_gcal_config(time_zone="America/Halifax")

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "tz-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "Halifax-display event",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 201
    gcal_client.create_event.assert_called_once()
    kwargs = gcal_client.create_event.call_args.kwargs
    assert kwargs.get("time_zone") == "America/Halifax"


async def test_empty_time_zone_omits_kwarg(app_factory):  # type: ignore[no-untyped-def]
    """Empty default_time_zone → no ``time_zone`` kwarg at all (lets
    the calendar's own zone govern display)."""
    gcal_client = MagicMock()
    gcal_client.list_events.return_value = []
    gcal_client.create_event.return_value = "tz-omit-id-1"
    gcal_config = _make_gcal_config(time_zone="")

    client = await app_factory(gcal_client=gcal_client, gcal_config=gcal_config)
    resp = await client.post(
        "/canonical/event/propose-create",
        json={
            "correlation_id": "tz-omit-test",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "title": "No tz override",
            "origin_instance": "kal-le",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 201
    gcal_client.create_event.assert_called_once()
    kwargs = gcal_client.create_event.call_args.kwargs
    # Critical: no time_zone kwarg, not None, just absent.
    assert "time_zone" not in kwargs


# ---------------------------------------------------------------------------
# P2-4: sentinel-aware skip logging
# ---------------------------------------------------------------------------


def test_register_gcal_intended_on_sets_storage_key(tmp_path):
    """Helper writes the intended-on flag under the documented key."""
    from aiohttp import web
    app = web.Application()
    assert app.get(_KEY_GCAL_INTENDED_ON) is None
    register_gcal_intended_on(app)
    assert app[_KEY_GCAL_INTENDED_ON] is True


async def test_intended_on_skip_logs_warning_in_conflict_check(  # type: ignore[no-untyped-def]
    app_factory,
):
    """Conflict-check skip with sentinel set → warning event; without → no warning.

    The handler still returns a clean 201 (vault-only flow); only the
    log signal differs. This is the operator-visibility fix.

    Uses :func:`structlog.testing.capture_logs` rather than pytest's
    ``caplog`` because the warning fires from inside aiohttp's request
    handler (event-loop coroutine). Pytest's ``caplog`` intercepts via
    the root stdlib logger's handler chain, but this codebase's
    ``alfred.transport.utils.get_logger`` returns a structlog
    BoundLogger configured with ``LoggerFactory()`` + ``ConsoleRenderer``
    — the rendered output reaches stdout via the formatter but the
    intermediate ``LogRecord`` doesn't reliably propagate up to
    caplog's handler when emit happens off the test thread (aiohttp
    request handlers run in the test client's task).

    ``capture_logs`` hooks structlog's processor pipeline directly, so
    it sees every event structlog emits regardless of stdlib's
    plumbing or which thread/task does the emit. Each captured entry
    is a dict with keys like ``event`` (the first positional arg to
    ``log.warning(...)``), ``log_level``, plus every bound kwarg.
    """
    from structlog.testing import capture_logs

    # Sentinel set, no client wired (simulates startup-failure state).
    client = await app_factory(gcal_intended_on=True)
    with capture_logs() as captured:
        resp = await client.post(
            "/canonical/event/propose-create",
            json={
                "correlation_id": "intended-on-no-client-test",
                "start": "2026-05-04T14:00:00-03:00",
                "end": "2026-05-04T15:00:00-03:00",
                "title": "GCal intended on but broken",
                "origin_instance": "kal-le",
            },
            headers={
                "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
                "X-Alfred-Client": "kal-le",
            },
        )
    assert resp.status == 201
    # Warning event emitted for both phases (conflict_check + sync).
    warnings = [
        c for c in captured
        if c.get("log_level") == "warning"
        and "skipped_but_intended_on" in c.get("event", "")
    ]
    assert len(warnings) >= 1, (
        f"expected >=1 'skipped_but_intended_on' warning, got 0. "
        f"Captured events: {[c.get('event') for c in captured]}"
    )
    # Bonus: confirm the structured fields the operator hint relies on
    # actually made it onto the event (catches a regression where the
    # hint kwarg gets renamed or dropped).
    first = warnings[0]
    assert "hint" in first, f"missing hint kwarg on warning: {first}"
    assert "alfred gcal status" in first["hint"]


async def test_no_sentinel_skip_stays_quiet(app_factory):  # type: ignore[no-untyped-def]
    """No sentinel + no client → no ``skipped_but_intended_on`` warning.

    Same ``capture_logs`` pattern as the sibling test — pytest
    ``caplog`` doesn't see structlog events emitted from aiohttp
    request handlers in this codebase (see sibling test's docstring
    for the full diagnosis). Asserting absence is even more sensitive
    to the capture mechanism than asserting presence, because a
    coincidentally-empty ``caplog.records`` would pass the test for
    the wrong reason.
    """
    from structlog.testing import capture_logs

    client = await app_factory()  # no client, no sentinel
    with capture_logs() as captured:
        resp = await client.post(
            "/canonical/event/propose-create",
            json={
                "correlation_id": "no-sentinel-test",
                "start": "2026-05-04T14:00:00-03:00",
                "end": "2026-05-04T15:00:00-03:00",
                "title": "Vault-only by design",
                "origin_instance": "kal-le",
            },
            headers={
                "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
                "X-Alfred-Client": "kal-le",
            },
        )
    assert resp.status == 201
    # No "intended_on" warning emitted.
    intended_warnings = [
        c for c in captured
        if c.get("log_level") == "warning"
        and "skipped_but_intended_on" in c.get("event", "")
    ]
    assert intended_warnings == [], (
        f"expected 0 'skipped_but_intended_on' warnings (sentinel "
        f"not set), got {len(intended_warnings)}: "
        f"{[c.get('event') for c in intended_warnings]}"
    )


def test_talker_daemon_sets_intended_on_before_client_construction():
    """Pin the source-text contract: gcal_intended_on flips True the
    moment ``gcal.enabled`` is observed, BEFORE GCalClient() is called.

    This is what makes the sentinel useful: a credentials-file-missing
    failure during construction must not reset the flag, otherwise the
    handler can't distinguish "intentionally off" from "intent set but
    setup broke".
    """
    here = Path(__file__).resolve().parent
    daemon_path = here.parent / "src" / "alfred" / "telegram" / "daemon.py"
    source = daemon_path.read_text(encoding="utf-8")

    # Sentinel default-False at the top of the GCal block.
    assert "gcal_intended_on = False" in source, (
        "talker daemon must initialise gcal_intended_on=False before "
        "the load_gcal block — without the var existing, the wire "
        "kwarg below references an undefined name."
    )

    # Sentinel set before client construction (the load-bearing line).
    # We assert the order textually: ``gcal_intended_on = True`` must
    # appear BEFORE ``GCalClient(`` in the source.
    intent_idx = source.find("gcal_intended_on = True")
    construct_idx = source.find("GCalClient(")
    assert intent_idx > 0, "gcal_intended_on must be flipped True somewhere"
    assert construct_idx > 0, "GCalClient construction must exist"
    assert intent_idx < construct_idx, (
        "gcal_intended_on = True must appear BEFORE GCalClient() so a "
        "construction failure preserves the operator's intent for the "
        "transport handler's skip-site warnings."
    )

    # Sentinel passed through to wire_transport_app.
    assert "gcal_intended_on=gcal_intended_on" in source, (
        "wire_transport_app must receive gcal_intended_on=gcal_intended_on; "
        "without it the transport app never sees the sentinel and the "
        "operator-visibility fix silently no-ops."
    )
