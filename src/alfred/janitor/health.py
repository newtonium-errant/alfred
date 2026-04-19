"""Janitor health check — registered with the BIT aggregator.

Janitor probes are deliberately cheap: vault writability, configured
sweep directories, state file readability, backend known, Anthropic
auth. The runtime cost of a full janitor sweep is irrelevant to this
health check — we're only checking that the preconditions to start
a sweep are satisfied.
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
    """Verify vault path exists and is writable."""
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
    """Verify the janitor state file is readable JSON (if it exists).

    A missing state file is OK — it just means the janitor hasn't
    swept yet.  A corrupt one is WARN, not FAIL, because the janitor
    auto-resets on load errors.
    """
    state_raw = (raw.get("janitor", {}) or {}).get("state", {}) or {}
    state_path = Path(state_raw.get("path", "./data/janitor_state.json"))
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


def _check_backend(raw: dict[str, Any]) -> CheckResult:
    """Static backend check."""
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
    """Run janitor health checks."""
    results: list[CheckResult] = [
        _check_vault(raw),
        _check_state_file(raw),
        _check_backend(raw),
    ]

    backend = (raw.get("agent", {}) or {}).get("backend", "claude")
    if backend == "claude":
        api_key = resolve_api_key(raw)
        results.append(await check_anthropic_auth(api_key))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="janitor", status=status, results=results)


register_check("janitor", health_check)
