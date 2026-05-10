"""Surveyor health check.

Probes:
  * surveyor section present in config — if missing, return SKIP with
    a reason (surveyor is an optional tool, ``[surveyor]`` extras)
  * ollama / OpenRouter embedding endpoint reachable (HTTP GET on /
    or /api/tags — cheap, no auth)
  * milvus-lite db file — existence (optional; auto-created) and
    parent dir writable
  * openrouter config present (static — key set, model id set)
  * last-successful-cycle — daemon liveness validation per the
    universal "intentionally left blank" / observability discipline
    (added 2026-05-10 as part of the cross-daemon BIT probe arc).
    Surveyor runs continuously; thresholds reflect a sub-hourly
    expected cadence.

Full mode adds nothing here; everything is already cheap.

We import httpx lazily inside the check so the module is importable
even in environments where surveyor's extras aren't installed —
pymilvus / numpy / scikit-learn are the reasons surveyor is optional,
not httpx (which is a base dep).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth


# Stale-threshold calibrations for the last-successful-cycle probe.
# Surveyor runs a continuous loop (default loop_interval ~minutes), so
# its expected cadence is sub-hourly. Module-level constants so a
# threshold tune is a 1-line change.
_SURVEYOR_STALE_OK_HOURS = 2
_SURVEYOR_STALE_FAIL_HOURS = 6


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


def _resolve_surveyor_state_path(raw: dict[str, Any]) -> Path:
    """Resolve surveyor's state-file path the same way
    ``alfred.surveyor.config.load_from_unified`` does — explicit path
    wins, otherwise the dataclass default ``./data/surveyor_state.json``.

    Per-instance scope: KAL-LE / Hypatia / future instances configure
    their own state paths; the probe consults whatever
    ``raw["surveyor"]["state"]["path"]`` resolves to.
    """
    state_section = (raw.get("surveyor", {}) or {}).get("state", {}) or {}
    explicit = state_section.get("path", "")
    if explicit:
        return Path(explicit)
    return Path("./data/surveyor_state.json")


def _read_surveyor_last_run(state_path: Path) -> str | None:
    """Read surveyor state file's top-level ``last_run`` ISO timestamp.

    Returns None on missing / unparseable / empty. Inline dict-walk
    rather than constructing ``alfred.surveyor.state.PipelineState`` —
    matches the precedent set by yesterday's
    ``brief.health._most_recent_successful_brief_date``.
    """
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    last_run = data.get("last_run", "")
    if isinstance(last_run, str) and last_run:
        return last_run
    return None


def _check_last_successful_cycle(raw: dict[str, Any]) -> CheckResult:
    """Validate that the surveyor daemon's loop has cycled recently.

    Surveyor runs continuously (no fixed schedule) — its loop ticks
    write to ``state.last_run`` whenever a cycle completes. A stale
    timestamp means the loop is wedged.

    Status mapping:
      * SKIP if state file missing (fresh install) / empty last_run /
        unparseable
      * OK   if last_run <= 2h ago (active loop, sub-hourly cadence)
      * WARN if last_run 2h..6h ago (slow loop or transient hiccup)
      * FAIL if last_run > 6h ago (loop wedged — silent failure)

    "Now" is computed in UTC because surveyor's ``last_run`` is
    written in UTC (state.save() uses ``datetime.now(timezone.utc)``).
    No timezone config to consult.

    Per ``feedback_intentionally_left_blank.md``: silence (surveyor
    daemon idle, no embed/cluster/label log activity) is ambiguous
    between healthy-quiet and broken; the probe disambiguates.
    """
    state_path = _resolve_surveyor_state_path(raw)
    last_run_iso = _read_surveyor_last_run(state_path)

    if last_run_iso is None:
        if not state_path.is_file():
            return CheckResult(
                name="last-successful-cycle",
                status=Status.SKIP,
                detail=f"no state file (fresh install): {state_path}",
                data={"state_path": str(state_path), "exists": False},
            )
        return CheckResult(
            name="last-successful-cycle",
            status=Status.SKIP,
            detail="no last_run recorded yet",
            data={"state_path": str(state_path), "exists": True},
        )

    try:
        normalized = (
            last_run_iso.replace("Z", "+00:00")
            if last_run_iso.endswith("Z") else last_run_iso
        )
        last_run = datetime.fromisoformat(normalized)
        if last_run.tzinfo is None:
            last_run = last_run.replace(tzinfo=timezone.utc)
    except ValueError:
        return CheckResult(
            name="last-successful-cycle",
            status=Status.SKIP,
            detail=f"unparseable last_run in state: {last_run_iso!r}",
            data={"state_path": str(state_path)},
        )

    now = datetime.now(timezone.utc)
    elapsed = now - last_run
    elapsed_hours = elapsed.total_seconds() / 3600.0
    payload: dict[str, Any] = {
        "state_path": str(state_path),
        "last_run": last_run_iso,
        "elapsed_hours": round(elapsed_hours, 2),
    }

    if elapsed < timedelta(hours=_SURVEYOR_STALE_OK_HOURS):
        return CheckResult(
            name="last-successful-cycle",
            status=Status.OK,
            detail=f"last cycle {round(elapsed_hours, 1)}h ago",
            data=payload,
        )
    if elapsed < timedelta(hours=_SURVEYOR_STALE_FAIL_HOURS):
        return CheckResult(
            name="last-successful-cycle",
            status=Status.WARN,
            detail=f"last cycle {round(elapsed_hours, 1)}h ago (slow loop or transient hiccup)",
            data=payload,
        )
    return CheckResult(
        name="last-successful-cycle",
        status=Status.FAIL,
        detail=f"last cycle {round(elapsed_hours, 1)}h ago (loop may be wedged)",
        data=payload,
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
        _check_last_successful_cycle(raw),
    ]

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="surveyor", status=status, results=results)


register_check("surveyor", health_check)
