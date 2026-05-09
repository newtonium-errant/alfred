"""Tests for ``alfred.transport.server``.

Uses aiohttp's ``AiohttpClient`` test fixture to spin up the real
``Application`` and exercise it end-to-end — auth middleware, route
dispatch, dedupe, scheduled-vs-immediate branching, 501 stubs, 503
when telegram isn't wired.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    SchedulerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.server import build_app, register_send_callable
from alfred.transport.state import TransportState


# Fake bearer token used across every test — the obviously-fake pattern
# per builder.md's GitGuardian post-mortem. Never a real ``sk-``/``gsk-``
# prefix in test fixtures.
DUMMY_TRANSPORT_TEST_TOKEN = "DUMMY_TRANSPORT_TEST_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234"


def _build_config(
    *,
    allowed_clients: list[str] | None = None,
    extra_peers: dict[str, AuthTokenEntry] | None = None,
) -> TransportConfig:
    tokens: dict[str, AuthTokenEntry] = {
        "local": AuthTokenEntry(
            token=DUMMY_TRANSPORT_TEST_TOKEN,
            allowed_clients=allowed_clients or ["scheduler", "brief", "janitor"],
        ),
    }
    if extra_peers:
        tokens.update(extra_peers)
    return TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens=tokens),
        state=StateConfig(),
    )


def _build_state(tmp_path: Path) -> TransportState:
    return TransportState.create(tmp_path / "transport_state.json")


@pytest.fixture
async def client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Client wired with default config + working send stub."""
    config = _build_config()
    state = _build_state(tmp_path)

    sent: list[dict[str, Any]] = []

    async def _send_stub(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        sent.append({"user_id": user_id, "text": text, "dedupe_key": dedupe_key})
        return [1000 + len(sent)]

    app = build_app(config, state, send_fn=_send_stub)
    app["_test_sent"] = sent
    app["_test_state"] = state
    tc: TestClient = await aiohttp_client(app)
    return tc


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------


async def test_health_is_public(client):  # type: ignore[no-untyped-def]
    """/health does not require auth — it's the bootstrap probe."""
    resp = await client.get("/health")
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "ok"
    assert body["telegram_connected"] is True
    assert body["queue_depth"] == 0


async def test_send_rejects_missing_auth_header(client):  # type: ignore[no-untyped-def]
    resp = await client.post("/outbound/send", json={"user_id": 1, "text": "hi"})
    assert resp.status == 401


async def test_send_rejects_wrong_token(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": "Bearer wrong-token",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "invalid_token"


async def test_send_rejects_client_not_in_allowlist(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "unregistered_client",
        },
    )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "client_not_allowed"
    assert body["peer"] == "local"


# ---------------------------------------------------------------------------
# Claim-first-then-verify auth (Stage 3.5 multi-peer)
#
# Refactor 2026-05-09 (Batch B) closes the fragility flagged 2026-05-01
# in ``feedback_per_peer_token_uniqueness.md``: previously the auth
# middleware iterated ``config.auth.tokens.items()`` and matched the
# FIRST token-equality. If two peer pairs (e.g. KAL-LE + STAY-C)
# accidentally shared a token, iter() picked one arbitrarily and the
# OTHER client got rejected with ``client_not_allowed`` based on the
# wrong peer's allowlist.
#
# The new resolver (``_resolve_peer_for_auth``) does claim-first lookup
# (X-Alfred-Client header → peer key), THEN verifies the token. v1
# internal clients (scheduler / brief / ...) preserve the legacy
# iter+token-match path because their client_name isn't a peer key.
# ---------------------------------------------------------------------------


# Distinct dummy tokens per peer — obvious-fake patterns, no
# realistic provider prefix per builder.md's GitGuardian rule.
DUMMY_KALLE_TEST_TOKEN = "DUMMY_KALLE_TEST_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234567"
DUMMY_STAYC_TEST_TOKEN = "DUMMY_STAYC_TEST_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234567"


@pytest.fixture
async def multi_peer_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Client wired with three peers: local + kal-le + stay-c.

    Each peer has its OWN distinct token + an allowlist that names just
    its own client. This is the Stage 3.5 shape — Salem's transport
    server fields requests from co-located internal clients (via the
    ``local`` entry) AND from cross-instance peers (via the per-peer
    entries).
    """
    extra_peers = {
        "kal-le": AuthTokenEntry(
            token=DUMMY_KALLE_TEST_TOKEN,
            allowed_clients=["kal-le"],
        ),
        "stay-c": AuthTokenEntry(
            token=DUMMY_STAYC_TEST_TOKEN,
            allowed_clients=["stay-c"],
        ),
    }
    config = _build_config(extra_peers=extra_peers)
    state = _build_state(tmp_path)

    async def _send_stub(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        return [42]

    app = build_app(config, state, send_fn=_send_stub)
    tc: TestClient = await aiohttp_client(app)
    return tc


async def test_claim_first_routes_kal_le_to_its_own_peer(  # type: ignore[no-untyped-def]
    multi_peer_client,
):
    """KAL-LE's request matches the kal-le peer (not local) via claim-first.

    The X-Alfred-Client header value (``kal-le``) is itself a peer key in
    the server's tokens dict. Phase 1 resolves directly to that peer and
    verifies its token. Pre-fix this DID work (KAL-LE's token is
    distinct from local's), but only because iter()-order happened to
    visit them in registration order — the new path is deterministic
    by design, not by happenstance.
    """
    resp = await multi_peer_client.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_TEST_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 200, await resp.text()


async def test_claim_first_routes_stay_c_to_its_own_peer(  # type: ignore[no-untyped-def]
    multi_peer_client,
):
    """Companion to the kal-le test — stay-c lookup is independent.

    Pinned separately to lock both peers, not just whichever happens to
    be the iter()-first entry.
    """
    resp = await multi_peer_client.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_STAYC_TEST_TOKEN}",
            "X-Alfred-Client": "stay-c",
        },
    )
    assert resp.status == 200, await resp.text()


async def test_claim_first_rejects_kal_le_with_stay_c_token(  # type: ignore[no-untyped-def]
    multi_peer_client,
):
    """The headline regression pin for the per-peer-uniqueness fragility.

    A request claiming X-Alfred-Client: kal-le but presenting STAY-C's
    token must reject — phase 1 looks up ``kal-le`` directly, finds the
    kal-le entry, compares its token against STAY-C's presented token,
    finds mismatch, falls through to phase 2. Phase 2 then matches
    STAY-C by token-equality, but STAY-C's allowlist is ``[stay-c]``,
    so the post-match ``allowed_clients`` gate refuses with
    ``client_not_allowed``.

    Pre-fix shape: iter() picked an arbitrary peer-by-token-match;
    behavior depended on dict insertion order. Post-fix: deterministic
    rejection regardless of order.
    """
    resp = await multi_peer_client.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_STAYC_TEST_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 401
    body = await resp.json()
    # Resolver flow: phase 1 finds kal-le entry, token mismatches
    # (kal-le's token != stay-c's presented token), falls through.
    # Phase 2 iter+token-match finds stay-c (whose token matches the
    # presented value). Middleware then runs allowed_clients gate
    # against stay-c's allowlist (["stay-c"]) — client_name "kal-le"
    # is NOT in it → reject with client_not_allowed, peer=stay-c.
    assert body["error"] == "client_not_allowed"
    assert body["peer"] == "stay-c"


async def test_claim_first_unknown_client_falls_through_to_legacy(  # type: ignore[no-untyped-def]
    multi_peer_client,
):
    """v1 internal clients preserve legacy iter+token-match path.

    A request from ``scheduler`` (an internal client, NOT a peer key)
    presents the local token. Phase 1's direct lookup misses (no
    ``scheduler`` entry in tokens). Phase 2 falls through to legacy
    iter+token-match — finds ``local`` via token-equality. Then the
    ``allowed_clients`` gate confirms ``scheduler`` is in
    ``local.allowed_clients`` and the request proceeds.

    This is the load-bearing v1-compat pin — without it the refactor
    would break every co-located client.
    """
    resp = await multi_peer_client.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 200, await resp.text()


async def test_claim_first_rejects_matching_client_wrong_token(  # type: ignore[no-untyped-def]
    multi_peer_client,
):
    """Phase 1 token mismatch + phase 2 no token match → invalid_token.

    Request claims X-Alfred-Client: kal-le but presents a junk token.
    Phase 1 finds the kal-le peer entry, compare_digest returns False.
    Falls through to phase 2; no peer's token matches the junk; resolver
    returns (None, "invalid_token") and middleware emits 401.
    """
    resp = await multi_peer_client.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": "Bearer junk-token-not-matching-anything",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "invalid_token"


# --- Resolver-level unit tests (skip the aiohttp middleware) ---------------
#
# These exercise ``_resolve_peer_for_auth`` directly to pin the
# claim-first-then-verify branching at the function boundary, without
# the aiohttp request plumbing. Each one corresponds to a distinct
# branch in the resolver.


def test_resolver_claim_first_direct_match() -> None:
    """Phase 1 happy path: client_name matches a peer key + token matches."""
    from alfred.transport.server import _resolve_peer_for_auth

    tokens = {
        "kal-le": AuthTokenEntry(token=DUMMY_KALLE_TEST_TOKEN),
        "stay-c": AuthTokenEntry(token=DUMMY_STAYC_TEST_TOKEN),
    }
    peer, reason = _resolve_peer_for_auth(
        tokens, client_name="kal-le", presented_token=DUMMY_KALLE_TEST_TOKEN,
    )
    assert peer == "kal-le"
    assert reason == ""


def test_resolver_claim_first_falls_through_on_token_mismatch() -> None:
    """Phase 1 finds the peer but token mismatches; phase 2 finds the
    REAL peer that owns the presented token. The fragility-fix headline
    case: claim-first doesn't lock you to a wrong-peer answer when the
    claim is wrong but the token is real."""
    from alfred.transport.server import _resolve_peer_for_auth

    tokens = {
        "kal-le": AuthTokenEntry(token=DUMMY_KALLE_TEST_TOKEN),
        "stay-c": AuthTokenEntry(token=DUMMY_STAYC_TEST_TOKEN),
    }
    # Claim kal-le but present stay-c's real token — phase 1 mismatches,
    # phase 2 finds stay-c by token-match.
    peer, reason = _resolve_peer_for_auth(
        tokens, client_name="kal-le", presented_token=DUMMY_STAYC_TEST_TOKEN,
    )
    assert peer == "stay-c"
    assert reason == ""


def test_resolver_legacy_path_for_internal_clients() -> None:
    """v1 internal clients (scheduler, brief, ...) miss phase 1 — their
    client_name isn't a peer key. Phase 2 finds ``local`` by token-match."""
    from alfred.transport.server import _resolve_peer_for_auth

    tokens = {
        "local": AuthTokenEntry(
            token=DUMMY_TRANSPORT_TEST_TOKEN,
            allowed_clients=["scheduler", "brief"],
        ),
    }
    peer, reason = _resolve_peer_for_auth(
        tokens, client_name="scheduler",
        presented_token=DUMMY_TRANSPORT_TEST_TOKEN,
    )
    assert peer == "local"
    assert reason == ""


def test_resolver_no_match_returns_invalid_token() -> None:
    """Both phases miss → (None, "invalid_token")."""
    from alfred.transport.server import _resolve_peer_for_auth

    tokens = {
        "local": AuthTokenEntry(token=DUMMY_TRANSPORT_TEST_TOKEN),
    }
    peer, reason = _resolve_peer_for_auth(
        tokens, client_name="kal-le", presented_token="nope",
    )
    assert peer is None
    assert reason == "invalid_token"


def test_resolver_empty_token_rejected() -> None:
    """Empty presented token never matches anything (defensive)."""
    from alfred.transport.server import _resolve_peer_for_auth

    tokens = {
        "local": AuthTokenEntry(token=DUMMY_TRANSPORT_TEST_TOKEN),
    }
    peer, reason = _resolve_peer_for_auth(
        tokens, client_name="local", presented_token="",
    )
    assert peer is None
    assert reason == "invalid_token"


def test_resolver_skips_empty_token_entries() -> None:
    """A peer entry with ``token=""`` (misconfigured) never matches —
    even when the request presents some real-shaped token. The defense:
    phase 1's ``direct_entry.token`` truthiness guard skips empty
    entries; phase 2's ``if not entry.token: continue`` does the same.

    Without this guard, a misconfigured peer with ``token=""`` would
    match every request whose presented token also happened to be empty
    (or — worse — could create false-positive match ambiguity if the
    string-comparison short-circuited on length-zero).
    """
    from alfred.transport.server import _resolve_peer_for_auth

    tokens = {
        "broken-peer": AuthTokenEntry(token=""),  # misconfig
        "local": AuthTokenEntry(token=DUMMY_TRANSPORT_TEST_TOKEN),
    }
    # Claim broken-peer with a non-empty fake token. Phase 1: entry
    # exists but entry.token is empty → guard skips. Phase 2 iterates
    # — broken-peer skipped (empty), local doesn't match the fake token.
    # No legitimate match.
    peer, reason = _resolve_peer_for_auth(
        tokens, client_name="broken-peer",
        presented_token="fake-but-non-empty-token",
    )
    assert peer is None
    assert reason == "invalid_token"


# ---------------------------------------------------------------------------
# Outbound send — immediate
# ---------------------------------------------------------------------------


async def test_send_immediate_delivers_via_send_fn(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={"user_id": 123, "text": "Reminder: call Dr Bailey"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "sent"
    assert body["telegram_message_id"] == 1001

    # The stub recorded the send.
    sent = client.app["_test_sent"]
    assert len(sent) == 1
    assert sent[0]["user_id"] == 123
    assert sent[0]["text"] == "Reminder: call Dr Bailey"

    # State persisted the send_log entry.
    state: TransportState = client.app["_test_state"]
    assert len(state.send_log) == 1
    assert state.send_log[0]["user_id"] == 123
    assert state.send_log[0]["telegram_message_ids"] == [1001]


async def test_send_rejects_missing_fields(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={"user_id": "not-an-int", "text": ""},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert "user_id" in body["error"]


async def test_send_with_scheduled_at_queues_instead_of_dispatching(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={
            "user_id": 123,
            "text": "Future reminder",
            "scheduled_at": "2099-01-01T00:00:00+00:00",
            "dedupe_key": "reminder-future",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "scheduled"
    assert body["id"]
    # No immediate dispatch happened.
    assert client.app["_test_sent"] == []
    # Entry now lives in pending_queue.
    state: TransportState = client.app["_test_state"]
    assert len(state.pending_queue) == 1


async def test_send_dedupe_returns_same_entry(client):  # type: ignore[no-untyped-def]
    """A send with a matching dedupe_key in the 24h window returns ``duplicate``."""
    first = await client.post(
        "/outbound/send",
        json={
            "user_id": 123,
            "text": "Only once",
            "dedupe_key": "brief-2026-04-20",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "brief",
        },
    )
    assert first.status == 200

    second = await client.post(
        "/outbound/send",
        json={
            "user_id": 123,
            "text": "Only once (retry)",
            "dedupe_key": "brief-2026-04-20",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "brief",
        },
    )
    assert second.status == 200
    body = await second.json()
    assert body["status"] == "duplicate"

    # send_fn was only called once.
    assert len(client.app["_test_sent"]) == 1


# ---------------------------------------------------------------------------
# Outbound send_batch
# ---------------------------------------------------------------------------


async def test_send_batch_delivers_each_chunk_in_order(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send_batch",
        json={
            "user_id": 123,
            "chunks": ["part 1", "part 2", "part 3"],
            "dedupe_key": "brief-2026-04-20",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "brief",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["sent_count"] == 3
    assert len(body["telegram_message_ids"]) == 3
    # send_fn called once per chunk, in order.
    sent = client.app["_test_sent"]
    assert [s["text"] for s in sent] == ["part 1", "part 2", "part 3"]


async def test_send_batch_rejects_empty_chunks(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send_batch",
        json={"user_id": 123, "chunks": []},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "brief",
        },
    )
    assert resp.status == 400


# ---------------------------------------------------------------------------
# Status lookup
# ---------------------------------------------------------------------------


async def test_status_returns_sent_entry(client):  # type: ignore[no-untyped-def]
    resp = await client.post(
        "/outbound/send",
        json={"user_id": 123, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    body = await resp.json()
    entry_id = body["id"]

    lookup = await client.get(
        f"/outbound/status/{entry_id}",
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert lookup.status == 200
    lb = await lookup.json()
    assert lb["status"] == "sent"
    assert lb["telegram_message_ids"]


async def test_status_unknown_returns_404(client):  # type: ignore[no-untyped-def]
    resp = await client.get(
        "/outbound/status/never-existed",
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 404


# ---------------------------------------------------------------------------
# 501 stubs — Stage 3.5 pre-commit
# ---------------------------------------------------------------------------


async def test_peer_routes_registered_not_501(client):  # type: ignore[no-untyped-def]
    """Stage 3.5 c3: /peer/* routes are live, not 501 stubs.

    /peer/send with no inbox registered returns 501
    ``peer_inbox_not_configured`` (distinct from the old
    ``peer_not_implemented``) — proves the new handler ran, not the
    stub. /peer/handshake returns 200 even without an inbox.
    """
    # /peer/send without inbox → 501 peer_inbox_not_configured
    resp = await client.post(
        "/peer/send",
        json={"kind": "message", "from": "local", "payload": {"text": "hi"}},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 501
    body = await resp.json()
    assert body["reason"] == "peer_inbox_not_configured"

    # /peer/handshake always returns 200 (no authz beyond bearer).
    resp = await client.post(
        "/peer/handshake",
        json={},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert "protocol_version" in body
    assert "capabilities" in body


async def test_canonical_route_not_owned(client):  # type: ignore[no-untyped-def]
    """Default transport config has ``canonical.owner: False`` → 404 canonical_not_owned."""
    resp = await client.get(
        "/canonical/person/Andrew Newton",
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 404
    body = await resp.json()
    assert body["reason"] == "canonical_not_owned"


# ---------------------------------------------------------------------------
# 503 when Telegram isn't configured
# ---------------------------------------------------------------------------


async def test_send_returns_503_when_no_callable_registered(
    aiohttp_client, tmp_path,  # type: ignore[no-untyped-def]
):
    """An unregistered send callable yields 503 telegram_not_configured."""
    config = _build_config()
    state = _build_state(tmp_path)
    app = build_app(config, state, send_fn=None)
    tc: TestClient = await aiohttp_client(app)

    resp = await tc.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 503
    body = await resp.json()
    assert body["reason"] == "telegram_not_configured"

    # /health still works and reports telegram_connected: False.
    health = await tc.get("/health")
    hb = await health.json()
    assert hb["telegram_connected"] is False


async def test_register_send_callable_enables_delivery(
    aiohttp_client, tmp_path,  # type: ignore[no-untyped-def]
):
    """``register_send_callable`` swaps a live callable onto a built app."""
    config = _build_config()
    state = _build_state(tmp_path)
    app = build_app(config, state, send_fn=None)
    # Register after build — same path the talker daemon uses.
    async def _stub(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        return [555]

    register_send_callable(app, _stub)
    tc: TestClient = await aiohttp_client(app)
    resp = await tc.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert resp.status == 200


# ---------------------------------------------------------------------------
# Multi-peer token dict — Stage 3.5 D2/D7 pre-commit sanity
# ---------------------------------------------------------------------------


async def test_multiple_peer_tokens_each_authenticate_independently(
    aiohttp_client, tmp_path,  # type: ignore[no-untyped-def]
):
    """Two entries in auth.tokens each authenticate with their own secret.

    This is the Stage 3.5 D2 pre-commit contract: the schema already
    supports per-peer tokens; adding a second peer (``kal-le``) is a
    config-only change.
    """
    kal_token = "DUMMY_KAL_LE_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_01234567890123"
    config = _build_config(
        extra_peers={
            "kal-le": AuthTokenEntry(
                token=kal_token,
                allowed_clients=["kal-le-router"],
            ),
        },
    )
    state = _build_state(tmp_path)

    async def _stub(user_id: int, text: str, dedupe_key: str | None = None) -> list[int]:
        return [777]

    app = build_app(config, state, send_fn=_stub)
    tc: TestClient = await aiohttp_client(app)

    # Local token with local client works.
    ok_local = await tc.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi"},
        headers={
            "Authorization": f"Bearer {DUMMY_TRANSPORT_TEST_TOKEN}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert ok_local.status == 200

    # Kal-le's token is accepted — its allowed_clients includes
    # kal-le-router.
    ok_kal = await tc.post(
        "/outbound/send",
        json={"user_id": 1, "text": "hi-kal"},
        headers={
            "Authorization": f"Bearer {kal_token}",
            "X-Alfred-Client": "kal-le-router",
        },
    )
    assert ok_kal.status == 200

    # Kal-le's token with a local client fails (client not allowed
    # under that peer's allowlist).
    bad = await tc.post(
        "/outbound/send",
        json={"user_id": 1, "text": "mismatch"},
        headers={
            "Authorization": f"Bearer {kal_token}",
            "X-Alfred-Client": "scheduler",
        },
    )
    assert bad.status == 401
