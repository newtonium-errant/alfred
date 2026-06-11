"""BIT daemon — run the health sweep on schedule and write a vault record.

Mirrors the brief daemon's scheduler pattern (sleep until target time,
run, sleep 60s to avoid double-fire). The daemon writes directly to
the vault without setting ``ALFRED_VAULT_SCOPE`` — unscoped writes
bypass ``check_scope`` (see ``vault/scope.py`` line 178), which is the
intended behavior per plan Part 11 Q7.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from alfred.brief.renderer import serialize_record
from alfred.common.schedule import (
    ScheduleConfig as CommonScheduleConfig,
    compute_next_fire,
    sleep_until,
)
from alfred.health.aggregator import run_all_checks
from alfred.health.types import Status

from .config import BITConfig
from .renderer import (
    _tool_counts,
    process_hub_name,
    render_bit_record,
    render_process_hub_record,
)
from .state import BITRun, StateManager

log = structlog.get_logger(__name__)


def ensure_process_hub(
    vault_path: Path,
    config: BITConfig,
    date_str: str,
) -> bool:
    """Create the BIT process hub note if it doesn't exist yet.

    Every BIT run record carries ``process: [[process/<hub>]]`` — with
    no hub note, the janitor flags LINK001 daily and (having no create
    scope) can never self-heal it. The BIT daemon already writes the
    vault directly/unscoped by design (see module docstring), so the
    writer owns the hub's existence.

    Returns True when the hub was created, False when it already
    existed or the create failed. The existing-hub path doesn't log —
    the hub's existence is vault-observable; only the CREATE and FAIL
    events are signal. Failure is loud (warning) but never fatal: the
    BIT record write must still proceed, and the janitor's LINK001
    keeps flagging until the hub exists, so the failure is doubly
    visible.
    """
    hub_name = process_hub_name(config.output.name_template)
    hub_path = vault_path / "process" / f"{hub_name}.md"
    if hub_path.exists():
        return False
    frontmatter, body = render_process_hub_record(hub_name, date_str)
    content = serialize_record(frontmatter, body)
    try:
        hub_path.parent.mkdir(parents=True, exist_ok=True)
        hub_path.write_text(content, encoding="utf-8")
    except OSError as exc:
        log.warning(
            "bit.process_hub_create_failed",
            path=str(hub_path),
            error=str(exc),
            error_type=exc.__class__.__name__,
        )
        return False
    log.info("bit.process_hub_created", path=str(hub_path))
    return True


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
    # Ensure the process hub the record's ``process`` field links to
    # exists — the janitor has no create scope, so it can never
    # self-heal the dangling link (LINK001) if we don't.
    ensure_process_hub(vault_path, config, today)
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
    """Next datetime at which to run.

    Thin wrapper over ``alfred.common.schedule.compute_next_fire`` kept
    for the existing ``_next_run_time`` test surface; new callers should
    use ``compute_next_fire`` directly.
    """
    tz = ZoneInfo(tz_name)
    return compute_next_fire(
        CommonScheduleConfig(time=schedule_time, timezone=tz_name),
        datetime.now(tz),
    )


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

    # Adapter to the shared scheduling primitive. BIT is daily-only;
    # ``day_of_week`` stays None.
    common_schedule = CommonScheduleConfig(
        time=config.schedule.time,
        timezone=config.schedule.timezone,
    )

    while True:
        tz = ZoneInfo(config.schedule.timezone)
        now = datetime.now(tz)
        # Clock-aligned next-fire via shared helper (see
        # ``alfred.common.schedule``). Daily-only for BIT.
        target = compute_next_fire(common_schedule, now)
        sleep_seconds = (target - now).total_seconds()

        if sleep_seconds > 0:
            log.info(
                "bit.daemon.sleeping",
                next_run=target.isoformat(),
                sleep_seconds=round(sleep_seconds, 1),
                sleep_hours=round(sleep_seconds / 3600, 2),
            )
            # Wall-clock-checked chunked sleep — defends against
            # monotonic clock drift during long sleeps (WSL2 host
            # suspend/resume, NTP adjustments). See
            # ``alfred.common.schedule.sleep_until`` for the rationale.
            actual_seconds = await sleep_until(target)
            log.info(
                "bit.daemon.woke",
                intended_seconds=round(sleep_seconds, 1),
                actual_seconds=round(actual_seconds, 1),
                drift_seconds=round(actual_seconds - sleep_seconds, 1),
            )

        try:
            path, status = await run_bit_once(config, raw, state_mgr)
            log.info("bit.daemon.ran", path=path, status=status.value)
        except Exception:  # noqa: BLE001
            log.exception("bit.daemon.error")

        # Sleep 60s so we don't double-fire within the same minute.
        # Short-horizon: not subject to the long-sleep drift bug, so a
        # plain ``asyncio.sleep`` is fine here.
        await asyncio.sleep(60)
