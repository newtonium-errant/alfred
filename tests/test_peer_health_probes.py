"""Tests for the c9 per-peer health probes + `alfred check --peer` flag."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web

from alfred.transport.health import (
    _check_peer_queue_depth,
    _check_peer_reachable,
    _infer_self_name,
    _run_peer_probes,
    health_check,
)
from alfred.health.types import Status


DUMMY_PEER_TOKEN = "DUMMY_PEER_TEST_TOKEN_PLACEHOLDER_NOT_REAL_0123456789"


# ---------------------------------------------------------------------------
# Peer stub server — mimics what KAL-LE would expose
# ---------------------------------------------------------------------------


@pytest.fixture
async def peer_server(aiohttp_server):  # type: ignore[no-untyped-def]
    """Build a peer-shaped stub with /health + /peer/handshake."""

    async def _health(request: web.Request) -> web.Response:
        return web.json_response({
            "status": "ok",
            "telegram_connected": True,
            "queue_depth": 0,
            "dead_letter_depth": 0,
        })

    async def _handshake(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {DUMMY_PEER_TOKEN}":
            return web.json_response({"reason": "invalid_token"}, status=401)
        return web.json_response({
            "instance": "KAL-LE",
            "alias": "Kali",
            "protocol_version": 1,
            "capabilities": ["peer_message", "bash_exec"],
            "peers": [],
            "correlation_id": request.headers.get("X-Correlation-Id", ""),
        })

    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_post("/peer/handshake", _handshake)
    server = await aiohttp_server(app)
    return f"http://{server.host}:{server.port}"


@pytest.fixture
async def outdated_peer_server(aiohttp_server):  # type: ignore[no-untyped-def]
    """Peer that returns a protocol version other than 1 → WARN."""

    async def _health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok", "queue_depth": 0, "dead_letter_depth": 0})

    async def _handshake(request: web.Request) -> web.Response:
        return web.json_response({
            "instance": "KAL-LE",
            "protocol_version": 2,  # skew!
            "capabilities": [],
            "peers": [],
        })

    app = web.Application()
    app.router.add_get("/health", _health)
    app.router.add_post("/peer/handshake", _handshake)
    server = await aiohttp_server(app)
    return f"http://{server.host}:{server.port}"


# ---------------------------------------------------------------------------
# peer-reachable probe
# ---------------------------------------------------------------------------


async def test_peer_reachable_ok(peer_server):  # type: ignore[no-untyped-def]
    result = await _check_peer_reachable(
        raw={},
        peer_name="kal-le",
        peer_entry={"base_url": peer_server, "token": DUMMY_PEER_TOKEN},
    )
    assert result.status == Status.OK
    assert result.name == "peer-reachable:kal-le"


async def test_peer_reachable_warns_on_unreachable():
    """Closed port (peer daemon down) → WARN, not FAIL."""
    result = await _check_peer_reachable(
        raw={},
        peer_name="kal-le",
        peer_entry={"base_url": "http://127.0.0.1:65534", "token": "x"},
    )
    assert result.status == Status.WARN
    assert "kal-le" in result.detail


async def test_peer_reachable_fails_on_missing_base_url():
    result = await _check_peer_reachable(
        raw={},
        peer_name="kal-le",
        peer_entry={"base_url": "", "token": "x"},
    )
    assert result.status == Status.FAIL


# ---------------------------------------------------------------------------
# peer-handshake probe
# ---------------------------------------------------------------------------


async def test_peer_handshake_ok(peer_server):  # type: ignore[no-untyped-def]
    from alfred.transport.health import _check_peer_handshake

    result = await _check_peer_handshake(
        raw={"telegram": {"instance": {"name": "Salem"}}},
        peer_name="kal-le",
        peer_entry={"base_url": peer_server, "token": DUMMY_PEER_TOKEN},
    )
    assert result.status == Status.OK
    assert "bash_exec" in result.data["capabilities"]


async def test_peer_handshake_warns_on_version_skew(outdated_peer_server):  # type: ignore[no-untyped-def]
    from alfred.transport.health import _check_peer_handshake

    result = await _check_peer_handshake(
        raw={},
        peer_name="kal-le",
        peer_entry={"base_url": outdated_peer_server, "token": DUMMY_PEER_TOKEN},
    )
    assert result.status == Status.WARN
    assert "skew" in result.detail


async def test_peer_handshake_fails_on_missing_token():
    from alfred.transport.health import _check_peer_handshake

    result = await _check_peer_handshake(
        raw={},
        peer_name="kal-le",
        peer_entry={"base_url": "http://127.0.0.1:1", "token": ""},
    )
    assert result.status == Status.FAIL


async def test_peer_handshake_warns_on_unreachable():
    from alfred.transport.health import _check_peer_handshake

    result = await _check_peer_handshake(
        raw={},
        peer_name="kal-le",
        peer_entry={"base_url": "http://127.0.0.1:65534", "token": DUMMY_PEER_TOKEN},
    )
    assert result.status == Status.WARN


# ---------------------------------------------------------------------------
# queue-depth probe
# ---------------------------------------------------------------------------


def test_peer_queue_depth_ok_when_state_missing(tmp_path: Path):
    raw = {
        "transport": {
            "state": {"path": str(tmp_path / "nonexistent.json")},
        },
    }
    result = _check_peer_queue_depth(raw, "kal-le")
    assert result.status == Status.OK
    assert result.data["depth"] == 0


def test_peer_queue_depth_counts_only_matching_peer(tmp_path: Path):
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({
        "pending_queue": [
            {"id": "a", "peer": "kal-le", "text": "msg-a"},
            {"id": "b", "peer": "stay-c", "text": "msg-b"},
            {"id": "c", "peer": "kal-le", "text": "msg-c"},
            {"id": "d", "peer": None, "text": "msg-d"},
        ],
    }), encoding="utf-8")

    raw = {"transport": {"state": {"path": str(state_path)}}}
    result = _check_peer_queue_depth(raw, "kal-le")
    assert result.data["depth"] == 2
    result_stayc = _check_peer_queue_depth(raw, "stay-c")
    assert result_stayc.data["depth"] == 1


def test_peer_queue_depth_warns_over_threshold(tmp_path: Path):
    state_path = tmp_path / "state.json"
    queue = [{"id": str(i), "peer": "kal-le"} for i in range(101)]
    state_path.write_text(json.dumps({"pending_queue": queue}), encoding="utf-8")

    raw = {"transport": {"state": {"path": str(state_path)}}}
    result = _check_peer_queue_depth(raw, "kal-le")
    assert result.status == Status.WARN


# ---------------------------------------------------------------------------
# _infer_self_name
# ---------------------------------------------------------------------------


def test_infer_self_name_alfred_maps_to_salem():
    raw = {"telegram": {"instance": {"name": "Alfred"}}}
    assert _infer_self_name(raw) == "salem"


def test_infer_self_name_kalle():
    raw = {"telegram": {"instance": {"name": "KAL-LE"}}}
    assert _infer_self_name(raw) == "kal-le"


def test_infer_self_name_salem():
    raw = {"telegram": {"instance": {"name": "S.A.L.E.M."}}}
    assert _infer_self_name(raw) == "salem"


# ---------------------------------------------------------------------------
# _run_peer_probes — filter behaviour
# ---------------------------------------------------------------------------


async def test_peer_probes_no_peers_returns_empty():
    raw = {"transport": {}}
    assert await _run_peer_probes(raw) == []


async def test_peer_probes_filter_to_single_peer(peer_server):  # type: ignore[no-untyped-def]
    raw = {
        "transport": {
            "peers": {
                "kal-le": {"base_url": peer_server, "token": DUMMY_PEER_TOKEN},
                "stay-c": {"base_url": "http://127.0.0.1:65535", "token": "tok"},
            },
        },
    }
    results = await _run_peer_probes(raw, filter_peer="kal-le")
    names = {r.name for r in results}
    assert any("kal-le" in n for n in names)
    assert not any("stay-c" in n for n in names)


# ---------------------------------------------------------------------------
# health_check — overall integration
# ---------------------------------------------------------------------------


async def test_health_check_with_peer_filter(peer_server):  # type: ignore[no-untyped-def]
    raw = {
        "transport": {
            "peers": {
                "kal-le": {"base_url": peer_server, "token": DUMMY_PEER_TOKEN},
            },
        },
        "telegram": {"instance": {"name": "Alfred"}},
    }
    health = await health_check(raw, filter_peer="kal-le")
    # At least peer-reachable + peer-handshake + queue-depth ran.
    assert len(health.results) >= 3
    # No local probes (no config-section / token-configured on the
    # filtered path).
    names = {r.name for r in health.results}
    assert "config-section" not in names
    assert "token-configured" not in names


async def test_health_check_unknown_peer_skips():
    raw = {
        "transport": {
            "peers": {
                "kal-le": {"base_url": "http://127.0.0.1:1", "token": "tok"},
            },
        },
    }
    # Filter to a peer that doesn't exist in config.
    health = await health_check(raw, filter_peer="never-configured")
    assert health.status == Status.SKIP


# ---------------------------------------------------------------------------
# tail subcommand over canonical audit log
# ---------------------------------------------------------------------------


def test_cmd_tail_peer_filter(tmp_path, capsys):
    from alfred.transport.canonical_audit import append_audit
    from alfred.transport import cli as tcli

    audit_path = tmp_path / "canonical_audit.jsonl"
    append_audit(
        audit_path, peer="kal-le", record_type="person",
        name="Andrew", requested=["name"], granted=["name"], denied=[],
        correlation_id="c1",
    )
    append_audit(
        audit_path, peer="stay-c", record_type="person",
        name="Andrew", requested=["name"], granted=["name"], denied=[],
        correlation_id="c2",
    )
    append_audit(
        audit_path, peer="kal-le", record_type="person",
        name="Bob", requested=["name", "email"], granted=["name", "email"],
        denied=[], correlation_id="c3",
    )

    raw = {
        "transport": {
            "canonical": {"audit_log_path": str(audit_path)},
        },
    }
    code = tcli.cmd_tail(raw, peer="kal-le", limit=10, wants_json=False)
    assert code == 0
    captured = capsys.readouterr()
    assert "kal-le" in captured.out
    # stay-c entry filtered out.
    assert "stay-c" not in captured.out


# ---------------------------------------------------------------------------
# _run_peer_probes env substitution (2026-04-21 BIT regression fix)
# ---------------------------------------------------------------------------


async def test_run_peer_probes_substitutes_env_placeholders_in_token(
    peer_server, monkeypatch: pytest.MonkeyPatch,
):  # type: ignore[no-untyped-def]
    """Raw config holds ``${VAR}``; the handshake probe must see the resolved value.

    Regression: before this fix, ``_run_peer_probes`` read
    ``raw["transport"]["peers"][name]["token"]`` directly, so the
    ``Authorization: Bearer ${ALFRED_KALLE_PEER_TOKEN}`` literal would
    hit the peer and surface as a false-negative 401 on the
    ``peer-handshake:*`` probe — even though real-runtime code paths
    that go through ``load_from_unified`` worked fine.
    """
    monkeypatch.setenv("ALFRED_KALLE_PEER_TOKEN", DUMMY_PEER_TOKEN)

    raw = {
        "transport": {
            "peers": {
                "kal-le": {
                    "base_url": peer_server,
                    "token": "${ALFRED_KALLE_PEER_TOKEN}",
                },
            },
        },
        "telegram": {"instance": {"name": "Salem"}},
    }

    results = await _run_peer_probes(raw, filter_peer="kal-le")
    handshake = next(r for r in results if r.name == "peer-handshake:kal-le")
    assert handshake.status == Status.OK, (
        f"handshake should succeed with env-substituted token, "
        f"got {handshake.status}: {handshake.detail}"
    )


async def test_run_peer_probes_fails_when_env_var_unset(
    peer_server, monkeypatch: pytest.MonkeyPatch,
):  # type: ignore[no-untyped-def]
    """Unresolved ``${VAR}`` placeholder → handshake still attempts and auth fails.

    If the env var is missing, ``_substitute_env`` leaves the raw
    ``${VAR}`` text in place (same behaviour as the talker's config
    loader). The peer rejects the literal placeholder as an invalid
    token, producing a FAIL auth-rejected. This documents the
    observable behaviour so an operator sees a real FAIL rather than
    a silent success.
    """
    monkeypatch.delenv("ALFRED_KALLE_PEER_TOKEN", raising=False)

    raw = {
        "transport": {
            "peers": {
                "kal-le": {
                    "base_url": peer_server,
                    "token": "${ALFRED_KALLE_PEER_TOKEN}",
                },
            },
        },
    }

    results = await _run_peer_probes(raw, filter_peer="kal-le")
    handshake = next(r for r in results if r.name == "peer-handshake:kal-le")
    assert handshake.status == Status.FAIL


def test_cmd_tail_without_peer_shows_all(tmp_path, capsys):
    from alfred.transport.canonical_audit import append_audit
    from alfred.transport import cli as tcli

    audit_path = tmp_path / "canonical_audit.jsonl"
    append_audit(
        audit_path, peer="kal-le", record_type="person",
        name="Andrew", requested=["name"], granted=["name"], denied=[],
    )
    append_audit(
        audit_path, peer="stay-c", record_type="person",
        name="Andrew", requested=["name"], granted=["name"], denied=[],
    )

    raw = {"transport": {"canonical": {"audit_log_path": str(audit_path)}}}
    code = tcli.cmd_tail(raw, peer=None, limit=10, wants_json=False)
    assert code == 0
    captured = capsys.readouterr()
    assert "kal-le" in captured.out
    assert "stay-c" in captured.out
