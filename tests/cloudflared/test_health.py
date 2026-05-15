"""Tests for ``alfred.cloudflared.health``.

Covers the four-state probe (OK / WARN / FAIL / SKIP) plus the
underlying ``_read_metrics`` parser. The probe is a thin HTTP-and-
parse wrapper; we mock ``httpx.get`` with various response shapes
rather than spinning up a real HTTP server.

Status mapping pinned here (matches the module docstring):

  * OK   — endpoint reachable, ha_connections >= 1
  * WARN — endpoint reachable, ha_connections == 0
  * FAIL — endpoint unreachable, cloudflared.enabled=true
  * SKIP — cloudflared.enabled=false (or block absent)
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from alfred.cloudflared import health as cf_health
from alfred.health.types import Status


# ---------------------------------------------------------------------------
# _read_metrics — parser failure modes
# ---------------------------------------------------------------------------

class TestReadMetrics:
    """Parser unit coverage. All failure modes map to reachable=False."""

    def test_success_with_ha_connections(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HTTP 200 + valid metrics → reachable=True with parsed count."""
        body = (
            "# HELP cloudflared_tunnel_ha_connections HA Connections\n"
            "# TYPE cloudflared_tunnel_ha_connections gauge\n"
            "cloudflared_tunnel_ha_connections 4\n"
        )

        class _Resp:
            status_code = 200
            text = body

        class _Stub:
            HTTPError = Exception

            @staticmethod
            def get(*args, **kwargs):
                return _Resp()

        monkeypatch.setitem(sys.modules, "httpx", _Stub)
        result = cf_health._read_metrics("http://localhost:20241/metrics")
        assert result == {"reachable": True, "ha_connections": 4}

    def test_success_with_zero_connections(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HTTP 200 + ``ha_connections 0`` → reachable=True, count=0.

        The probe still parses successfully; the WARN classification
        happens at the ``_check_tunnel_connections`` layer.
        """
        body = "cloudflared_tunnel_ha_connections 0\n"

        class _Resp:
            status_code = 200
            text = body

        class _Stub:
            HTTPError = Exception

            @staticmethod
            def get(*args, **kwargs):
                return _Resp()

        monkeypatch.setitem(sys.modules, "httpx", _Stub)
        result = cf_health._read_metrics("http://localhost:20241/metrics")
        assert result == {"reachable": True, "ha_connections": 0}

    def test_connection_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Connection refused → reachable=False with class-name error."""
        import httpx as real_httpx

        class _Refusing:
            HTTPError = real_httpx.HTTPError

            @staticmethod
            def get(*args, **kwargs):
                raise real_httpx.ConnectError("connection refused")

        monkeypatch.setitem(sys.modules, "httpx", _Refusing)
        result = cf_health._read_metrics("http://localhost:20241/metrics")
        assert result["reachable"] is False
        assert "ConnectError" in result["error"]

    def test_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Read timeout → reachable=False."""
        import httpx as real_httpx

        class _Slow:
            HTTPError = real_httpx.HTTPError

            @staticmethod
            def get(*args, **kwargs):
                raise real_httpx.ReadTimeout("timed out")

        monkeypatch.setitem(sys.modules, "httpx", _Slow)
        result = cf_health._read_metrics("http://localhost:20241/metrics")
        assert result["reachable"] is False
        assert "ReadTimeout" in result["error"]

    def test_non_200_status_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """HTTP 404 / 500 → reachable=False with status-code error."""

        class _Resp:
            status_code = 500
            text = ""

        class _Stub:
            HTTPError = Exception

            @staticmethod
            def get(*args, **kwargs):
                return _Resp()

        monkeypatch.setitem(sys.modules, "httpx", _Stub)
        result = cf_health._read_metrics("http://localhost:20241/metrics")
        assert result["reachable"] is False
        assert "HTTP 500" in result["error"]

    def test_malformed_body_missing_gauge(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """HTTP 200 but no ha_connections gauge → reachable=False.

        Treat a metrics endpoint missing the gauge as a degraded state
        — we can't confirm tunnel health from a body that doesn't
        contain the signal.
        """
        body = "# HELP build_info Build\nbuild_info 1\n"

        class _Resp:
            status_code = 200
            text = body

        class _Stub:
            HTTPError = Exception

            @staticmethod
            def get(*args, **kwargs):
                return _Resp()

        monkeypatch.setitem(sys.modules, "httpx", _Stub)
        result = cf_health._read_metrics("http://localhost:20241/metrics")
        assert result["reachable"] is False
        assert "gauge" in result["error"]

    def test_labelled_gauge_variant_skipped(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Only the bare gauge (no labels) is matched.

        Future cloudflared versions might add a labelled variant
        (``cloudflared_tunnel_ha_connections{...}``); the parser
        deliberately skips labelled forms so the value is unambiguous.
        Without the bare form, the parser reports "gauge not found".
        """
        body = (
            "cloudflared_tunnel_ha_connections{region=\"ewr\"} 2\n"
            "cloudflared_tunnel_ha_connections{region=\"sfo\"} 2\n"
        )

        class _Resp:
            status_code = 200
            text = body

        class _Stub:
            HTTPError = Exception

            @staticmethod
            def get(*args, **kwargs):
                return _Resp()

        monkeypatch.setitem(sys.modules, "httpx", _Stub)
        result = cf_health._read_metrics("http://localhost:20241/metrics")
        assert result["reachable"] is False
        assert "gauge" in result["error"]


# ---------------------------------------------------------------------------
# _check_tunnel_connections — four-state mapping
# ---------------------------------------------------------------------------

def _install_metrics_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    reachable: bool,
    ha_connections: int = 0,
    error: str = "",
) -> None:
    """Helper: stub ``_read_metrics`` directly so the four-state mapping
    can be exercised without re-mocking httpx."""

    def _fake(*_args, **_kwargs):
        if reachable:
            return {"reachable": True, "ha_connections": ha_connections}
        return {"reachable": False, "error": error}

    monkeypatch.setattr(cf_health, "_read_metrics", _fake)


def test_ok_when_connections_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reachable + ha_connections>=1 → Status.OK with count in detail."""
    _install_metrics_stub(monkeypatch, reachable=True, ha_connections=4)
    result = cf_health._check_tunnel_connections(
        metrics_url="http://localhost:20241/metrics",
        enabled=True,
    )
    assert result.status == Status.OK
    assert "tunnel connections active: 4" in result.detail
    assert result.data["ha_connections"] == 4


def test_warn_when_connections_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reachable but ha_connections==0 → Status.WARN.

    cloudflared is up but not registered to Cloudflare edge — auth or
    network issue. Distinguishable from "binary crashed" (FAIL).
    """
    _install_metrics_stub(monkeypatch, reachable=True, ha_connections=0)
    result = cf_health._check_tunnel_connections(
        metrics_url="http://localhost:20241/metrics",
        enabled=True,
    )
    assert result.status == Status.WARN
    assert "tunnel connections active: 0" in result.detail
    assert "auth or network" in result.detail


def test_fail_when_endpoint_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unreachable + enabled=True → Status.FAIL."""
    _install_metrics_stub(
        monkeypatch,
        reachable=False,
        error="ConnectError: connection refused",
    )
    result = cf_health._check_tunnel_connections(
        metrics_url="http://localhost:20241/metrics",
        enabled=True,
    )
    assert result.status == Status.FAIL
    assert "unreachable" in result.detail
    assert "may have crashed" in result.detail
    assert result.data["error"] == "ConnectError: connection refused"


def test_skip_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """``enabled=False`` → Status.SKIP regardless of reachability."""
    # Stub returns "unreachable" but the SKIP should fire BEFORE the
    # probe runs (enabled=False short-circuits).
    _install_metrics_stub(monkeypatch, reachable=False, error="anything")
    result = cf_health._check_tunnel_connections(
        metrics_url="http://localhost:20241/metrics",
        enabled=False,
    )
    assert result.status == Status.SKIP
    assert "disabled" in result.detail


def test_skip_does_not_call_read_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabled config → ``_read_metrics`` never called.

    Reason: probing the endpoint when the operator opted out is
    wasted work AND could fail-noisily on an unrelated 20241 binding.
    """
    call_count: list[int] = []

    def _should_not_be_called(*args, **kwargs):
        call_count.append(1)
        return {"reachable": False, "error": "should not be called"}

    monkeypatch.setattr(cf_health, "_read_metrics", _should_not_be_called)
    cf_health._check_tunnel_connections(
        metrics_url="http://localhost:20241/metrics",
        enabled=False,
    )
    assert call_count == []


# ---------------------------------------------------------------------------
# health_check — top-level integration
# ---------------------------------------------------------------------------

def test_health_check_absent_section_returns_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No cloudflared section in config → ToolHealth.status=SKIP."""
    raw: dict = {"logging": {"dir": "./data"}}
    th = asyncio.run(cf_health.health_check(raw, mode="quick"))
    assert th.status == Status.SKIP
    assert th.tool == "cloudflared"
    assert "no cloudflared section" in th.detail


def test_health_check_disabled_block_returns_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``enabled: false`` in section → ToolHealth.status=SKIP."""
    raw = {"cloudflared": {"enabled": False}}
    th = asyncio.run(cf_health.health_check(raw, mode="quick"))
    assert th.status == Status.SKIP
    # One probe result with the SKIP reason.
    assert len(th.results) == 1
    assert th.results[0].status == Status.SKIP


def test_health_check_ok_path_uses_loaded_metrics_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe target URL comes from config.metrics_url (custom port honored)."""
    captured_urls: list[str] = []

    def _capture(url, *args, **kwargs):
        captured_urls.append(url)
        return {"reachable": True, "ha_connections": 4}

    monkeypatch.setattr(cf_health, "_read_metrics", _capture)

    raw = {
        "cloudflared": {
            "enabled": True,
            "tunnel_id": "abc",
            "metrics_port": 30241,
        },
    }
    th = asyncio.run(cf_health.health_check(raw, mode="quick"))
    assert th.status == Status.OK
    # The probe consulted the custom port, not the default 20241.
    assert captured_urls == ["http://localhost:30241/metrics"]


def test_health_check_full_mode_uses_longer_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full mode (15s budget) passes a 5s timeout; quick mode passes 2s.

    Pins the mode plumbing — a future caller passing ``mode="full"``
    expects the longer timeout to take effect.
    """
    captured_timeouts: list[float] = []

    def _capture(url, timeout_seconds: float = 2.0, **kwargs):
        captured_timeouts.append(timeout_seconds)
        return {"reachable": True, "ha_connections": 1}

    monkeypatch.setattr(cf_health, "_read_metrics", _capture)

    raw = {"cloudflared": {"enabled": True, "tunnel_id": "abc"}}
    asyncio.run(cf_health.health_check(raw, mode="quick"))
    asyncio.run(cf_health.health_check(raw, mode="full"))
    assert captured_timeouts == [2.0, 5.0]
