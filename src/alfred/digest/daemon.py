"""Digest daemon — fires the weekly digest at the configured slot.

Mirrors the Daily Sync daemon's shape: load config, compute next
fire, drift-bounded ``sleep_until``, fire, repeat. ``ScheduleConfig``
carries ``day_of_week`` so this naturally fires once per week.

The daemon DOES NOT start automatically unless ``digest.enabled`` is
true in config. The orchestrator's ``TOOL_RUNNERS`` entry exits 78
when disabled (matching every other optional daemon).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from alfred.common.schedule import compute_next_fire, sleep_until

from .config import DigestConfig
from .writer import resolve_repo_paths, write_digest

log = structlog.get_logger(__name__)


async def fire_once(
    config: DigestConfig, raw: dict[str, Any], *, now: datetime | None = None,
) -> dict[str, Any]:
    """Run one digest write cycle. Returns a summary dict."""
    if now is None:
        now = datetime.now(ZoneInfo(config.schedule.timezone))
    project_paths = resolve_repo_paths(raw)
    output_dir = Path(config.output_dir)
    out_path, body, payload = write_digest(
        output_dir=output_dir,
        project_paths=project_paths,
        today=now,
        window_days=config.window_days,
    )
    log.info(
        "digest.fired",
        path=str(out_path),
        decisions=len(payload.decisions),
        promotions=len(payload.promotions),
        open_questions=len(payload.open_questions),
        recurrences=len(payload.recurrences),
        byte_count=len(body.encode("utf-8")),
    )
    return {
        "ok": True,
        "path": str(out_path),
        "decisions_count": len(payload.decisions),
        "promotions_count": len(payload.promotions),
        "open_questions_count": len(payload.open_questions),
        "recurrences_count": len(payload.recurrences),
    }


async def run_daemon(config: DigestConfig, raw: dict[str, Any]) -> None:
    """Main loop — one fire per week at the configured slot."""
    log.info(
        "digest.daemon.starting",
        schedule_time=config.schedule.time,
        tz=config.schedule.timezone,
        day_of_week=config.schedule.day_of_week,
        output_dir=config.output_dir,
        window_days=config.window_days,
    )
    while True:
        tz = ZoneInfo(config.schedule.timezone)
        now = datetime.now(tz)
        target = compute_next_fire(config.schedule, now)
        sleep_seconds = (target - now).total_seconds()
        if sleep_seconds > 0:
            log.info(
                "digest.daemon.sleeping",
                next_run=target.isoformat(),
                sleep_seconds=round(sleep_seconds, 1),
                sleep_hours=round(sleep_seconds / 3600, 1),
            )
            actual_seconds = await sleep_until(target)
            log.info(
                "digest.daemon.woke",
                intended_seconds=round(sleep_seconds, 1),
                actual_seconds=round(actual_seconds, 1),
                drift_seconds=round(actual_seconds - sleep_seconds, 1),
            )
        try:
            await fire_once(config, raw, now=datetime.now(tz))
        except Exception:  # noqa: BLE001
            log.exception("digest.daemon.fire_error")
        # Sleep 90s past fire so the next ``compute_next_fire`` lands
        # on the following week, not the current minute.
        await asyncio.sleep(90)
