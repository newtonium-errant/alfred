"""Async query-broker (kind=query on /peer/send) — disclosure parity + audit.

The P-lane placeholder path. The SECURITY-CRITICAL property: the async
broker reuses the IDENTICAL ``_execute_filtered_search`` core as the
synchronous ``/peer/search`` — same fail-closed gates, same field gate,
same ``kind:"search"`` audit. These tests prove parity by running the
SAME query through both surfaces and asserting identical disclosure +
audit, and pin that the async denial path discloses nothing more.

The broker replies out-of-band via an outbound ``peer_send`` (kind=
query_result); we monkeypatch that to capture the reply payload instead
of making a real HTTP call.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from alfred.transport.canonical_audit import read_audit
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    FilterDimRule,
    PeerEntry,
    PeerFieldRules,
    PeerQueryRules,
    SchedulerConfig,
    ServerConfig,
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


DUMMY_HYPATIA_PEER_TOKEN = "DUMMY_HYPATIA_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_01234"


def _hypatia_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {DUMMY_HYPATIA_PEER_TOKEN}",
        "X-Alfred-Client": "hypatia",
        # body.from must equal the authenticated peer (anti-spoof).
    }


@pytest.fixture
async def salem_app(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Salem app: canonical owner, Hypatia permissioned to SEARCH events.

    Mirrors test_peer_search_handler's fixture so the parity assertions
    compare like-for-like. Registers a no-op peer_inbox so /peer/send's
    non-query kinds don't 501 (the broker handles kind=query BEFORE the
    inbox lookup, so the inbox is irrelevant to the query path — but
    registering it keeps the message/notice regression path live).
    """
    audit_path = tmp_path / "canonical_audit.jsonl"
    config = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={
            "hypatia": AuthTokenEntry(
                token=DUMMY_HYPATIA_PEER_TOKEN,
                allowed_clients=["hypatia"],
            ),
        }),
        state=StateConfig(),
        canonical=CanonicalConfig(
            owner=True,
            audit_log_path=str(audit_path),
            peer_permissions={
                "hypatia": {
                    "event": PeerFieldRules(
                        fields=["name", "title", "date", "participants"],
                        query=PeerQueryRules(
                            filter_dims={
                                "participants": FilterDimRule(op=["eq", "contains"]),
                                "date": FilterDimRule(op=["gte", "lte", "between"]),
                            },
                            sort=["date"],
                            max_limit=10,
                            default_limit=5,
                        ),
                    ),
                    "person": PeerFieldRules(fields=["name", "email"]),
                },
            },
        ),
        # Salem must know hypatia as an OUTBOUND peer to reply query_result.
        peers={
            "hypatia": PeerEntry(
                base_url="http://127.0.0.1:8893",
                token=DUMMY_HYPATIA_PEER_TOKEN,
            ),
        },
    )
    state = TransportState.create(tmp_path / "transport_state.json")

    vault_root = tmp_path / "vault"
    (vault_root / "event").mkdir(parents=True)

    def _event(name: str, date: str, participants: list[str], title: str) -> None:
        plist = "\n".join(f"  - '{p}'" for p in participants)
        (vault_root / "event" / f"{name}.md").write_text(
            f"---\nname: {name}\ntype: event\ntitle: {title}\n"
            f"date: {date}\nsecret_notes: do not leak\n"
            f"participants:\n{plist}\n---\nBody never exposed.\n",
            encoding="utf-8",
        )

    _event("Coffee with Andrew", "2026-05-30",
           ["[[person/Andrew Newton]]"], "Coffee chat")
    _event("Old meeting", "2025-02-01",
           ["[[person/Andrew Newton]]"], "Old one")
    _event("Jamie sync", "2026-06-01",
           ["[[person/Jamie Newton]]"], "Jamie only")

    app = build_app(config, state)
    register_vault_path(app, vault_root)
    register_instance_identity(app, name="S.A.L.E.M.", alias="Salem")

    async def _noop_inbox(**kwargs: Any) -> dict[str, Any]:
        return {"relayed": False}

    register_peer_inbox(app, _noop_inbox)
    app["_audit_path"] = audit_path

    tc: TestClient = await aiohttp_client(app)
    return tc


def _patch_capture_reply(monkeypatch) -> list[dict[str, Any]]:
    """Patch the broker's outbound peer_send to capture the query_result.

    The async broker fires ``peer_send(peer, "query_result", payload, ...)``
    as a detached task. We replace ``alfred.transport.client.peer_send``
    with a capture so the reply payload is recorded instead of POSTed.
    Returns the list the capture appends to.
    """
    captured: list[dict[str, Any]] = []

    async def _fake_peer_send(
        peer_name: str, kind: str, payload: dict[str, Any], **kwargs: Any,
    ) -> dict[str, Any]:
        captured.append({
            "peer_name": peer_name, "kind": kind, "payload": payload,
            "correlation_id": kwargs.get("correlation_id"),
        })
        return {"status": "accepted"}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "peer_send", _fake_peer_send)
    return captured


# ---------------------------------------------------------------------------
# Disclosure PARITY — async broker == /peer/search
# ---------------------------------------------------------------------------


async def test_async_query_discloses_identically_to_search(salem_app, monkeypatch):  # type: ignore[no-untyped-def]
    """SECURITY: kind=query returns the SAME records as /peer/search.

    Run the same participant filter through both surfaces; the async
    broker's query_result records must equal the synchronous search's
    records (same field gate, same disclosure).
    """
    captured = _patch_capture_reply(monkeypatch)

    query_body = {
        "record_type": "event",
        "filter": [
            {"dim": "participants", "op": "contains", "value": "Andrew Newton"},
        ],
        "sort": {"by": "date", "dir": "desc"},
        "limit": 1,
    }

    # Synchronous /peer/search.
    sync_resp = await salem_app.post(
        "/peer/search", json=query_body, headers=_hypatia_headers(),
    )
    sync_body = await sync_resp.json()

    # Async kind=query on /peer/send (precedence P).
    async_resp = await salem_app.post(
        "/peer/send",
        json={
            "kind": "query",
            "from": "hypatia",
            "payload": {**query_body, "precedence": "P"},
            "correlation_id": "cid-parity-1",
        },
        headers=_hypatia_headers(),
    )
    assert async_resp.status == 200
    ack = await async_resp.json()
    assert ack["status"] == "accepted"
    assert ack["precedence"] == "P"

    # Let the detached reply task run.
    await asyncio.sleep(0)

    assert len(captured) == 1
    reply = captured[0]
    assert reply["kind"] == "query_result"
    assert reply["correlation_id"] == "cid-parity-1"
    # PARITY: the async reply's records == the sync search's records.
    assert reply["payload"]["status"] == "ok"
    assert reply["payload"]["records"] == sync_body["records"]
    assert reply["payload"]["count"] == sync_body["count"]


async def test_async_query_never_exposes_body_or_unpermitted(salem_app, monkeypatch):  # type: ignore[no-untyped-def]
    """SECURITY: the async reply withholds bodies + unpermitted fields too."""
    import json

    captured = _patch_capture_reply(monkeypatch)

    await salem_app.post(
        "/peer/send",
        json={
            "kind": "query",
            "from": "hypatia",
            "payload": {
                "record_type": "event",
                "filter": [
                    {"dim": "participants", "op": "contains", "value": "Andrew Newton"},
                ],
                "fields": ["title", "date", "secret_notes", "content", "body"],
                "precedence": "P",
            },
            "correlation_id": "cid-leak-1",
        },
        headers=_hypatia_headers(),
    )
    await asyncio.sleep(0)

    reply = captured[0]["payload"]
    serialized = json.dumps(reply)
    assert "Body never exposed" not in serialized
    assert "secret_notes" not in serialized
    assert "do not leak" not in serialized


async def test_async_query_denied_when_type_not_queryable(salem_app, monkeypatch):  # type: ignore[no-untyped-def]
    """SECURITY: async denial discloses nothing — status=denied, no records."""
    captured = _patch_capture_reply(monkeypatch)

    await salem_app.post(
        "/peer/send",
        json={
            "kind": "query",
            "from": "hypatia",
            "payload": {
                "record_type": "person",  # has fields but NO query block
                "filter": [{"dim": "name", "op": "eq", "value": "x"}],
                "precedence": "P",
            },
            "correlation_id": "cid-deny-1",
        },
        headers=_hypatia_headers(),
    )
    await asyncio.sleep(0)

    reply = captured[0]["payload"]
    assert reply["status"] == "denied"
    assert reply["code"] == "filtered_query_not_permitted"
    assert "records" not in reply  # denial discloses no records


async def test_async_query_denied_on_unlisted_dim(salem_app, monkeypatch):  # type: ignore[no-untyped-def]
    captured = _patch_capture_reply(monkeypatch)
    await salem_app.post(
        "/peer/send",
        json={
            "kind": "query",
            "from": "hypatia",
            "payload": {
                "record_type": "event",
                "filter": [{"dim": "secret_notes", "op": "contains", "value": "x"}],
                "precedence": "P",
            },
            "correlation_id": "cid-deny-2",
        },
        headers=_hypatia_headers(),
    )
    await asyncio.sleep(0)
    reply = captured[0]["payload"]
    assert reply["status"] == "denied"
    assert reply["code"] == "filter_dim_denied"


# ---------------------------------------------------------------------------
# Audit parity — async query writes the SAME kind:"search" audit row
# ---------------------------------------------------------------------------


async def test_async_query_audited_same_as_search(salem_app, monkeypatch):  # type: ignore[no-untyped-def]
    """The async broker audits with kind:"search" + the predicate, identically."""
    _patch_capture_reply(monkeypatch)
    await salem_app.post(
        "/peer/send",
        json={
            "kind": "query",
            "from": "hypatia",
            "payload": {
                "record_type": "event",
                "filter": [
                    {"dim": "participants", "op": "contains", "value": "Andrew Newton"},
                ],
                "limit": 1,
                "precedence": "P",
            },
            "correlation_id": "cid-audit-1",
        },
        headers=_hypatia_headers(),
    )
    await asyncio.sleep(0)
    audit = read_audit(salem_app.app["_audit_path"])
    rows = [r for r in audit if r.get("kind") == "search"
            and r.get("correlation_id") == "cid-audit-1"]
    assert len(rows) == 1
    row = rows[0]
    assert row["peer"] == "hypatia"
    assert row["type"] == "event"
    assert row["match_count"] == 1
    assert row["filter"][0]["value"] == "Andrew Newton"


# ---------------------------------------------------------------------------
# Regression: existing /peer/send kinds still work
# ---------------------------------------------------------------------------


async def test_message_kind_still_routes_to_inbox(salem_app):  # type: ignore[no-untyped-def]
    """REGRESSION: a plain message still reaches the inbox callable (200)."""
    resp = await salem_app.post(
        "/peer/send",
        json={
            "kind": "message",
            "from": "hypatia",
            "payload": {"text": "hello"},
            "correlation_id": "cid-msg-1",
        },
        headers=_hypatia_headers(),
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["status"] == "accepted"


async def test_bad_kind_still_400(salem_app):  # type: ignore[no-untyped-def]
    """REGRESSION: an unknown kind still 400s schema_error."""
    resp = await salem_app.post(
        "/peer/send",
        json={"kind": "bogus", "from": "hypatia", "payload": {}},
        headers=_hypatia_headers(),
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"


# ---------------------------------------------------------------------------
# GC-hazard regression — the detached reply task is strongly referenced
# ---------------------------------------------------------------------------


async def test_detached_reply_task_is_retained_until_complete(salem_app, monkeypatch):  # type: ignore[no-untyped-def]
    """The reply task must survive until ``peer_send`` completes.

    A bare ``asyncio.create_task`` is held only by a WEAK ref in the loop;
    once the handler returns the 200 ack, nothing references the task and
    it can be GC'd mid-flight — the requester's ``await_response`` then
    never gets the reply and waits out the full timeout. The fix parks the
    task in ``request.app["_bg_tasks"]`` and discards on completion.

    This test widens the GC window: the fake ``peer_send`` does a real
    multi-``await`` (two ``sleep(0)`` yields) so the reply is NOT delivered
    in a single tick. We assert (a) the task is referenced on the app
    WHILE in flight, (b) the reply still lands after the awaits resolve,
    and (c) the set is emptied (discard callback fired) once complete. A
    bare create_task would let the reply still land HERE (the test holds a
    ref via the loop) — so the load-bearing assertion is (a): the app-set
    is non-empty mid-flight, which only the strong-ref fix produces.
    """
    captured: list[dict[str, Any]] = []
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def _slow_peer_send(
        peer_name: str, kind: str, payload: dict[str, Any], **kwargs: Any,
    ) -> dict[str, Any]:
        # Signal we've entered the outbound send, then yield across
        # multiple awaits so the task is genuinely mid-flight while the
        # handler has already returned its ack.
        in_flight.set()
        await release.wait()
        await asyncio.sleep(0)
        captured.append({"kind": kind, "payload": payload,
                         "correlation_id": kwargs.get("correlation_id")})
        return {"status": "accepted"}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "peer_send", _slow_peer_send)

    resp = await salem_app.post(
        "/peer/send",
        json={
            "kind": "query",
            "from": "hypatia",
            "payload": {
                "record_type": "event",
                "filter": [
                    {"dim": "participants", "op": "contains", "value": "Andrew Newton"},
                ],
                "precedence": "P",
            },
            "correlation_id": "cid-gc-1",
        },
        headers=_hypatia_headers(),
    )
    # Handler returned its ack already.
    assert resp.status == 200

    # Let the detached task reach the outbound send (it's now mid-flight,
    # parked on ``release``). The reply has NOT been captured yet.
    await in_flight.wait()
    assert captured == []  # still in flight — reply not sent

    # LOAD-BEARING: the task is strongly referenced on the app while in
    # flight. A bare create_task (the bug) would leave nothing holding it.
    bg_tasks = salem_app.app["_bg_tasks"]
    assert len(bg_tasks) >= 1

    # Release the send; let it finish across its remaining awaits.
    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # The reply landed.
    assert len(captured) == 1
    assert captured[0]["correlation_id"] == "cid-gc-1"

    # The done-callback that discards the finished task fires on a LATER
    # loop tick than task completion — yield once more so it runs before
    # we assert the set is emptied.
    await asyncio.sleep(0)
    assert len(salem_app.app["_bg_tasks"]) == 0
