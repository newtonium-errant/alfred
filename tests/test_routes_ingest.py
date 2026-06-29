"""Tests for ``alfred.transport.routes_ingest`` — cross-instance ingest.

The peer-token-gated ``POST /vault/ingest`` route writes a single VERBATIM
{document, note, source} record into the target instance's vault via a
deterministic ``vault_create`` (NO run_turn, NO LLM) under the
``web_ingest`` scope.

Coverage (mandatory regression pins, run unconditionally):
    * Verbatim write — the body lands byte-for-byte (the wrong-order fix);
      provenance frontmatter stamped; the created log fires.
    * 409 title_collision — re-POST surfaces the existing path.
    * 413 body_too_large — the per-instance char cap is enforced.
    * 400 invalid_type / empty_title / empty_body; 503 vault_not_configured.
    * Layer-1 peer gate (no token → 401); opt-in inertness (disabled →
      route not mounted).
"""

from __future__ import annotations

import frontmatter
import pytest
import structlog

from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import register_vault_path
from alfred.transport.routes_ingest import (
    _handle_vault_ingest,  # noqa: F401 (import-presence sanity)
    register_ingest_routes,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState

# Obviously-fake test secrets — never a real provider prefix (builder.md
# GitGuardian rule).
DUMMY_INGEST_PEER_TOKEN = (
    "DUMMY_WEB_INGEST_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_0123456"
)
# A sibling chat ``web`` token (distinct from the ingest token) so the
# WARN-1 peer-pin escalation test can present a valid Layer-1 ``web`` token
# and prove the ingest handler refuses it.
DUMMY_WEB_CHAT_TOKEN = (
    "DUMMY_WEB_CHAT_TOKEN_64CHAR_PLACEHOLDER_FOR_TESTING_ONLY_012345678"
)

_PEER_HEADERS = {
    "Authorization": f"Bearer {DUMMY_INGEST_PEER_TOKEN}",
    "X-Alfred-Client": "web",
    "Content-Type": "application/json",
}


def _transport_config() -> TransportConfig:
    """The ingest token lives under the dedicated ``web_ingest`` peer (the
    production peer NAME the handler peer-pins on). A sibling chat ``web``
    peer (same ``allowed_clients: [web]``) is present so the escalation test
    can present a valid Layer-1 ``web`` token."""
    return TransportConfig(
        server=ServerConfig(),
        auth=AuthConfig(
            tokens={
                "web_ingest": AuthTokenEntry(
                    token=DUMMY_INGEST_PEER_TOKEN,
                    allowed_clients=["web"],
                ),
                "web": AuthTokenEntry(
                    token=DUMMY_WEB_CHAT_TOKEN,
                    allowed_clients=["web"],
                ),
            }
        ),
        state=StateConfig(),
    )


def _make_vault(tmp_path):
    vault = tmp_path / "vault"
    for sub in ("document", "note", "source"):
        (vault / sub).mkdir(parents=True)
    return vault


@pytest.fixture
async def ingest_client(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Transport app with the ingest route mounted (enabled) + a vault."""
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    vault = _make_vault(tmp_path)
    register_vault_path(app, vault)
    mounted = register_ingest_routes(
        app, enabled=True, instance_name="Salem", max_body_chars=2048,
    )
    assert mounted is True
    app["_vault"] = vault
    return await aiohttp_client(app)


def _payload(**overrides):
    base = {
        "record_type": "document",
        "title": "Quarterly Plan 2026",
        "body": "# Heading\n\nLine one.\n\n- bullet A\n- bullet B\n",
        "source": "pasted from notes app",
        "ingested_by": "andrew",
        "correlation_id": "ingest-test-001",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Verbatim write + provenance + created log
# ---------------------------------------------------------------------------


async def test_ingest_verbatim_write(ingest_client) -> None:
    vault = ingest_client.app["_vault"]
    body_text = (
        "# Section 1\n\nFirst paragraph.\n\n"
        "# Section 2\n\n1. step one\n2. step two\n3. step three\n"
    )
    with structlog.testing.capture_logs() as captured:
        resp = await ingest_client.post(
            "/vault/ingest",
            json=_payload(body=body_text),
            headers={**_PEER_HEADERS, "X-Alfred-Ingest-User": "andrew"},
        )
    assert resp.status == 200
    out = await resp.json()
    assert out["status"] == "created"
    assert out["record_type"] == "document"
    assert out["instance"] == "Salem"
    assert out["path"] == "document/Quarterly Plan 2026.md"

    # The body landed BYTE-FOR-BYTE (the wrong-order fix) — no agent
    # rearrangement, no chunking.
    record = vault / "document" / "Quarterly Plan 2026.md"
    assert record.exists()
    post = frontmatter.load(str(record))
    assert post.content.rstrip("\n") == body_text.rstrip("\n")

    # Provenance frontmatter — header assertion wins for ingested_by.
    assert post.metadata["type"] == "document"
    assert post.metadata["source"] == "pasted from notes app"
    assert post.metadata["ingested_by"] == "andrew"
    assert post.metadata["ingested_via"] == "web"
    assert post.metadata["ingested_at"]
    assert post.metadata["ingest_correlation_id"] == "ingest-test-001"

    # Created log fired with the key fields (observability discipline #9).
    created = [c for c in captured if c.get("event") == "transport.ingest.created"]
    assert len(created) == 1
    assert created[0]["record_type"] == "document"
    assert created[0]["path"] == "document/Quarterly Plan 2026.md"
    assert created[0]["instance"] == "Salem"
    assert created[0]["correlation_id"] == "ingest-test-001"


async def test_ingest_header_user_overrides_body(ingest_client) -> None:
    resp = await ingest_client.post(
        "/vault/ingest",
        json=_payload(title="Header User Doc", ingested_by="body-claimed"),
        headers={**_PEER_HEADERS, "X-Alfred-Ingest-User": "header-asserted"},
    )
    assert resp.status == 200
    vault = ingest_client.app["_vault"]
    post = frontmatter.load(str(vault / "document" / "Header User Doc.md"))
    assert post.metadata["ingested_by"] == "header-asserted"


# ---------------------------------------------------------------------------
# 409 title collision
# ---------------------------------------------------------------------------


async def test_ingest_409_collision_surfaces_path(ingest_client) -> None:
    p = _payload(title="Dup Title Doc")
    r1 = await ingest_client.post("/vault/ingest", json=p, headers=_PEER_HEADERS)
    assert r1.status == 200

    with structlog.testing.capture_logs() as captured:
        r2 = await ingest_client.post("/vault/ingest", json=p, headers=_PEER_HEADERS)
    assert r2.status == 409
    out = await r2.json()
    assert out["error"] == "title_collision"
    assert out["path"] == "document/Dup Title Doc.md"
    collision = [c for c in captured if c.get("event") == "transport.ingest.collision"]
    assert len(collision) == 1
    assert collision[0]["path"] == "document/Dup Title Doc.md"


# ---------------------------------------------------------------------------
# Body-size cap (413)
# ---------------------------------------------------------------------------


async def test_ingest_body_too_large(ingest_client) -> None:
    # Fixture cap is 2048 chars.
    big = "x" * 3000
    with structlog.testing.capture_logs() as captured:
        resp = await ingest_client.post(
            "/vault/ingest",
            json=_payload(title="Too Big Doc", body=big),
            headers=_PEER_HEADERS,
        )
    assert resp.status == 413
    out = await resp.json()
    assert out["error"] == "body_too_large"
    assert out["max_chars"] == 2048
    rejected = [c for c in captured if c.get("event") == "transport.ingest.rejected"]
    assert any(r.get("reason") == "body_too_large" for r in rejected)


# ---------------------------------------------------------------------------
# 400 validation paths
# ---------------------------------------------------------------------------


async def test_ingest_invalid_type(ingest_client) -> None:
    resp = await ingest_client.post(
        "/vault/ingest",
        json=_payload(record_type="task"),
        headers=_PEER_HEADERS,
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "invalid_type"


async def test_ingest_empty_title(ingest_client) -> None:
    resp = await ingest_client.post(
        "/vault/ingest",
        json=_payload(title="   "),
        headers=_PEER_HEADERS,
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "empty_title"


async def test_ingest_empty_body(ingest_client) -> None:
    resp = await ingest_client.post(
        "/vault/ingest",
        json=_payload(title="Empty Body Doc", body="   "),
        headers=_PEER_HEADERS,
    )
    assert resp.status == 400
    assert (await resp.json())["error"] == "empty_body"


async def test_ingest_note_and_source_types(ingest_client) -> None:
    for rec_type, sub in (("note", "note"), ("source", "source")):
        resp = await ingest_client.post(
            "/vault/ingest",
            json=_payload(record_type=rec_type, title=f"{rec_type} doc"),
            headers=_PEER_HEADERS,
        )
        assert resp.status == 200, rec_type
        assert (await resp.json())["record_type"] == rec_type


# ---------------------------------------------------------------------------
# Layer-1 peer gate + vault-not-configured + opt-in inertness
# ---------------------------------------------------------------------------


async def test_ingest_requires_peer_token(ingest_client) -> None:
    resp = await ingest_client.post("/vault/ingest", json=_payload())
    assert resp.status == 401


async def test_ingest_rejects_chat_web_token(ingest_client) -> None:
    # WARN-1 regression pin: a VALID Layer-1 chat ``web`` token (clears
    # auth_middleware as peer ``web`` since web/web_ingest share
    # allowed_clients:[web]) must NOT drive an ingest write — the peer-pin
    # rejects it (the ``web`` token is for full chat, not deterministic
    # ingest).
    headers = {
        "Authorization": f"Bearer {DUMMY_WEB_CHAT_TOKEN}",
        "X-Alfred-Client": "web",
        "Content-Type": "application/json",
    }
    with structlog.testing.capture_logs() as captured:
        resp = await ingest_client.post(
            "/vault/ingest", json=_payload(title="Escalation Doc"), headers=headers
        )
    assert resp.status == 401
    assert (await resp.json())["error"] == "wrong_peer"
    rejected = [c for c in captured if c.get("event") == "transport.ingest.rejected"]
    assert any(r.get("reason") == "wrong_peer" for r in rejected)
    # And the write did NOT land.
    vault = ingest_client.app["_vault"]
    assert not (vault / "document" / "Escalation Doc.md").exists()


async def test_ingest_vault_not_configured(aiohttp_client, tmp_path) -> None:
    tstate = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(_transport_config(), tstate)
    # NOTE: register_vault_path deliberately NOT called.
    register_ingest_routes(app, enabled=True, instance_name="Salem")
    client = await aiohttp_client(app)
    resp = await client.post("/vault/ingest", json=_payload(), headers=_PEER_HEADERS)
    assert resp.status == 503
    assert (await resp.json())["error"] == "vault_not_configured"


def test_register_ingest_routes_disabled_mounts_nothing() -> None:
    from aiohttp import web

    app = web.Application()
    mounted = register_ingest_routes(app, enabled=False, instance_name="Salem")
    assert mounted is False
    paths = [
        r.resource.canonical
        for r in app.router.routes()
        if r.resource is not None
    ]
    assert "/vault/ingest" not in paths


def test_register_ingest_routes_enabled_mounts_route() -> None:
    from aiohttp import web

    app = web.Application()
    mounted = register_ingest_routes(app, enabled=True, instance_name="Salem")
    assert mounted is True
    paths = [
        r.resource.canonical
        for r in app.router.routes()
        if r.resource is not None
    ]
    assert "/vault/ingest" in paths
