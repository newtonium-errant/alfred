"""Brief health check.

Probes:
  * schedule.time parseable as HH:MM
  * schedule.timezone resolvable via zoneinfo
  * vault output directory writable
  * weather API reachable (quick HTTP probe; WARN on failure — the
    brief falls back to cached weather at runtime, so it's not FAIL)
  * last-successful-brief — daemon liveness validation (added
    2026-05-10 after a 10-day silent-failure incident: the brief
    daemon's ``except Exception:`` swallowed a TypeError on every
    clear day, no error surfaced to BIT until the operator noticed
    ``vault/run/`` was empty. Per the universal "intentionally left
    blank" / observability discipline — silence is ambiguous, so an
    explicit liveness probe distinguishes idle-healthy from broken.
    See ``feedback_intentionally_left_blank.md``.)

Brief is a scheduler — there's nothing token-expensive about its
preconditions. We keep the quick/full distinction lightweight.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alfred.health.aggregator import register_check
from alfred.health.types import CheckResult, Status, ToolHealth

from .utils import SectionReadStatus, safe_read_section_file


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

    We probe the same endpoint shape the real client uses
    (``{api_base}/metar?ids=<first-station>&format=json``) so a probe
    success proves the brief's actual request path works, not just that
    the domain resolves. Status mapping:

    * HTTP 200        → OK    (endpoint healthy)
    * HTTP 4xx        → WARN  (service reachable; probe URL or params
                               may be wrong but the API itself is up)
    * HTTP 5xx        → FAIL  (service is broken upstream)
    * timeout / conn  → FAIL  (DNS / network / dead endpoint)
    """
    import httpx  # base dep

    stations = weather.get("stations") or []
    if not stations:
        return CheckResult(
            name="weather-api",
            status=Status.SKIP,
            detail="no stations configured",
        )

    # Use the first configured station — matches what the real client
    # does at runtime (see brief/weather.py::fetch_metars).
    first_id = ""
    first = stations[0]
    if isinstance(first, dict):
        first_id = first.get("id", "") or ""
    elif isinstance(first, str):
        first_id = first
    # Fall back to a known-good ICAO if the config entry is malformed —
    # we still want the probe to exercise the real endpoint.
    if not first_id:
        first_id = "KJFK"

    api_base = weather.get("api_base", "https://aviationweather.gov/api/data")
    url = f"{api_base.rstrip('/')}/metar?ids={first_id}&format=json"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="weather-api",
            status=Status.FAIL,
            detail=f"unreachable: {exc.__class__.__name__}: {str(exc)[:120]}",
            data={"url": url},
        )

    status_code = resp.status_code
    if 200 <= status_code < 300:
        status = Status.OK
    elif 400 <= status_code < 500:
        status = Status.WARN
    else:
        # 5xx (and any other non-2xx/non-4xx) — the upstream service is
        # unhealthy.
        status = Status.FAIL
    return CheckResult(
        name="weather-api",
        status=status,
        detail=f"HTTP {status_code}",
        data={"url": url, "status_code": status_code},
    )


def _resolve_state_path(raw: dict[str, Any], brief: dict) -> Path:
    """Mirror ``alfred.brief.config.load_from_unified``'s state-path
    resolution so the probe consults the same file the daemon writes.

    Resolution order (matches the loader):
      1. ``brief.state.path`` if explicitly set
      2. ``{logging.dir}/brief_state.json`` (logging.dir defaults to
         ``./data``)
    """
    state_section = brief.get("state", {}) or {}
    explicit = state_section.get("path", "")
    if explicit:
        return Path(explicit)
    log_dir = (raw.get("logging", {}) or {}).get("dir", "./data")
    return Path(f"{log_dir}/brief_state.json")


def _most_recent_successful_brief_date(state_path: Path) -> str | None:
    """Read ``brief_state.json`` and return the max ISO date string of
    any run with ``success=True``. Returns None if file missing /
    unparseable / no successful runs.

    Inlined dict-walking rather than constructing
    ``alfred.brief.state.StateManager`` to keep the health module's
    import surface minimal — this probe is only consuming, not
    persisting, and a malformed state file should produce a graceful
    N/A rather than crash the whole BIT run on a JSONDecodeError.
    """
    if not state_path.is_file():
        return None
    # Defensive read via the shared helper — the old ``(json.JSONDecodeError,
    # OSError)`` catch missed UnicodeDecodeError (a SIBLING of JSONDecodeError
    # under ValueError, not a subclass), so a non-UTF-8 state file escaped and
    # crashed the BIT run. Semantics preserved: read/decode failure → None.
    read = safe_read_section_file(state_path)
    if read.status is not SectionReadStatus.OK:
        return None
    try:
        data = json.loads(read.text)
    except json.JSONDecodeError:
        return None
    runs = data.get("runs", [])
    if not isinstance(runs, list):
        return None
    successful_dates: list[str] = []
    for r in runs:
        if not isinstance(r, dict):
            continue
        if not r.get("success", False):
            continue
        d = r.get("date", "")
        if isinstance(d, str) and d:
            successful_dates.append(d)
    if not successful_dates:
        return None
    return max(successful_dates)


def _read_last_error(state_path: Path) -> dict | None:
    """Read ``brief_state.json`` and return the ``last_error`` payload
    (shape: ``{"ts": iso_string, "message": str}``) or None when
    absent/unreadable.

    Same defensive-read posture as ``_most_recent_successful_brief_date``
    — a corrupt state file degrades silently to None so the probe still
    runs the date-based threshold check rather than crashing.
    """
    if not state_path.is_file():
        return None
    # Defensive read via the shared helper — same UnicodeDecodeError gap as
    # ``_most_recent_successful_brief_date``: a non-UTF-8 state file escaped
    # the ``(json.JSONDecodeError, OSError)`` catch. Semantics preserved:
    # read/decode failure → None (graceful N/A, probe still runs).
    read = safe_read_section_file(state_path)
    if read.status is not SectionReadStatus.OK:
        return None
    try:
        data = json.loads(read.text)
    except json.JSONDecodeError:
        return None
    err = data.get("last_error")
    if not isinstance(err, dict):
        return None
    msg = err.get("message")
    if not isinstance(msg, str) or not msg:
        return None
    return err


def _check_last_successful_brief(
    raw: dict[str, Any],
    brief: dict,
) -> CheckResult:
    """Validate that the brief daemon actually produced a brief recently.

    Status mapping:
      * SKIP if ``brief_state.json`` doesn't exist (fresh install — no
        runs yet; not an error)
      * SKIP if state file exists but has no successful runs (same
        rationale)
      * OK   if most-recent-success-date == yesterday in the brief's
        configured timezone (BIT runs at 05:55 ADT before the brief
        daemon at 06:00; today's brief doesn't exist at probe time)
      * WARN if most-recent-success-date == day-before-yesterday
        (one missed day — could be a transient API blip)
      * FAIL if older than that (multi-day silent failure — the exact
        pattern the 2026-04-30 → 2026-05-10 incident exhibited)

    "Yesterday" is computed in the brief's configured timezone
    (``brief.schedule.timezone``) NOT UTC — a brief generated at
    06:00 America/Halifax on 2026-05-09 has ``date == "2026-05-09"``,
    and a probe at 05:55 America/Halifax on 2026-05-10 should accept
    it as yesterday regardless of UTC offset.

    Per ``feedback_intentionally_left_blank.md``: this is the
    operator-visible signal that a daemon-level silent failure
    surfaces. Silence (``brief.daemon.fired`` keeps logging,
    ``vault/run/`` stays empty) is ambiguous between idle-healthy
    and broken; the probe disambiguates.
    """
    state_path = _resolve_state_path(raw, brief)
    most_recent = _most_recent_successful_brief_date(state_path)

    schedule = brief.get("schedule", {}) or {}
    tz_name = schedule.get("timezone", "America/Halifax")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        # Bad timezone — already FAILed by _check_schedule. Don't
        # double-fail; SKIP this probe so the operator sees the
        # canonical timezone error rather than two redundant ones.
        return CheckResult(
            name="last-successful-brief",
            status=Status.SKIP,
            detail=f"timezone {tz_name!r} unresolvable (see schedule-timezone)",
        )

    today_local = datetime.now(tz).date()
    yesterday = today_local - timedelta(days=1)
    day_before = today_local - timedelta(days=2)

    if most_recent is None:
        if not state_path.is_file():
            return CheckResult(
                name="last-successful-brief",
                status=Status.SKIP,
                detail=f"no state file (fresh install): {state_path}",
                data={"state_path": str(state_path), "exists": False},
            )
        return CheckResult(
            name="last-successful-brief",
            status=Status.SKIP,
            detail="no successful runs recorded yet",
            data={"state_path": str(state_path), "exists": True},
        )

    try:
        most_recent_d = date.fromisoformat(most_recent)
    except ValueError:
        return CheckResult(
            name="last-successful-brief",
            status=Status.SKIP,
            detail=f"unparseable date in state: {most_recent!r}",
            data={"state_path": str(state_path)},
        )

    days_old = (today_local - most_recent_d).days
    payload: dict[str, Any] = {
        "state_path": str(state_path),
        "most_recent_date": most_recent,
        "today_local": today_local.isoformat(),
        "days_old": days_old,
    }

    # WARN/FAIL details get the ``last_error.message`` suffix when
    # populated, so the BIT line carries the failure cause without the
    # operator grepping ``data/brief.log``. Capped at 150 chars to
    # keep the line readable. OK detail stays clean (no suffix) since
    # last_error is wiped on success. Added 2026-05-14 — closes the
    # diagnostic gap above the date-based threshold check that landed
    # 2026-05-10 (the probe could detect "no brief in N days" but not
    # say WHY).
    last_error = _read_last_error(state_path)
    if last_error is not None:
        message = last_error.get("message", "")
        if isinstance(message, str) and len(message) > 150:
            message = message[:147] + "..."
        payload["last_error"] = last_error
        error_suffix = f"; last error: {message}" if message else ""
    else:
        error_suffix = ""

    if most_recent_d >= yesterday:
        # most_recent_d could equal today (operator already ran
        # ``alfred brief generate`` manually after BIT) — that's
        # also OK; treat anything from yesterday-or-newer as healthy.
        return CheckResult(
            name="last-successful-brief",
            status=Status.OK,
            detail=f"last brief: {most_recent} ({days_old}d ago)",
            data=payload,
        )
    if most_recent_d == day_before:
        # One missed day — could be transient API blip. WARN, not FAIL.
        return CheckResult(
            name="last-successful-brief",
            status=Status.WARN,
            detail=f"last brief: {most_recent} (2d ago — one missed run){error_suffix}",
            data=payload,
        )
    # Multi-day silent failure — the bug class the 2026-04-30 → 05-10
    # incident demonstrated.
    return CheckResult(
        name="last-successful-brief",
        status=Status.FAIL,
        detail=f"last brief: {most_recent} ({days_old}d ago — daemon may be silently failing){error_suffix}",
        data=payload,
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
    results.append(_check_last_successful_brief(raw, brief))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="brief", status=status, results=results)


register_check("brief", health_check)
