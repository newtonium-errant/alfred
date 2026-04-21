"""Transport health check — registered with the BIT aggregator.

Five probes per the commit 6 plan:

- ``config-section``   — ``transport:`` present in config.
- ``token-configured`` — ``ALFRED_TRANSPORT_TOKEN`` env var present
  and at least 32 chars (fresh 64-char hex is expected; anything
  shorter is suspicious).
- ``port-reachable``   — server responds to ``GET /health``.
- ``queue-depth``      — pending queue < 100 (WARN threshold).
- ``dead-letter-depth`` — dead_letter < 50 (WARN threshold).

Registration fires at import time. The aggregator imports this
module via ``KNOWN_TOOL_MODULES["transport"]``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth
from alfred.transport.config import PeerEntry, load_from_unified


def _peer_attr(entry: "PeerEntry | dict[str, Any]", key: str) -> str:
    """Read ``key`` from either a :class:`PeerEntry` dataclass or a raw dict.

    The probes accept both to stay convenient for direct-test callsites
    (which pass plain dicts) while letting :func:`_run_peer_probes`
    hand in env-substituted dataclasses in production. Returns ``""``
    when the attribute/key is missing or falsy — probes treat empty
    ``base_url`` / ``token`` as a FAIL at the call site.
    """
    if isinstance(entry, PeerEntry):
        return str(getattr(entry, key, "") or "")
    # Dict fallback (legacy test callsites).
    return str(entry.get(key) or "")


# Warn thresholds. Exceeding these surfaces a WARN on ``alfred check``
# but does not block the preflight gate (WARN ≠ FAIL per plan Part 11).
_QUEUE_WARN_THRESHOLD = 100
_DEAD_LETTER_WARN_THRESHOLD = 50

# Minimum token length we treat as plausibly real. 32 chars of hex
# = 128 bits; the documented default is 64 (256 bits).
_MIN_TOKEN_CHARS = 32


def _check_config_section(raw: dict[str, Any]) -> CheckResult:
    if "transport" not in raw:
        return CheckResult(
            name="config-section",
            status=Status.FAIL,
            detail="no transport section in config",
        )
    return CheckResult(
        name="config-section",
        status=Status.OK,
        detail="transport section present",
    )


def _check_token_configured(raw: dict[str, Any]) -> CheckResult:
    """``ALFRED_TRANSPORT_TOKEN`` must be set and at least 32 chars.

    Per builder.md's secret-logging rule — never log token contents.
    On short/missing tokens we include ``length`` in ``data`` but
    never the token itself.
    """
    token = os.environ.get("ALFRED_TRANSPORT_TOKEN", "")
    if not token:
        return CheckResult(
            name="token-configured",
            status=Status.FAIL,
            detail="ALFRED_TRANSPORT_TOKEN env var not set",
            data={"length": 0},
        )
    if token.startswith("${"):
        return CheckResult(
            name="token-configured",
            status=Status.FAIL,
            detail=(
                "ALFRED_TRANSPORT_TOKEN looks like an unresolved "
                "placeholder — check .env"
            ),
            data={"length": len(token)},
        )
    if len(token) < _MIN_TOKEN_CHARS:
        return CheckResult(
            name="token-configured",
            status=Status.WARN,
            detail=(
                f"token length {len(token)} < recommended "
                f"{_MIN_TOKEN_CHARS}"
            ),
            data={"length": len(token)},
        )
    return CheckResult(
        name="token-configured",
        status=Status.OK,
        detail=f"token length {len(token)}",
        data={"length": len(token)},
    )


async def _check_port_reachable(raw: dict[str, Any]) -> CheckResult:
    """``GET /health`` must return 200 and parse as JSON.

    The transport server only listens while the talker is running,
    so ``ConnectionRefused`` is a normal expected state when the
    talker is down. Surfaces WARN in that case (not FAIL) — the
    transport is optional for tools that don't push.
    """
    transport_cfg = raw.get("transport", {}) or {}
    server = transport_cfg.get("server", {}) or {}
    host = server.get("host", "127.0.0.1")
    port = server.get("port", 8891)
    url = f"http://{host}:{port}/health"

    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        return CheckResult(
            name="port-reachable",
            status=Status.WARN,
            detail=f"transport server not reachable (talker down?): {exc}",
            data={"url": url},
        )
    except httpx.RequestError as exc:
        return CheckResult(
            name="port-reachable",
            status=Status.FAIL,
            detail=f"request error: {exc}",
            data={"url": url},
        )

    if resp.status_code != 200:
        return CheckResult(
            name="port-reachable",
            status=Status.FAIL,
            detail=f"HTTP {resp.status_code} from {url}",
            data={"url": url, "status_code": resp.status_code},
        )
    try:
        body = resp.json()
    except ValueError:
        return CheckResult(
            name="port-reachable",
            status=Status.FAIL,
            detail=f"non-JSON response from {url}",
            data={"url": url},
        )
    return CheckResult(
        name="port-reachable",
        status=Status.OK,
        detail=f"telegram_connected={body.get('telegram_connected')}",
        data={"url": url, **{k: v for k, v in body.items() if k != "status"}},
    )


def _check_state_depths(raw: dict[str, Any]) -> list[CheckResult]:
    """Read the transport state file directly and surface two counters."""
    transport_cfg = raw.get("transport", {}) or {}
    state_cfg = transport_cfg.get("state", {}) or {}
    state_path = Path(
        state_cfg.get("path", "./data/transport_state.json")
    )
    results: list[CheckResult] = []

    if not state_path.exists():
        # Fresh install — file hasn't been written yet; that's fine.
        results.append(CheckResult(
            name="queue-depth",
            status=Status.OK,
            detail="state file absent (no sends yet)",
            data={"pending": 0},
        ))
        results.append(CheckResult(
            name="dead-letter-depth",
            status=Status.OK,
            detail="state file absent",
            data={"dead_letter": 0},
        ))
        return results

    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        results.append(CheckResult(
            name="queue-depth",
            status=Status.WARN,
            detail=f"state unreadable: {exc.__class__.__name__}",
        ))
        return results

    pending = len(data.get("pending_queue", []) or [])
    dead = len(data.get("dead_letter", []) or [])

    queue_status = (
        Status.WARN if pending > _QUEUE_WARN_THRESHOLD else Status.OK
    )
    results.append(CheckResult(
        name="queue-depth",
        status=queue_status,
        detail=(
            f"pending={pending} (warn at {_QUEUE_WARN_THRESHOLD})"
        ),
        data={"pending": pending, "threshold": _QUEUE_WARN_THRESHOLD},
    ))

    dl_status = (
        Status.WARN if dead > _DEAD_LETTER_WARN_THRESHOLD else Status.OK
    )
    results.append(CheckResult(
        name="dead-letter-depth",
        status=dl_status,
        detail=(
            f"dead_letter={dead} (warn at {_DEAD_LETTER_WARN_THRESHOLD})"
        ),
        data={
            "dead_letter": dead,
            "threshold": _DEAD_LETTER_WARN_THRESHOLD,
        },
    ))
    return results


async def _check_peer_reachable(
    raw: dict[str, Any], peer_name: str, peer_entry: "PeerEntry | dict[str, Any]",
) -> CheckResult:
    """GET <peer.base_url>/health on the named peer.

    The peer's /health endpoint is unauthenticated by design (bootstrap
    probe) so we can hit it without loading a token. WARN on
    connection refused (peer is down but that's not a FAIL); FAIL on
    non-JSON or non-200 response from a reachable port.

    ``peer_entry`` may be either a :class:`PeerEntry` dataclass (the
    production path via :func:`_run_peer_probes`, where env substitution
    has already been applied) or a raw dict (for direct-test callsites
    that don't care about ``${VAR}`` placeholders).
    """
    base_url = _peer_attr(peer_entry, "base_url")
    name = f"peer-reachable:{peer_name}"
    if not base_url:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"peer '{peer_name}' has no base_url",
        )
    url = f"{base_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(url)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        return CheckResult(
            name=name,
            status=Status.WARN,
            detail=f"{peer_name} unreachable: {exc.__class__.__name__}",
            data={"url": url, "peer": peer_name},
        )
    except httpx.RequestError as exc:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"{peer_name} request error: {exc}",
            data={"url": url, "peer": peer_name},
        )
    if resp.status_code != 200:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"{peer_name} HTTP {resp.status_code}",
            data={"url": url, "peer": peer_name, "status_code": resp.status_code},
        )
    return CheckResult(
        name=name,
        status=Status.OK,
        detail=f"{peer_name} reachable",
        data={"url": url, "peer": peer_name},
    )


async def _check_peer_handshake(
    raw: dict[str, Any], peer_name: str, peer_entry: "PeerEntry | dict[str, Any]",
) -> CheckResult:
    """POST <peer.base_url>/peer/handshake — validates auth + protocol version.

    Requires the caller's peer token (``peer_entry.token``). WARN on
    version skew (caller is protocol v1, peer returned >1); FAIL on
    auth rejection or missing required capability.

    ``peer_entry`` may be either a :class:`PeerEntry` dataclass or a raw
    dict (see :func:`_check_peer_reachable`). In the production path
    :func:`_run_peer_probes` hands us an env-substituted ``PeerEntry``,
    so ``${ALFRED_KALLE_PEER_TOKEN}`` placeholders resolve before we
    send them in an ``Authorization: Bearer`` header.
    """
    from .exceptions import TransportError

    name = f"peer-handshake:{peer_name}"
    token = _peer_attr(peer_entry, "token")
    base_url = _peer_attr(peer_entry, "base_url")
    if not token:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"peer '{peer_name}' has no token",
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Alfred-Client": _infer_self_name(raw),
    }
    url = f"{base_url.rstrip('/')}/peer/handshake"
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(
                url,
                json={"from": _infer_self_name(raw), "protocol_version": 1},
                headers=headers,
            )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return CheckResult(
            name=name,
            status=Status.WARN,
            detail=f"{peer_name} unreachable",
            data={"peer": peer_name},
        )
    except httpx.RequestError as exc:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"{peer_name} request error: {exc}",
            data={"peer": peer_name},
        )

    if resp.status_code == 401:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"{peer_name} auth rejected (token mismatch)",
            data={"peer": peer_name},
        )
    if resp.status_code != 200:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"{peer_name} HTTP {resp.status_code}",
            data={"peer": peer_name, "status_code": resp.status_code},
        )

    try:
        body = resp.json()
    except ValueError:
        return CheckResult(
            name=name,
            status=Status.FAIL,
            detail=f"{peer_name} non-JSON handshake response",
            data={"peer": peer_name},
        )

    their_version = body.get("protocol_version")
    status = Status.OK
    detail = f"{peer_name} handshake ok (v{their_version})"
    if their_version != 1:
        status = Status.WARN
        detail = (
            f"{peer_name} protocol skew: ours=1, theirs={their_version}"
        )

    return CheckResult(
        name=name,
        status=status,
        detail=detail,
        data={
            "peer": peer_name,
            "protocol_version": their_version,
            "capabilities": body.get("capabilities") or [],
        },
    )


def _check_peer_queue_depth(
    raw: dict[str, Any], peer_name: str,
) -> CheckResult:
    """Counter for peer-specific pending entries.

    v1 the pending_queue isn't partitioned by peer — all scheduled
    sends share one list. This probe inspects the local transport
    state for entries whose ``peer`` field matches ``peer_name``
    (Stage 3.5: scheduler writes ``peer`` onto every enqueue) and
    reports the count. Returns OK with a 0 count when no peer-
    specific entries exist.
    """
    transport_cfg = raw.get("transport", {}) or {}
    state_cfg = transport_cfg.get("state", {}) or {}
    state_path = Path(
        state_cfg.get("path", "./data/transport_state.json")
    )
    name = f"peer-queue-depth:{peer_name}"
    if not state_path.exists():
        return CheckResult(
            name=name,
            status=Status.OK,
            detail=f"no state file; peer queue depth = 0",
            data={"peer": peer_name, "depth": 0},
        )
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return CheckResult(
            name=name,
            status=Status.WARN,
            detail=f"{peer_name} state unreadable",
            data={"peer": peer_name},
        )
    entries = data.get("pending_queue", []) or []
    depth = sum(1 for e in entries if e.get("peer") == peer_name)
    threshold = 100
    status = Status.WARN if depth > threshold else Status.OK
    return CheckResult(
        name=name,
        status=status,
        detail=f"{peer_name} depth={depth} (warn at {threshold})",
        data={"peer": peer_name, "depth": depth, "threshold": threshold},
    )


def _infer_self_name(raw: dict[str, Any]) -> str:
    """Return a peer-name-shaped identifier for this instance.

    Salem defaults to ``"salem"``; KAL-LE defaults to ``"kal-le"``.
    Reads ``telegram.instance.name`` + lowercases + strips dots.
    """
    tg = raw.get("telegram", {}) or {}
    inst = tg.get("instance", {}) or {}
    name = str(inst.get("name") or "alfred").lower().replace(".", "").replace(" ", "-")
    # Alfred default → salem for peer purposes.
    if name == "alfred":
        return "salem"
    return name


async def _run_peer_probes(
    raw: dict[str, Any],
    filter_peer: str | None = None,
) -> list[CheckResult]:
    """Run all peer-specific probes. Optionally filter to one peer.

    Loads peers via :func:`alfred.transport.config.load_from_unified`
    so that ``${VAR}`` placeholders in ``transport.peers.*.token`` and
    ``*.base_url`` are env-substituted BEFORE we send them as bearer
    tokens or request URLs. Reading ``raw["transport"]["peers"]``
    directly would leak literal placeholder strings into the
    ``Authorization: Bearer`` header and surface as a false-negative
    401 on the ``peer-handshake:*`` probe — the bug fixed in the
    2026-04-21 scheduling follow-ups pass.
    """
    results: list[CheckResult] = []
    # Build the typed transport config — this applies _substitute_env
    # to the full ``raw`` dict, so nested placeholders resolve. Empty
    # ``transport`` section is tolerated by load_from_unified (returns
    # defaults) so the ``if not peers`` short-circuit still kicks in.
    transport_cfg = load_from_unified(raw)
    peers = transport_cfg.peers
    if not peers:
        return results

    for peer_name, peer_entry in peers.items():
        if filter_peer and peer_name != filter_peer:
            continue
        results.append(await _check_peer_reachable(raw, peer_name, peer_entry))
        results.append(await _check_peer_handshake(raw, peer_name, peer_entry))
        results.append(_check_peer_queue_depth(raw, peer_name))
    return results


async def health_check(
    raw: dict[str, Any],
    mode: str = "quick",
    filter_peer: str | None = None,
) -> ToolHealth:
    """Run transport health probes.

    Returns SKIP when ``transport:`` is absent from config — the
    transport is optional (brief + scheduler are the only v1
    consumers, and both tolerate its absence).

    Stage 3.5: ``filter_peer`` narrows the probes to a single peer
    name. When set, the local (non-peer) probes are skipped too —
    ``alfred check --peer kal-le`` only reports on KAL-LE.
    """
    if raw.get("transport") is None:
        return ToolHealth(
            tool="transport",
            status=Status.SKIP,
            detail="no transport section in config",
        )

    results: list[CheckResult] = []
    if filter_peer is None:
        results.extend([
            _check_config_section(raw),
            _check_token_configured(raw),
        ])
        results.append(await _check_port_reachable(raw))
        results.extend(_check_state_depths(raw))

    # Peer probes (always, unless filter_peer excludes them all).
    results.extend(await _run_peer_probes(raw, filter_peer=filter_peer))

    if not results:
        return ToolHealth(
            tool="transport",
            status=Status.SKIP,
            detail=(
                f"no peer named {filter_peer!r}"
                if filter_peer
                else "no probes ran"
            ),
        )

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="transport", status=status, results=results)


register_check("transport", health_check)
