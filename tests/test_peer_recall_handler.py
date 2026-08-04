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
    * Snippet frontmatter tier (#20) — person answers carry substance, the
      field allowlist is the ceiling (a non-granted field NEVER reaches a
      snippet), body-substance types are byte-unchanged, and the disclosure
      accounting is logged.
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
    compose_recall_snippet,
    register_recall_routes,
    render_frontmatter_summary,
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


# ---------------------------------------------------------------------------
# #20 — snippet frontmatter tier (substance, not template boilerplate)
#
# A person record's BODY is pure base-transclusion scaffolding (see the
# shipped scaffold/_templates/person.md); every piece of substance lives in
# frontmatter. The snippet therefore leads with the peer-visible frontmatter
# — gated by the SAME canonical field allowlist that serves
# /canonical/<type>/<name>, so a snippet can never disclose more than that
# peer could already read.
# ---------------------------------------------------------------------------

# The template-shaped body: what a real person record actually contains.
_PERSON_TEMPLATE_BODY = (
    "# Ben McMillan\n\n"
    "## Decisions\n![[person.base#Decisions]]\n\n"
    "## Tasks\n![[person.base#Tasks]]\n\n"
    "## Projects\n![[person.base#Projects]]\n\n"
    "## Sessions\n![[person.base#Sessions]]\n"
)
# Granted to kal-le for `person`. email/phone are deliberately WITHHELD —
# they are the leak targets the security pin hunts for.
_KALLE_PERSON_FIELDS = ["name", "role", "org", "description"]
_WITHHELD_EMAIL = "ben@example.invalid"
_WITHHELD_PHONE = "555-0100"


def _fm_transport_config(*, audit_path: str) -> TransportConfig:
    """Like `_transport_config`, plus a canonical field grant for kal-le."""
    cfg = _transport_config(audit_path=audit_path)
    cfg.canonical = CanonicalConfig(
        audit_log_path=audit_path,
        peer_permissions={
            "kal-le": {
                # Raw-dict shape (accepted by apply_field_permissions
                # alongside the PeerFieldRules dataclass).
                "person": {"fields": list(_KALLE_PERSON_FIELDS), "bodies": False},
                # NOTE: no "project" entry → default-deny for that type.
            },
        },
    )
    return cfg


def _write_person_with_frontmatter(vault) -> None:
    d = vault / "person"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Ben McMillan.md").write_text(
        "---\n"
        "type: person\n"
        "name: Ben McMillan\n"
        "description: Ops partner on the RRTS rollout\n"
        "org: RRTS\n"
        "role: Operations lead\n"
        f"email: {_WITHHELD_EMAIL}\n"
        f"phone: '{_WITHHELD_PHONE}'\n"
        "phone_note:\n"  # empty value → must not render a dangling key
        "---\n" + _PERSON_TEMPLATE_BODY,
        encoding="utf-8",
    )


@pytest.fixture
async def recall_fm_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Recall app whose canonical config grants kal-le person fields."""
    audit_path = str(tmp_path / "canonical_audit.jsonl")
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_fm_transport_config(audit_path=audit_path), tstate)
    vault = _make_vault(tmp_path)
    _write_person_with_frontmatter(vault)  # overwrite with the template shape
    register_vault_path(app, vault)
    register_instance_identity(app, name="Salem")
    assert register_recall_routes(app, enabled=True, instance_name="Salem") is True
    app["_vault"] = vault
    return await aiohttp_client(app)


async def _ben_match(client, query: str = "Ben McMillan") -> dict:
    resp = await client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": query},
    )
    assert resp.status == 200
    data = await resp.json()
    people = [m for m in data["matches"] if m["type"] == "person"]
    assert people, f"expected a person match, got {data['matches']}"
    return people[0]


async def test_recall_person_snippet_carries_frontmatter_substance(
    recall_fm_client,
) -> None:
    """THE #20 defect pin: a person answer returns meaning, not boilerplate."""
    match = await _ben_match(recall_fm_client)
    snippet = match["snippet"]
    # The substance the operator was missing.
    assert "role: Operations lead" in snippet
    assert "description: Ops partner on the RRTS rollout" in snippet
    assert "org: RRTS" in snippet
    # Pre-fix this was the ENTIRE snippet.
    assert snippet.split("\n")[0] != "# Ben McMillan"


async def test_recall_snippet_never_leaks_a_non_allowlisted_field(
    recall_fm_client,
) -> None:
    """THE security pin: a field outside the peer's grant NEVER appears.

    email/phone are present in the record's frontmatter and absent from
    kal-le's canonical allowlist, so they must not reach the wire by any
    path. Reddens if the extraction is widened past
    ``apply_field_permissions`` (e.g. to raw frontmatter).
    """
    match = await _ben_match(recall_fm_client)
    snippet = match["snippet"]
    assert _WITHHELD_EMAIL not in snippet
    assert _WITHHELD_PHONE not in snippet
    assert "email" not in snippet
    assert "phone" not in snippet
    # And the whole response, not just this field — belt for any other
    # surface that might echo the record.
    assert _WITHHELD_EMAIL not in str(match)


async def test_recall_snippet_drops_empty_frontmatter_values(
    recall_fm_client,
) -> None:
    # `phone_note:` is empty AND non-allowlisted; a granted-but-empty key
    # must not render as a dangling "key: " either.
    match = await _ben_match(recall_fm_client)
    assert "phone_note" not in match["snippet"]
    assert not any(
        line.endswith(": ") for line in match["snippet"].splitlines()
    )


async def test_recall_snippet_type_without_grant_stays_body_only(
    recall_fm_client,
) -> None:
    """Default-deny per TYPE: project has no grant → today's body snippet."""
    resp = await recall_fm_client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "rollout"},
    )
    data = await resp.json()
    projects = [m for m in data["matches"] if m["type"] == "project"]
    assert projects, "expected a project match"
    snippet = projects[0]["snippet"]
    # Body prose preserved; no frontmatter head injected.
    assert "RRTS rollout" in snippet
    assert "type: project" not in snippet
    assert "name: RRTS Rollout" not in snippet


async def test_recall_body_substance_snippet_is_unchanged_by_the_tier(
    recall_fm_client,
) -> None:
    """Preservation: a body-substance record's snippet is byte-identical to
    what `build_snippet` alone produces (no grant → no head, no reflow)."""
    resp = await recall_fm_client.post(
        "/peer/recall", headers=_KALLE_HEADERS, json={"query": "rollout"},
    )
    data = await resp.json()
    project = next(m for m in data["matches"] if m["type"] == "project")
    body = (
        "Andrew is driving the RRTS rollout across the region this quarter."
    )
    assert project["snippet"] == build_snippet(body, "rollout", 500)[0]


async def test_recall_snippet_respects_max_chars_with_frontmatter(
    tmp_path, aiohttp_client,
) -> None:
    """The cap still bounds the TOTAL (frontmatter head + body tail)."""
    audit_path = str(tmp_path / "canonical_audit.jsonl")
    cfg = _fm_transport_config(audit_path=audit_path)
    cfg.recall.snippet_max_chars = 120
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(cfg, tstate)
    vault = _make_vault(tmp_path)
    _write_person_with_frontmatter(vault)
    register_vault_path(app, vault)
    register_instance_identity(app, name="Salem")
    register_recall_routes(app, enabled=True, instance_name="Salem")
    client = await aiohttp_client(app)

    match = await _ben_match(client)
    assert len(match["snippet"]) <= 120
    assert match["truncated"] is True
    # ORDER IS LOAD-BEARING: under a tight cap the SUBSTANCE survives and the
    # transclusion scaffolding is what gets dropped. Body-first would invert
    # this and reproduce the original defect.
    assert "role: Operations lead" in match["snippet"]
    assert "![[person.base#Decisions]]" not in match["snippet"]


async def test_recall_logs_the_disclosed_snippet_fields(recall_fm_client) -> None:
    """ILB: the answer names WHICH fields the frontmatter tier disclosed."""
    with structlog.testing.capture_logs() as captured:
        await recall_fm_client.post(
            "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Ben McMillan"},
        )
    answered = [c for c in captured if c.get("event") == "transport.recall.answered"]
    assert len(answered) == 1
    entry = answered[0]
    # `_granted` is the config CEILING (a name list); `_rendered` is a COUNT.
    assert set(entry["snippet_fields_granted"]) == {"name", "role", "org", "description"}
    # Every granted field is populated on this record → no gap.
    assert entry["snippet_fields_rendered"] == 4
    assert entry["snippet_records_with_frontmatter"] == 1
    assert "email" not in entry["snippet_fields_granted"]
    # The old ambiguous name must not survive anywhere — a stale audit query
    # keyed on it would silently read the ceiling as "what we disclosed".
    assert "snippet_fields" not in entry
    # The empty-tier signal must NOT fire when fields did contribute.
    assert not [
        c for c in captured
        if c.get("event") == "transport.recall.snippet_frontmatter_empty"
    ]


async def test_recall_logs_when_the_allowlist_contributes_nothing(
    recall_client,
) -> None:
    """ILB: matches but no field grant → the config-gap signal fires.

    `recall_client`'s canonical config has NO peer_permissions, so every
    snippet degrades to body-only. That is exactly the "names without
    meaning" shape, and it must be greppable rather than a silent 200.
    """
    with structlog.testing.capture_logs() as captured:
        resp = await recall_client.post(
            "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Andrew"},
        )
    assert resp.status == 200
    assert (await resp.json())["count"] >= 1
    empty = [
        c for c in captured
        if c.get("event") == "transport.recall.snippet_frontmatter_empty"
    ]
    assert len(empty) == 1
    assert empty[0]["peer"] == "kal-le"
    assert empty[0]["match_count"] >= 1
    assert empty[0]["reason"] == "no_field_grant"  # WHY it's empty, not just that
    answered = [c for c in captured if c.get("event") == "transport.recall.answered"]
    # Diagnosis 1 of 3: granted=0 → widen canonical.peer_permissions.
    assert answered[0]["snippet_fields_granted"] == []
    assert answered[0]["snippet_fields_rendered"] == 0
    assert answered[0]["snippet_records_with_frontmatter"] == 0
    assert answered[0]["snippet_records_filtered"] >= 1


async def test_recall_logs_the_live_granted_gt_rendered_shape(
    tmp_path, aiohttp_client,
) -> None:
    """Diagnosis 3 of 3 — and the MORNING GREP TARGET.

    Reproduces Salem's live kal-le/person grant
    (`[name, email, timezone, aliases, "preferences.coding"]`) against a
    template-shaped record. Two of those five are not fields on `person` at
    all, so the gate can never grant them — an allowlist entry is a ceiling,
    not a promise. `aliases` IS granted but empty, so it renders nothing.
    Expected on the box: granted 3, rendered 2 → partial substance, and
    NOTHING to action. Without both numbers this reads as a failure.
    """
    audit_path = str(tmp_path / "canonical_audit.jsonl")
    cfg = _transport_config(audit_path=audit_path)
    cfg.canonical = CanonicalConfig(
        audit_log_path=audit_path,
        peer_permissions={
            "kal-le": {
                "person": {
                    "fields": [
                        "name", "email", "timezone", "aliases",
                        "preferences.coding",
                    ],
                },
            },
        },
    )
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(cfg, tstate)
    vault = _make_vault(tmp_path)
    _write_person_with_frontmatter(vault)
    (vault / "person" / "Ben McMillan.md").write_text(
        "---\ntype: person\nname: Ben McMillan\n"
        f"email: {_WITHHELD_EMAIL}\n"
        "aliases: []\n"           # granted, EMPTY → counted, not rendered
        "description: Ops partner on the RRTS rollout\n"  # substance, ungranted
        "org: RRTS\nrole: Operations lead\n"
        "---\n" + _PERSON_TEMPLATE_BODY,
        encoding="utf-8",
    )
    register_vault_path(app, vault)
    register_instance_identity(app, name="Salem")
    register_recall_routes(app, enabled=True, instance_name="Salem")
    client = await aiohttp_client(app)

    with structlog.testing.capture_logs() as captured:
        resp = await client.post(
            "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Ben McMillan"},
        )
    assert resp.status == 200
    answered = [c for c in captured if c.get("event") == "transport.recall.answered"]
    entry = answered[0]
    assert entry["snippet_fields_granted"] == ["aliases", "email", "name"]  # 3
    assert entry["snippet_fields_rendered"] == 2                            # aliases empty
    assert entry["snippet_records_with_frontmatter"] == 1
    # Granted-but-unavailable allowlist entries never appear: they are not
    # fields on `person`, so the gate cannot grant them however config reads.
    assert "timezone" not in entry["snippet_fields_granted"]
    assert "preferences.coding" not in entry["snippet_fields_granted"]
    # Partial substance is NOT the empty state — the signal must stay quiet.
    assert not [
        c for c in captured
        if c.get("event") == "transport.recall.snippet_frontmatter_empty"
    ]
    # And the config-withheld substance is genuinely absent from the wire.
    person = next(m for m in (await resp.json())["matches"] if m["type"] == "person")
    assert "role: Operations lead" not in person["snippet"]
    assert "description:" not in person["snippet"]


async def test_recall_empty_signal_distinguishes_grant_exists_but_all_empty(
    tmp_path, aiohttp_client,
) -> None:
    """The second empty reason: a grant EXISTS but every granted field is empty.

    Same body-only outcome, completely different fix (the grant is fine — the
    records or the chosen fields carry nothing), so `reason` must tell them
    apart rather than both reading as "no grant".
    """
    audit_path = str(tmp_path / "canonical_audit.jsonl")
    cfg = _transport_config(audit_path=audit_path)
    # Granted a field that exists on the record but is EMPTY.
    cfg.canonical = CanonicalConfig(
        audit_log_path=audit_path,
        peer_permissions={"kal-le": {"person": {"fields": ["aliases"]}}},
    )
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(cfg, tstate)
    vault = _make_vault(tmp_path)
    _write_person_with_frontmatter(vault)  # has `aliases` absent → add it empty
    (vault / "person" / "Ben McMillan.md").write_text(
        "---\ntype: person\nname: Ben McMillan\naliases: []\n---\n"
        + _PERSON_TEMPLATE_BODY,
        encoding="utf-8",
    )
    register_vault_path(app, vault)
    register_instance_identity(app, name="Salem")
    register_recall_routes(app, enabled=True, instance_name="Salem")
    client = await aiohttp_client(app)

    with structlog.testing.capture_logs() as captured:
        resp = await client.post(
            "/peer/recall", headers=_KALLE_HEADERS, json={"query": "Ben McMillan"},
        )
    assert resp.status == 200
    empty = [
        c for c in captured
        if c.get("event") == "transport.recall.snippet_frontmatter_empty"
    ]
    assert len(empty) == 1
    assert empty[0]["reason"] == "granted_fields_all_empty"
    # Same key as the `answered` event → one grep reaches both sides.
    assert empty[0]["snippet_fields_granted"] == ["aliases"]  # the grant DID resolve
    answered = [c for c in captured if c.get("event") == "transport.recall.answered"]
    # Diagnosis 2 of 3: granted=1, rendered=0 → the grant is fine, the records
    # are empty. Indistinguishable from diagnosis 1 without BOTH numbers.
    assert answered[0]["snippet_fields_granted"] == ["aliases"]
    assert answered[0]["snippet_fields_rendered"] == 0


# --- pure helpers -----------------------------------------------------------


def test_render_frontmatter_summary_follows_allowlist_order() -> None:
    filtered = {"name": "Ben", "role": "Ops lead", "org": "RRTS"}
    granted = ["role", "org", "name"]  # operator's configured order
    summary, rendered = render_frontmatter_summary(filtered, granted)
    assert summary == "role: Ops lead\norg: RRTS\nname: Ben"
    assert rendered == ["role", "org", "name"]  # rendered order tracks the summary


def test_render_frontmatter_summary_joins_lists_and_drops_empties() -> None:
    filtered = {"aliases": ["Benny", "B"], "role": None, "org": "", "name": "Ben"}
    summary, rendered = render_frontmatter_summary(
        filtered, ["aliases", "role", "org", "name"],
    )
    assert summary == "aliases: Benny, B\nname: Ben"
    # The granted-vs-rendered split: role/org were granted but empty.
    assert rendered == ["aliases", "name"]


def test_render_frontmatter_summary_rendered_excludes_granted_but_empty() -> None:
    """The exact live shape: `aliases: []` is GRANTED but renders nothing.

    This is what makes the log read "granted 3, rendered 2" — without the
    second list, an empty granted field is indistinguishable from the gate
    withholding it.
    """
    filtered = {"name": "Ben McMillan", "email": "ben@example.invalid", "aliases": []}
    granted = ["name", "email", "aliases"]
    summary, rendered = render_frontmatter_summary(filtered, granted)
    assert summary == "name: Ben McMillan\nemail: ben@example.invalid"
    assert rendered == ["name", "email"]
    assert len(granted) == 3 and len(rendered) == 2


def test_render_frontmatter_summary_renders_dotted_paths() -> None:
    summary, rendered = render_frontmatter_summary(
        {"preferences": {"coding": "python"}}, ["preferences.coding"],
    )
    assert summary == "preferences.coding: python"
    assert rendered == ["preferences.coding"]


def test_render_frontmatter_summary_empty_is_empty() -> None:
    assert render_frontmatter_summary({}, []) == ("", [])


def test_compose_snippet_without_summary_is_byte_identical_to_body_only() -> None:
    """The degrade path is EXACTLY today's behavior — no reflow, no marker."""
    body = "alpha beta gamma NEEDLE delta epsilon " * 4
    for cap in (30, 80, 500):
        assert compose_recall_snippet("", body, "needle", cap) == build_snippet(
            body, "needle", cap,
        )


def test_compose_snippet_leads_with_summary() -> None:
    snippet, truncated = compose_recall_snippet(
        "role: Ops lead", "# Ben\n![[person.base#Decisions]]", "ben", 500,
    )
    assert snippet.startswith("role: Ops lead")
    assert "![[person.base#Decisions]]" in snippet
    assert truncated is False


def test_compose_snippet_drops_body_when_the_cap_is_spent() -> None:
    summary = "role: Ops lead"
    snippet, truncated = compose_recall_snippet(
        summary, "a body we cannot afford", "x", len(summary) + 1,
    )
    assert snippet == summary
    assert truncated is True  # honest: body content WAS withheld


def test_compose_snippet_truncates_an_oversize_summary() -> None:
    snippet, truncated = compose_recall_snippet("x" * 400, "body", "x", 50)
    assert len(snippet) <= 50
    assert truncated is True


def test_compose_snippet_summary_only_when_body_is_blank() -> None:
    snippet, truncated = compose_recall_snippet("role: Ops lead", "   ", "x", 500)
    assert snippet == "role: Ops lead"
    assert truncated is False  # nothing was withheld
