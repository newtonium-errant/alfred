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


def _install_fake_aiohttp(monkeypatch, fail_hosts=()):
    """Patch aiohttp.web AppRunner/TCPSite to record binds without sockets.

    ``fail_hosts`` — addresses whose ``start()`` raises ``OSError`` (errno 99,
    EADDRNOTAVAIL) to simulate a non-assignable bind (e.g. the WireGuard
    overlay IP when wg0 is down). Returns ``(created, started, runners)`` —
    ``runners`` lets a test assert ``cleanup()`` ran on the fatal path.
    """
    created: list[tuple[str, int]] = []
    started: list[str] = []
    runners: list[_FakeRunner] = []
    fail = set(fail_hosts)

    orig_runner_init = _FakeRunner.__init__

    def _recording_init(self, app) -> None:
        orig_runner_init(self, app)
        runners.append(self)

    class _FakeSite:
        def __init__(self, runner, host, port) -> None:
            self.runner = runner
            self.host = host
            self.port = port
            created.append((host, port))

        async def start(self) -> None:
            if self.host in fail:
                raise OSError(99, "Cannot assign requested address")
            started.append(self.host)

    monkeypatch.setattr(_FakeRunner, "__init__", _recording_init)
    monkeypatch.setattr("aiohttp.web.AppRunner", _FakeRunner)
    monkeypatch.setattr("aiohttp.web.TCPSite", _FakeSite)
    return created, started, runners


async def test_run_server_string_host_single_site(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alfred.transport.server import run_server

    created, started, _runners = _install_fake_aiohttp(monkeypatch)
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

    created, started, _runners = _install_fake_aiohttp(monkeypatch)
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
    assert bound[0]["failed"] == 0
    assert bound[0]["port"] == 8894


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


# --- A: failure-isolated bind loop (the live-PHI robustness fix) -------------


async def test_run_server_partial_bind_failure_keeps_loopback_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-assignable overlay address (wg0 down) must NOT abort the transport
    — loopback stays up, the failure is warned, run_server does NOT propagate.

    Would have FAILED against 58009e8: the un-guarded ``await site.start()``
    raised OSError out of the loop → whole transport dead.
    """
    from alfred.transport.server import run_server

    created, started, _runners = _install_fake_aiohttp(
        monkeypatch, fail_hosts={"10.99.0.1"},
    )
    cfg = TransportConfig(
        server=ServerConfig(host=["127.0.0.1", "10.99.0.1"], port=8894),
    )
    ev = asyncio.Event()
    ev.set()
    with structlog.testing.capture_logs() as cap:
        await run_server(object(), cfg, shutdown_event=ev)  # must NOT raise

    assert started == ["127.0.0.1"]  # loopback up; overlay failed
    failed = [c for c in cap if c.get("event") == "transport.server.bind_failed"]
    assert [c["host"] for c in failed] == ["10.99.0.1"]
    bound = [c for c in cap if c.get("event") == "transport.server.bound"]
    assert len(bound) == 1
    assert bound[0]["hosts"] == ["127.0.0.1"]
    assert bound[0]["failed"] == 1


async def test_run_server_loopback_failure_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Loopback (the co-located lifeline) failing → run_server RAISES even if
    an overlay bound, so the daemon supervisor restarts. Cleanup still runs."""
    from alfred.transport.server import run_server

    _created, started, runners = _install_fake_aiohttp(
        monkeypatch, fail_hosts={"127.0.0.1"},
    )
    cfg = TransportConfig(
        server=ServerConfig(host=["127.0.0.1", "10.99.0.1"], port=8894),
    )
    ev = asyncio.Event()
    ev.set()
    with pytest.raises(RuntimeError):
        await run_server(object(), cfg, shutdown_event=ev)
    # The overlay may have bound, but loopback-failed is fatal regardless.
    assert "127.0.0.1" not in started
    # cleanup() ALWAYS runs on the failure path (no socket leak).
    assert runners and runners[0].cleaned


async def test_run_server_zero_bound_is_fatal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every bind failing → run_server RAISES (nothing to serve)."""
    from alfred.transport.server import run_server

    _created, _started, runners = _install_fake_aiohttp(
        monkeypatch, fail_hosts={"127.0.0.1", "10.99.0.1"},
    )
    cfg = TransportConfig(
        server=ServerConfig(host=["127.0.0.1", "10.99.0.1"], port=8894),
    )
    ev = asyncio.Event()
    ev.set()
    with pytest.raises(RuntimeError):
        await run_server(object(), cfg, shutdown_event=ev)
    assert runners and runners[0].cleaned


async def test_run_server_binds_loopback_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misordered ``[overlay, loopback]`` config still attempts loopback
    first (so a slow/failing overlay can't starve the lifeline)."""
    from alfred.transport.server import run_server

    created, started, _runners = _install_fake_aiohttp(monkeypatch)
    cfg = TransportConfig(
        server=ServerConfig(host=["10.99.0.1", "127.0.0.1"], port=8894),
    )
    ev = asyncio.Event()
    ev.set()
    await run_server(object(), cfg, shutdown_event=ev)
    assert started[0] == "127.0.0.1"  # loopback attempted first despite order


# --- C: wildcard fail-closed guard (never 0.0.0.0, in code not comment) ------


def test_host_list_drops_wildcard_and_garbage() -> None:
    """The single choke point DROPS any all-interfaces / un-resolvable entry
    and NEVER falls back to a wildcard.

    Would have FAILED against 58009e8: host_list() did a bare
    ``normalize_host_list() or [LOOPBACK_HOST]`` — a configured ``0.0.0.0``
    sailed through to an all-interfaces bind on the no-TLS PHI transport.
    """
    # int 0 → str-coerced "0" → getaddrinfo → 0.0.0.0 → wildcard → dropped.
    assert ServerConfig(host=0).host_list() == ["127.0.0.1"]
    # Explicit 0.0.0.0 in a list → dropped; the real address kept.
    kept = ServerConfig(host=["127.0.0.1", "0.0.0.0"]).host_list()
    assert "0.0.0.0" not in kept
    assert kept == ["127.0.0.1"]
    # IPv6 unspecified likewise.
    assert "::" not in ServerConfig(host=["127.0.0.1", "::"]).host_list()
    # All-garbage / all-wildcard → fail-safe to loopback, NEVER a wildcard.
    assert ServerConfig(host=["0.0.0.0", "*", ""]).host_list() == ["127.0.0.1"]
    assert ServerConfig(host="0.0.0.0").host_list() == ["127.0.0.1"]


# --- E: full loopback recognition + IPv6-safe probe URL ----------------------


def test_resolve_local_host_recognises_ipv6_and_localhost_loopback() -> None:
    from alfred.transport.config import resolve_local_host

    assert resolve_local_host(["::1", "10.99.0.1"]) == "::1"
    assert resolve_local_host(["10.99.0.1", "::1"]) == "::1"
    assert resolve_local_host(["localhost", "10.99.0.1"]) == "localhost"
    assert resolve_local_host(["127.0.0.5", "10.99.0.1"]) == "127.0.0.5"


async def test_health_probe_url_ipv6_safe() -> None:
    """An ``::1`` loopback target must yield a bracketed, parseable URL."""
    from alfred.transport.health import _check_port_reachable

    raw = {
        "transport": {
            "server": {"host": ["10.99.0.1", "::1"], "port": 1},
        },
    }
    result = await _check_port_reachable(raw)
    assert result.data["url"] == "http://[::1]:1/health"


# --- fix2.1: the transport CLIENT base URL is IPv6-safe too (shared helper) ---


def test_client_resolve_base_url_brackets_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every co-located push builds its base URL from ALFRED_TRANSPORT_HOST,
    which can be a bare ``::1`` — it MUST bracket, via the same shared helper
    as the health probe (so they can't drift)."""
    from alfred.transport.client import _resolve_base_url

    monkeypatch.setenv("ALFRED_TRANSPORT_HOST", "::1")
    monkeypatch.setenv("ALFRED_TRANSPORT_PORT", "8891")
    assert _resolve_base_url() == "http://[::1]:8891"
    # IPv4 / hostname pass through unbracketed (back-compat).
    monkeypatch.setenv("ALFRED_TRANSPORT_HOST", "127.0.0.1")
    assert _resolve_base_url() == "http://127.0.0.1:8891"


def test_format_host_for_url_shared_helper() -> None:
    from alfred.transport.config import format_host_for_url

    assert format_host_for_url("::1") == "[::1]"
    assert format_host_for_url("fd00::1") == "[fd00::1]"
    assert format_host_for_url("127.0.0.1") == "127.0.0.1"
    assert format_host_for_url("localhost") == "localhost"


# --- fix2.2: supervisor outcome classifier (all four branches) ---------------


def test_classify_transport_task_outcome_all_branches() -> None:
    """The 'a dead transport never looks idle' decision — pinned per branch."""
    from alfred.telegram.daemon import _classify_transport_task_outcome as cls

    # Cancelled → clean shutdown path, silent.
    assert cls(cancelled=True, exc=None, shutdown_requested=False) == "silent"
    # Raised → fatal bind path; surface + restart.
    assert cls(
        cancelled=False, exc=RuntimeError("x"), shutdown_requested=False,
    ) == "died_exception"
    # Returned with NO shutdown requested → silent stop while daemon "healthy".
    assert cls(
        cancelled=False, exc=None, shutdown_requested=False,
    ) == "died_returned"
    # Returned BECAUSE shutdown was requested → expected, silent.
    assert cls(
        cancelled=False, exc=None, shutdown_requested=True,
    ) == "silent"


# --- fix2.3: loopback OMITTED from config → loud WARN, not fatal -------------


async def test_run_server_no_loopback_bound_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An overlay-only config (loopback omitted) binds + proceeds (operator's
    choice, NOT fatal) but emits a distinct loud WARN — never silent."""
    from alfred.transport.server import run_server

    _created, started, _runners = _install_fake_aiohttp(monkeypatch)
    cfg = TransportConfig(server=ServerConfig(host=["10.99.0.1"], port=8894))
    ev = asyncio.Event()
    ev.set()
    with structlog.testing.capture_logs() as cap:
        await run_server(object(), cfg, shutdown_event=ev)  # must NOT raise

    assert started == ["10.99.0.1"]  # overlay bound, transport up
    warns = [c for c in cap if c.get("event") == "transport.server.no_loopback_bound"]
    assert len(warns) == 1
    assert warns[0]["bound"] == ["10.99.0.1"]
    # A normal loopback-present config does NOT warn.
    _c2, _s2, _r2 = _install_fake_aiohttp(monkeypatch)
    cfg2 = TransportConfig(
        server=ServerConfig(host=["127.0.0.1", "10.99.0.1"], port=8894),
    )
    ev2 = asyncio.Event()
    ev2.set()
    with structlog.testing.capture_logs() as cap2:
        await run_server(object(), cfg2, shutdown_event=ev2)
    assert not [
        c for c in cap2 if c.get("event") == "transport.server.no_loopback_bound"
    ]
