"""Non-gating boot probe for the best-effort kernel egress belt (#42).

STAY-C ships a hardened ``systemd --user`` unit whose ``IPAddressDeny=any``
+ ``IPAddressAllow=localhost`` directives are a BEST-EFFORT egress belt. Per
GROUND #8 that belt can silently no-op on an unprivileged user manager / a
WSL2 kernel lacking cgroup-v2 BPF, so it carries ZERO load-bearing weight —
the VERIFIED, load-bearing egress control is the Python layer (the four-barrier
load gate in :mod:`alfred.sovereign.boundary` → ``sys.exit(79)`` before any
fork, plus the always-on :class:`~alfred.sovereign.http_guard.SovereignHttpGuard`
armed in the scribe child).

This module PROBES the belt at scribe boot and LOGS what it found — it never
trusts it, never gates serving, never brings the daemon down. Two independent
checks, both observability-only:

  1. DENY side — connect (SYN only, NO payload) to a routable canary. An
     enforced eBPF egress hook rejects the SYN synchronously with EPERM, so
     not one byte leaves the box → ``enforced``. If the connection SUCCEEDS
     (egress open) or the attempt times out / errors for any other reason we
     cannot PROVE enforcement → ``unverified``, and the WARNING names the
     Python guard + barriers a-e as the SOLE verified egress control. We fail
     OPEN on the verdict (``unverified`` + boot), NEVER closed — an air-gapped
     / route-less clinic box legitimately yields a timeout and MUST still come
     up (rejecting C's fail-closed-on-inconclusive, which would brick the
     safest deployment).

  2. LOOPBACK-POSITIVE side (graft from Approach C) — separately connect to the
     Ollama loopback (``127.0.0.1:11434``). If an over-broad ``IPAddressAllow``
     silently severed loopback, that connect FAILS and we WARN
     ``scribe.egress_firewall.loopback_severed`` so a real-need-i regression
     fails LOUD instead of degrading silently.

Contract (asserted by ``tests/test_egress_probe.py``): the probe NEVER raises,
NEVER calls ``sys.exit``, NEVER returns a gating value, NEVER prints "no egress
possible", NEVER conflates unavailability with safety. It always closes its
sockets. The single off-box canary SYN it fires on the unverified path is
documented + consented via ``scribe.egress_probe.enabled`` (default true;
set false to suppress the SYN entirely) — note EPERM is synchronous, so when
the firewall IS enforced NO packet ever leaves.
"""

from __future__ import annotations

import socket

import structlog

_log = structlog.get_logger(__name__)

#: Named on the ``unverified`` path so the operator always knows what IS
#: verified when the kernel belt could not be proven.
_UNVERIFIED_DETAIL = (
    "kernel IPAddressDeny not proven on this manager/kernel — the Python "
    "SovereignHttpGuard + barriers a-e are the SOLE verified egress control"
)


def _split_hostport(hostport: str, default_port: int) -> tuple[str, int]:
    """Split ``"host:port"`` into ``(host, port)``.

    Tolerant on purpose (the probe never raises): a missing/garbage port
    falls back to ``default_port``, and a bare host (no colon) is returned
    as-is. IPv4 literals only — canary/loopback default to literal IPs.
    """
    host, sep, port_s = hostport.rpartition(":")
    if not sep:
        # No colon at all → the whole string is the host.
        return hostport, default_port
    try:
        return host, int(port_s)
    except ValueError:
        return host, default_port


def _connect_probe(host: str, port: int, timeout: float) -> None:
    """Open a TCP socket, connect, close. Sends NO payload; raises on failure.

    The single testable seam: tests monkeypatch this to simulate EPERM
    (enforced), a successful connect (egress open), or a timeout/OSError
    (unverified) — independently for the canary host vs the loopback host.
    Always closes the socket in ``finally`` so a probe never leaks an fd.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout)
        sock.connect((host, port))
    finally:
        sock.close()


def probe_kernel_egress_firewall(
    canary: str = "1.1.1.1:443",
    loopback: str = "127.0.0.1:11434",
    timeout: float = 1.0,
    *,
    logger=None,
) -> str:
    """Probe (never trust, never gate) the best-effort kernel egress belt.

    Returns ``"enforced"`` when the canary SYN is rejected with EPERM (the
    kernel filter is live), else ``"unverified"``. Emits exactly one of
    ``scribe.egress_firewall.enforced`` / ``.unverified`` for the deny side and,
    for the loopback side, ``.loopback_ok`` on success or ``.loopback_severed``
    on failure (intentionally-left-blank: an idle probe is distinguishable from
    a broken one on BOTH sides). NEVER raises / NEVER gates.
    """
    log = logger if logger is not None else _log

    # ---- DENY side: prove (or fail to prove) IPAddressDeny=any ------------
    verdict = "unverified"
    try:
        host, port = _split_hostport(canary, 443)
        try:
            _connect_probe(host, port, timeout)
            # Reached only if the connection SUCCEEDED → egress is open. The
            # SYN did leave the box (consented via egress_probe.enabled).
            verdict = "unverified"
            log.warning(
                "scribe.egress_firewall.unverified",
                canary=canary,
                reason="connect_succeeded",
                detail=_UNVERIFIED_DETAIL,
            )
        except PermissionError:
            # EPERM at the syscall = the kernel egress filter rejected the SYN
            # synchronously → NO packet left the box. The belt is live here.
            verdict = "enforced"
            log.info(
                "scribe.egress_firewall.enforced",
                canary=canary,
                detail=(
                    "kernel IPAddressDeny rejected the canary SYN (EPERM) — "
                    "best-effort egress belt is live on this manager/kernel"
                ),
            )
        except OSError as exc:
            # Timeout or any other socket error → cannot PROVE enforcement.
            # Fail OPEN on the verdict (never brick): 'unverified', not a gate.
            # A route-less / air-gapped box legitimately lands here and boots.
            verdict = "unverified"
            log.warning(
                "scribe.egress_firewall.unverified",
                canary=canary,
                reason=type(exc).__name__,
                detail=_UNVERIFIED_DETAIL,
            )
    except Exception as exc:  # noqa: BLE001 — probe is observability-only, NEVER raises
        verdict = "unverified"
        log.warning(
            "scribe.egress_firewall.unverified",
            canary=canary,
            reason=f"probe_error:{type(exc).__name__}",
            detail=_UNVERIFIED_DETAIL,
        )

    # ---- LOOPBACK-POSITIVE side (graft from Approach C) -------------------
    # An over-broad IPAddressAllow that silently severed loopback would break
    # real-need i (Ollama) with no other signal — so probe it and WARN LOUD.
    try:
        lhost, lport = _split_hostport(loopback, 11434)
        try:
            _connect_probe(lhost, lport, timeout)
            log.info(
                "scribe.egress_firewall.loopback_ok",
                loopback=loopback,
                detail=(
                    "loopback reachable — IPAddressAllow did not over-block "
                    "Ollama/local services (real-need i intact)"
                ),
            )
        except OSError as exc:
            log.warning(
                "scribe.egress_firewall.loopback_severed",
                loopback=loopback,
                reason=type(exc).__name__,
                detail=(
                    "IPAddressAllow over-block severed Ollama loopback "
                    "(real-need i) — local model unreachable"
                ),
            )
    except Exception as exc:  # noqa: BLE001 — probe is observability-only, NEVER raises
        log.warning(
            "scribe.egress_firewall.loopback_severed",
            loopback=loopback,
            reason=f"probe_error:{type(exc).__name__}",
            detail="loopback probe failed unexpectedly",
        )

    return verdict
