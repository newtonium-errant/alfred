"""BIT daemon — run the health sweep on schedule and write a vault record.

Mirrors the brief daemon's scheduler pattern (sleep until target time,
run, sleep 60s to avoid double-fire). The daemon writes directly to
the vault without setting ``ALFRED_VAULT_SCOPE`` — unscoped writes
bypass ``check_scope`` (see ``vault/scope.py`` line 178), which is the
intended behavior per plan Part 11 Q7.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from alfred.brief.renderer import serialize_record
from alfred.health.aggregator import run_all_checks
from alfred.health.types import Status

from .config import BITConfig
from .renderer import _tool_counts, render_bit_record
from .state import BITRun, StateManager

log = structlog.get_logger(__name__)


async def run_bit_once(
    config: BITConfig,
    raw: dict[str, Any],
    state_mgr: StateManager,
) -> tuple[str, Status]:
    """Execute one BIT run — probe all tools, write the record.

    Returns the vault-relative path of the record and the overall status.
    Safe to call from both the scheduler loop and ``alfred bit run-now``.
    """
    today = date.today().isoformat()
    log.info("bit.running", date=today, mode=config.schedule.mode)

    report = await run_all_checks(raw, mode=config.schedule.mode)

    # Write the vault record
    vault_path = Path(config.vault_path)
    frontmatter, body = render_bit_record(report, today, config)
    content = serialize_record(frontmatter, body)

    name = config.output.name_template.replace("{date}", today)
    rel_path = f"{config.output.directory}/{name}.md"
    file_path = vault_path / rel_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")

    log.info("bit.written", path=rel_path, status=report.overall_status.value)

    # Update state
    state_mgr.state.add_run(
        BITRun(
            date=today,
            generated_at=datetime.now(timezone.utc).isoformat(),
            vault_path=rel_path,
            overall_status=report.overall_status.value,
            mode=config.schedule.mode,
            tool_counts=_tool_counts(report),
        ),
        max_history=config.state.max_history,
    )
    state_mgr.save()

    return rel_path, report.overall_status


def _next_run_time(schedule_time: str, tz_name: str) -> datetime:
    """Next datetime at which to run."""
    tz = ZoneInfo(tz_name)
    now = datetime.now(tz)
    hour, minute = map(int, schedule_time.split(":"))
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def run_daemon(config: BITConfig, raw: dict[str, Any]) -> None:
    """BIT scheduler daemon. Runs at configured time daily."""
    log.info(
        "bit.daemon.starting",
        schedule_time=config.schedule.time,
        tz=config.schedule.timezone,
        mode=config.schedule.mode,
    )

    state_mgr = StateManager(config.state.path)
    state_mgr.load()

    while True:
        tz = ZoneInfo(config.schedule.timezone)
        target = _next_run_time(config.schedule.time, config.schedule.timezone)
        now = datetime.now(tz)
        sleep_seconds = (target - now).total_seconds()

        if sleep_seconds > 0:
            log.info(
                "bit.daemon.sleeping",
                next_run=target.isoformat(),
                sleep_hours=round(sleep_seconds / 3600, 2),
            )
            await asyncio.sleep(sleep_seconds)

        try:
            path, status = await run_bit_once(config, raw, state_mgr)
            log.info("bit.daemon.ran", path=path, status=status.value)
        except Exception:  # noqa: BLE001
            log.exception("bit.daemon.error")

        # Sleep 60s so we don't double-fire within the same minute
        await asyncio.sleep(60)
