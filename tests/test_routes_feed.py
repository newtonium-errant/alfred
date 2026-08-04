"""Tests for ``alfred.transport.routes_feed`` — the feed surface.

The peer-token-gated ``GET /feed/items`` + ``POST /feed/act`` routes read the
folded feed store and act on cards through
:func:`alfred.daily_sync.action_router.act` (the SAME resolvers the reply
grammar uses). The router's ``(kind, action_id)`` map is the capability ceiling.

Coverage (mandatory regression pins, run unconditionally):
    * GET returns the folded store, filtered by state/mode/kind.
    * POST act drives the real router → a real corpus mutation + acted state.
    * Peer-pin (mutation-verified): a VALID chat ``web`` token (shares
      ``allowed_clients: [web]``) is refused 401 ``feed_wrong_peer`` and does
      NOT mutate — neutering the pin would let the act through, reddening this.
    * Capability ceiling via the route: a wrong-verb act → 400 invalid_action,
      no mutation.
    * stale_item → 409; missing id/action → 400; feed_not_configured → 503.
    * Opt-in inertness: disabled → routes not mounted; enabled → mounted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import structlog

from alfred.daily_sync.config import DailySyncConfig
from alfred.daily_sync.confidence import save_state
from alfred.daily_sync.corpus import iter_corrections
from alfred.daily_sync.feed_producer import build_feed_items
from alfred.feed import FeedStore
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import register_vault_path
from alfred.transport.routes_feed import (
    FEED_PEER_NAME,
    _handle_feed_act,  # noqa: F401 (import-presence sanity)
    register_feed_routes,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState

# Obviously-fake test secrets — never a real provider prefix (builder.md
# GitGuardian rule).
DUMMY_FEED_PEER_TOKEN = (
    "DUMMY_WEB_FEED_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_012345678"
)
# A sibling chat ``web`` token (distinct from the feed token) so the peer-pin
# escalation test can present a valid Layer-1 ``web`` token and prove refusal.
DUMMY_WEB_CHAT_TOKEN = (
    "DUMMY_WEB_CHAT_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_012345678"
)

_FEED_HEADERS = {
    "Authorization": f"Bearer {DUMMY_FEED_PEER_TOKEN}",
    "X-Alfred-Client": "web",
    "Content-Type": "application/json",
}
_CHAT_HEADERS = {
    "Authorization": f"Bearer {DUMMY_WEB_CHAT_TOKEN}",
    "X-Alfred-Client": "web",
    "Content-Type": "application/json",
}


def _transport_config() -> TransportConfig:
    """The feed token lives under the dedicated ``web_feed`` peer (the peer NAME
    the handlers pin on). A sibling chat ``web`` peer (same allowed_clients) is
    present so the escalation test can present a valid Layer-1 ``web`` token."""
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(tokens={
            "web_feed": AuthTokenEntry(token=DUMMY_FEED_PEER_TOKEN, allowed_clients=["web"]),
            "web": AuthTokenEntry(token=DUMMY_WEB_CHAT_TOKEN, allowed_clients=["web"]),
        }),
        state=StateConfig(),
    )


def _email_item(num: int = 1, *, priority: str = "medium") -> dict:
    return {
        "item_number": num,
        "record_path": f"note/Email{num}.md",
        "classifier_priority": priority,
        "classifier_action_hint": None,
        "classifier_reason": f"reason {num}",
        "sender": f"s{num}@example.com",
        "subject": f"Subject {num}",
        "snippet": f"Snippet {num}",
    }


def _ds_config(tmp_path: Path) -> DailySyncConfig:
    cfg = DailySyncConfig(enabled=True, batch_size=5)
    cfg.corpus.path = str(tmp_path / "email_corpus.jsonl")
    cfg.state.path = str(tmp_path / "state.json")
    return cfg


def _seed_batch(cfg: DailySyncConfig, **families) -> None:
    payload = {"date": "2026-07-30", "message_ids": [100]}
    payload.update(families)
    save_state(cfg.state.path, {"last_batch": payload})


@pytest.fixture
async def feed_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Transport app with the feed routes mounted + a seeded store/batch.

    Publishes one open email_tier feed item and seeds the matching last_batch,
    so a POST act can drive the real email resolver end-to-end."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    register_vault_path(app, tmp_path / "vault")

    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    item = _email_item(priority="medium")
    fid = build_feed_items("email_tier", [item], "salem")[0].id
    store.upsert(build_feed_items("email_tier", [item], "salem")[0])
    _seed_batch(cfg, items=[item])

    mounted = register_feed_routes(
        app, enabled=True, feed_store=store, daily_sync_config=cfg,
        instance_name="Salem", instance_scope="talker", raw_config={},
    )
    assert mounted is True
    app["_store"] = store
    app["_cfg"] = cfg
    app["_fid"] = fid
    return await aiohttp_client(app)


# ---------------------------------------------------------------------------
# GET /feed/items
# ---------------------------------------------------------------------------


async def test_get_items_returns_folded_store(feed_client) -> None:
    resp = await feed_client.get("/feed/items", headers=_FEED_HEADERS)
    assert resp.status == 200
    body = await resp.json()
    assert body["count"] == 1
    assert body["items"][0]["id"] == feed_client.app["_fid"]
    assert body["items"][0]["kind"] == "email_tier"


async def test_get_items_filters_by_kind(feed_client) -> None:
    resp = await feed_client.get("/feed/items?kind=proposal", headers=_FEED_HEADERS)
    assert resp.status == 200
    assert (await resp.json())["count"] == 0  # no proposal items published


async def test_get_items_requires_feed_peer(feed_client) -> None:
    # A valid chat ``web`` token clears Layer 1 as peer ``web`` (shared
    # allowed_clients) but the peer-pin refuses it.
    with structlog.testing.capture_logs() as cap:
        resp = await feed_client.get("/feed/items", headers=_CHAT_HEADERS)
    assert resp.status == 401
    assert (await resp.json())["error"] == "feed_wrong_peer"
    rejected = [c for c in cap if c.get("event") == "transport.feed.rejected"]
    assert any(r.get("reason") == "wrong_peer" for r in rejected)


async def test_get_items_no_token_401(feed_client) -> None:
    resp = await feed_client.get("/feed/items")
    assert resp.status == 401


# ---------------------------------------------------------------------------
# POST /feed/act — drives the real router
# ---------------------------------------------------------------------------


async def test_act_confirm_drives_resolver_and_acts(feed_client) -> None:
    fid = feed_client.app["_fid"]
    resp = await feed_client.post(
        "/feed/act", json={"id": fid, "action_id": "confirm"}, headers=_FEED_HEADERS,
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True and body["status"] == "acted"
    # Real store mutation: the email calibration corpus got a row.
    rows = list(iter_corrections(feed_client.app["_cfg"].corpus.path))
    assert len(rows) == 1 and rows[0].andrew_priority == "medium"
    assert feed_client.app["_store"].load()[fid].state == "acted"


async def test_act_peer_pin_refuses_chat_token_and_does_not_mutate(feed_client) -> None:
    # Mutation-verified peer-pin: a VALID chat ``web`` token must NOT drive a
    # feed act. Neutering the pin would let this through (200 + a corpus row),
    # reddening the test.
    fid = feed_client.app["_fid"]
    with structlog.testing.capture_logs() as cap:
        resp = await feed_client.post(
            "/feed/act", json={"id": fid, "action_id": "confirm"}, headers=_CHAT_HEADERS,
        )
    assert resp.status == 401
    assert (await resp.json())["error"] == "feed_wrong_peer"
    assert any(
        c.get("event") == "transport.feed.rejected" and c.get("reason") == "wrong_peer"
        for c in cap
    )
    # The act did NOT land.
    assert list(iter_corrections(feed_client.app["_cfg"].corpus.path)) == []
    assert feed_client.app["_store"].load()[fid].state == "open"


async def test_act_wrong_verb_is_400_invalid_action_no_mutation(feed_client) -> None:
    """Capability ceiling via the route: 'reject' is not a mapped email action."""
    fid = feed_client.app["_fid"]
    resp = await feed_client.post(
        "/feed/act", json={"id": fid, "action_id": "reject"}, headers=_FEED_HEADERS,
    )
    assert resp.status == 400
    assert (await resp.json())["status"] == "invalid_action"
    assert list(iter_corrections(feed_client.app["_cfg"].corpus.path)) == []
    assert feed_client.app["_store"].load()[fid].state == "open"


async def test_act_stale_item_is_409(feed_client) -> None:
    resp = await feed_client.post(
        "/feed/act", json={"id": "email_tier:note/Ghost.md", "action_id": "confirm"},
        headers=_FEED_HEADERS,
    )
    assert resp.status == 409
    assert (await resp.json())["status"] == "stale_item"


async def test_act_missing_fields_400(feed_client) -> None:
    r1 = await feed_client.post("/feed/act", json={"action_id": "confirm"}, headers=_FEED_HEADERS)
    assert r1.status == 400
    assert (await r1.json())["error"] == "missing_id"
    r2 = await feed_client.post("/feed/act", json={"id": "email_tier:x"}, headers=_FEED_HEADERS)
    assert r2.status == 400
    assert (await r2.json())["error"] == "missing_action_id"


async def test_act_invalid_json_400(feed_client) -> None:
    resp = await feed_client.post(
        "/feed/act", data="not json", headers=_FEED_HEADERS,
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_json"


async def test_act_requires_feed_peer(feed_client) -> None:
    resp = await feed_client.post(
        "/feed/act", json={"id": "x", "action_id": "confirm"}, headers=_CHAT_HEADERS,
    )
    assert resp.status == 401
    assert (await resp.json())["error"] == "feed_wrong_peer"


# ---------------------------------------------------------------------------
# feed_not_configured / mount inertness
# ---------------------------------------------------------------------------


async def test_act_feed_not_configured_503(aiohttp_client, tmp_path) -> None:
    """Routes mounted but store/config not stashed → 503 (not a 500)."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    # enabled but feed_store / daily_sync_config left None.
    register_feed_routes(app, enabled=True)
    client = await aiohttp_client(app)
    resp = await client.post(
        "/feed/act", json={"id": "x", "action_id": "confirm"}, headers=_FEED_HEADERS,
    )
    assert resp.status == 503
    assert (await resp.json())["error"] == "feed_not_configured"


def test_register_feed_routes_disabled_mounts_nothing() -> None:
    from aiohttp import web

    app = web.Application()
    mounted = register_feed_routes(app, enabled=False)
    assert mounted is False
    paths = [r.resource.canonical for r in app.router.routes() if r.resource is not None]
    assert "/feed/items" not in paths and "/feed/act" not in paths


def test_register_feed_routes_enabled_mounts_both() -> None:
    from aiohttp import web

    app = web.Application()
    mounted = register_feed_routes(app, enabled=True, instance_name="Salem")
    assert mounted is True
    paths = [r.resource.canonical for r in app.router.routes() if r.resource is not None]
    assert "/feed/items" in paths and "/feed/act" in paths


def test_feed_peer_name_is_web_feed() -> None:
    # The fixture pins the PRODUCTION peer NAME, not just any name that clears
    # allowed_clients (per the relay peer-pin rule).
    assert FEED_PEER_NAME == "web_feed"


# ---------------------------------------------------------------------------
# POST /feed/act — correction_target passthrough (#13)
# ---------------------------------------------------------------------------


def _routine_match_item() -> dict:
    return {
        "item_number": 1,
        "query": "clean hammer",
        "matched_to": "Clean house",
        "record": "Weekly",
        "confidence": 0.4,
        "completion_date": "2026-08-03",
        "captured_at": "2026-08-03T09:00:00+00:00",
        "kind": "low_conf",
    }


@pytest.fixture
async def routine_client(aiohttp_client, tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Transport app serving one open routine_match card over a REAL vault, so a
    #13 correction can be driven all the way to a corpus row through the route."""
    import yaml as _yaml

    from alfred.daily_sync import reply_dispatch as _rd

    vault = tmp_path / "vault"
    routine_dir = vault / "routine"
    routine_dir.mkdir(parents=True)
    payload = {
        "type": "routine", "name": "Weekly", "status": "active",
        "cadence": {"type": "weekly"},
        "items": [{"text": "Clean house"}, {"text": "Tidy the workshop"}],
    }
    (routine_dir / "Weekly.md").write_text(
        "---\n" + _yaml.dump(payload, sort_keys=False) + "---\n", encoding="utf-8",
    )

    corpus = tmp_path / "routine_corpus.jsonl"
    monkeypatch.setattr(
        _rd, "_routine_match_corpus_path", lambda *a, **kw: str(corpus),
    )

    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    register_vault_path(app, vault)

    store = FeedStore(str(tmp_path / "feed.jsonl"))
    cfg = _ds_config(tmp_path)
    item = _routine_match_item()
    fi = build_feed_items("routine_match", [item], "salem")[0]
    store.upsert(fi)
    _seed_batch(cfg, routine_match_items=[item])

    assert register_feed_routes(
        app, enabled=True, feed_store=store, daily_sync_config=cfg,
        instance_name="Salem", instance_scope="talker", raw_config={},
    ) is True
    app["_store"] = store
    app["_corpus"] = corpus
    app["_fid"] = fi.id
    return await aiohttp_client(app)


def _corpus_types(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [
        json.loads(ln)["type"]
        for ln in path.read_text().splitlines() if ln.strip()
    ]


async def test_act_correction_target_reaches_the_resolver(routine_client) -> None:
    """The passthrough pin. Without it the target is dropped at the transport and
    a `correct` arrives targetless — which the router refuses, so the operator's
    answer would vanish between the BFF and the resolver."""
    fid = routine_client.app["_fid"]
    resp = await routine_client.post(
        "/feed/act",
        json={
            "id": fid, "action_id": "correct",
            "correction_target": "Tidy the workshop",
        },
        headers=_FEED_HEADERS,
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True and body["status"] == "acted"
    corpus = routine_client.app["_corpus"]
    assert _corpus_types(corpus) == ["match_reject", "match_alias"]
    rows = [json.loads(ln) for ln in corpus.read_text().splitlines() if ln.strip()]
    assert rows[1]["item_text"] == "Tidy the workshop"
    assert routine_client.app["_store"].load()[fid].state == "acted"


async def test_act_one_off_needs_no_target(routine_client) -> None:
    fid = routine_client.app["_fid"]
    resp = await routine_client.post(
        "/feed/act", json={"id": fid, "action_id": "one_off"},
        headers=_FEED_HEADERS,
    )
    assert resp.status == 200
    assert _corpus_types(routine_client.app["_corpus"]) == [
        "match_reject", "match_oneoff",
    ]


async def test_act_correct_without_a_target_is_refused_no_mutation(
    routine_client,
) -> None:
    fid = routine_client.app["_fid"]
    resp = await routine_client.post(
        "/feed/act", json={"id": fid, "action_id": "correct"},
        headers=_FEED_HEADERS,
    )
    assert resp.status == 400
    assert (await resp.json())["status"] == "invalid_action"
    assert _corpus_types(routine_client.app["_corpus"]) == []
    assert routine_client.app["_store"].load()[fid].state == "open"


async def test_act_bogus_target_is_refused_and_writes_nothing(
    routine_client,
) -> None:
    """End-to-end corpus-poisoning guard: a target that isn't a live routine item
    dies at the resolver, and the reject doesn't land either."""
    fid = routine_client.app["_fid"]
    resp = await routine_client.post(
        "/feed/act",
        json={
            "id": fid, "action_id": "correct",
            "correction_target": "Polish the DeLorean",
        },
        headers=_FEED_HEADERS,
    )
    assert resp.status >= 400
    assert _corpus_types(routine_client.app["_corpus"]) == []
    assert routine_client.app["_store"].load()[fid].state == "open"


async def test_act_non_string_correction_target_400(routine_client) -> None:
    """Type gate at the transport — the CONTENT is the resolver's business, but a
    non-string can't reach it."""
    fid = routine_client.app["_fid"]
    resp = await routine_client.post(
        "/feed/act",
        json={"id": fid, "action_id": "correct", "correction_target": {"evil": 1}},
        headers=_FEED_HEADERS,
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_correction_target"
    assert _corpus_types(routine_client.app["_corpus"]) == []
