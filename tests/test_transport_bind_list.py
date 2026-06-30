"""Tests for the Stage 3.5 transport multi-bind host list.

Pins:
  * ``ServerConfig.host_list`` / ``host_display`` normalizers (string vs
    list, de-dup, empty-fails-safe-to-loopback);
  * ``resolve_local_host`` — the loopback-preferred resolver SHARED by
    the health probe + the orchestrator's ALFRED_TRANSPORT_HOST injection
    (string passthrough, list prefers 127.0.0.1, first-when-no-loopback);
  * the config load path passing a YAML list value for ``host`` through
    ``_build`` intact;
  * ``run_server`` creating exactly one ``TCPSite`` per host with its
    per-host ``transport.server.listening`` log emission (driven through
    the real bind loop, sockets faked) — string → one site, list → N;
  * the health probe resolving to a localhost URL when host is a list;
  * the orchestrator injecting a clean loopback string (never the list
    literal ``"['127.0.0.1', ...]"``).

Unconditional — no ``importorskip``; aiohttp + structlog are transport
deps and these pin a production-breaking malformed-URL bug for the list
case.
"""

from __future__ import annotations

import asyncio
import os

import pytest
import structlog

from alfred.transport.config import (
    LOOPBACK_HOST,
    ServerConfig,
    TransportConfig,
    load_from_unified,
    normalize_host_list,
    resolve_local_host,
)

# Obviously-fake credential-shaped fixture (per builder.md test-fixture
# rule) — long enough to clear the token-length floor, no ${} placeholder
# so the orchestrator resolves it to itself.
DUMMY_BIND_TEST_TOKEN = "DUMMY_TRANSPORT_BIND_LIST_TEST_TOKEN_PLACEHOLDER_0123456789"


# --- ServerConfig.host_list / host_display ---------------------------------


def test_string_host_yields_single_bind() -> None:
    """A string host normalizes to exactly one bind (back-compat)."""
    cfg = ServerConfig(host="127.0.0.1")
    assert cfg.host_list() == ["127.0.0.1"]
    assert cfg.host_display() == "127.0.0.1"


def test_list_host_yields_each_bind() -> None:
    """A list host binds every address, order-preserved."""
    cfg = ServerConfig(host=["127.0.0.1", "10.99.0.1"])
    assert cfg.host_list() == ["127.0.0.1", "10.99.0.1"]
    assert cfg.host_display() == "127.0.0.1, 10.99.0.1"


def test_list_host_dedups_preserving_order() -> None:
    cfg = ServerConfig(host=["10.99.0.1", "127.0.0.1", "10.99.0.1"])
    assert cfg.host_list() == ["10.99.0.1", "127.0.0.1"]


def test_empty_host_fails_safe_to_loopback() -> None:
    """An empty/garbage host never binds nothing — falls back to loopback."""
    assert ServerConfig(host="").host_list() == [LOOPBACK_HOST]
    assert ServerConfig(host=[]).host_list() == [LOOPBACK_HOST]
    assert ServerConfig(host=["", "  "]).host_list() == [LOOPBACK_HOST]


# --- normalize_host_list ----------------------------------------------------


def test_normalize_host_list_forms() -> None:
    assert normalize_host_list("127.0.0.1") == ["127.0.0.1"]
    assert normalize_host_list(None) == []
    assert normalize_host_list("") == []
    assert normalize_host_list(["a", "b", "a"]) == ["a", "b"]
    assert normalize_host_list(("a", " b ")) == ["a", "b"]


# --- resolve_local_host (health probe + orchestrator injection) -------------


def test_resolve_local_host_string_passthrough() -> None:
    """A single string passes through verbatim (incl. non-loopback)."""
    assert resolve_local_host("127.0.0.1") == "127.0.0.1"
    assert resolve_local_host("192.168.1.1") == "192.168.1.1"


def test_resolve_local_host_prefers_loopback_in_list() -> None:
    assert resolve_local_host(["10.99.0.1", "127.0.0.1"]) == "127.0.0.1"
    assert resolve_local_host(["127.0.0.1", "10.99.0.1"]) == "127.0.0.1"


def test_resolve_local_host_first_when_no_loopback() -> None:
    assert resolve_local_host(["10.99.0.1", "10.99.0.2"]) == "10.99.0.1"


def test_resolve_local_host_empty_uses_default() -> None:
    assert resolve_local_host("", default="") == ""
    assert resolve_local_host(None) == LOOPBACK_HOST
    assert resolve_local_host([]) == LOOPBACK_HOST


# --- config load path passes a list through _build intact -------------------


def test_load_from_unified_string_host() -> None:
    cfg = load_from_unified(
        {"transport": {"server": {"host": "127.0.0.1", "port": 8894}}}
    )
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.host_list() == ["127.0.0.1"]
    assert cfg.server.port == 8894


def test_load_from_unified_list_host_passes_through() -> None:
    """A YAML list value for host survives _build's key-filter intact."""
    cfg = load_from_unified(
        {
            "transport": {
                "server": {"host": ["127.0.0.1", "10.99.0.1"], "port": 8894},
            },
        }
    )
    assert cfg.server.host == ["127.0.0.1", "10.99.0.1"]
    assert cfg.server.host_list() == ["127.0.0.1", "10.99.0.1"]
    assert cfg.server.port == 8894


# --- run_server binds one TCPSite per host + logs each ----------------------


class _FakeRunner:
    def __init__(self, app) -> None:
        self.app = app
        self.cleaned = False

    async def setup(self) -> None:
        pass

    async def cleanup(self) -> None:
        self.cleaned = True


def _install_fake_aiohttp(monkeypatch):
    """Patch aiohttp.web AppRunner/TCPSite to record binds without sockets."""
    created: list[tuple[str, int]] = []
    started: list[str] = []

    class _FakeSite:
        def __init__(self, runner, host, port) -> None:
            self.runner = runner
            self.host = host
            self.port = port
            created.append((host, port))

        async def start(self) -> None:
            started.append(self.host)

    monkeypatch.setattr("aiohttp.web.AppRunner", _FakeRunner)
    monkeypatch.setattr("aiohttp.web.TCPSite", _FakeSite)
    return created, started


async def test_run_server_string_host_single_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alfred.transport.server import run_server

    created, started = _install_fake_aiohttp(monkeypatch)
    cfg = TransportConfig(server=ServerConfig(host="127.0.0.1", port=8891))
    ev = asyncio.Event()
    ev.set()
    with structlog.testing.capture_logs() as cap:
        await run_server(object(), cfg, shutdown_event=ev)

    # Exactly one site for a string host (unchanged single-bind path).
    assert created == [("127.0.0.1", 8891)]
    assert started == ["127.0.0.1"]
    listening = [c for c in cap if c.get("event") == "transport.server.listening"]
    assert [c["host"] for c in listening] == ["127.0.0.1"]
    assert listening[0]["port"] == 8891
    bound = [c for c in cap if c.get("event") == "transport.server.bound"]
    assert len(bound) == 1
    assert bound[0]["hosts"] == ["127.0.0.1"]
    assert bound[0]["host_count"] == 1
    assert bound[0]["port"] == 8891


async def test_run_server_list_host_binds_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alfred.transport.server import run_server

    created, started = _install_fake_aiohttp(monkeypatch)
    cfg = TransportConfig(
        server=ServerConfig(host=["127.0.0.1", "10.99.0.1"], port=8894),
    )
    ev = asyncio.Event()
    ev.set()
    with structlog.testing.capture_logs() as cap:
        await run_server(object(), cfg, shutdown_event=ev)

    # Two sites, both hosts, sharing the one port.
    assert created == [("127.0.0.1", 8894), ("10.99.0.1", 8894)]
    assert started == ["127.0.0.1", "10.99.0.1"]
    listening = [c for c in cap if c.get("event") == "transport.server.listening"]
    assert [c["host"] for c in listening] == ["127.0.0.1", "10.99.0.1"]
    assert all(c["port"] == 8894 for c in listening)
    bound = [c for c in cap if c.get("event") == "transport.server.bound"]
    assert len(bound) == 1
    assert bound[0]["hosts"] == ["127.0.0.1", "10.99.0.1"]
    assert bound[0]["host_count"] == 2


# --- health probe resolves to localhost when host is a list -----------------


async def test_health_probe_targets_localhost_for_list_host() -> None:
    """`_check_port_reachable` must hit loopback, never the raw list literal."""
    from alfred.transport.health import _check_port_reachable

    raw = {
        "transport": {
            # Port 1 is unused → ConnectError → WARN, but the url is built
            # (and recorded in data) before the connect attempt.
            "server": {"host": ["10.99.0.1", "127.0.0.1"], "port": 1},
        },
    }
    result = await _check_port_reachable(raw)
    url = result.data["url"]
    assert url == "http://127.0.0.1:1/health"
    assert "10.99.0.1" not in url
    assert "[" not in url  # never the str() of a list


# --- orchestrator ALFRED_TRANSPORT_HOST injection ---------------------------


def test_inject_transport_host_list_resolves_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list host injects a clean loopback string, not the list literal."""
    from alfred.orchestrator import _inject_transport_env_vars

    monkeypatch.delenv("ALFRED_TRANSPORT_HOST", raising=False)
    monkeypatch.delenv("ALFRED_TRANSPORT_PORT", raising=False)
    monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)

    raw = {
        "transport": {
            "server": {"host": ["10.99.0.1", "127.0.0.1"], "port": 8894},
            "auth": {"tokens": {"local": {"token": DUMMY_BIND_TEST_TOKEN}}},
        },
    }
    _inject_transport_env_vars(raw)
    assert os.environ["ALFRED_TRANSPORT_HOST"] == "127.0.0.1"
    assert os.environ["ALFRED_TRANSPORT_PORT"] == "8894"


def test_inject_transport_host_string_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A string host injects verbatim (back-compat)."""
    from alfred.orchestrator import _inject_transport_env_vars

    monkeypatch.delenv("ALFRED_TRANSPORT_HOST", raising=False)
    monkeypatch.delenv("ALFRED_TRANSPORT_PORT", raising=False)
    monkeypatch.delenv("ALFRED_TRANSPORT_TOKEN", raising=False)

    raw = {
        "transport": {
            "server": {"host": "192.168.1.1", "port": 9999},
            "auth": {"tokens": {"local": {"token": DUMMY_BIND_TEST_TOKEN}}},
        },
    }
    _inject_transport_env_vars(raw)
    assert os.environ["ALFRED_TRANSPORT_HOST"] == "192.168.1.1"
    assert os.environ["ALFRED_TRANSPORT_PORT"] == "9999"
