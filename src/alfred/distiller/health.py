"""Distiller health check — registered with the BIT aggregator.

Probes:
  * vault path writable
  * distiller state file readable (if present)
  * candidate_threshold is in the expected (0.0 .. 1.0) range — a
    misconfigured threshold is a silent footgun; the daemon won't
    refuse to start but it will produce nonsense.
  * backend known
  * anthropic auth (when backend == claude)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.anthropic_auth import check_anthropic_auth, resolve_api_key
from alfred.health.types import CheckResult, Status, ToolHealth


_KNOWN_BACKENDS = ("claude", "zo", "openclaw")


def _check_vault(raw: dict[str, Any]) -> CheckResult:
    vault_path_str = (raw.get("vault", {}) or {}).get("path", "") or ""
    if not vault_path_str:
        return CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail="vault.path is empty",
        )
    vault_path = Path(vault_path_str)
    if not vault_path.exists():
        return CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail=f"vault path does not exist: {vault_path}",
        )
    if not os.access(vault_path, os.W_OK):
        return CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail=f"vault path not writable: {vault_path}",
        )
    return CheckResult(
        name="vault-path",
        status=Status.OK,
        detail=str(vault_path),
        data={"path": str(vault_path)},
    )


def _check_state_file(raw: dict[str, Any]) -> CheckResult:
    state_raw = (raw.get("distiller", {}) or {}).get("state", {}) or {}
    state_path = Path(state_raw.get("path", "./data/distiller_state.json"))
    if not state_path.exists():
        return CheckResult(
            name="state-file",
            status=Status.OK,
            detail="no state file yet (fresh install)",
            data={"path": str(state_path)},
        )
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        return CheckResult(
            name="state-file",
            status=Status.WARN,
            detail=f"state file unreadable: {exc.__class__.__name__}",
            data={"path": str(state_path)},
        )
    return CheckResult(
        name="state-file",
        status=Status.OK,
        detail=str(state_path),
        data={"path": str(state_path)},
    )


def _check_threshold(raw: dict[str, Any]) -> CheckResult:
    """Static sanity check on the candidate_threshold value.

    A threshold outside [0, 1] is almost certainly a typo — the
    candidate scorer produces values in that range. We warn rather
    than fail so a user experimenting with aggressive filtering can
    override the check if they really mean it.
    """
    ext_raw = (raw.get("distiller", {}) or {}).get("extraction", {}) or {}
    threshold = ext_raw.get("candidate_threshold", 0.3)
    try:
        value = float(threshold)
    except (TypeError, ValueError):
        return CheckResult(
            name="candidate-threshold",
            status=Status.FAIL,
            detail=f"candidate_threshold is not a number: {threshold!r}",
        )
    if not 0.0 <= value <= 1.0:
        return CheckResult(
            name="candidate-threshold",
            status=Status.WARN,
            detail=f"candidate_threshold out of [0, 1]: {value}",
            data={"value": value},
        )
    return CheckResult(
        name="candidate-threshold",
        status=Status.OK,
        detail=f"{value}",
        data={"value": value},
    )


def _check_backend(raw: dict[str, Any]) -> CheckResult:
    backend = (raw.get("agent", {}) or {}).get("backend", "claude")
    if backend in _KNOWN_BACKENDS:
        return CheckResult(
            name="backend",
            status=Status.OK,
            detail=f"backend={backend}",
            data={"backend": backend},
        )
    return CheckResult(
        name="backend",
        status=Status.WARN,
        detail=f"unknown backend '{backend}'",
        data={"backend": backend},
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run distiller health checks."""
    results: list[CheckResult] = [
        _check_vault(raw),
        _check_state_file(raw),
        _check_threshold(raw),
        _check_backend(raw),
    ]

    backend = (raw.get("agent", {}) or {}).get("backend", "claude")
    if backend == "claude":
        api_key = resolve_api_key(raw)
        results.append(await check_anthropic_auth(api_key))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="distiller", status=status, results=results)


register_check("distiller", health_check)
