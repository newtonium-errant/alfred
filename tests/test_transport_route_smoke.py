"""Per-route smoke test — every transport route must NOT 500 with proper wiring.

Background: the talker daemon historically wired transport-app
dependencies via individual ``register_*`` helpers, with no single
checkpoint that ensured every needed wiring was in place. By 2026-05-01
six register helpers existed; the daemon was missing one
(``register_vault_path``), causing every ``/canonical/*`` and
``/peer/brief_digest`` request to 500 with ``vault_not_configured``
for the talker daemon's entire lifetime. The hotfix in commit
``f0f8a03`` wired the missing call. The structural fix
(:func:`alfred.transport.server.wire_transport_app`) consolidates
every register call into one explicit-by-omission function.

This module is the safety net **for the next missed wiring**. It
walks every registered route on a fully-wired transport app and
asserts no route returns 500. The test:

* Builds the app via :func:`build_app`.
* Wires every transport-app dependency via
  :func:`wire_transport_app`, using sensible test stubs for every
  callable.
* Walks ``app.router.resources()`` to discover every (method, path).
* Substitutes test values for path parameters
  (``/canonical/{type}/{name}`` → ``/canonical/person/test``).
* Sends one request per route with a valid body shape (auth'd) and
  asserts the response is **not 500** — the failure mode that says
  "handler tried to read an app key that wasn't wired".

If a future contributor adds a new endpoint that needs an app key
but forgets to register it (or to add the key as a
``wire_transport_app`` parameter), this test fires immediately on CI.
It catches what the structural consolidation in
``wire_transport_app`` doesn't enforce — namely, that every handler's
runtime dependencies are registered.

Why not assert the exact expected 4xx for each route? Because the
test is route-discovery-driven, not route-knowledge-driven — adding a
new route to ``ROUTE_NAMESPACES`` should automatically pull it into
the smoke without needing per-route case knowledge here. The single
contract this test pins is: with full wiring, no 500. Anything else
(401, 404, 422, 501, etc.) is a handler-side decision the smoke
shouldn't pre-judge.

Validation that this test would have caught the bug: reverting
``f0f8a03`` (the original ``register_vault_path`` hotfix in the daemon)
no longer makes the smoke fail, because the wiring now lives inside
``wire_transport_app`` and the daemon-source revert wouldn't touch the
test. To prove the smoke catches missing wiring, see
:func:`test_smoke_catches_missing_vault_path_wiring` below — it
manually skips the vault_path kwarg on a freshly-wired app and asserts
that ``/canonical/*`` then returns 500 (which would fail the broader
smoke). That's the "proof of catch" for the regression class.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient

from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    SchedulerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.server import build_app, wire_transport_app
from alfred.transport.state import TransportState


# Obviously-fake bearer token — builder.md GitGuardian rule. Never a
# real provider prefix.
DUMMY_SMOKE_TOKEN = "DUMMY_TRANSPORT_SMOKE_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTS_01234"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _build_full_config() -> TransportConfig:
    """Config with a single test peer + every client allow-listed.

    The smoke needs one valid (token, client) tuple to pass auth; the
    test client uses ``X-Alfred-Client: smoke`` consistently.
    """
    return TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(
            tokens={
                "smoke-peer": AuthTokenEntry(
                    token=DUMMY_SMOKE_TOKEN,
                    allowed_clients=["smoke"],
                ),
            },
        ),
        state=StateConfig(),
    )


def _build_state(tmp_path: Path) -> TransportState:
    return TransportState.create(tmp_path / "transport_state.json")


async def _stub_send(
    user_id: int,
    text: str,
    dedupe_key: str | None = None,
) -> list[int]:
    """Send stub — returns a single fake telegram message id."""
    return [10001]


async def _stub_resolver(
    *,
    item_id: str,
    resolution: str,
    resolved_at: str | None = None,
    correlation_id: str = "",
) -> dict[str, Any]:
    """Pending-items resolver stub — claims success."""
    return {"executed": True, "summary": "ok", "error": None}


async def _stub_peer_inbox(
    *,
    kind: str,
    payload: dict[str, Any],
    from_peer: str,
    correlation_id: str,
) -> dict[str, Any]:
    """Peer-inbox stub — claims success without delivering anywhere."""
    return {"delivered": True}


def _wire_full_app(
    config: TransportConfig,
    state: TransportState,
    vault_path: Path,
    aggregate_path: Path,
) -> web.Application:
    """Build + wire an app with every resource present."""
    app = build_app(config, state)
    wire_transport_app(
        app,
        config,
        instance_name="smoke-instance",
        instance_alias="Smoke",
        vault_path=vault_path,
        send_fn=_stub_send,
        pending_items_aggregate_path=str(aggregate_path),
        pending_items_resolve_callable=_stub_resolver,
        peer_inbox_callable=_stub_peer_inbox,
    )
    return app


@pytest.fixture
async def fully_wired_client(  # type: ignore[no-untyped-def]
    aiohttp_client,
    tmp_path,
) -> AsyncIterator[TestClient]:
    """Test client backed by a fully-wired app + sensible test vault."""
    config = _build_full_config()
    state = _build_state(tmp_path)

    # Minimal vault scaffold — handlers that walk the vault need at
    # least the directory to exist. Empty subdirs are fine; the smoke
    # is asserting "no 500", not "handler returns 200 with content".
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    (vault / "event").mkdir(parents=True)

    aggregate = tmp_path / "aggregate.jsonl"
    aggregate.write_text("", encoding="utf-8")

    app = _wire_full_app(config, state, vault, aggregate)
    client = await aiohttp_client(app)
    return client


# ---------------------------------------------------------------------------
# Path-param substitution + per-route body shapes
# ---------------------------------------------------------------------------


# Map of ``{param_name}`` → test value for path-param substitution.
# Routes that need a value in a parameter slot pull from here.
_PATH_PARAM_VALUES = {
    "id": "smoke-id-123",
    "type": "person",
    "name": "test-person",
}


def _fill_path_params(canonical_path: str) -> str:
    """Substitute test values for ``{param}`` slots in a canonical path.

    ``/outbound/status/{id}`` → ``/outbound/status/smoke-id-123``.
    Unknown params raise ``KeyError`` so a new route with an unmapped
    param surfaces here as a test-config gap rather than silently
    sending a literal ``{param}`` string.
    """
    if "{" not in canonical_path:
        return canonical_path
    out = canonical_path
    # aiohttp canonical paths look like ``/canonical/{type}/{name}``.
    # Walk each ``{...}`` slot and substitute. Sufficient for v1 — no
    # nested or typed param slots in the current route surface.
    for key, value in _PATH_PARAM_VALUES.items():
        out = out.replace("{" + key + "}", value)
    if "{" in out:
        unmapped = out[out.index("{") : out.index("}") + 1]
        raise KeyError(
            f"Path param {unmapped} in {canonical_path!r} has no test "
            f"value in _PATH_PARAM_VALUES — add one before the smoke "
            f"can exercise this route."
        )
    return out


# Per-(method, canonical-path-prefix) body builders. Each route gets a
# minimally-valid body shape so the handler can at least parse it; the
# handler may still return 4xx (auth, schema, not-found) but it
# should NOT 500.
#
# Looked-up by exact canonical path. New routes need an entry here to
# get a sensible body — the default is an empty dict for POSTs.
def _bodies_by_route() -> dict[tuple[str, str], dict[str, Any]]:
    return {
        ("POST", "/outbound/send"): {
            "user_id": 12345,
            "text": "smoke ping",
        },
        ("POST", "/outbound/send_batch"): {
            "user_id": 12345,
            "chunks": ["smoke chunk 1", "smoke chunk 2"],
        },
        ("POST", "/peer/send"): {
            "kind": "message",
            "from": "smoke-peer",
            "payload": {"text": "smoke"},
        },
        ("POST", "/peer/query"): {
            "kind": "ping",
            "payload": {},
        },
        ("POST", "/peer/handshake"): {
            "from": "smoke-peer",
            "protocol_version": 1,
        },
        ("POST", "/peer/brief_digest"): {
            "peer": "smoke-peer",
            "date": "2026-05-02",
            "digest_markdown": "smoke digest body content",
        },
        ("POST", "/peer/pending_items_push"): {
            "items": [],
        },
        ("POST", "/peer/pending_items_resolve"): {
            "item_id": "smoke-item-1",
            "resolution": "smoke-option-a",
        },
        ("POST", "/canonical/event/propose-create"): {
            "title": "Smoke event",
            "start": "2026-05-04T14:00:00-03:00",
            "end": "2026-05-04T15:00:00-03:00",
            "summary": "smoke",
        },
        ("POST", "/canonical/{type}/propose"): {
            "title": "Smoke proposal",
            "fields": {},
        },
    }


# ---------------------------------------------------------------------------
# Route discovery — walk app.router.resources()
# ---------------------------------------------------------------------------


def _discover_routes(app: web.Application) -> list[tuple[str, str]]:
    """Yield (method, canonical_path) for every concrete route on the app.

    Filters out HEAD (auto-mounted by aiohttp for every GET) and
    OPTIONS (no handler), since they're framework-managed and not part
    of the surface we're protecting.
    """
    out: list[tuple[str, str]] = []
    for resource in app.router.resources():
        canonical = resource.canonical
        for route in resource:
            method = route.method
            if method in {"HEAD", "OPTIONS"}:
                continue
            out.append((method, canonical))
    # Stable ordering for diagnostic reproducibility.
    return sorted(set(out))


# ---------------------------------------------------------------------------
# The smoke test
# ---------------------------------------------------------------------------


# The set of HTTP statuses the smoke considers "expected and acceptable
# for an auth'd request with a body shape that may or may not satisfy
# the handler's schema". Anything outside this set (especially 5xx)
# fails the smoke.
_ACCEPTABLE_STATUSES = {
    200, 201, 202,           # success
    400, 401, 403, 404,      # client-side error
    405, 409, 415, 422,      # method/conflict/media/schema
    501, 503,                # not-implemented / not-configured
}


async def test_no_route_returns_500_with_full_wiring(  # type: ignore[no-untyped-def]
    fully_wired_client: TestClient,
):
    """No registered route may 500 when every transport-app key is wired.

    The single contract this test pins: 500 with full wiring means a
    handler is reading an app-storage key that wasn't registered. That's
    the exact regression class the 2026-05-01 vault_not_configured
    incident belonged to. With ``wire_transport_app`` registering every
    known dependency, no handler should fall into that path.

    A handler that returns 4xx (auth, schema, not-found) is fine — that's
    a deliberate rejection. A handler that returns 500 is broken.

    If you've added a new route, also add an entry in ``_bodies_by_route``
    so the smoke can send it a sensible body. The default empty body
    will trigger 400 (schema_error) on most handlers, which is itself
    acceptable — but a route-specific body lets the handler exercise more
    of its happy path before bailing.
    """
    app = fully_wired_client.app  # type: ignore[union-attr]
    routes = _discover_routes(app)
    assert routes, "no routes discovered — app fixture is misconfigured"

    bodies = _bodies_by_route()
    headers = {
        "Authorization": f"Bearer {DUMMY_SMOKE_TOKEN}",
        "X-Alfred-Client": "smoke",
        "Content-Type": "application/json",
    }

    failures: list[str] = []
    for method, canonical in routes:
        url = _fill_path_params(canonical)
        # Match body lookup against the canonical (with placeholders),
        # not the filled URL — placeholders are how routes are
        # registered.
        body = bodies.get((method, canonical), {})
        body_text = json.dumps(body)

        if method == "GET":
            resp = await fully_wired_client.get(url, headers=headers)
        elif method == "POST":
            resp = await fully_wired_client.post(
                url, headers=headers, data=body_text,
            )
        elif method == "PUT":
            resp = await fully_wired_client.put(
                url, headers=headers, data=body_text,
            )
        elif method == "DELETE":
            resp = await fully_wired_client.delete(url, headers=headers)
        else:
            failures.append(
                f"{method} {url} — unsupported method in smoke "
                f"(extend the test if you've added a new method)",
            )
            continue

        body_text_resp = await resp.text()

        if resp.status == 500:
            failures.append(
                f"{method} {url} returned 500 — handler likely missing "
                f"an app-key registration. body_sent={body_text!r} "
                f"body_received={body_text_resp[:300]!r}",
            )
        elif resp.status not in _ACCEPTABLE_STATUSES:
            failures.append(
                f"{method} {url} returned unexpected status "
                f"{resp.status} — neither acceptable 4xx nor a 5xx. "
                f"body_received={body_text_resp[:300]!r}",
            )

    if failures:
        msg = "\n".join(f"  - {f}" for f in failures)
        raise AssertionError(
            f"{len(failures)} route(s) failed the smoke:\n{msg}",
        )


async def test_smoke_catches_missing_vault_path_wiring(  # type: ignore[no-untyped-def]
    aiohttp_client,
    tmp_path,
):
    """Negative-control: a daemon that omits vault_path WILL 500.

    Repros the 2026-05-01 class of bug under a controlled wiring. Builds
    the app via wire_transport_app but explicitly skips ``vault_path``
    (the kwarg defaults to ``None``, which the helper treats as "skip
    this registration"). Then asserts that the canonical handlers
    return 500 with vault_not_configured — proving the broader smoke
    above would catch the same gap if a future daemon forgets the
    kwarg.

    This test is the "proof of catch" — it documents the failure mode
    the broader smoke is designed to detect.
    """
    config = _build_full_config()
    state = _build_state(tmp_path)
    aggregate = tmp_path / "aggregate.jsonl"
    aggregate.write_text("", encoding="utf-8")

    app = build_app(config, state)
    wire_transport_app(
        app,
        config,
        instance_name="smoke-instance",
        # Deliberately omit vault_path — repros the 2026-05-01 bug.
        send_fn=_stub_send,
        pending_items_aggregate_path=str(aggregate),
        pending_items_resolve_callable=_stub_resolver,
        peer_inbox_callable=_stub_peer_inbox,
    )

    client = await aiohttp_client(app)
    headers = {
        "Authorization": f"Bearer {DUMMY_SMOKE_TOKEN}",
        "X-Alfred-Client": "smoke",
        "Content-Type": "application/json",
    }

    # /peer/brief_digest is the literal handler that 500'd in
    # production on 2026-05-01: it explicitly returns 500
    # ``vault_not_configured`` when ``_get_vault_path`` returns None
    # (line ~841 in peer_handlers.py). Other handlers like
    # /canonical/{type}/{name} GET fall through to 404
    # ``record_not_found`` with detail="vault not configured" — they
    # don't 500 on missing vault, but they also can't function. The
    # 500 path is the load-bearing one because it surfaces the
    # configuration gap to the caller as a server error rather than a
    # silent feature-degradation.
    resp = await client.post(
        "/peer/brief_digest",
        headers=headers,
        data=json.dumps({
            # Auth peer is "smoke-peer"; from must match.
            "peer": "smoke-peer",
            "date": "2026-05-02",
            "digest_markdown": "smoke digest body content",
        }),
    )
    body = await resp.text()
    assert resp.status == 500, (
        f"expected 500 vault_not_configured when vault_path is omitted, "
        f"got status={resp.status} body={body!r}. If the brief_digest "
        f"handler no longer 500s on missing vault_path, the structural "
        f"fix may now have a default fallback — update this test to "
        f"reflect the new behavior, and confirm the broader smoke still "
        f"protects against the regression class."
    )
    assert "vault_not_configured" in body, (
        f"500 returned but body lacked vault_not_configured marker: {body!r}"
    )


# ---------------------------------------------------------------------------
# Diagnostic — dump the routes the smoke actually exercises
# ---------------------------------------------------------------------------


async def test_smoke_covers_expected_route_count(  # type: ignore[no-untyped-def]
    fully_wired_client: TestClient,
):
    """Pin the number of routes the smoke exercises.

    A drop in count indicates a route was accidentally removed; an
    increase indicates a route was added. Either way the contributor
    should look at this test, confirm the new shape is intentional, and
    bump the expected count.

    Current surface (2026-05-02):
      * /outbound/send, /outbound/send_batch, /outbound/status/{id}
      * /peer/send, /peer/query, /peer/handshake, /peer/brief_digest,
        /peer/pending_items_push, /peer/pending_items_resolve
      * /canonical/event/propose-create, /canonical/{type}/propose,
        /canonical/{type}/{name}
      * /health
    Total: 13.
    """
    app = fully_wired_client.app  # type: ignore[union-attr]
    routes = _discover_routes(app)
    expected = 13
    assert len(routes) == expected, (
        f"Expected {expected} routes, found {len(routes)}: {routes}. "
        f"If you've added or removed a transport route, update this "
        f"count and confirm _bodies_by_route still covers the surface."
    )
