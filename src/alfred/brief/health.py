"""Brief health check.

Probes:
  * schedule.time parseable as HH:MM
  * schedule.timezone resolvable via zoneinfo
  * vault output directory writable
  * weather API reachable (quick HTTP probe; WARN on failure — the
    brief falls back to cached weather at runtime, so it's not FAIL)

Brief is a scheduler — there's nothing token-expensive about its
preconditions. We keep the quick/full distinction lightweight.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth


def _check_schedule(schedule: dict) -> list[CheckResult]:
    """Validate schedule.time (HH:MM) and schedule.timezone."""
    out: list[CheckResult] = []

    time_str = schedule.get("time", "06:00")
    try:
        h, m = time_str.split(":")
        hour = int(h)
        minute = int(m)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"out of range: {hour}:{minute}")
        out.append(CheckResult(
            name="schedule-time",
            status=Status.OK,
            detail=time_str,
            data={"time": time_str},
        ))
    except (ValueError, AttributeError) as exc:
        out.append(CheckResult(
            name="schedule-time",
            status=Status.FAIL,
            detail=f"invalid schedule.time={time_str!r}: {exc}",
        ))

    tz_name = schedule.get("timezone", "America/Halifax")
    try:
        ZoneInfo(tz_name)
        out.append(CheckResult(
            name="schedule-timezone",
            status=Status.OK,
            detail=tz_name,
            data={"timezone": tz_name},
        ))
    except ZoneInfoNotFoundError as exc:
        out.append(CheckResult(
            name="schedule-timezone",
            status=Status.FAIL,
            detail=f"unknown timezone {tz_name!r}: {exc}",
        ))

    return out


def _check_output_dir(raw: dict[str, Any], brief: dict) -> CheckResult:
    """The brief writes to ``vault/<output.directory>/``."""
    vault_path_str = (raw.get("vault", {}) or {}).get("path", "") or ""
    if not vault_path_str:
        return CheckResult(
            name="output-dir",
            status=Status.FAIL,
            detail="vault.path not set",
        )
    output = brief.get("output", {}) or {}
    rel = output.get("directory", "run")
    full = Path(vault_path_str) / rel
    if not full.exists():
        # The brief creates this at write time, so "missing" is OK —
        # flagged WARN only when vault itself is missing (handled above).
        return CheckResult(
            name="output-dir",
            status=Status.OK,
            detail=f"missing (will be created): {full}",
            data={"path": str(full), "exists": False},
        )
    if not os.access(full, os.W_OK):
        return CheckResult(
            name="output-dir",
            status=Status.FAIL,
            detail=f"not writable: {full}",
        )
    return CheckResult(
        name="output-dir",
        status=Status.OK,
        detail=str(full),
        data={"path": str(full), "exists": True},
    )


async def _check_weather_api(weather: dict, timeout: float) -> CheckResult:
    """Optional weather API probe.

    Stations list may be empty (weather section skipped in brief).
    In that case return SKIP rather than FAIL — no need to probe an
    endpoint the brief won't use.
    """
    import httpx  # base dep

    stations = weather.get("stations") or []
    if not stations:
        return CheckResult(
            name="weather-api",
            status=Status.SKIP,
            detail="no stations configured",
        )

    api_base = weather.get("api_base", "https://aviationweather.gov/api/data")
    url = api_base.rstrip("/") + "/"  # root doc endpoint
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="weather-api",
            status=Status.WARN,
            detail=f"unreachable: {exc.__class__.__name__}: {str(exc)[:120]}",
            data={"url": url},
        )
    return CheckResult(
        name="weather-api",
        status=Status.OK,
        detail=f"HTTP {resp.status_code}",
        data={"url": url, "status_code": resp.status_code},
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run brief health checks."""
    brief = raw.get("brief")
    if brief is None:
        return ToolHealth(
            tool="brief",
            status=Status.SKIP,
            detail="no brief section in config",
        )

    timeout = 3.0 if mode == "quick" else 8.0

    results: list[CheckResult] = []
    results.extend(_check_schedule(brief.get("schedule", {}) or {}))
    results.append(_check_output_dir(raw, brief))
    results.append(await _check_weather_api(brief.get("weather", {}) or {}, timeout))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="brief", status=status, results=results)


register_check("brief", health_check)
