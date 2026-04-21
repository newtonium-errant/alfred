"""End-to-end integration: Salem + KAL-LE aiohttp apps side-by-side.

Builds two ``aiohttp.Application`` instances on ephemeral ports and
exercises the full Stage 3.5 peer protocol:

- cross-instance /peer/handshake succeeds
- Salem's /peer/query over its own canonical record respects field
  permissions
- Salem peer-forwards a coding request to KAL-LE; KAL-LE runs a
  (mocked) bash_exec and POSTs the result back via /peer/send to
  Salem, which relays via the correlation-id inbox
- fallback when one peer is unreachable

Intentionally NOT touching Telegram — the bot layer is mocked via the
peer-inbox callable. Full Telegram-to-Telegram round-trips are
``--real-telegram`` gated (operator runs locally).
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest
from aiohttp.test_utils import TestClient

from alfred.transport.canonical_audit import read_audit
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    CanonicalConfig,
    PeerEntry,
    PeerFieldRules,
    ServerConfig,
    SchedulerConfig,
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


DUMMY_SALEM_TOKEN = "DUMMY_SALEM_TEST_TOKEN_PLACEHOLDER_0123456789ABCDEF"
DUMMY_KALLE_TOKEN = "DUMMY_KALLE_TEST_TOKEN_PLACEHOLDER_0123456789ABCDEF"
# Distinct peer tokens for the peer-pair plumbing. Matching the
# .env.example convention where ALFRED_KALLE_PEER_TOKEN and
# ALFRED_SALEM_PEER_TOKEN are separate secrets.
DUMMY_KALLE_PEER_TOKEN = "DUMMY_KALLE_PEER_TEST_PLACEHOLDER_NOT_REAL_9876543210"
DUMMY_SALEM_PEER_TOKEN = "DUMMY_SALEM_PEER_TEST_PLACEHOLDER_NOT_REAL_9876543210"


def _free_port() -> int:
    """Ephemeral port — avoid 8891/8892 so we never collide with a
    running Alfred instance during dev.
    """
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _salem_config(
    kalle_url: str,
    audit_path: str,
) -> TransportConfig:
    """Salem's view: canonical owner, KAL-LE as a permissioned peer.

    auth.tokens:
    - ``local``: Salem's own inbound token for co-located tools.
    - ``kal-le``: token KAL-LE uses when reaching Salem
      (DUMMY_SALEM_PEER_TOKEN matches what KAL-LE will send as
      its outbound token in peers["salem"].token).

    peers.kal-le: outbound to KAL-LE — Salem sends
    DUMMY_KALLE_PEER_TOKEN, which KAL-LE's auth.tokens.salem
    validates.
    """
    return TransportConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={
            "local": AuthTokenEntry(
                token=DUMMY_SALEM_TOKEN,
                allowed_clients=["scheduler", "salem"],
            ),
            "kal-le": AuthTokenEntry(
                token=DUMMY_SALEM_PEER_TOKEN,
                allowed_clients=["kal-le"],
            ),
        }),
        state=StateConfig(),
        canonical=CanonicalConfig(
            owner=True,
            audit_log_path=audit_path,
            peer_permissions={
                "kal-le": {
                    "person": PeerFieldRules(
                        fields=["name", "email", "timezone"],
                    ),
                },
            },
        ),
        peers={
            "kal-le": PeerEntry(
                base_url=kalle_url,
                token=DUMMY_KALLE_PEER_TOKEN,
            ),
        },
    )


def _kalle_config(salem_url: str) -> TransportConfig:
    """KAL-LE's view: not canonical owner, Salem is a peer.

    KAL-LE's inbound auth.tokens:
    - ``local``: tokens for co-located tools on the KAL-LE host
      (scheduler, brief, etc. — none in the test).
    - ``salem``: the token SALEM uses to auth when talking to KAL-LE.
      allowed_clients allows ``salem`` as a client name.

    In tests we use a single token per peer — DUMMY_KALLE_TOKEN on
    KAL-LE's local entry (for kal-le-originated calls) and
    DUMMY_SALEM_TOKEN on the salem entry (for salem-originated
    calls from Salem).
    """
    return TransportConfig(
        server=ServerConfig(host="127.0.0.1", port=0),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={
            "local": AuthTokenEntry(
                token=DUMMY_KALLE_TOKEN,
                allowed_clients=["scheduler", "kal-le"],
            ),
            "salem": AuthTokenEntry(
                # Salem uses DUMMY_KALLE_PEER_TOKEN as its outbound
                # token to KAL-LE; KAL-LE's auth.tokens.salem entry
                # validates it here.
                token=DUMMY_KALLE_PEER_TOKEN,
                allowed_clients=["salem"],
            ),
        }),
        state=StateConfig(),
        canonical=CanonicalConfig(owner=False),
        peers={
            "salem": PeerEntry(
                base_url=salem_url,
                # KAL-LE → Salem: send ALFRED_SALEM_PEER_TOKEN.
                token=DUMMY_SALEM_PEER_TOKEN,
            ),
        },
    )


# ---------------------------------------------------------------------------
# Test 1: cross-instance handshake
# ---------------------------------------------------------------------------


async def test_cross_instance_handshake(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Build Salem + KAL-LE. Salem's client hits KAL-LE's /peer/handshake."""
    # Build KAL-LE first (placeholder URL on Salem).
    kalle_state = TransportState.create(tmp_path / "kalle_state.json")
    kalle_config = _kalle_config(salem_url="http://127.0.0.1:1")
    kalle_app = build_app(kalle_config, kalle_state)
    register_instance_identity(kalle_app, name="KAL-LE", alias="Kali")
    kalle_client: TestClient = await aiohttp_client(kalle_app)

    # Salem knows the live KAL-LE URL.
    kalle_url = f"http://127.0.0.1:{kalle_client.port}"
    salem_state = TransportState.create(tmp_path / "salem_state.json")
    salem_config = _salem_config(
        kalle_url=kalle_url,
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    salem_app = build_app(salem_config, salem_state)
    register_instance_identity(salem_app, name="S.A.L.E.M.", alias="Salem")

    # Salem POSTs to KAL-LE's handshake directly (using its test client
    # is awkward from the salem-side; use httpx against the live test
    # port).
    import httpx

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{kalle_url}/peer/handshake",
            json={"from": "salem", "protocol_version": 1},
            headers={
                # Salem sends DUMMY_KALLE_PEER_TOKEN — KAL-LE's
                # auth.tokens.salem entry validates it.
                "Authorization": f"Bearer {DUMMY_KALLE_PEER_TOKEN}",
                "X-Alfred-Client": "salem",
                "X-Correlation-Id": "handshake-cid-1",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["instance"] == "KAL-LE"
    assert body["alias"] == "Kali"
    assert body["correlation_id"] == "handshake-cid-1"


# ---------------------------------------------------------------------------
# Test 2: Salem canonical query with field-permission filter
# ---------------------------------------------------------------------------


async def test_salem_canonical_query_filters_fields(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """Salem hosts a canonical person record; KAL-LE queries it over /peer/query."""
    salem_state = TransportState.create(tmp_path / "salem_state.json")
    salem_config = _salem_config(
        kalle_url="http://127.0.0.1:1",
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    # Create vault with a canonical person record.
    vault = tmp_path / "vault"
    (vault / "person").mkdir(parents=True)
    (vault / "person" / "Andrew Newton.md").write_text(
        "---\n"
        "name: Andrew Newton\n"
        "email: andrew@example.com\n"
        "timezone: America/Halifax\n"
        "phone: +1-555-1234\n"
        "type: person\n"
        "---\n"
        "Body should never be exposed.\n",
        encoding="utf-8",
    )

    salem_app = build_app(salem_config, salem_state)
    register_vault_path(salem_app, vault)
    register_instance_identity(salem_app, name="S.A.L.E.M.")
    salem_client: TestClient = await aiohttp_client(salem_app)

    resp = await salem_client.post(
        "/peer/query",
        json={
            "record_type": "person",
            "name": "Andrew Newton",
            "fields": ["name", "email"],
        },
        headers={
            # KAL-LE → Salem uses DUMMY_SALEM_PEER_TOKEN, which
            # Salem's auth.tokens.kal-le entry validates.
            "Authorization": f"Bearer {DUMMY_SALEM_PEER_TOKEN}",
            "X-Alfred-Client": "kal-le",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["frontmatter"]["name"] == "Andrew Newton"
    assert body["frontmatter"]["email"] == "andrew@example.com"
    # Body never exposed.
    assert "body" not in body
    # Requested fields respected (timezone was permitted but not requested).
    assert "timezone" not in body["frontmatter"]
    # phone was never permitted.
    assert "phone" not in body["frontmatter"]

    # Audit entry exists.
    entries = read_audit(str(tmp_path / "audit.jsonl"))
    assert len(entries) == 1
    assert entries[0]["peer"] == "kal-le"
    assert "phone" in entries[0]["denied"]


# ---------------------------------------------------------------------------
# Test 3: Salem peer-forwards to KAL-LE → mock bash_exec → relay back
# ---------------------------------------------------------------------------


async def test_salem_forwards_to_kalle_and_receives_reply(aiohttp_client, tmp_path):  # type: ignore[no-untyped-def]
    """The full loop: Salem /peer/send → KAL-LE inbox → KAL-LE /peer/send back to Salem.

    The KAL-LE inbox stub simulates the c11 dogfood flow: receive
    message, run a (mocked) bash_exec via a callable that returns
    a canned result, POST the result back to Salem's /peer/send with
    the same correlation_id. Salem's inbox calls
    ``register_response(cid, reply)`` to unblock the waiting router.
    """
    from alfred.transport.peers import await_response, register_response, _INBOX, _ORPHANS

    # Clear inbox between tests.
    _INBOX.clear()
    _ORPHANS.clear()

    # --- KAL-LE side ---
    kalle_received: list[dict[str, Any]] = []

    # KAL-LE's inbox: receives Salem's forwarded message, "runs" the
    # bash_exec (mocked), POSTs the result back to Salem asynchronously.
    async def _kalle_inbox(*, kind, payload, from_peer, correlation_id):
        kalle_received.append({
            "kind": kind, "payload": payload,
            "from_peer": from_peer, "cid": correlation_id,
        })

        async def _reply_to_salem() -> None:
            # Mock bash_exec result.
            bash_result = {
                "exit_code": 0,
                "stdout": "52 passed, 1 failed",
                "stderr": "",
            }
            import httpx
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{salem_url}/peer/send",
                    json={
                        "kind": "query_result",
                        "from": "kal-le",
                        "payload": {
                            "text": (
                                f"tests: {bash_result['stdout']}"
                            ),
                            "bash_result": bash_result,
                        },
                        "correlation_id": correlation_id,
                    },
                    headers={
                        # KAL-LE → Salem outbound uses
                        # DUMMY_SALEM_PEER_TOKEN.
                        "Authorization": f"Bearer {DUMMY_SALEM_PEER_TOKEN}",
                        "X-Alfred-Client": "kal-le",
                        "X-Correlation-Id": correlation_id,
                    },
                )

        asyncio.create_task(_reply_to_salem())
        return {"accepted_by": "kal-le-inbox"}

    # --- Salem side ---
    async def _salem_inbox(*, kind, payload, from_peer, correlation_id):
        """Salem's inbox: relay the reply back to the correlation-id
        inbox so whoever's awaiting the response can wake up.
        """
        register_response(correlation_id, dict(payload))
        return {"accepted_by": "salem-inbox"}

    # Build KAL-LE first.
    kalle_state = TransportState.create(tmp_path / "kalle_state.json")
    kalle_config = _kalle_config(salem_url="http://127.0.0.1:1")
    kalle_app = build_app(kalle_config, kalle_state)
    register_instance_identity(kalle_app, name="KAL-LE")
    register_peer_inbox(kalle_app, _kalle_inbox)
    kalle_client: TestClient = await aiohttp_client(kalle_app)
    kalle_url = f"http://127.0.0.1:{kalle_client.port}"

    # Build Salem with correct KAL-LE URL.
    salem_state = TransportState.create(tmp_path / "salem_state.json")
    salem_config = _salem_config(
        kalle_url=kalle_url,
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    salem_app = build_app(salem_config, salem_state)
    register_instance_identity(salem_app, name="S.A.L.E.M.")
    register_peer_inbox(salem_app, _salem_inbox)
    salem_client: TestClient = await aiohttp_client(salem_app)
    salem_url = f"http://127.0.0.1:{salem_client.port}"

    # Simulate Salem's router sending a coding request to KAL-LE.
    from alfred.transport.client import peer_send

    correlation_id = "dual-instance-cid-42"

    await peer_send(
        "kal-le",
        kind="message",
        payload={
            "user_id": 123,
            "text": "run pytest",
            "originating_session": "sess-abc",
        },
        config=salem_config,
        self_name="salem",
        correlation_id=correlation_id,
    )

    # KAL-LE's inbox recorded the message.
    assert len(kalle_received) == 1
    assert kalle_received[0]["cid"] == correlation_id
    assert kalle_received[0]["payload"]["text"] == "run pytest"

    # Now wait for KAL-LE to POST back.
    reply = await await_response(correlation_id, timeout=3.0)
    assert reply["text"] == "tests: 52 passed, 1 failed"
    assert reply["bash_result"]["exit_code"] == 0


# ---------------------------------------------------------------------------
# Test 4: fallback when peer unreachable
# ---------------------------------------------------------------------------


async def test_fallback_when_peer_unreachable(tmp_path):  # type: ignore[no-untyped-def]
    """peer_send to a dead URL raises TransportServerDown."""
    from alfred.transport.client import peer_send
    from alfred.transport.exceptions import TransportServerDown

    # Salem config points at a nonexistent KAL-LE port.
    salem_config = _salem_config(
        kalle_url="http://127.0.0.1:1",  # RFC 1149 port, universally closed
        audit_path=str(tmp_path / "audit.jsonl"),
    )

    with pytest.raises(TransportServerDown):
        await peer_send(
            "kal-le",
            kind="message",
            payload={"text": "hello"},
            config=salem_config,
            self_name="salem",
        )
