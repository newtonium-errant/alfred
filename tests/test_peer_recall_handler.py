"""Tests for ``alfred.transport.routes_recall`` — the recall ANSWER side.

The peer-token-gated ``POST /peer/recall`` route searches THIS instance's
vault for a peer's free-text query and returns bounded matches (a capped
snippet + a record pointer) or an honest empty match set.

Coverage (mandatory regression pins, run unconditionally):
    * Happy path — an allowed peer's query returns bounded matches with the
      record pointer + snippet + truncated flag.
    * Allowlist enforcement (the centerpiece) — a disallowed type is NEVER
      searched or returned; a peer not in recall.peers → 401 wrong_peer.
    * Bounded projection — max_matches cap + snippet char cap + truncated.
    * Audit emission — every answer appends a kind=recall_read line.
    * ILB no-match — a query with no hit → 200 {matches: []}.
    * Fail-closed — disabled → route not mounted (404); STAY-C fence.
    * Pure engine — resolve_search_types + build_snippet unit-level.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.transport.canonical_audit import read_audit
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    RecallConfig,
    RecallConfigError,
    RecallPeerRules,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.nl_broker import TRUNCATION_MARKER
from alfred.transport.peer_handlers import register_instance_identity, register_vault_path
from alfred.transport.routes_recall import (
    _handle_peer_recall,  # noqa: F401 (import-presence sanity)
    build_snippet,
    register_recall_routes,
    resolve_search_types,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState

# Obviously-fake test secrets — never a real provider prefix (builder.md
# GitGuardian rule).
DUMMY_KALLE_TOKEN = (
    "DUMMY_KALLE_RECALL_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_01234"
)
DUMMY_HYPATIA_TOKEN = (
    "DUMMY_HYPATIA_RECALL_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_012"
)

_KALLE_HEADERS = {
    "Authorization": f"Bearer {DUMMY_KALLE_TOKEN}",
    "X-Alfred-Client": "kal-le",
    "Content-Type": "application/json",
}
# hypatia authenticates fine (valid token) but is NOT a configured recall
# asker in the fixture — the peer-pin (wrong_peer 401) target.
_HYPATIA_HEADERS = {
    "Authorization": f"Bearer {DUMMY_HYPATIA_TOKEN}",
    "X-Alfred-Client": "hypatia",
    "Content-Type": "application/json",
}


def _transport_config(*, audit_path: str, **recall_overrides) -> TransportConfig:
    """A config where kal-le is a recall asker (person/project) but hypatia,
    though a valid auth peer, is NOT in recall.peers."""
    recall_kwargs = dict(
        enabled=True,
        max_matches=10,
        snippet_max_chars=500,
        peers={"kal-le": RecallPeerRules(types=["person", "project"])},
    )
    recall_kwargs.update(recall_overrides)
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                "kal-le": AuthTokenEntry(
                    token=DUMMY_KALLE_TOKEN, allowed_clients=["kal-le"],
                ),
                "hypatia": AuthTokenEntry(
                    token=DUMMY_HYPATIA_TOKEN, allowed_clients=["hypatia"],
                ),
            }
        ),
        state=StateConfig(),
        canonical=CanonicalConfig(audit_log_path=audit_path),
        recall=RecallConfig(**recall_kwargs),
    )


def _write_record(vault, rtype: str, name: str, body: str) -> None:
    d = vault / rtype
    d.mkdir(parents=True, exist_ok=True)
    fm = f"---\ntype: {rtype}\nname: {name}\n---\n{body}\n"
    (d / f"{name}.md").write_text(fm, encoding="utf-8")


def _make_vault(tmp_path):
    vault = tmp_path / "vault"
    _write_record(
        vault, "person", "Andrew Newton",
        "Owner of RRTS. Works closely with the ops team on scheduling.",
    )
    _write_record(
        vault, "person", "Ben McMillan",
        "Ops partner. Coordinates the weekly review with Andrew.",
    )
    _write_record(
        vault, "project", "RRTS Rollout",
        "Andrew is driving the RRTS rollout across the region this quarter.",
    )
    # A task record mentioning the query — task is NOT in kal-le's allowlist,
    # so it must NEVER surface (filter-before-search pin).
    _write_record(
        vault, "task", "Call Andrew back",
        "Andrew asked for a callback about the RRTS rollout.",
    )
    return vault


@pytest.fixture
async def recall_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Transport app with /peer/recall mounted (enabled) + a vault."""
    audit_path = str(tmp_path / "canonical_audit.jsonl")
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(audit_path=audit_path), tstate)
    vault = _make_vault(tmp_path)
    register_vault_path(app, vault)
    register_instance_identity(app, name="Salem")
    mounted = register_recall_routes(app, enabled=True, instance_name="Salem")
    assert mounted is True
    app["_vault"] = vault
    app["_audit_path"] = audit_path
    return await aiohttp_client(app)


# ---------------------------------------------------------------------------
# Happy path — bounded matches + pointer + attribution
# ---------------------------------------------------------------------------


async def test_recall_happy_path_returns_bounded_matches(recall_client) -> None:
    resp = await recall_client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Andrew"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["instance"] == "Salem"
    assert data["count"] == len(data["matches"]) >= 1
    types_returned = {m["type"] for m in data["matches"]}
    # Only allowlisted types (person, project) — never the task record.
    assert types_returned <= {"person", "project"}
    for m in data["matches"]:
        assert set(m) == {"type", "name", "snippet", "truncated", "record_pointer"}
        assert m["record_pointer"]["instance"] == "Salem"
        assert m["record_pointer"]["path"].endswith(".md")
        assert isinstance(m["snippet"], str)
        assert isinstance(m["truncated"], bool)


async def test_recall_disallowed_type_never_surfaces(recall_client) -> None:
    # The task/ record mentions "Andrew" but task is not in kal-le's
    # allowlist → it must never appear (filter-before-search pin).
    resp = await recall_client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Andrew"},
    )
    data = await resp.json()
    paths = [m["record_pointer"]["path"] for m in data["matches"]]
    assert not any(p.startswith("task/") for p in paths)


async def test_recall_requested_types_narrow_the_allowlist(recall_client) -> None:
    # Ask for person only → project record excluded even though it matches.
    resp = await recall_client.post(
        "/peer/recall",
        headers=_KALLE_HEADERS,
        json={"query": "Andrew", "types": ["person"]},
    )
    data = await resp.json()
    assert {m["type"] for m in data["matches"]} == {"person"}


async def test_recall_requesting_disallowed_type_returns_empty(recall_client) -> None:
    # Ask for ONLY a disallowed type → nothing searched, honest empty set.
    resp = await recall_client.post(
        "/peer/recall",
        headers=_KALLE_HEADERS,
        json={"query": "Andrew", "types": ["task"]},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["matches"] == []
    assert data["count"] == 0


# ---------------------------------------------------------------------------
# Peer-pin — wrong_peer 401 + logged reason
# ---------------------------------------------------------------------------


async def test_recall_wrong_peer_401(recall_client) -> None:
    # hypatia authenticates (valid token) but is not a configured recall
    # asker → fail-closed 401 wrong_peer + a logged reason.
    with structlog.testing.capture_logs() as captured:
        resp = await recall_client.post(
            "/peer/recall", headers=_HYPATIA_HEADERS, json={"query": "Andrew"},
        )
    assert resp.status == 401
    assert (await resp.json())["reason"] == "wrong_peer"
    rejected = [c for c in captured if c.get("event") == "transport.recall.rejected"]
    assert any(r.get("reason") == "wrong_peer" for r in rejected)


async def test_recall_requires_peer_token(recall_client) -> None:
    resp = await recall_client.post("/peer/recall", json={"query": "Andrew"})
    assert resp.status == 401


# ---------------------------------------------------------------------------
# Bounded projection
# ---------------------------------------------------------------------------


async def test_recall_caps_at_max_matches(aiohttp_client, tmp_path) -> None:
    audit_path = str(tmp_path / "audit.jsonl")
    tstate = TransportState.create(tmp_path / "state.json")
    app = build_app(
        _transport_config(audit_path=audit_path, max_matches=2), tstate,
    )
    vault = tmp_path / "vault"
    for i in range(5):
        _write_record(vault, "person", f"Person {i}", "mentions Andrew here")
    register_vault_path(app, vault)
    register_instance_identity(app, name="Salem")
    register_recall_routes(app, enabled=True, instance_name="Salem")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Andrew"},
    )
    data = await resp.json()
    assert data["count"] == 2


async def test_recall_snippet_capped_and_flagged(aiohttp_client, tmp_path) -> None:
    audit_path = str(tmp_path / "audit.jsonl")
    tstate = TransportState.create(tmp_path / "state.json")
    app = build_app(
        _transport_config(audit_path=audit_path, snippet_max_chars=40), tstate,
    )
    vault = tmp_path / "vault"
    big_body = ("lorem ipsum " * 100) + " Andrew appears deep in here " + ("tail " * 100)
    _write_record(vault, "person", "Big Record", big_body)
    register_vault_path(app, vault)
    register_instance_identity(app, name="Salem")
    register_recall_routes(app, enabled=True, instance_name="Salem")
    client = await aiohttp_client(app)

    resp = await client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Andrew"},
    )
    data = await resp.json()
    assert data["count"] == 1
    m = data["matches"][0]
    assert m["truncated"] is True
    assert len(m["snippet"]) <= 40


# ---------------------------------------------------------------------------
# Path-traversal wall — glob-site backstop (defense-in-depth)
# ---------------------------------------------------------------------------


async def test_recall_glob_backstop_skips_traversal_type(aiohttp_client, tmp_path) -> None:
    # Belt-and-braces: even if an unsafe type bypassed load-validation (here
    # constructed directly), the glob-site backstop refuses to glob it — no
    # file outside the vault is ever reached.
    audit_path = str(tmp_path / "audit.jsonl")
    tstate = TransportState.create(tmp_path / "state.json")
    app = build_app(
        _transport_config(
            audit_path=audit_path,
            peers={"kal-le": RecallPeerRules(types=["../", "person"])},
        ),
        tstate,
    )
    vault = _make_vault(tmp_path)
    # A record OUTSIDE the vault a "../" glob could otherwise reach.
    (tmp_path / "leak.md").write_text(
        "---\ntype: person\nname: Leak\n---\nAndrew leaked\n", encoding="utf-8",
    )
    register_vault_path(app, vault)
    register_instance_identity(app, name="Salem")
    register_recall_routes(app, enabled=True, instance_name="Salem")
    client = await aiohttp_client(app)

    with structlog.testing.capture_logs() as captured:
        resp = await client.post(
            "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Andrew"},
        )
    data = await resp.json()
    paths = [m["record_pointer"]["path"] for m in data["matches"]]
    # "person" still searched; "../" never globbed → no outside leak.
    assert all(not p.startswith("..") for p in paths)
    assert not any("leak" in p.lower() for p in paths)
    assert any(p.startswith("person/") for p in paths)
    skipped = [c for c in captured if c.get("event") == "transport.recall.unsafe_type_skipped"]
    assert any(s.get("record_type") == "../" for s in skipped)


# ---------------------------------------------------------------------------
# Audit — every answer
# ---------------------------------------------------------------------------


async def test_recall_audits_every_answer(recall_client) -> None:
    await recall_client.post(
        "/peer/recall",
        headers=_KALLE_HEADERS,
        json={"query": "Andrew", "types": ["person", "task"]},
    )
    entries = read_audit(recall_client.app["_audit_path"])
    assert len(entries) == 1
    e = entries[0]
    assert e["kind"] == "recall_read"
    assert e["peer"] == "kal-le"
    assert e["granted"] == ["person"]           # searched (allowed) types
    assert e["denied"] == ["task"]              # requested-but-not-allowed
    assert e["match_count"] >= 1
    assert all(p.startswith("person/") for p in e["returned_paths"])
    assert e["query"] == "Andrew"


async def test_recall_audits_empty_answer(recall_client) -> None:
    await recall_client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "nonexistent-xyz"},
    )
    entries = read_audit(recall_client.app["_audit_path"])
    assert len(entries) == 1
    assert entries[0]["kind"] == "recall_read"
    assert entries[0]["match_count"] == 0


async def test_recall_answered_log_fires(recall_client) -> None:
    with structlog.testing.capture_logs() as captured:
        await recall_client.post(
            "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Andrew"},
        )
    answered = [c for c in captured if c.get("event") == "transport.recall.answered"]
    assert len(answered) == 1
    assert answered[0]["peer"] == "kal-le"
    assert answered[0]["match_count"] >= 1


# ---------------------------------------------------------------------------
# ILB no-match + input validation
# ---------------------------------------------------------------------------


async def test_recall_no_match_is_200_empty(recall_client) -> None:
    resp = await recall_client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "zzz-no-such-token"},
    )
    assert resp.status == 200
    data = await resp.json()
    assert data["matches"] == []
    assert data["count"] == 0


async def test_recall_empty_query_400(recall_client) -> None:
    resp = await recall_client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "   "},
    )
    assert resp.status == 400
    assert (await resp.json())["reason"] == "empty_query"


async def test_recall_query_too_long_400(recall_client) -> None:
    resp = await recall_client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "x" * 5000},
    )
    assert resp.status == 400
    assert (await resp.json())["reason"] == "query_too_long"


async def test_recall_types_not_a_list_400(recall_client) -> None:
    resp = await recall_client.post(
        "/peer/recall",
        headers=_KALLE_HEADERS,
        json={"query": "Andrew", "types": "person"},
    )
    assert resp.status == 400
    assert (await resp.json())["reason"] == "schema_error"


async def test_recall_invalid_json_400(recall_client) -> None:
    resp = await recall_client.post(
        "/peer/recall", headers=_KALLE_HEADERS, data="not json",
    )
    assert resp.status == 400
    assert (await resp.json())["reason"] == "invalid_json"


async def test_recall_vault_not_configured_503(aiohttp_client, tmp_path) -> None:
    audit_path = str(tmp_path / "audit.jsonl")
    tstate = TransportState.create(tmp_path / "state.json")
    app = build_app(_transport_config(audit_path=audit_path), tstate)
    # No register_vault_path — vault deliberately absent.
    register_instance_identity(app, name="Salem")
    register_recall_routes(app, enabled=True, instance_name="Salem")
    client = await aiohttp_client(app)
    resp = await client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Andrew"},
    )
    assert resp.status == 503
    assert (await resp.json())["reason"] == "vault_not_configured"


# ---------------------------------------------------------------------------
# Registrar — opt-in inertness + STAY-C fence
# ---------------------------------------------------------------------------


async def test_recall_disabled_route_not_mounted(aiohttp_client, tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "state.json")
    app = build_app(
        _transport_config(audit_path=str(tmp_path / "a.jsonl"), enabled=False),
        tstate,
    )
    mounted = register_recall_routes(app, enabled=False, instance_name="Salem")
    assert mounted is False
    client = await aiohttp_client(app)
    resp = await client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Andrew"},
    )
    assert resp.status == 404


def test_register_recall_routes_enabled_mounts_route(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "state.json")
    app = build_app(_transport_config(audit_path=str(tmp_path / "a.jsonl")), tstate)
    assert register_recall_routes(app, enabled=True, instance_name="Salem") is True
    paths = {r.resource.canonical for r in app.router.routes()}
    assert "/peer/recall" in paths


def test_register_recall_routes_stayc_enabled_raises(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "state.json")
    app = build_app(_transport_config(audit_path=str(tmp_path / "a.jsonl")), tstate)
    with pytest.raises(RecallConfigError, match="STAY-C"):
        register_recall_routes(app, enabled=True, instance_name="STAY-C")


def test_register_recall_routes_stayc_disabled_not_mounted(tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "state.json")
    app = build_app(_transport_config(audit_path=str(tmp_path / "a.jsonl")), tstate)
    with structlog.testing.capture_logs() as captured:
        mounted = register_recall_routes(app, enabled=False, instance_name="STAY-C")
    assert mounted is False
    disabled = [c for c in captured if c.get("event") == "transport.recall.disabled"]
    assert any("stay-c" in (d.get("reason") or "") for d in disabled)


# ---------------------------------------------------------------------------
# Pure engine — allowlist resolution + snippet projection (no I/O)
# ---------------------------------------------------------------------------


def test_resolve_search_types_none_requested_returns_full_allowlist() -> None:
    search, denied = resolve_search_types(["person", "project"], None)
    assert search == ["person", "project"]
    assert denied == []


def test_resolve_search_types_narrows_to_intersection() -> None:
    search, denied = resolve_search_types(
        ["person", "project"], ["person", "task"],
    )
    assert search == ["person"]
    assert denied == ["task"]


def test_resolve_search_types_all_disallowed_yields_empty_search() -> None:
    search, denied = resolve_search_types(["person"], ["task", "note"])
    assert search == []
    assert denied == ["note", "task"]


def test_resolve_search_types_dedups_and_drops_blanks() -> None:
    search, _ = resolve_search_types(["person"], ["person", "person", "", 5])
    assert search == ["person"]


def test_build_snippet_windows_around_match() -> None:
    body = "alpha beta gamma NEEDLE delta epsilon"
    snippet, truncated = build_snippet(body, "needle", 100)
    assert "NEEDLE" in snippet
    assert truncated is False  # whole body fits


def test_build_snippet_truncates_long_body() -> None:
    body = "x" * 50 + " NEEDLE " + "y" * 500
    snippet, truncated = build_snippet(body, "NEEDLE", 40)
    assert truncated is True
    assert len(snippet) <= 40


def test_build_snippet_match_in_frontmatter_only_returns_leading() -> None:
    # query not present in the body → leading excerpt, flagged if clipped.
    body = "some body text without the term, quite long " * 5
    snippet, truncated = build_snippet(body, "absent-term", 30)
    assert snippet.startswith("some body")
    assert truncated is True
    assert TRUNCATION_MARKER in snippet or len(snippet) <= 30


def test_build_snippet_empty_body() -> None:
    assert build_snippet("", "x", 100) == ("", False)
