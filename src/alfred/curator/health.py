"""Curator health check — registered with the BIT aggregator.

Probes:
  * vault path exists + writable
  * inbox_dir exists (where the curator watches for new files)
  * anthropic auth (via the shared probe in alfred.health.anthropic_auth)
  * backend type is known (static check — warns on misconfigured backends)

This module is imported by ``alfred.health.aggregator._load_tool_checks``;
the import side-effect is registering the ``health_check`` callable.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.anthropic_auth import check_anthropic_auth, resolve_api_key
from alfred.health.types import CheckResult, Status, ToolHealth


_KNOWN_BACKENDS = ("claude", "zo", "openclaw", "hermes")


def _check_vault(raw: dict[str, Any]) -> list[CheckResult]:
    """Verify vault path + curator-specific inbox directory."""
    results: list[CheckResult] = []

    vault_path_str = (raw.get("vault", {}) or {}).get("path", "") or ""
    if not vault_path_str:
        results.append(CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail="vault.path is empty in config",
        ))
        return results

    vault_path = Path(vault_path_str)
    if not vault_path.exists():
        results.append(CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail=f"vault path does not exist: {vault_path}",
        ))
        return results
    if not os.access(vault_path, os.W_OK):
        results.append(CheckResult(
            name="vault-path",
            status=Status.FAIL,
            detail=f"vault path not writable: {vault_path}",
        ))
    else:
        results.append(CheckResult(
            name="vault-path",
            status=Status.OK,
            detail=str(vault_path),
            data={"path": str(vault_path)},
        ))

    inbox_rel = (raw.get("curator", {}) or {}).get("inbox_dir", "inbox")
    inbox_path = vault_path / inbox_rel
    if inbox_path.exists():
        results.append(CheckResult(
            name="inbox-dir",
            status=Status.OK,
            detail=str(inbox_path),
            data={"path": str(inbox_path)},
        ))
    else:
        # Missing inbox_dir is not fatal — it's auto-created when the
        # curator ingests the first email. Surface as WARN so operators
        # notice on fresh installs.
        results.append(CheckResult(
            name="inbox-dir",
            status=Status.WARN,
            detail=f"inbox dir missing (will be created on first use): {inbox_path}",
        ))

    return results


def _check_backend(raw: dict[str, Any]) -> CheckResult:
    """Static check of the configured agent backend."""
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
        detail=f"unknown backend '{backend}' (known: {', '.join(_KNOWN_BACKENDS)})",
        data={"backend": backend},
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run curator health checks.

    The ``mode`` argument is accepted for interface uniformity; curator
    checks are all cheap so quick and full do the same work today.
    """
    results: list[CheckResult] = []
    results.extend(_check_vault(raw))
    results.append(_check_backend(raw))

    # Anthropic auth — only probe if the configured backend is one that
    # uses the Anthropic SDK / CLI. Other backends (zo, openclaw via local
    # models) don't need Anthropic credentials.
    backend = (raw.get("agent", {}) or {}).get("backend", "claude")
    if backend == "claude":
        api_key = resolve_api_key(raw)
        results.append(await check_anthropic_auth(api_key))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="curator", status=status, results=results)


# Registration side-effect at import time — the aggregator imports this
# module in ``_load_tool_checks`` to populate its registry.
register_check("curator", health_check)
