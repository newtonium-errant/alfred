"""Non-gating kernel-egress belt probe (#42).

``probe_kernel_egress_firewall`` PROBES the best-effort systemd IPAddressDeny
belt at scribe boot and LOGS what it found — it NEVER raises, NEVER calls
``sys.exit``, NEVER gates serving. Contract:

  * canary connect raises EPERM (PermissionError) → ``"enforced"`` +
    ``scribe.egress_firewall.enforced`` (INFO).
  * canary connect SUCCEEDS or raises Timeout/OSError → ``"unverified"`` +
    ``scribe.egress_firewall.unverified`` (WARNING); NEVER raises.
  * ANY unexpected error → still ``"unverified"`` (broad-swallow), NEVER raises.
  * loopback (Ollama) connect fails → ``scribe.egress_firewall.loopback_severed``
    (WARNING) so an IPAddressAllow over-block fails LOUD; still NEVER raises.

The single testable seam is ``_connect_probe(host, port, timeout)`` — tests
monkeypatch it to drive the canary host and the loopback host independently.
Log emission is pinned via ``structlog.testing.capture_logs`` (async/threadpool
-safe and the standard for asserting structured events).
"""

from __future__ import annotations

import pytest
from structlog.testing import capture_logs

import alfred.sovereign.egress_probe as egress_probe


def _events(captured: list[dict]) -> list[str]:
    return [c.get("event") for c in captured]


def _one(captured: list[dict], event: str) -> dict:
    matches = [c for c in captured if c.get("event") == event]
    assert len(matches) == 1, f"expected exactly one {event!r}, got {len(matches)}"
    return matches[0]


# ---------------------------------------------------------------------------
# DENY side — enforced
# ---------------------------------------------------------------------------


def test_probe_reports_enforced_on_eperm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Canary EPERM → 'enforced' + one INFO scribe.egress_firewall.enforced;
    loopback still reachable → loopback_ok."""
    def fake_connect(host: str, port: int, timeout: float) -> None:
        if host == "192.0.2.1":
            raise PermissionError(1, "Operation not permitted")
        return None  # loopback (127.0.0.1) succeeds

    monkeypatch.setattr(egress_probe, "_connect_probe", fake_connect)

    with capture_logs() as captured:
        verdict = egress_probe.probe_kernel_egress_firewall()

    assert verdict == "enforced"
    enforced = _one(captured, "scribe.egress_firewall.enforced")
    assert enforced["log_level"] == "info"
    assert enforced["canary"] == "192.0.2.1:443"
    # Deny side proven → the unverified WARNING must NOT fire.
    assert "scribe.egress_firewall.unverified" not in _events(captured)
    # Loopback reachable → loopback_ok, no severed warning.
    assert "scribe.egress_firewall.loopback_ok" in _events(captured)
    assert "scribe.egress_firewall.loopback_severed" not in _events(captured)


# ---------------------------------------------------------------------------
# DENY side — unverified (connect succeeds OR times out OR errors)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "canary_behavior",
    ["connect_succeeds", "timeout", "oserror"],
    ids=["egress_open", "timeout", "oserror"],
)
def test_probe_reports_unverified_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, canary_behavior: str,
) -> None:
    """Egress open / timeout / other OSError → 'unverified' + one WARNING, and
    the probe NEVER raises (reaching the asserts proves no raise / no sys.exit /
    no gate). Each failure mode that drives the unverified path is exercised."""
    def fake_connect(host: str, port: int, timeout: float) -> None:
        if host == "192.0.2.1":
            if canary_behavior == "connect_succeeds":
                return None  # egress is OPEN → belt not enforced
            if canary_behavior == "timeout":
                raise TimeoutError("timed out")
            raise OSError("network unreachable")
        return None  # loopback ok

    monkeypatch.setattr(egress_probe, "_connect_probe", fake_connect)

    with capture_logs() as captured:
        verdict = egress_probe.probe_kernel_egress_firewall()

    assert verdict == "unverified"
    unverified = _one(captured, "scribe.egress_firewall.unverified")
    assert unverified["log_level"] == "warning"
    assert unverified["canary"] == "192.0.2.1:443"
    # The WARNING names the SOLE verified control (Python guard + barriers).
    assert "SOLE verified egress control" in unverified["detail"]
    # 'enforced' must NOT fire on any unverified path.
    assert "scribe.egress_firewall.enforced" not in _events(captured)


def test_probe_never_raises_on_unexpected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-socket error inside the connect helper (e.g. a bug) is broad-
    swallowed → still 'unverified', still NO raise. Guards the 'NEVER raises'
    contract against the unexpected-exception path, not just OSError."""
    def fake_connect(host: str, port: int, timeout: float) -> None:
        raise RuntimeError("unexpected boom")  # neither PermissionError nor OSError

    monkeypatch.setattr(egress_probe, "_connect_probe", fake_connect)

    with capture_logs() as captured:
        verdict = egress_probe.probe_kernel_egress_firewall()

    assert verdict == "unverified"
    unverified = _one(captured, "scribe.egress_firewall.unverified")
    assert unverified["reason"].startswith("probe_error:")


# ---------------------------------------------------------------------------
# LOOPBACK-POSITIVE side — severed warning (graft from Approach C)
# ---------------------------------------------------------------------------


def test_probe_loopback_severed_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback connect fails → one WARNING scribe.egress_firewall.loopback_severed
    (real-need-i over-block fails LOUD) WITHOUT raising. The deny side is
    independent (here: enforced)."""
    def fake_connect(host: str, port: int, timeout: float) -> None:
        if host == "127.0.0.1":
            raise ConnectionRefusedError("connection refused")  # loopback severed
        raise PermissionError(1, "Operation not permitted")  # deny side enforced

    monkeypatch.setattr(egress_probe, "_connect_probe", fake_connect)

    with capture_logs() as captured:
        verdict = egress_probe.probe_kernel_egress_firewall()

    # Loopback failure does NOT change the deny-side verdict.
    assert verdict == "enforced"
    severed = _one(captured, "scribe.egress_firewall.loopback_severed")
    assert severed["log_level"] == "warning"
    assert severed["loopback"] == "127.0.0.1:11434"
    assert "real-need i" in severed["detail"]
    assert "scribe.egress_firewall.loopback_ok" not in _events(captured)


def test_probe_loopback_broad_swallow_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unexpected (non-OSError) failure on the loopback side is broad-
    swallowed to loopback_severed — NEVER raises."""
    def fake_connect(host: str, port: int, timeout: float) -> None:
        if host == "127.0.0.1":
            raise RuntimeError("unexpected loopback boom")
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(egress_probe, "_connect_probe", fake_connect)

    with capture_logs() as captured:
        verdict = egress_probe.probe_kernel_egress_firewall()

    assert verdict == "enforced"
    severed = _one(captured, "scribe.egress_firewall.loopback_severed")
    assert severed["reason"].startswith("probe_error:")


# ---------------------------------------------------------------------------
# host:port parsing tolerance (never raises on garbage)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostport,default,expected",
    [
        ("192.0.2.1:443", 443, ("192.0.2.1", 443)),
        ("127.0.0.1:11434", 11434, ("127.0.0.1", 11434)),
        ("192.0.2.1", 443, ("192.0.2.1", 443)),          # no colon → default port
        ("192.0.2.1:notaport", 443, ("192.0.2.1", 443)),  # garbage port → default
    ],
)
def test_split_hostport_is_tolerant(hostport, default, expected) -> None:
    assert egress_probe._split_hostport(hostport, default) == expected
