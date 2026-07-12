"""Scribe daemon → egress-probe wiring (#42, step c.2).

The probe is best-effort observability: ``startup()`` fires it after the http
guard is armed and before the ``scribe.daemon.up`` log, gated on
``scribe.egress_probe.enabled`` (default true), and swallows any probe
exception (never gates boot). These tests DRIVE that production wiring and PIN
its two new log emissions (``probe_disabled`` / ``probe_skipped``) so a future
refactor that drops them goes RED rather than silently degrading observability
(discipline: log-emission tests must drive the production code path).
"""

from __future__ import annotations

import pytest
import structlog

import alfred.sovereign.egress_probe as egress_probe_mod
from alfred.scribe.daemon import startup
from alfred.sovereign import CLOUD_KEY_ENV_VARS, uninstall_sovereign_http_guard


def _raw(*, egress_probe=None):
    """Minimal sovereign+synthetic config with the fake STT backend (no extra
    needed). Optionally inject a scribe.egress_probe subsection."""
    scribe = {
        "mode": "synthetic",
        "stt": {"provider": "fake"},
        "llm": {"base_url": "http://127.0.0.1:11434"},
    }
    if egress_probe is not None:
        scribe["egress_probe"] = egress_probe
    return {
        "instance": {"name": "STAY-C"},
        "sovereign": {"enabled": True},
        "scribe": scribe,
        "logging": {"dir": "./data"},
    }


@pytest.fixture(autouse=True)
def _guard_cleanup():
    # startup() installs the process-global http guard — uninstall after each
    # test so it never leaks into another test's httpx calls.
    yield
    uninstall_sovereign_http_guard()


@pytest.fixture(autouse=True)
def _scrub_cloud_env(monkeypatch):
    for key in CLOUD_KEY_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def test_startup_fires_egress_probe_when_enabled(monkeypatch):
    """Default (no egress_probe block) → probe fires once with the default
    canary/loopback and the daemon's logger."""
    calls = []

    def fake_probe(canary="1.1.1.1:443", loopback="127.0.0.1:11434", timeout=1.0, *, logger=None):
        calls.append({"canary": canary, "loopback": loopback, "logger": logger})
        return "unverified"

    monkeypatch.setattr(egress_probe_mod, "probe_kernel_egress_firewall", fake_probe)
    startup(_raw(), env={})

    assert len(calls) == 1
    assert calls[0]["canary"] == "1.1.1.1:443"
    assert calls[0]["loopback"] == "127.0.0.1:11434"
    assert calls[0]["logger"] is not None, "daemon must pass its own logger to the probe"


def test_startup_forwards_custom_canary_and_loopback(monkeypatch):
    """Operator-configured canary/loopback are forwarded to the probe."""
    calls = []

    def fake_probe(canary="1.1.1.1:443", loopback="127.0.0.1:11434", timeout=1.0, *, logger=None):
        calls.append((canary, loopback))
        return "enforced"

    monkeypatch.setattr(egress_probe_mod, "probe_kernel_egress_firewall", fake_probe)
    startup(_raw(egress_probe={"enabled": True, "canary": "9.9.9.9:53", "loopback": "127.0.0.1:9999"}), env={})

    assert calls == [("9.9.9.9:53", "127.0.0.1:9999")]


def test_startup_skips_probe_when_disabled(monkeypatch):
    """enabled:false → probe NOT called + one scribe.egress_firewall.probe_disabled
    INFO (no off-box SYN fired)."""
    calls = []
    monkeypatch.setattr(
        egress_probe_mod, "probe_kernel_egress_firewall",
        lambda *a, **k: calls.append(1),
    )

    with structlog.testing.capture_logs() as caps:
        startup(_raw(egress_probe={"enabled": False}), env={})

    assert calls == [], "disabled probe must not run (no off-box SYN)"
    disabled = [c for c in caps if c.get("event") == "scribe.egress_firewall.probe_disabled"]
    assert len(disabled) == 1
    assert "NO off-box canary SYN" in disabled[0]["detail"]


def test_startup_swallows_probe_exception_and_still_boots(monkeypatch):
    """A probe that raises is swallowed → boot COMPLETES (scribe.daemon.up
    emitted) + one scribe.egress_firewall.probe_skipped WARNING. The probe is
    observability-only; it must NEVER gate boot."""
    def boom(*a, **k):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(egress_probe_mod, "probe_kernel_egress_firewall", boom)

    with structlog.testing.capture_logs() as caps:
        config = startup(_raw(), env={})

    assert config.mode == "synthetic"
    up = [c for c in caps if c.get("event") == "scribe.daemon.up"]
    assert len(up) == 1, "boot must complete despite the probe raising"
    skipped = [c for c in caps if c.get("event") == "scribe.egress_firewall.probe_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["log_level"] == "warning"
