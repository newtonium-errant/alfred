"""Routine daemon configuration.

Mirrors ``brief/config.py`` / ``bit/config.py`` — typed dataclasses,
``load_from_unified`` builder, hand-rolled construction (we avoid the
generic ``_build`` helper to sidestep the ``_DATACLASS_MAP`` collision
+ empty-dict traps per project CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alfred.common.schedule import ScheduleConfig


DEFAULT_TIMEZONE = "America/Halifax"
# Fires one minute before the brief (06:00 default) so the brief at
# 06:00 reads the freshly-written daily aggregator note. Mirrors the
# BIT default-lead pattern (BIT at 05:55, routine at 05:59).
DEFAULT_TIME = "05:59"
# Per ``feedback_hardcoding_and_alfred_naming.md``: the salem-only
# guarantee is an instance check, not a literal — the daemon-start
# guard reads ``config.telegram.instance.name`` and refuses to start
# on any other instance. ``REQUIRED_INSTANCE`` is the canonical
# normalised form (``_normalize_instance_name`` output) that passes
# the guard.
REQUIRED_INSTANCE = "salem"


@dataclass
class OutputConfig:
    """Where the aggregator writes the daily summary note.

    The default ``daily/`` directory is the operator-facing landing
    page for today's routines + (eventually) other day-scoped content.
    Janitor should NOT scan this directory (the file is derivative);
    operator config wires ``vault.dont_scan_dirs`` accordingly.
    """

    directory: str = "daily"
    name_template: str = "{date}"


@dataclass
class StateConfig:
    """State file path — tracks per-day write history for status output."""

    path: str = "./data/routine_state.json"
    max_history: int = 30


@dataclass
class RoutineConfig:
    """Top-level routine daemon config.

    ``vault_path`` and ``log_file`` are populated from the unified
    top-level ``vault.path`` / ``logging.dir`` blocks; everything else
    has dataclass defaults so an empty ``routine: {}`` block in
    config.yaml works.

    ``instance_name`` carries the normalised peer-key form of
    ``telegram.instance.name`` — pre-resolved here so the daemon-start
    guard doesn't have to re-import the telegram-compat helper.
    """

    vault_path: str = ""
    enabled: bool = True
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    state: StateConfig = field(default_factory=StateConfig)
    log_file: str = "./data/routine.log"
    instance_name: str = ""


def load_from_unified(raw: dict[str, Any]) -> RoutineConfig:
    """Build ``RoutineConfig`` from the unified config dict."""
    section = raw.get("routine", {}) or {}
    vault_path = (raw.get("vault") or {}).get("path", "./vault")
    log_dir = (raw.get("logging") or {}).get("dir", "./data")

    schedule_raw = section.get("schedule", {}) or {}
    schedule = ScheduleConfig(
        time=schedule_raw.get("time", DEFAULT_TIME),
        timezone=schedule_raw.get("timezone", DEFAULT_TIMEZONE),
    )

    output_raw = section.get("output", {}) or {}
    output = OutputConfig(
        directory=output_raw.get("directory", "daily"),
        name_template=output_raw.get("name_template", "{date}"),
    )

    state_raw = section.get("state", {}) or {}
    state = StateConfig(
        path=state_raw.get("path", f"{log_dir}/routine_state.json"),
        max_history=int(state_raw.get("max_history", 30)),
    )

    # Resolve instance name via the canonical normaliser. Empty when
    # the operator omitted ``telegram.instance.name`` from config —
    # the daemon-start guard refuses to start in that case (rather
    # than silently treating an unset instance as Salem). Per
    # ``feedback_hardcoding_and_alfred_naming.md`` we fail-loud on
    # missing instance name.
    from alfred.telegram._compat import _normalize_instance_name
    telegram_raw = raw.get("telegram") or {}
    instance_raw = telegram_raw.get("instance") or {}
    raw_name = ""
    if isinstance(instance_raw, dict):
        raw_name = str(instance_raw.get("name") or "")
    instance_name = _normalize_instance_name(raw_name)

    return RoutineConfig(
        vault_path=vault_path,
        enabled=bool(section.get("enabled", True)),
        schedule=schedule,
        output=output,
        state=state,
        log_file=f"{log_dir}/routine.log",
        instance_name=instance_name,
    )


__all__ = [
    "RoutineConfig",
    "OutputConfig",
    "StateConfig",
    "DEFAULT_TIME",
    "DEFAULT_TIMEZONE",
    "REQUIRED_INSTANCE",
    "load_from_unified",
]
