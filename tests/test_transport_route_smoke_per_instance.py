"""Per-instance route smoke — Salem / KAL-LE / Hypatia configurations.

Extension of ``test_transport_route_smoke.py``. The original smoke
tests a single fully-wired app (every kwarg passed). This file
parametrizes over three instance-shaped configurations to catch
"config X enabled feature Y but didn't wire Z" — Flavor 3 of the
multi-instance wiring antipattern that single-config-smoke can't
surface.

The three personas and what they wire (the contract):

  Salem (canonical owner, full features):
    instance_name="S.A.L.E.M.", canonical.owner=True
    + vault_path, send_fn, pending_items_aggregate_path,
      pending_items_resolve_callable, peer_inbox_callable,
      gcal_client+gcal_config, gcal_intended_on=True
    All 7 register helpers fire. /canonical/* routes operate
    against a real (test) vault.

  KAL-LE (peer-only, no canonical, no GCal, no pending-items aggregate):
    instance_name="KAL-LE", canonical.owner=False
    + vault_path, send_fn, pending_items_resolve_callable,
      peer_inbox_callable
    Skips: pending_items_aggregate_path (KAL-LE doesn't aggregate
      Salem-style cross-instance pushes), gcal_client+gcal_config
      (no calendar integration), gcal_intended_on (no GCal)
    /canonical/* must return 404 ``canonical_not_owned`` (handler
    correctly identifies non-owner), NOT 500.

  Hypatia (similar to KAL-LE but with no peer brief_digest receiver):
    instance_name="HYPATIA", canonical.owner=False
    + vault_path, send_fn, peer_inbox_callable
    Skips: pending_items_aggregate_path,
      pending_items_resolve_callable (Hypatia doesn't do pending-items
      dispatch), gcal_client+gcal_config, gcal_intended_on
    /peer/pending_items_resolve must return 501
    ``peer_inbox_not_configured`` or similar non-500 — the handler
    bails because no resolver is wired.

Each persona's smoke iterates ``app.router.resources()`` and POSTs
to every route, asserting response status is in
``_ACCEPTABLE_STATUSES``. A 500 means a handler tried to read an app
key the instance's config legitimately omitted — that's a wiring
contract violation.

Why three personas and not (yet) the live config files:

  * The live ``config.salem.yaml`` / ``config.kalle.yaml`` /
    ``config.hypatia.yaml`` reference env vars (tokens, paths) that
    don't exist in CI. A test that requires real config files would
    fail at config-load not at wiring.
  * The personas here are built from typed dataclasses with
    test-stub values — exercises the same ``wire_transport_app`` path
    the daemon uses, with the full app surface, but without
    per-environment config-file loading.
  * If a future instance ships with a unique wiring shape
    (V.E.R.A. with RRTS GCal, STAY-C with client-cal), add a fourth
    parametrize entry here that mirrors its kwarg subset.

Why the contract-coverage test below:

  * Piece A enhancement — pins that every register_* helper exported
    from peer_handlers has a corresponding wire_transport_app kwarg.
  * Catches Flavor 3 at PR-review time: "developer added a new
    register helper but forgot the wire_transport_app entry" fails
    this test instead of waiting for production traffic to hit the
    route.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Callable

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    SchedulerConfig,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.server import build_app, wire_transport_app
from alfred.transport.state import TransportState


# Obviously-fake bearer token — builder.md GitGuardian rule.
DUMMY_PER_INSTANCE_TOKEN = (
    "DUMMY_PER_INSTANCE_SMOKE_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTS_X1"
)


# ---------------------------------------------------------------------------
# Test stubs (same shapes as the original smoke test, kept local to
# avoid cross-test fixture coupling)
# ---------------------------------------------------------------------------


async def _stub_send(
    user_id: int,
    text: str,
    dedupe_key: str | None = None,
) -> list[int]:
    return [10001]


async def _stub_resolver(
    *,
    item_id: str,
    resolution: str,
    resolved_at: str | None = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    return {"executed": True, "summary": "ok", "error": None}


async def _stub_peer_inbox(
    *,
    kind: str,
    payload: dict[str, Any],
    from_peer: str,
    correlation_id: str,
) -> dict[str, Any]:
    return {"delivered": True}


# ---------------------------------------------------------------------------
# Per-instance config + wiring builders
# ---------------------------------------------------------------------------


def _base_config(*, owner: bool) -> TransportConfig:
    return TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(
            tokens={
                "smoke-peer": AuthTokenEntry(
                    token=DUMMY_PER_INSTANCE_TOKEN,
                    allowed_clients=["smoke"],
                ),
            },
        ),
        state=StateConfig(),
        canonical=CanonicalConfig(owner=owner),
    )


def _make_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    (vault / "event").mkdir(parents=True)
    return vault


def _wire_salem(
    config: TransportConfig,
    state: TransportState,
    tmp_path: Path,
) -> web.Application:
    """Salem persona — canonical owner, full feature surface."""
    app = build_app(config, state)
    vault = _make_vault(tmp_path)
    aggregate = tmp_path / "aggregate.jsonl"
    aggregate.write_text("", encoding="utf-8")
    wire_transport_app(
        app,
        config,
        instance_name="S.A.L.E.M.",
        instance_alias="Salem",
        vault_path=vault,
        send_fn=_stub_send,
        pending_items_aggregate_path=str(aggregate),
        pending_items_resolve_callable=_stub_resolver,
        peer_inbox_callable=_stub_peer_inbox,
        # Salem opted into GCal — pass a real-shaped stub so the
        # transport handler treats GCal as wired. Stubs come from
        # gcal_sync test patterns: any object with ``enabled`` +
        # ``alfred_calendar_id`` attributes works as the config; the
        # client only matters when the handler actually invokes it.
        gcal_client=_GCalClientStub(),
        gcal_config=_GCalConfigStub(enabled=True),
        gcal_intended_on=True,
    )
    return app


def _wire_kalle(
    config: TransportConfig,
    state: TransportState,
    tmp_path: Path,
) -> web.Application:
    """KAL-LE persona — peer-only, no canonical, no GCal, no aggregate.

    Wires what KAL-LE actually needs: vault for /peer/brief_digest
    storage, send_fn for outbound, pending-items resolver for
    Salem→KAL-LE dispatch, peer_inbox for inbound /peer/send. Skips
    the rest. /canonical/* must return 404 ``canonical_not_owned``
    not 500.
    """
    app = build_app(config, state)
    vault = _make_vault(tmp_path)
    wire_transport_app(
        app,
        config,
        instance_name="KAL-LE",
        instance_alias="kalle",
        vault_path=vault,
        send_fn=_stub_send,
        # No pending_items_aggregate_path — KAL-LE doesn't aggregate
        # peer pushes (Salem does)
        pending_items_resolve_callable=_stub_resolver,
        peer_inbox_callable=_stub_peer_inbox,
        # No GCal — KAL-LE has no calendar integration
    )
    return app


def _wire_hypatia(
    config: TransportConfig,
    state: TransportState,
    tmp_path: Path,
) -> web.Application:
    """Hypatia persona — minimal: vault + send + peer_inbox only.

    Hypatia does not do pending-items dispatch (no resolver) and is
    not a canonical owner. Routes that depend on the missing wirings
    must return non-500 (501 / 404 / etc.).
    """
    app = build_app(config, state)
    vault = _make_vault(tmp_path)
    wire_transport_app(
        app,
        config,
        instance_name="HYPATIA",
        instance_alias="Pat",
        vault_path=vault,
        send_fn=_stub_send,
        # No pending_items_resolve_callable — Hypatia doesn't run
        # the Salem→peer dispatch surface
        peer_inbox_callable=_stub_peer_inbox,
    )
    return app


# Minimal stubs for the GCal kwargs — duck-typed against what the
# transport handlers + skip_check read. Avoids importing real gcal
# config (which would pull in google-auth optional deps in CI).
class _GCalConfigStub:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.alfred_calendar_id = "smoke-cal-id@group.calendar.google.com"
        self.primary_calendar_id = "smoke-primary@example.com"
        self.alfred_calendar_label = "alfred"
        self.default_time_zone = ""


class _GCalClientStub:
    """Minimal GCal client surface — every method returns a benign result.

    Used by the per-instance smoke when wiring Salem's GCal kwargs.
    Real handlers don't invoke GCal during a smoke POST (the
    propose-create handler does conflict-check + sync, but only if
    the schema-validation succeeds; the smoke's body shape may be
    rejected at schema-validation, in which case GCal is never
    called). When the handler does call through, the stub returns a
    plausible event ID so the response is non-500.
    """

    def list_events(self, calendar_id, time_min, time_max):
        return []

    def create_event(
        self, calendar_id, *, start, end, title, description="",
        time_zone=None,
    ):
        return "smoke-gcal-event-id-1"

    def get_event(self, calendar_id, event_id):
        return None

    def delete_event(self, calendar_id, event_id):
        return True

    def update_event(self, *args, **kwargs):
        return None


# ---------------------------------------------------------------------------
# Path-param + body builders (shared with the original smoke; kept
# local since the original test module's helpers are private)
# ---------------------------------------------------------------------------


_PATH_PARAM_VALUES = {
    "id": "smoke-id-123",
    "type": "person",
    "name": "test-person",
}


def _fill_path_params(canonical_path: str) -> str:
    out = canonical_path
    for key, value in _PATH_PARAM_VALUES.items():
        out = out.replace("{" + key + "}", value)
    if "{" in out:
        unmapped = out[out.index("{") : out.index("}") + 1]
        raise KeyError(
            f"Path param {unmapped} in {canonical_path!r} has no test "
            f"value — extend _PATH_PARAM_VALUES."
        )
    return out


def _bodies_by_route() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        ("POST", "/outbound/send"): {
            "user_id": 12345, "text": "smoke ping",
        },
        ("POST", "/outbound/send_batch"): {
            "user_id": 12345, "chunks": ["a", "b"],
        },
        ("POST", "/peer/send"): {
            "kind": "message", "from": "smoke-peer",
            "payload": {"text": "smoke"},
        },
        ("POST", "/peer/query"): {"kind": "ping", "payload": {}},
        ("POST", "/peer/handshake"): {
            "from": "smoke-peer", "protocol_version": 1,
        },
        ("POST", "/peer/brief_digest"): {
            "peer": "smoke-peer", "date": "2026-05-02",
            "digest_markdown": "smoke digest",
        },
        ("POST", "/peer/pending_items_push"): {"items": []},
        ("POST", "/peer/pending_items_resolve"): {
            "item_id": "smoke-1", "resolution": "ok",
        },
        ("POST", "/canonical/event/propose-create"): {
            "title": "Smoke event",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "summary": "smoke",
        },
        ("POST", "/canonical/{type}/propose"): {
            "title": "Smoke proposal", "fields": {},
        },
    }


def _discover_routes(app: web.Application) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for resource in app.router.resources():
        canonical = resource.canonical
        for route in resource:
            method = route.method
            if method in {"HEAD", "OPTIONS"}:
                continue
            out.append((method, canonical))
    return sorted(set(out))


# Same acceptable status set as the original smoke. 500 is the
# load-bearing failure signal.
_ACCEPTABLE_STATUSES = {
    200, 201, 202,
    400, 401, 403, 404,
    405, 409, 415, 422,
    501, 503,
}


# ---------------------------------------------------------------------------
# Per-instance parametrize + smoke
# ---------------------------------------------------------------------------


# Parametrize entries: (persona_name, owner_flag, wire_fn).
# Each persona builds an app with a different wire_transport_app
# kwarg subset — the same pattern as the live daemon's wiring,
# without per-instance config-file loading.
_PERSONAS = [
    pytest.param(
        ("salem", True, _wire_salem),  # canonical owner, full features
        id="salem-canonical-owner-full-features",
    ),
    pytest.param(
        ("kalle", False, _wire_kalle),  # peer-only, no gcal
        id="kalle-peer-only-no-gcal",
    ),
    pytest.param(
        ("hypatia", False, _wire_hypatia),  # minimal, no resolver, no gcal
        id="hypatia-minimal-no-resolver-no-gcal",
    ),
]


@pytest.fixture(params=_PERSONAS)
async def per_instance_client(  # type: ignore[no-untyped-def]
    request,
    aiohttp_client,
    tmp_path,
) -> AsyncIterator[TestClient]:
    """Test client for one persona's wiring shape.

    Each persona builds the app via ``wire_transport_app`` with its
    instance-specific kwarg subset. The smoke then walks every
    route and asserts no 5xx — catching wiring gaps that single-
    persona smoke can't surface.
    """
    persona_name, owner, wire_fn = request.param
    config = _base_config(owner=owner)
    state = TransportState.create(tmp_path / f"{persona_name}_state.json")
    app = wire_fn(config, state, tmp_path)
    client = await aiohttp_client(app)
    # Stash the persona name on the test for failure-message clarity.
    setattr(client, "_persona", persona_name)
    return client


async def test_no_route_returns_500_per_instance(  # type: ignore[no-untyped-def]
    per_instance_client: TestClient,
):
    """For each persona's wiring shape, every route must return non-500.

    A 500 from a route the persona's config legitimately doesn't
    enable means a handler is reading an app key without checking
    that the registration happened. Examples of correct non-500
    rejections:
      - KAL-LE (no canonical) → /canonical/* returns 404
        ``canonical_not_owned``
      - Hypatia (no resolver) → /peer/pending_items_resolve returns
        501 (resolver not registered)
      - Any persona without GCal → /canonical/event/propose-create
        succeeds without GCal sync, returns 201
    """
    persona = getattr(per_instance_client, "_persona", "unknown")
    app = per_instance_client.app  # type: ignore[union-attr]
    routes = _discover_routes(app)
    assert routes, f"persona {persona!r}: no routes discovered"

    bodies = _bodies_by_route()
    headers = {
        "Authorization": f"Bearer {DUMMY_PER_INSTANCE_TOKEN}",
        "X-Alfred-Client": "smoke",
        "Content-Type": "application/json",
    }

    failures: list[str] = []
    for method, canonical in routes:
        url = _fill_path_params(canonical)
        body = bodies.get((method, canonical), {})
        body_text = json.dumps(body)
        if method == "GET":
            resp = await per_instance_client.get(url, headers=headers)
        elif method == "POST":
            resp = await per_instance_client.post(
                url, headers=headers, data=body_text,
            )
        else:
            failures.append(
                f"{persona}: {method} {url} unsupported method"
            )
            continue
        body_resp = await resp.text()
        if resp.status == 500:
            failures.append(
                f"{persona}: {method} {url} → 500 — handler likely "
                f"reading an unregistered app key. "
                f"body_received={body_resp[:300]!r}"
            )
        elif resp.status not in _ACCEPTABLE_STATUSES:
            failures.append(
                f"{persona}: {method} {url} → unexpected {resp.status}. "
                f"body_received={body_resp[:300]!r}"
            )

    if failures:
        msg = "\n".join(f"  - {f}" for f in failures)
        raise AssertionError(
            f"persona {persona!r}: {len(failures)} route(s) failed:\n{msg}"
        )


# ---------------------------------------------------------------------------
# Persona-specific contract assertions — one per persona, hand-written
# rather than parametrized because each pins a different
# correctly-wired-but-rejecting state per the canonical Phase A
# contract
# ---------------------------------------------------------------------------


async def test_kalle_canonical_routes_return_404_not_owned(  # type: ignore[no-untyped-def]
    aiohttp_client, tmp_path,
):
    """KAL-LE explicitly returns 404 ``canonical_not_owned`` on
    /canonical/*. NOT 500 (which would mean the handler tried to
    read vault_path or audit_log without checking ownership first).
    """
    config = _base_config(owner=False)
    state = TransportState.create(tmp_path / "kalle_canonical_test.json")
    app = _wire_kalle(config, state, tmp_path)
    client = await aiohttp_client(app)

    headers = {
        "Authorization": f"Bearer {DUMMY_PER_INSTANCE_TOKEN}",
        "X-Alfred-Client": "smoke",
        "Content-Type": "application/json",
    }
    resp = await client.post(
        "/canonical/event/propose-create",
        headers=headers,
        data=json.dumps({
            "title": "smoke",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
        }),
    )
    body = await resp.text()
    assert resp.status == 404, (
        f"non-owner KAL-LE must return 404 canonical_not_owned, got "
        f"{resp.status}: {body!r}"
    )
    assert "canonical_not_owned" in body, (
        f"404 returned but body lacks canonical_not_owned marker: {body!r}"
    )


async def test_hypatia_pending_resolve_returns_501_when_resolver_unwired(  # type: ignore[no-untyped-def]
    aiohttp_client, tmp_path,
):
    """Hypatia doesn't wire a pending-items resolver. The
    /peer/pending_items_resolve route must return 501 (or
    similar non-500) — NOT crash with KeyError on the missing
    app key.
    """
    config = _base_config(owner=False)
    state = TransportState.create(tmp_path / "hypatia_resolve_test.json")
    app = _wire_hypatia(config, state, tmp_path)
    client = await aiohttp_client(app)

    headers = {
        "Authorization": f"Bearer {DUMMY_PER_INSTANCE_TOKEN}",
        "X-Alfred-Client": "smoke",
        "Content-Type": "application/json",
    }
    resp = await client.post(
        "/peer/pending_items_resolve",
        headers=headers,
        data=json.dumps({"item_id": "x", "resolution": "ok"}),
    )
    body = await resp.text()
    # The exact status depends on the handler's choice (501 / 404);
    # both are correctly-wired-but-rejecting. Pin "not 500" as the
    # contract — the handler must check resolver presence before
    # invoking.
    assert resp.status != 500, (
        f"Hypatia (no resolver wired) must NOT 500 on "
        f"/peer/pending_items_resolve. Got {resp.status}: {body!r}. "
        f"Handler is reading the resolver app key without checking "
        f"presence first — wiring contract violation."
    )


async def test_kalle_outbound_send_works_without_gcal(  # type: ignore[no-untyped-def]
    aiohttp_client, tmp_path,
):
    """KAL-LE has no GCal but the outbound/send route must still
    work. Negative-control for "GCal-skip-doesn't-cascade-to-other-
    surfaces".
    """
    config = _base_config(owner=False)
    state = TransportState.create(tmp_path / "kalle_outbound_test.json")
    app = _wire_kalle(config, state, tmp_path)
    client = await aiohttp_client(app)

    headers = {
        "Authorization": f"Bearer {DUMMY_PER_INSTANCE_TOKEN}",
        "X-Alfred-Client": "smoke",
        "Content-Type": "application/json",
    }
    resp = await client.post(
        "/outbound/send",
        headers=headers,
        data=json.dumps({"user_id": 12345, "text": "ping"}),
    )
    body = await resp.text()
    assert resp.status != 500, (
        f"KAL-LE /outbound/send must work even without GCal. "
        f"Got {resp.status}: {body!r}"
    )


# ---------------------------------------------------------------------------
# Piece A contract test — every register helper has a wire_transport_app
# kwarg
# ---------------------------------------------------------------------------


async def test_every_register_helper_has_wire_transport_app_kwarg():
    """Pin the contract: every ``register_*`` helper exported from
    ``peer_handlers`` (the ones that store an app key) must have a
    corresponding kwarg in ``wire_transport_app``.

    Catches Flavor 3 of the multi-instance wiring antipattern at
    PR-review time: "developer added a new register_* helper but
    forgot the wire_transport_app entry → silent wiring gap until a
    request hits the related endpoint and 500s in production."

    The check is structural (signature inspection), not exhaustive
    (we don't try to call each register helper here — that's what
    the smoke tests do). It catches the most common drift mode.

    Helpers excluded from the check:
      * ``register_peer_routes`` — installs route handlers, not an
        app key. Called from ``build_app``, not ``wire_transport_app``.
      * ``register_canonical_routes`` — same as above.
      * ``register_send_callable`` — defined in ``server.py``, not
        ``peer_handlers.py``. ``wire_transport_app`` calls it
        directly via the ``send_fn`` kwarg.

    The mapping from register-helper name → expected kwarg name is
    the documented contract; mismatches here are bugs:
      register_pending_items_aggregate_path → pending_items_aggregate_path
      register_pending_items_resolve_callable → pending_items_resolve_callable
      register_peer_inbox → peer_inbox_callable
      register_vault_path → vault_path
      register_instance_identity → instance_name (+ instance_alias)
      register_gcal_client → gcal_client (+ gcal_config)
      register_gcal_intended_on → gcal_intended_on
    """
    from alfred.transport import peer_handlers

    # Discover register_* helpers from peer_handlers (skip the
    # route-installer ones — they're called from build_app, not
    # wire_transport_app).
    register_helpers = sorted(
        name for name in dir(peer_handlers)
        if name.startswith("register_")
        and name not in {"register_peer_routes", "register_canonical_routes"}
        and callable(getattr(peer_handlers, name))
    )
    # Sanity baseline — if the discovery returns nothing, the test
    # is misconfigured (broken import, peer_handlers reorg, etc.).
    assert len(register_helpers) >= 5, (
        f"Expected >=5 register_* helpers in peer_handlers, found "
        f"{len(register_helpers)}: {register_helpers}. The discovery "
        f"loop is misconfigured or peer_handlers has been reorganized."
    )

    # Pull wire_transport_app's kwarg names.
    sig = inspect.signature(wire_transport_app)
    wire_kwargs = set(sig.parameters.keys())

    # Documented mapping. New register helpers landing in
    # peer_handlers must be added here (and a corresponding
    # wire_transport_app kwarg must exist) for this test to pass.
    expected_mapping = {
        "register_pending_items_aggregate_path": "pending_items_aggregate_path",
        "register_pending_items_resolve_callable": "pending_items_resolve_callable",
        "register_peer_inbox": "peer_inbox_callable",
        "register_vault_path": "vault_path",
        "register_instance_identity": "instance_name",
        "register_gcal_client": "gcal_client",
        "register_gcal_intended_on": "gcal_intended_on",
    }

    # Every discovered register helper must be in the mapping
    # (catches "new helper added, mapping not updated").
    unmapped = sorted(set(register_helpers) - set(expected_mapping.keys()))
    assert not unmapped, (
        f"register_* helpers in peer_handlers without a documented "
        f"wire_transport_app kwarg mapping: {unmapped}. Either add "
        f"the new helper to wire_transport_app + extend the mapping "
        f"in this test, OR explicitly exclude the helper from the "
        f"discovery loop above with a comment explaining why."
    )

    # Every mapping entry must point to a wire_transport_app kwarg
    # that actually exists (catches "kwarg renamed but mapping not
    # updated").
    missing_kwargs = [
        (helper, kwarg)
        for helper, kwarg in expected_mapping.items()
        if kwarg not in wire_kwargs
    ]
    assert not missing_kwargs, (
        f"Documented mapping references wire_transport_app kwargs "
        f"that don't exist: {missing_kwargs}. Either add the kwarg "
        f"to wire_transport_app's signature OR update the mapping "
        f"to point at the renamed kwarg."
    )


async def test_wire_transport_app_logs_skip_for_omitted_kwargs(
    tmp_path,
):
    """Per ``feedback_intentionally_left_blank.md``: every kwarg
    omission in ``wire_transport_app`` must emit a debug-level skip
    log so an audit can distinguish "feature intentionally not
    wired" from "feature forgotten".

    Builds an app with only the unconditional kwargs (instance_name).
    Every other registration should fire a
    ``transport.wire_transport_app.*_skipped`` event.
    """
    from structlog.testing import capture_logs

    config = _base_config(owner=False)
    state = TransportState.create(tmp_path / "skip_log_test.json")
    app = build_app(config, state)

    with capture_logs() as captured:
        wire_transport_app(
            app,
            config,
            instance_name="MINIMAL",
            # All other kwargs omitted.
        )

    skip_events = sorted(
        c.get("event", "")
        for c in captured
        if c.get("event", "").endswith("_skipped")
    )
    # Pin the expected skip set. Each ``_skipped`` event corresponds
    # to one optional registration that was opted out by omission.
    expected_skips = {
        "transport.wire_transport_app.vault_path_skipped",
        "transport.wire_transport_app.send_fn_skipped",
        "transport.wire_transport_app.pending_items_aggregate_skipped",
        "transport.wire_transport_app.pending_items_resolver_skipped",
        "transport.wire_transport_app.peer_inbox_skipped",
        "transport.wire_transport_app.gcal_skipped",
        "transport.wire_transport_app.gcal_intended_on_skipped",
    }
    actual_skips = set(skip_events)
    missing = expected_skips - actual_skips
    extra = actual_skips - expected_skips
    assert not missing, (
        f"Expected skip-log events not emitted: {sorted(missing)}. "
        f"wire_transport_app must log every omitted kwarg at debug "
        f"level per intentionally-left-blank principle."
    )
    assert not extra, (
        f"Unexpected skip-log events: {sorted(extra)}. Either add "
        f"to expected_skips or remove the new debug log."
    )
