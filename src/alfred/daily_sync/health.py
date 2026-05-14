"""Daily Sync health check — registered with the BIT aggregator.

Probes (in order):
  * daily_sync section present in unified config — if absent, return
    SKIP (graceful for instances that don't run daily_sync, e.g.
    Hypatia today)
  * ``daily_sync.enabled`` flag — if False, return SKIP at the
    ToolHealth level so the operator sees an explicit "off by config"
    rather than a green OK that misrepresents intent
  * ``schedule-time`` — HH:MM parseable
  * ``schedule-timezone`` — resolvable via zoneinfo
  * ``state-path`` — resolves cleanly (parent dir writable when the
    file doesn't exist; readable when it does)
  * ``last-successful-fire`` — daemon-liveness signal (the bug-of-
    record class the cross-daemon BIT-probe arc closed). Reads
    ``last_fired_date`` from the state file and compares against
    today in the configured timezone.

Closes the cross-daemon silent-failure observability sweep —
brief / curator / surveyor / janitor / distiller all got their
last-successful-* probes earlier today; daily_sync mirrors the
same pattern. Per ``feedback_intentionally_left_blank.md``:
silence (daily_sync daemon idle, no morning-message log activity,
operator notices missing 09:00 ADT Telegram message) is ambiguous
between healthy-quiet and broken; the probe disambiguates.
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


def _check_schedule(schedule: dict) -> list[CheckResult]:
    """Validate schedule.time (HH:MM) and schedule.timezone.

    Mirrors ``alfred.brief.health._check_schedule`` exactly so the
    cross-daemon BIT output stays uniform.
    """
    out: list[CheckResult] = []

    time_str = schedule.get("time", "09:00")
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


def _resolve_state_path(daily_sync: dict) -> Path:
    """Resolve daily_sync's state-file path the same way
    ``alfred.daily_sync.config.load_from_unified`` does — explicit
    ``daily_sync.state.path`` wins, otherwise the dataclass default
    ``./data/daily_sync_state.json``.

    Per-instance scope: KAL-LE / Salem / Hypatia configure their own
    state paths in their per-instance YAML; the probe consults
    whatever the unified config produces, no hardcoded literals.
    """
    state_section = (daily_sync.get("state", {}) or {})
    explicit = state_section.get("path", "")
    if explicit:
        return Path(explicit)
    return Path("./data/daily_sync_state.json")


def _check_state_path(state_path: Path) -> CheckResult:
    """State file probe: present + readable, or absent + parent
    writable (fresh-install path).

    Mirrors ``brief.health._check_output_dir``'s tolerance — a
    missing state file at fresh-install time is OK (the daemon
    creates it on first fire); only "exists but unreadable" or
    "parent dir not writable" warrant a non-OK status.
    """
    if state_path.is_file():
        if not os.access(state_path, os.R_OK):
            return CheckResult(
                name="state-path",
                status=Status.WARN,
                detail=f"state file not readable: {state_path}",
                data={"path": str(state_path), "exists": True},
            )
        return CheckResult(
            name="state-path",
            status=Status.OK,
            detail=str(state_path),
            data={"path": str(state_path), "exists": True},
        )
    parent = state_path.parent if str(state_path.parent) else Path(".")
    if not parent.exists():
        return CheckResult(
            name="state-path",
            status=Status.WARN,
            detail=f"parent dir missing (will be created): {parent}",
            data={"path": str(state_path), "exists": False},
        )
    if not os.access(parent, os.W_OK):
        return CheckResult(
            name="state-path",
            status=Status.FAIL,
            detail=f"parent dir not writable: {parent}",
            data={"path": str(state_path), "exists": False},
        )
    return CheckResult(
        name="state-path",
        status=Status.OK,
        detail=f"missing (will be created): {state_path}",
        data={"path": str(state_path), "exists": False},
    )


def _read_last_fired_date(state_path: Path) -> str | None:
    """Read daily_sync state file and return its top-level
    ``last_fired_date`` ISO date string. Returns None on missing file
    / missing field / unparseable JSON / non-string value.

    Inline dict-walk rather than constructing daily_sync state
    machinery — the probe is consume-only, and a malformed state
    file should produce a graceful SKIP rather than crash the BIT
    run mid-sweep. Mirrors the precedent established by
    ``brief.health._most_recent_successful_brief_date``.
    """
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    last_fired = data.get("last_fired_date", "")
    if isinstance(last_fired, str) and last_fired:
        return last_fired
    return None


def _read_last_error(state_path: Path) -> dict | None:
    """Read daily_sync state file and return the ``last_error`` payload
    (shape: ``{"ts": iso_string, "message": str}``) or None when
    absent / unreadable / corrupted-shape.

    Same defensive-read posture as ``_read_last_fired_date`` — a
    corrupt state file degrades silently to None so the probe still
    runs the date-based threshold check rather than crashing. Mirrors
    ``brief.health._read_last_error`` (2026-05-14).
    """
    if not state_path.is_file():
        return None
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    err = data.get("last_error")
    if not isinstance(err, dict):
        return None
    msg = err.get("message")
    if not isinstance(msg, str) or not msg:
        return None
    return err


def _check_last_successful_fire(
    raw: dict[str, Any],
    daily_sync: dict,
) -> CheckResult:
    """Validate that daily_sync fired recently.

    daily_sync writes ``state["last_fired_date"]`` (ISO date string,
    not full timestamp) at the end of each successful fire. Probe
    compares against today in the configured timezone:

      * SKIP if state file missing (fresh install) / no
        ``last_fired_date`` field / unparseable date / unresolvable
        timezone (the schedule-timezone probe is the canonical
        surface for the last)
      * OK   if last_fired_date is today OR yesterday (BIT runs at
        05:55 ADT before daily_sync at 09:00 ADT — yesterday's fire
        is the freshest possible value at probe time; today's
        last_fired_date appears if the operator manually re-fired)
      * WARN if last_fired_date is day-before-yesterday (one missed
        run — could be a transient hiccup)
      * FAIL if older than 2 days (multi-day silent failure pattern)

    "Today" / "yesterday" computed in
    ``daily_sync.schedule.timezone`` per the brief precedent —
    daily_sync writes ``today_iso`` (line 297 of daemon.py) using
    ``today.isoformat()`` where ``today`` is the local date in the
    configured timezone, NOT UTC.

    Per ``feedback_intentionally_left_blank.md``: this is the
    operator-visible signal. Operator currently notices a missing
    09:00 ADT Telegram message; the probe surfaces the failure to
    BIT instead so the morning brief carries the warning.
    """
    state_path = _resolve_state_path(daily_sync)
    most_recent = _read_last_fired_date(state_path)

    schedule = daily_sync.get("schedule", {}) or {}
    tz_name = schedule.get("timezone", "America/Halifax")
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return CheckResult(
            name="last-successful-fire",
            status=Status.SKIP,
            detail=f"timezone {tz_name!r} unresolvable (see schedule-timezone)",
        )

    today_local = datetime.now(tz).date()
    yesterday = today_local - timedelta(days=1)
    day_before = today_local - timedelta(days=2)

    if most_recent is None:
        if not state_path.is_file():
            return CheckResult(
                name="last-successful-fire",
                status=Status.SKIP,
                detail=f"no state file (fresh install): {state_path}",
                data={"state_path": str(state_path), "exists": False},
            )
        return CheckResult(
            name="last-successful-fire",
            status=Status.SKIP,
            detail="no last_fired_date recorded yet",
            data={"state_path": str(state_path), "exists": True},
        )

    try:
        most_recent_d = date.fromisoformat(most_recent)
    except ValueError:
        return CheckResult(
            name="last-successful-fire",
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

    # Build the WARN/FAIL error suffix. Capped at 150 chars so the BIT
    # line stays a single readable row. Full structured error always
    # rides in ``result.data["last_error"]`` for JSON consumers
    # regardless of the cap. OK status path skips the suffix because
    # last_error is wiped on success — a stale entry surviving past a
    # successful fire would only happen if an operator hand-edited
    # the state file, which the defensive _read_last_error has
    # already filtered for.
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
        # today_local OR yesterday — both are healthy at BIT-time.
        return CheckResult(
            name="last-successful-fire",
            status=Status.OK,
            detail=f"last fire: {most_recent} ({days_old}d ago)",
            data=payload,
        )
    if most_recent_d == day_before:
        return CheckResult(
            name="last-successful-fire",
            status=Status.WARN,
            detail=f"last fire: {most_recent} (2d ago — one missed run){error_suffix}",
            data=payload,
        )
    return CheckResult(
        name="last-successful-fire",
        status=Status.FAIL,
        detail=(
            f"last fire: {most_recent} ({days_old}d ago — "
            f"daemon may be silently failing){error_suffix}"
        ),
        data=payload,
    )


async def health_check(raw: dict[str, Any], mode: str = "quick") -> ToolHealth:
    """Run daily_sync health checks.

    Two gates apply at the ToolHealth level (return early with SKIP
    rather than emit a row):

      1. ``daily_sync`` section absent in unified config — instance
         doesn't run daily_sync (Hypatia case). Returns SKIP with a
         "no daily_sync section in config" detail.
      2. ``daily_sync.enabled: false`` — operator opted out
         intentionally. Returns SKIP with an "explicitly disabled"
         detail so a green OK doesn't misrepresent intent.

    Otherwise the rollup returns the worst-of the per-probe results.
    """
    daily_sync = raw.get("daily_sync")
    if daily_sync is None:
        return ToolHealth(
            tool="daily_sync",
            status=Status.SKIP,
            detail="no daily_sync section in config",
        )
    if not isinstance(daily_sync, dict):
        return ToolHealth(
            tool="daily_sync",
            status=Status.SKIP,
            detail=f"daily_sync section is not a dict: {type(daily_sync).__name__}",
        )
    if daily_sync.get("enabled", False) is False:
        return ToolHealth(
            tool="daily_sync",
            status=Status.SKIP,
            detail="daily_sync.enabled is false (explicitly disabled)",
        )

    results: list[CheckResult] = []
    results.extend(_check_schedule(daily_sync.get("schedule", {}) or {}))
    state_path = _resolve_state_path(daily_sync)
    results.append(_check_state_path(state_path))
    results.append(_check_last_successful_fire(raw, daily_sync))

    status = Status.worst([r.status for r in results])
    return ToolHealth(tool="daily_sync", status=status, results=results)


# Registration side-effect at import time — the aggregator imports
# this module in ``_load_tool_checks`` to populate its registry.
register_check("daily_sync", health_check)
