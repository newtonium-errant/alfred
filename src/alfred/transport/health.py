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


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run transport health probes.

    Returns SKIP when ``transport:`` is absent from config — the
    transport is optional (brief + scheduler are the only v1
    consumers, and both tolerate its absence).
    """
    if raw.get("transport") is None:
        return ToolHealth(
            tool="transport",
            status=Status.SKIP,
            detail="no transport section in config",
        )

    results: list[CheckResult] = [
        _check_config_section(raw),
        _check_token_configured(raw),
    ]
    results.append(await _check_port_reachable(raw))
    results.extend(_check_state_depths(raw))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="transport", status=status, results=results)


register_check("transport", health_check)
