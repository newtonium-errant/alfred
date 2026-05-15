"""Cloudflared health check — registered with the BIT aggregator.

Mirrors the brief/janitor/distiller/daily_sync/instructor
``last-successful-*`` daemon-liveness pattern shipped on 2026-05-14
but for an HTTP-reachable subsystem rather than a state-file
inspector. Cloudflared exposes Prometheus-format metrics on
``localhost:20241/metrics`` by default; we probe it for the
``cloudflared_tunnel_ha_connections`` gauge.

Status mapping:

* **OK**   — metrics endpoint reachable AND ``ha_connections >= 1``
  (cloudflared registered to one or more Cloudflare edge locations;
  typically 4 — one per geographically distributed datacenter).
* **WARN** — endpoint reachable but ``ha_connections == 0``
  (cloudflared running but not connected — transient network or
  auth issue; the binary is up so SIGTERM/SIGKILL paths still work).
* **FAIL** — endpoint unreachable AND ``cloudflared.enabled=true``
  (binary crashed; orchestrator's auto-restart will kick in).
* **SKIP** — ``cloudflared.enabled=false`` or the block is absent
  (operator opted out; nothing to probe).

**Metric name discovery (2026-05-15):** the project brief originally
referenced ``cloudflared_tunnel_active_connections``; the live
endpoint on the dev box exposes the count as
``cloudflared_tunnel_ha_connections`` (cloudflared 2026.3.0 + earlier).
Documented here so a future cloudflared version-bump that renames the
gauge has a clear single source of truth to fix.

Per ``feedback_intentionally_left_blank.md``: the FAIL case is the
operator-visible signal that the tunnel is down. Silence
(``cloudflared.exited`` flagged but no operator-side notification)
would be ambiguous between "tunnel down" and "no traffic"; this probe
disambiguates.
"""

from __future__ import annotations

from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth

from alfred.cloudflared.config import load_from_unified


# Gauge name we extract from the Prometheus metrics output. Documented
# in the module docstring above; isolate the literal here so a future
# rename is a one-line edit.
_HA_CONNECTIONS_METRIC = "cloudflared_tunnel_ha_connections"


def _read_metrics(metrics_url: str, timeout_seconds: float = 2.0) -> dict[str, Any]:
    """Fetch the metrics endpoint and parse the connection-count gauge.

    Returns a dict shaped:

      ``{"reachable": True, "ha_connections": int}`` on success
      ``{"reachable": False, "error": str}`` on any failure

    All failure modes (connection refused, timeout, malformed body,
    missing gauge) collapse to ``reachable: False`` with a short
    error string. Callers map that to FAIL when the daemon is enabled.

    We use ``httpx`` (already a base dep across the project) so this
    module doesn't add a new dependency. Sync rather than async because
    the probe runs from within the aggregator's async ``run_one`` —
    a 2s sync HTTP call is fine here and keeps the function trivially
    testable without ``asyncio.run`` wrappers.
    """
    import httpx

    try:
        resp = httpx.get(metrics_url, timeout=timeout_seconds)
    except httpx.HTTPError as exc:
        # Connection refused, timeout, DNS, etc. — all the network-side
        # failure modes. ``HTTPError`` is httpx's root for transport-
        # level errors; we catch the broad base on purpose.
        return {
            "reachable": False,
            "error": f"{exc.__class__.__name__}: {str(exc)[:120]}",
        }
    except Exception as exc:  # noqa: BLE001
        # Defensive — httpx shouldn't raise anything else, but a
        # transient stdlib issue (socket fork-leak, etc.) shouldn't
        # crash the BIT run.
        return {
            "reachable": False,
            "error": f"{exc.__class__.__name__}: {str(exc)[:120]}",
        }

    if resp.status_code != 200:
        return {
            "reachable": False,
            "error": f"HTTP {resp.status_code}",
        }

    # Parse Prometheus text format. We only need one gauge; walk lines
    # and stop on first match. Comment lines (``# HELP ...`` /
    # ``# TYPE ...``) get skipped naturally by the prefix check.
    try:
        body = resp.text
    except Exception as exc:  # noqa: BLE001
        return {
            "reachable": False,
            "error": f"body-read failed: {exc.__class__.__name__}",
        }

    ha_connections: int | None = None
    prefix = _HA_CONNECTIONS_METRIC
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Match the bare gauge (no label set). Prometheus format:
        # ``<metric_name>{labels} <value>`` or ``<metric_name> <value>``.
        # cloudflared exposes ha_connections as bare (no labels).
        if not line.startswith(prefix):
            continue
        # Guard against a future labelled variant (e.g.
        # ``cloudflared_tunnel_ha_connections{...}``) — only accept the
        # bare form so the gauge value is unambiguous.
        rest = line[len(prefix):]
        if rest.startswith("{"):
            # Labelled — skip; will fall through to "missing gauge".
            continue
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            ha_connections = int(float(parts[0]))
        except ValueError:
            continue
        break

    if ha_connections is None:
        return {
            "reachable": False,
            "error": f"gauge {_HA_CONNECTIONS_METRIC!r} not found in metrics body",
        }

    return {"reachable": True, "ha_connections": ha_connections}


def _check_tunnel_connections(
    metrics_url: str,
    enabled: bool,
    timeout_seconds: float = 2.0,
) -> CheckResult:
    """Probe the metrics endpoint and map the result to a CheckResult.

    Args:
        metrics_url: The full URL to the cloudflared metrics endpoint.
        enabled: ``cloudflared.enabled`` flag from config — when False
            we never return FAIL even on unreachability (the operator
            opted out and we have no expectation of liveness).
        timeout_seconds: HTTP timeout. Default 2s matches the BIT
            quick-mode budget; full-mode callers can bump it.
    """
    if not enabled:
        return CheckResult(
            name="last-successful-tunnel",
            status=Status.SKIP,
            detail="cloudflared disabled in config",
            data={"enabled": False},
        )

    result = _read_metrics(metrics_url, timeout_seconds=timeout_seconds)
    payload: dict[str, Any] = {"metrics_url": metrics_url, "enabled": True}

    if not result["reachable"]:
        payload["error"] = result["error"]
        return CheckResult(
            name="last-successful-tunnel",
            status=Status.FAIL,
            detail=(
                f"metrics endpoint unreachable at {metrics_url} "
                f"(cloudflared may have crashed): {result['error']}"
            ),
            data=payload,
        )

    count = result["ha_connections"]
    payload["ha_connections"] = count

    if count <= 0:
        return CheckResult(
            name="last-successful-tunnel",
            status=Status.WARN,
            detail=(
                f"tunnel connections active: {count} (cloudflared running "
                "but not registered to Cloudflare edge — auth or network issue)"
            ),
            data=payload,
        )

    return CheckResult(
        name="last-successful-tunnel",
        status=Status.OK,
        detail=f"tunnel connections active: {count}",
        data=payload,
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run cloudflared health checks.

    Only one probe currently — ``last-successful-tunnel`` (the metrics
    endpoint reachability + connection-count gauge). Returns SKIP at
    the tool level when the ``cloudflared`` section is absent so the
    BIT output cleanly distinguishes "not configured" from "disabled
    by flag" (both surface as SKIP but the detail differs).
    """
    if raw.get("cloudflared") is None:
        return ToolHealth(
            tool="cloudflared",
            status=Status.SKIP,
            detail="no cloudflared section in config",
        )

    config = load_from_unified(raw)
    # Full-mode gets a longer HTTP timeout — matches the aggregator's
    # 15s-per-tool budget. Quick mode keeps the 2s default.
    timeout = 5.0 if mode == "full" else 2.0

    result = _check_tunnel_connections(
        metrics_url=config.metrics_url,
        enabled=config.enabled,
        timeout_seconds=timeout,
    )

    status = result.status
    return ToolHealth(tool="cloudflared", status=status, results=[result])


register_check("cloudflared", health_check)
