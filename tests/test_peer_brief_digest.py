"""Tests for the Salem-side ``POST /peer/brief_digest`` endpoint.

Covers the V.E.R.A. content-arc receiver: a peer (KAL-LE in v1) pushes
a one-slide markdown digest, Salem stores it under ``vault/run/`` for
the brief renderer to pick up.
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
from aiohttp.test_utils import TestClient

from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    PeerEntry,
    SchedulerConfig,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.peer_handlers import (
    register_instance_identity,
    register_vault_path,
)
from alfred.transport.server import build_app
from alfred.transport.state import TransportState


DUMMY_SALEM_LOCAL_TOKEN = "DUMMY_SALEM_LOCAL_TEST_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"
DUMMY_KALLE_PEER_TOKEN = "DUMMY_KALLE_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"


def _build_salem_config(audit_log_path: str = "") -> TransportConfig:
    """Salem-style config: kal-le peer authenticates with its own token."""
    tokens: dict[str, AuthTokenEntry] = {
        "local": AuthTokenEntry(
            token=DUMMY_SALEM_LOCAL_TOKEN,
            allowed_clients=["scheduler", "brief", "talker"],
        ),
        "kal-le": AuthTokenEntry(
            token=DUMMY_KALLE_PEER_TOKEN,
            allowed_clients=["kal-le"],
        ),
    }
    return TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens=tokens),
        state=StateConfig(),
        canonical=CanonicalConfig(
            owner=True,
            audit_log_path=audit_log_path,
            peer_permissions={},
        ),
        peers={
            "kal-le": PeerEntry(
                base_url="http://127.0.0.1:8892",
                token=DUMMY_KALLE_PEER_TOKEN,
            ),
        },
    )


@pytest.fixture
async def salem_app(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Salem-style app with vault root registered."""
    vault_root = tmp_path / "vault"
    vault_root.mkdir()
    config = _build_salem_config()
    state = TransportState.create(tmp_path / "transport_state.json")
    app = build_app(config, state)
    register_vault_path(app, vault_root)
    register_instance_identity(app, name="S.A.L.E.M.", alias="Salem")
    app["_vault_root"] = vault_root
    tc: TestClient = await aiohttp_client(app)
    return tc


# ---------------------------------------------------------------------------
# Happy path — 202 Accepted + vault write
# ---------------------------------------------------------------------------


async def test_brief_digest_accepted_writes_vault_record(salem_app):  # type: ignore[no-untyped-def]
    digest_md = (
        "**Yesterday:**\n"
        "- Shipped /peer/brief_digest c1\n"
        "- Wired KAL-LE scheduled push\n\n"
        "**Today:**\n"
        "- Polish v1 follow-ups\n\n"
        "**Posture:** green — all systems nominal."
    )

    resp = await salem_app.post(
        "/peer/brief_digest",
        json={
            "peer": "kal-le",
            "date": "2026-04-23",
            "digest_markdown": digest_md,
            "correlation_id": "kal-le-brief-20260423",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 202
    body = await resp.json()
    assert body["status"] == "accepted"
    assert body["path"] == "run/Peer Digest kal-le 2026-04-23.md"
    assert body["correlation_id"] == "kal-le-brief-20260423"

    vault_root: Path = salem_app.server.app["_vault_root"]
    record_path = vault_root / "run" / "Peer Digest kal-le 2026-04-23.md"
    assert record_path.exists()

    post = frontmatter.load(str(record_path))
    fm = dict(post.metadata or {})
    assert fm["type"] == "run"
    assert fm["source"] == "peer"
    assert fm["peer"] == "kal-le"
    assert fm["correlation_id"] == "kal-le-brief-20260423"
    assert fm["created"] == "2026-04-23"
    assert fm["content_length"] == len(digest_md.encode("utf-8"))
    # Body is the digest_markdown verbatim (rstrip-trailing).
    assert "Shipped /peer/brief_digest c1" in post.content
    assert "**Posture:** green" in post.content


async def test_brief_digest_overwrites_same_day(salem_app):  # type: ignore[no-untyped-def]
    """Re-pushing the same day's digest replaces in-place (idempotent re-fire)."""
    headers = {
        "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
        "X-Alfred-Client": "kal-le",
    }
    payload1 = {
        "peer": "kal-le", "date": "2026-04-23",
        "digest_markdown": "first version",
    }
    payload2 = {
        "peer": "kal-le", "date": "2026-04-23",
        "digest_markdown": "SECOND VERSION",
    }
    r1 = await salem_app.post("/peer/brief_digest", json=payload1, headers=headers)
    assert r1.status == 202
    r2 = await salem_app.post("/peer/brief_digest", json=payload2, headers=headers)
    assert r2.status == 202

    vault_root: Path = salem_app.server.app["_vault_root"]
    text = (vault_root / "run" / "Peer Digest kal-le 2026-04-23.md").read_text()
    assert "SECOND VERSION" in text
    assert "first version" not in text


# ---------------------------------------------------------------------------
# Auth + allowlist failures
# ---------------------------------------------------------------------------


async def test_brief_digest_missing_bearer_returns_401(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/brief_digest",
        json={"peer": "kal-le", "date": "2026-04-23", "digest_markdown": "x"},
        headers={"X-Alfred-Client": "kal-le"},
    )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "missing_bearer"


async def test_brief_digest_invalid_token_returns_401(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/brief_digest",
        json={"peer": "kal-le", "date": "2026-04-23", "digest_markdown": "x"},
        headers={
            "Authorization": "Bearer DUMMY_NOT_A_REAL_TOKEN_PLACEHOLDER",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "invalid_token"


async def test_brief_digest_client_not_allowed_returns_401(salem_app):  # type: ignore[no-untyped-def]
    """X-Alfred-Client must be in the peer's allowed_clients list."""
    resp = await salem_app.post(
        "/peer/brief_digest",
        json={"peer": "kal-le", "date": "2026-04-23", "digest_markdown": "x"},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "imposter",
        },
    )
    # The middleware returns 401 client_not_allowed (not 403) — keeps
    # the auth failure mode unified at the bearer-token boundary.
    assert resp.status == 401
    body = await resp.json()
    assert body["error"] == "client_not_allowed"


async def test_brief_digest_peer_spoofed_returns_403(salem_app):  # type: ignore[no-untyped-def]
    """body.peer must equal the authenticated peer (anti-spoof)."""
    resp = await salem_app.post(
        "/peer/brief_digest",
        json={
            "peer": "stay-c",  # Lying about identity — auth is kal-le
            "date": "2026-04-23",
            "digest_markdown": "malicious",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 403
    body = await resp.json()
    assert body["reason"] == "from_mismatch"


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


async def test_brief_digest_missing_peer_returns_400(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/brief_digest",
        json={"date": "2026-04-23", "digest_markdown": "x"},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"
    assert "peer" in body["detail"]


async def test_brief_digest_missing_date_returns_400(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/brief_digest",
        json={"peer": "kal-le", "digest_markdown": "x"},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"
    assert "date" in body["detail"]


async def test_brief_digest_empty_body_returns_400(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/brief_digest",
        json={"peer": "kal-le", "date": "2026-04-23", "digest_markdown": ""},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"


async def test_brief_digest_oversize_returns_400(salem_app):  # type: ignore[no-untyped-def]
    """Digests over the 50KB cap are rejected to protect the vault."""
    # 60 KB of content — well over the 50 KB cap.
    huge = "A" * 60_000
    resp = await salem_app.post(
        "/peer/brief_digest",
        json={"peer": "kal-le", "date": "2026-04-23", "digest_markdown": huge},
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "schema_error"
    assert "byte cap" in body["detail"]


async def test_brief_digest_invalid_json_returns_400(salem_app):  # type: ignore[no-untyped-def]
    resp = await salem_app.post(
        "/peer/brief_digest",
        data="not-json{",
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
            "Content-Type": "application/json",
        },
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["reason"] == "invalid_json"


# ---------------------------------------------------------------------------
# Filename sanitisation — peer-supplied date can't escape vault dir
# ---------------------------------------------------------------------------


async def test_brief_digest_filename_sanitises_path_traversal(salem_app):  # type: ignore[no-untyped-def]
    """A malicious date with ``../`` characters is sanitised into the filename."""
    resp = await salem_app.post(
        "/peer/brief_digest",
        json={
            "peer": "kal-le",
            "date": "../../etc/passwd",
            "digest_markdown": "harmless",
        },
        headers={
            "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 202
    body = await resp.json()
    # The peer-supplied date path-separators are sanitised — only the
    # vault-controlled "run/" prefix appears as a directory boundary.
    assert body["path"].startswith("run/")
    # Strip the controlled prefix; the remainder must not introduce
    # additional path separators.
    tail = body["path"][len("run/"):]
    assert "/" not in tail
    assert "\\" not in tail
    vault_root: Path = salem_app.server.app["_vault_root"]
    # File materialised under the vault, not somewhere outside it.
    file_path = vault_root / body["path"]
    assert file_path.exists()
    assert vault_root.resolve() in file_path.resolve().parents
