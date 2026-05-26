"""Routine daemon — fires once daily, builds tomorrow's daily aggregator note.

Schedules at ``config.schedule.time`` (default 05:59 Halifax) using the
shared ``alfred.common.schedule`` primitive. Mirrors the BIT / brief
daemon shape (sleep until target, run, sleep 60s to avoid double-fire).

Salem-only by daemon-start guard: if
``config.telegram.instance.name != "Salem"`` (after normalisation), the
daemon refuses to start and exits with the missing-deps code (78) so
the orchestrator's auto-restart loop skips it cleanly. Per
``feedback_intentionally_left_blank.md`` the refusal emits a structured
``routine.daemon.start_blocked`` event so operators on non-Salem
instances see the deliberate skip rather than a silent absence.

Aggregator state is stale-tolerated: mid-day routine record edits do
NOT update today's note. The daemon writes once per day; ``alfred
routine done`` mutates ``completion_log`` on the routine record (read
by tomorrow's aggregator). Phase 2 may add a ``refresh`` verb.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import structlog

from alfred.common.schedule import compute_next_fire, sleep_until

from .aggregator import run_aggregator_once
from .config import REQUIRED_INSTANCE, RoutineConfig
from .state import StateManager

log = structlog.get_logger(__name__)

# Same sentinel the orchestrator uses for "missing deps, don't restart."
# Kept as a local constant rather than imported so the routine module
# has no orchestrator dependency.
_MISSING_DEPS_EXIT = 78


def _check_instance_guard(config: RoutineConfig) -> bool:
    """Return True iff this instance is allowed to run the daemon.

    Emits the deliberate-skip log when refusing. Per
    ``feedback_intentionally_left_blank.md`` and the Plan's "daemon-start
    guard" requirement — the empty-string case (operator omitted
    ``telegram.instance.name``) is treated the same as wrong-instance.
    """
    if not config.instance_name:
        log.warning(
            "routine.daemon.start_blocked",
            reason="missing_instance_name",
            detail=(
                "telegram.instance.name not configured — the routine "
                "daemon is Salem-only and cannot determine the current "
                "instance. Add ``telegram.instance.name`` to config.yaml."
            ),
        )
        return False
    if config.instance_name != REQUIRED_INSTANCE:
        log.warning(
            "routine.daemon.start_blocked",
            reason="non_salem_instance",
            instance_name=config.instance_name,
            required=REQUIRED_INSTANCE,
            detail=(
                "routine daemon is Salem-only in Phase 1 — non-Salem "
                "instances should omit the routine: block from their "
                "config.<instance>.yaml. Exiting with code 78 so the "
                "orchestrator skips auto-restart."
            ),
        )
        return False
    return True


async def run_daemon(config: RoutineConfig) -> None:
    """Daily routine aggregator loop. Fires at ``config.schedule.time``."""
    if not _check_instance_guard(config):
        sys.exit(_MISSING_DEPS_EXIT)

    if not config.enabled:
        log.warning(
            "routine.daemon.disabled_in_config",
            detail=(
                "routine block present but enabled=false. Exiting 78 — "
                "orchestrator's auto-restart will skip this daemon."
            ),
        )
        sys.exit(_MISSING_DEPS_EXIT)

    log.info(
        "routine.daemon.starting",
        schedule_time=config.schedule.time,
        tz=config.schedule.timezone,
        vault_path=config.vault_path,
    )

    state_mgr = StateManager(config.state.path)
    state_mgr.load()

    while True:
        tz = ZoneInfo(config.schedule.timezone)
        now = datetime.now(tz)
        target = compute_next_fire(config.schedule, now)
        sleep_seconds = (target - now).total_seconds()

        if sleep_seconds > 0:
            log.info(
                "routine.daemon.sleeping",
                next_run=target.isoformat(),
                sleep_seconds=round(sleep_seconds, 1),
                sleep_hours=round(sleep_seconds / 3600, 2),
            )
            actual_seconds = await sleep_until(target)
            log.info(
                "routine.daemon.woke",
                intended_seconds=round(sleep_seconds, 1),
                actual_seconds=round(actual_seconds, 1),
                drift_seconds=round(actual_seconds - sleep_seconds, 1),
            )

        try:
            today_local = datetime.now(tz).date()
            path = run_aggregator_once(config, today_local, state_mgr)
            log.info("routine.daemon.ran", path=path, date=today_local.isoformat())
        except Exception:  # noqa: BLE001
            log.exception("routine.daemon.error")

        # Sleep 60s so we don't double-fire within the same minute.
        await asyncio.sleep(60)
