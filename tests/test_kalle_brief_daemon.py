"""Tests for the KAL-LE brief-digest pusher daemon (V.E.R.A. sender).

Covers:
- Config loading (defaults, overrides, missing block)
- peer_send_brief_digest client request shape (mocked HTTP layer)
- fire_once happy path: assemble + push + return
- fire_once failure path: TransportError logged, ok=False, no raise
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from alfred.brief.kalle_brief_daemon import (
    BriefDigestPushConfig,
    fire_once,
    load_brief_digest_push_config,
)
from alfred.transport.config import (
    AuthConfig,
    AuthTokenEntry,
    PeerEntry,
    SchedulerConfig,
    ServerConfig,
    StateConfig,
    TransportConfig,
)
from alfred.transport.exceptions import TransportServerDown


DUMMY_KALLE_PEER_TOKEN = "DUMMY_KALLE_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_config_block_missing_returns_disabled() -> None:
    raw: dict[str, Any] = {"logging": {"dir": "/tmp/data"}}
    cfg = load_brief_digest_push_config(raw)
    assert cfg.enabled is False
    assert cfg.self_name == ""


def test_load_config_with_overrides(tmp_path: Path) -> None:
    raw: dict[str, Any] = {
        "logging": {"dir": str(tmp_path)},
        "brief_digest_push": {
            "enabled": True,
            "self_name": "kal-le",
            "target_peer": "salem",
            "schedule": {"time": "05:30", "timezone": "America/Halifax"},
            "repo_paths": ["/home/andrew/aftermath-lab", "/home/andrew/aftermath-alfred"],
        },
    }
    cfg = load_brief_digest_push_config(raw)
    assert cfg.enabled is True
    assert cfg.self_name == "kal-le"
    assert cfg.target_peer == "salem"
    assert cfg.schedule.time == "05:30"
    assert cfg.schedule.timezone == "America/Halifax"
    assert cfg.repo_paths == [
        "/home/andrew/aftermath-lab",
        "/home/andrew/aftermath-alfred",
    ]
    # data_dir defaults to logging.dir when omitted.
    assert cfg.data_dir == str(tmp_path)


def test_load_config_data_dir_explicit_override(tmp_path: Path) -> None:
    custom = tmp_path / "custom-data"
    raw: dict[str, Any] = {
        "logging": {"dir": str(tmp_path)},
        "brief_digest_push": {
            "enabled": True,
            "self_name": "kal-le",
            "data_dir": str(custom),
        },
    }
    cfg = load_brief_digest_push_config(raw)
    assert cfg.data_dir == str(custom)


def test_load_config_defaults_target_peer_to_salem() -> None:
    raw: dict[str, Any] = {
        "logging": {"dir": "/tmp/d"},
        "brief_digest_push": {"enabled": True, "self_name": "stay-c"},
    }
    cfg = load_brief_digest_push_config(raw)
    assert cfg.target_peer == "salem"


# ---------------------------------------------------------------------------
# peer_send_brief_digest client
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peer_send_brief_digest_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client builds the right URL / body / headers."""
    captured: dict[str, Any] = {}

    async def _fake_peer_request(
        *,
        base_url: str,
        token: str,
        method: str,
        path: str,
        self_name: str,
        correlation_id: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        captured["base_url"] = base_url
        captured["token"] = token
        captured["method"] = method
        captured["path"] = path
        captured["self_name"] = self_name
        captured["correlation_id"] = correlation_id
        captured["json_body"] = json_body
        return {"status": "accepted", "path": "run/Peer Digest kal-le 2026-04-23.md", "correlation_id": correlation_id}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "_peer_request", _fake_peer_request)

    transport_cfg = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={}),
        state=StateConfig(),
        peers={
            "salem": PeerEntry(
                base_url="http://127.0.0.1:8891",
                token=DUMMY_KALLE_PEER_TOKEN,
            ),
        },
    )

    response = await client_mod.peer_send_brief_digest(
        "salem",
        digest_markdown="**Yesterday:**\n- 1 commit\n",
        digest_date="2026-04-23",
        self_name="kal-le",
        config=transport_cfg,
    )

    assert response["status"] == "accepted"
    assert captured["base_url"] == "http://127.0.0.1:8891"
    assert captured["token"] == DUMMY_KALLE_PEER_TOKEN
    assert captured["method"] == "POST"
    assert captured["path"] == "/peer/brief_digest"
    assert captured["self_name"] == "kal-le"
    # Default correlation id format = "{self_name}-brief-{date}".
    assert captured["correlation_id"] == "kal-le-brief-2026-04-23"
    body = captured["json_body"]
    assert body["peer"] == "kal-le"
    assert body["date"] == "2026-04-23"
    assert "1 commit" in body["digest_markdown"]
    assert body["correlation_id"] == "kal-le-brief-2026-04-23"


@pytest.mark.asyncio
async def test_peer_send_brief_digest_explicit_correlation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    async def _fake_peer_request(**kw: Any) -> dict[str, Any]:
        captured.update(kw)
        return {"status": "accepted"}

    import alfred.transport.client as client_mod
    monkeypatch.setattr(client_mod, "_peer_request", _fake_peer_request)

    transport_cfg = TransportConfig(
        peers={
            "salem": PeerEntry(base_url="http://x", token="t"),
        },
    )
    await client_mod.peer_send_brief_digest(
        "salem",
        digest_markdown="x",
        digest_date="2026-04-23",
        self_name="kal-le",
        config=transport_cfg,
        correlation_id="custom-cid-1234",
    )
    assert captured["correlation_id"] == "custom-cid-1234"
    assert captured["json_body"]["correlation_id"] == "custom-cid-1234"


# ---------------------------------------------------------------------------
# fire_once — happy path
# ---------------------------------------------------------------------------


def _make_data_dir(tmp_path: Path) -> Path:
    """Create a minimal KAL-LE data dir with one bash_exec entry yesterday."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "bash_exec.jsonl").write_text(
        json.dumps({"ts": "2026-04-22T12:00:00+00:00", "cwd": "/x/aftermath-alfred"}) + "\n",
        encoding="utf-8",
    )
    (data_dir / "instructor_state.json").write_text(
        json.dumps({"retry_counts": {}}),
        encoding="utf-8",
    )
    return data_dir


@pytest.mark.asyncio
async def test_fire_once_assembles_and_pushes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    captured_pushes: list[dict[str, Any]] = []

    async def _fake_send(
        peer_name: str,
        *,
        digest_markdown: str,
        digest_date: str,
        self_name: str,
        config: TransportConfig | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        captured_pushes.append({
            "peer_name": peer_name,
            "digest_markdown": digest_markdown,
            "digest_date": digest_date,
            "self_name": self_name,
        })
        return {"status": "accepted", "path": f"run/Peer Digest kal-le {digest_date}.md", "correlation_id": "abc"}

    import alfred.brief.kalle_brief_daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "peer_send_brief_digest", _fake_send)

    data_dir = _make_data_dir(tmp_path)
    config = BriefDigestPushConfig(
        enabled=True,
        self_name="kal-le",
        target_peer="salem",
        repo_paths=[],
        data_dir=str(data_dir),
    )
    transport_cfg = TransportConfig(
        peers={"salem": PeerEntry(base_url="http://x", token="t")},
    )

    result = await fire_once(config, transport_cfg, today=date(2026, 4, 23))

    assert result["ok"] is True
    assert result["date"] == "2026-04-23"
    assert result["digest_length"] > 0
    assert len(captured_pushes) == 1
    push = captured_pushes[0]
    assert push["peer_name"] == "salem"
    assert push["self_name"] == "kal-le"
    assert push["digest_date"] == "2026-04-23"
    assert "**Yesterday:**" in push["digest_markdown"]
    assert "**Posture:**" in push["digest_markdown"]


# ---------------------------------------------------------------------------
# fire_once — failure path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_once_transport_failure_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A TransportServerDown returns ok=False but never raises."""

    async def _fail(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise TransportServerDown("salem is restarting")

    import alfred.brief.kalle_brief_daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "peer_send_brief_digest", _fail)

    captured: list[dict[str, Any]] = []

    def _capture(event: str, **kw: Any) -> None:
        captured.append({"event": event, **kw})

    monkeypatch.setattr(daemon_mod.log, "warning", _capture)

    data_dir = _make_data_dir(tmp_path)
    config = BriefDigestPushConfig(
        enabled=True, self_name="kal-le", target_peer="salem",
        repo_paths=[], data_dir=str(data_dir),
    )

    result = await fire_once(
        config, TransportConfig(), today=date(2026, 4, 23),
    )
    assert result["ok"] is False
    assert result["error_type"] == "TransportServerDown"
    assert "salem is restarting" in result["error"]
    # Exactly one push_failed warning emitted with the contract fields.
    push_failures = [c for c in captured if c["event"] == "kalle.brief_digest.push_failed"]
    assert len(push_failures) == 1
    f = push_failures[0]
    assert f["error_type"] == "TransportServerDown"
    assert "response_summary" in f


@pytest.mark.asyncio
async def test_fire_once_unexpected_error_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """A bare Exception (non-TransportError) also doesn't propagate."""

    async def _boom(*_args: Any, **_kw: Any) -> dict[str, Any]:
        raise RuntimeError("network glitch")

    import alfred.brief.kalle_brief_daemon as daemon_mod
    monkeypatch.setattr(daemon_mod, "peer_send_brief_digest", _boom)

    data_dir = _make_data_dir(tmp_path)
    config = BriefDigestPushConfig(
        enabled=True, self_name="kal-le", target_peer="salem",
        repo_paths=[], data_dir=str(data_dir),
    )
    result = await fire_once(
        config, TransportConfig(), today=date(2026, 4, 23),
    )
    assert result["ok"] is False
    assert result["error_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# End-to-end integration — fire_once → real aiohttp /peer/brief_digest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fire_once_against_real_salem_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, aiohttp_client,  # type: ignore[no-untyped-def]
) -> None:
    """Spin up a real Salem-style /peer/brief_digest server and verify
    that fire_once produces a valid request that the receiver accepts
    + materialises into a vault record."""
    from alfred.transport.peer_handlers import (
        register_instance_identity,
        register_vault_path,
    )
    from alfred.transport.server import build_app
    from alfred.transport.state import TransportState
    from alfred.transport.config import (
        AuthConfig, AuthTokenEntry, CanonicalConfig,
    )

    # Build a real Salem-style server.
    vault_root = tmp_path / "salem-vault"
    vault_root.mkdir()
    transport_cfg = TransportConfig(
        server=ServerConfig(),
        scheduler=SchedulerConfig(),
        auth=AuthConfig(tokens={
            "kal-le": AuthTokenEntry(
                token=DUMMY_KALLE_PEER_TOKEN,
                allowed_clients=["kal-le"],
            ),
        }),
        state=StateConfig(),
        canonical=CanonicalConfig(owner=True),
        peers={},
    )
    state = TransportState.create(tmp_path / "transport_state.json")
    salem_app = build_app(transport_cfg, state)
    register_vault_path(salem_app, vault_root)
    register_instance_identity(salem_app, name="S.A.L.E.M.")
    salem_client = await aiohttp_client(salem_app)

    # Patch the client's _peer_request to dispatch through the
    # in-process aiohttp test client instead of httpx.
    import alfred.transport.client as client_mod

    async def _dispatch_via_test_client(
        *,
        base_url: str,
        token: str,
        method: str,
        path: str,
        self_name: str,
        correlation_id: str,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Alfred-Client": self_name,
            "X-Correlation-Id": correlation_id,
        }
        resp = await salem_client.request(
            method, path, json=json_body, headers=headers,
        )
        return await resp.json()

    monkeypatch.setattr(client_mod, "_peer_request", _dispatch_via_test_client)

    # Build KAL-LE's pusher config + transport config.
    data_dir = _make_data_dir(tmp_path)
    push_config = BriefDigestPushConfig(
        enabled=True, self_name="kal-le", target_peer="salem",
        repo_paths=[], data_dir=str(data_dir),
    )
    kalle_transport_cfg = TransportConfig(
        peers={
            "salem": PeerEntry(
                base_url="http://127.0.0.1:8891",  # ignored by the patched dispatcher
                token=DUMMY_KALLE_PEER_TOKEN,
            ),
        },
    )

    # Fire once — should produce a 202 + materialise a vault record.
    result = await fire_once(
        push_config, kalle_transport_cfg, today=date(2026, 4, 23),
    )
    assert result["ok"] is True
    response = result["response"]
    assert response["status"] == "accepted"
    assert response["path"] == "run/Peer Digest kal-le 2026-04-23.md"

    # Verify the receiver wrote the file Salem's brief renderer expects.
    written = vault_root / "run" / "Peer Digest kal-le 2026-04-23.md"
    assert written.exists()
    text = written.read_text(encoding="utf-8")
    assert "**Yesterday:**" in text
    assert "**Posture:**" in text
    assert "type: run" in text
    assert "source: peer" in text
    assert "peer: kal-le" in text
