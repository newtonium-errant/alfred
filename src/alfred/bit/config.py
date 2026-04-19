"""BIT configuration — typed dataclasses + ``load_from_unified``.

Schedule resolution precedence (plan Part 11 Q1):
    1. ``bit.schedule.time`` (explicit override, HH:MM)
    2. ``brief.schedule.time`` minus ``bit.schedule.lead_minutes`` (default 5)
    3. Hardcoded fallback ``"05:55"`` (brief default 06:00 − 5 min)

Timezone similarly falls back to the brief's TZ, then to
``America/Halifax`` — matches the existing brief defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_LEAD_MINUTES = 5
DEFAULT_TIMEZONE = "America/Halifax"
# Hardcoded BIT time when no brief config exists (06:00 − 5 min).
DEFAULT_FALLBACK_TIME = "05:55"


@dataclass
class ScheduleConfig:
    """BIT scheduling config.

    ``time`` — explicit HH:MM override. Empty string means "derive from
    brief.schedule.time minus lead_minutes".
    ``lead_minutes`` — offset before the brief; default 5.
    ``timezone`` — timezone id (falls back to brief.schedule.timezone).
    ``mode`` — ``"quick"`` or ``"full"``. Quickstart sets this to
    ``"quick"`` for fresh installs (plan Part 11 Q6).
    """

    time: str = ""
    lead_minutes: int = DEFAULT_LEAD_MINUTES
    timezone: str = DEFAULT_TIMEZONE
    mode: str = "quick"


@dataclass
class OutputConfig:
    """Where the BIT daemon writes its run record."""

    directory: str = "process"
    name_template: str = "Alfred BIT {date}"


@dataclass
class StateConfig:
    """BIT state file path."""

    path: str = "./data/bit_state.json"
    max_history: int = 30


@dataclass
class BITConfig:
    vault_path: str = ""
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    state: StateConfig = field(default_factory=StateConfig)
    log_file: str = "./data/bit.log"


def _compute_scheduled_time(
    bit_time: str,
    brief_time: str,
    lead_minutes: int,
) -> str:
    """Resolve the actual HH:MM the BIT should run at.

    Explicit ``bit_time`` wins. Otherwise subtract ``lead_minutes``
    from ``brief_time``. If neither is available, return the hardcoded
    fallback. Invalid input falls back rather than raising — a config
    error shouldn't stop the daemon from starting; it surfaces via
    the BIT health check itself.
    """
    if bit_time:
        return bit_time
    if not brief_time:
        return DEFAULT_FALLBACK_TIME
    try:
        h_str, m_str = brief_time.split(":")
        hour = int(h_str)
        minute = int(m_str)
    except (ValueError, AttributeError):
        return DEFAULT_FALLBACK_TIME
    total = hour * 60 + minute - lead_minutes
    # Wrap around midnight gracefully
    total %= 24 * 60
    new_h, new_m = divmod(total, 60)
    return f"{new_h:02d}:{new_m:02d}"


def load_from_unified(raw: dict[str, Any]) -> BITConfig:
    """Build ``BITConfig`` from the unified config dict."""
    section = raw.get("bit", {}) or {}
    brief = raw.get("brief", {}) or {}
    vault_path = (raw.get("vault", {}) or {}).get("path", "./vault")
    log_dir = (raw.get("logging", {}) or {}).get("dir", "./data")

    schedule_raw = section.get("schedule", {}) or {}
    brief_schedule = brief.get("schedule", {}) or {}

    lead_minutes = int(schedule_raw.get("lead_minutes", DEFAULT_LEAD_MINUTES))
    explicit_time = schedule_raw.get("time", "") or ""
    brief_time = brief_schedule.get("time", "") or ""
    tz = (
        schedule_raw.get("timezone")
        or brief_schedule.get("timezone")
        or DEFAULT_TIMEZONE
    )
    mode = schedule_raw.get("mode", "quick")

    resolved_time = _compute_scheduled_time(explicit_time, brief_time, lead_minutes)

    schedule = ScheduleConfig(
        time=resolved_time,
        lead_minutes=lead_minutes,
        timezone=tz,
        mode=mode,
    )

    output_raw = section.get("output", {}) or {}
    output = OutputConfig(
        directory=output_raw.get("directory", "process"),
        name_template=output_raw.get("name_template", "Alfred BIT {date}"),
    )

    state_raw = section.get("state", {}) or {}
    state = StateConfig(
        path=state_raw.get("path", f"{log_dir}/bit_state.json"),
        max_history=int(state_raw.get("max_history", 30)),
    )

    return BITConfig(
        vault_path=vault_path,
        schedule=schedule,
        output=output,
        state=state,
        log_file=f"{log_dir}/bit.log",
    )
