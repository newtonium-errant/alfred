"""Digest daemon — fires the weekly digest at the configured slot.

Mirrors the Daily Sync daemon's shape: load config, compute next
fire, drift-bounded ``sleep_until``, fire, repeat. ``ScheduleConfig``
carries ``day_of_week`` so this naturally fires once per week.

The daemon DOES NOT start automatically unless ``digest.enabled`` is
true in config. The orchestrator's ``TOOL_RUNNERS`` entry exits 78
when disabled (matching every other optional daemon).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from alfred.common.scheduled_daemon import run_scheduled_daemon

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
        synthesis_vault=(
            Path(config.synthesis_vault) if config.synthesis_vault else None
        ),
        synthesis_top_n=config.synthesis_top_n,
        synthesis_weights=config.synthesis_weights or None,
    )
    log.info(
        "digest.fired",
        path=str(out_path),
        decisions=len(payload.decisions),
        promotions=len(payload.promotions),
        open_questions=len(payload.open_questions),
        recurrences=len(payload.recurrences),
        cross_arc_patterns=len(payload.cross_arc_patterns),
        byte_count=len(body.encode("utf-8")),
    )
    return {
        "ok": True,
        "path": str(out_path),
        "decisions_count": len(payload.decisions),
        "promotions_count": len(payload.promotions),
        "open_questions_count": len(payload.open_questions),
        "recurrences_count": len(payload.recurrences),
        "cross_arc_patterns_count": len(payload.cross_arc_patterns),
    }


async def run_daemon(config: DigestConfig, raw: dict[str, Any]) -> None:
    """Main loop — one fire per week at the configured slot.

    Delegates the canonical sleep/wake/fire/catch loop to
    ``alfred.common.scheduled_daemon.run_scheduled_daemon``; this
    function owns only the daemon-specific ``starting`` log event +
    the ``fire_once`` partial.
    """
    log.info(
        "digest.daemon.starting",
        schedule_time=config.schedule.time,
        tz=config.schedule.timezone,
        day_of_week=config.schedule.day_of_week,
        output_dir=config.output_dir,
        window_days=config.window_days,
    )

    async def _fire(now: datetime) -> Any:
        return await fire_once(config, raw, now=now)

    await run_scheduled_daemon(
        schedule=config.schedule,
        fire=_fire,
        log_namespace="digest.daemon",
        log=log,
    )
