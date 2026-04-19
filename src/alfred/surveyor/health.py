"""Surveyor health check.

Probes:
  * surveyor section present in config — if missing, return SKIP with
    a reason (surveyor is an optional tool, ``[surveyor]`` extras)
  * ollama / OpenRouter embedding endpoint reachable (HTTP GET on /
    or /api/tags — cheap, no auth)
  * milvus-lite db file — existence (optional; auto-created) and
    parent dir writable
  * openrouter config present (static — key set, model id set)

Full mode adds nothing here; everything is already cheap.

We import httpx lazily inside the check so the module is importable
even in environments where surveyor's extras aren't installed —
pymilvus / numpy / scikit-learn are the reasons surveyor is optional,
not httpx (which is a base dep).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth


async def _check_ollama(base_url: str, api_key: str, timeout: float) -> CheckResult:
    """Probe the configured ollama (or OpenRouter) endpoint.

    For native Ollama we hit ``GET /`` (returns plain text in 200).
    For OpenRouter-style we hit the base URL. Either way, a 2xx or
    3xx means reachable; anything else is WARN (the daemon will retry
    at runtime but we want the operator to notice now).
    """
    import httpx  # base dep

    # Trim trailing slash so we can compose the ping URL safely
    base = base_url.rstrip("/") if base_url else ""
    if not base:
        return CheckResult(
            name="ollama-reachable",
            status=Status.FAIL,
            detail="ollama.base_url is empty",
        )
    # For native ollama the root responds 200; for OpenRouter, the /
    # path may 404 but the connection itself succeeds. Treat any HTTP
    # response (including 4xx) as "service reachable" — what we're
    # actually testing is TCP/TLS plus DNS.
    url = base + "/"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="ollama-reachable",
            status=Status.WARN,
            detail=f"unreachable: {exc.__class__.__name__}: {str(exc)[:120]}",
            data={"url": url},
        )
    return CheckResult(
        name="ollama-reachable",
        status=Status.OK,
        detail=f"HTTP {resp.status_code}",
        data={"url": url, "status_code": resp.status_code, "has_api_key": bool(api_key)},
    )


def _check_milvus(milvus_uri: str) -> CheckResult:
    """Verify the milvus-lite db directory is writable (file auto-created)."""
    if not milvus_uri:
        return CheckResult(
            name="milvus-lite",
            status=Status.WARN,
            detail="milvus.uri not configured",
        )
    path = Path(milvus_uri)
    parent = path.parent if path.parent != Path("") else Path(".")
    if not parent.exists():
        return CheckResult(
            name="milvus-lite",
            status=Status.WARN,
            detail=f"parent dir missing: {parent}",
            data={"uri": str(path)},
        )
    if not os.access(parent, os.W_OK):
        return CheckResult(
            name="milvus-lite",
            status=Status.FAIL,
            detail=f"parent dir not writable: {parent}",
        )
    return CheckResult(
        name="milvus-lite",
        status=Status.OK,
        detail=f"db: {path}",
        data={"uri": str(path), "exists": path.exists()},
    )


def _check_openrouter(openrouter: dict) -> CheckResult:
    """Static config check — key present + model id set."""
    key = openrouter.get("api_key") or ""
    model = openrouter.get("model") or ""
    if not key or key.startswith("${"):
        return CheckResult(
            name="openrouter-key",
            status=Status.WARN,
            detail="OpenRouter api_key not set (labeler stage will skip)",
            data={"has_key": False, "model": model},
        )
    if not model:
        return CheckResult(
            name="openrouter-key",
            status=Status.WARN,
            detail="openrouter.model not set",
            data={"has_key": True, "model": ""},
        )
    return CheckResult(
        name="openrouter-key",
        status=Status.OK,
        detail=f"key set, model={model}",
        data={"has_key": True, "model": model},
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run surveyor health checks."""
    surveyor = raw.get("surveyor")
    if surveyor is None:
        return ToolHealth(
            tool="surveyor",
            status=Status.SKIP,
            detail="no surveyor section in config",
        )

    timeout = 3.0 if mode == "quick" else 8.0

    ollama = surveyor.get("ollama", {}) or {}
    milvus = surveyor.get("milvus", {}) or {}
    openrouter = surveyor.get("openrouter", {}) or {}

    results: list[CheckResult] = [
        await _check_ollama(
            ollama.get("base_url", ""),
            ollama.get("api_key", "") or "",
            timeout,
        ),
        _check_milvus(milvus.get("uri", "")),
        _check_openrouter(openrouter),
    ]

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="surveyor", status=status, results=results)


register_check("surveyor", health_check)
