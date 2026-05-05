"""Daily radar daemon — auto-fires Phase 3a's ``run_daily_radar`` on a
once-per-day schedule.

Mirrors the digest daemon's shape (one-fire-per-period schedule loop):
load config, compute next fire, drift-bounded ``sleep_until``, fire,
repeat. ``ScheduleConfig.day_of_week=None`` (the default) means daily.

The daemon DOES NOT start automatically unless
``distiller.radar_day.enabled`` is ``True``. The orchestrator's
``_run_radar_day`` entry exits 78 when disabled (matching every other
optional daemon).

Default fire 08:00 ADT — 1h ahead of KAL-LE's Daily Sync at 09:00 ADT
so the radar provider has a freshly-written daily file to read on the
morning fire.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from alfred.common.schedule import compute_next_fire, sleep_until

from .config import DistillerConfig
from .radar_day import run_daily_radar

log = structlog.get_logger(__name__)


def _resolve_dirs(
    config: DistillerConfig,
) -> tuple[Path, Path]:
    """Resolve ``(digests_dir, state_dir)`` from the distiller config.

    ``radar_day.digests_dir`` and ``radar_day.state_dir`` win when set;
    otherwise:
      * digests_dir = ``<vault>/digests`` (KAL-LE convention)
      * state_dir = parent of ``state.path``

    The Phase 3a CLI does the same fallback derivation in
    ``cmd_rank_day``; the daemon mirrors it so an operator running
    ``alfred distiller rank-day`` and the daemon's auto-fire produce
    the same on-disk layout.
    """
    rd = config.radar_day
    if rd.digests_dir:
        digests_path = Path(rd.digests_dir).expanduser().resolve()
    else:
        digests_path = (config.vault.vault_path / "digests").resolve()
    if rd.state_dir:
        state_path = Path(rd.state_dir).expanduser().resolve()
    else:
        state_path = Path(config.state.path).expanduser().resolve().parent
    return digests_path, state_path


async def fire_once(
    config: DistillerConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one daily-radar fire. Returns a summary dict.

    Per ``feedback_intentionally_left_blank.md``: every fire emits a
    structured ``radar_day.scheduled_fire_complete`` log event with
    item count + path so a no-radar-items day is observably distinct
    from a daemon that never ran. The empty-state daily file is
    written as well (Phase 3a's render_daily_file handles the
    "no radar items today" copy).

    ``now`` is the wall-clock fire time. Defaults to
    ``datetime.now(ZoneInfo(rd.schedule.timezone))`` — the
    schedule's configured zone, NOT the system locale. The daemon
    loop passes this; tests + manual fires inherit the TZ-aware
    default. Threaded into ``run_daily_radar`` as ``today=now.date()``
    so the date computation honours the schedule's TZ — pre-fix
    the parameter was unused and ``run_daily_radar`` fell back to
    ``date.today()`` (system locale) which produced wrong-day
    filenames + dedup keys for late-evening Halifax fires near UTC
    midnight (e.g. 23:30 ADT = 02:30 UTC next day).
    """
    rd = config.radar_day
    if now is None:
        now = datetime.now(ZoneInfo(rd.schedule.timezone))
    digests_dir, state_dir = _resolve_dirs(config)
    result = run_daily_radar(
        config.vault.vault_path,
        digests_dir,
        state_dir,
        top_n=rd.top_n,
        min_score=rd.min_score,
        today=now.date(),
        now=now,
    )

    # Single load-bearing log event — operator greps for this to see
    # which days the daemon fired. Empty-items days still log so the
    # daemon's silence doesn't get misread as broken.
    log.info(
        "radar_day.scheduled_fire_complete",
        date=result.date,
        items_count=len(result.items),
        ranker_count=result.ranker_count,
        deduped=max(0, result.ranker_count - len(result.items)),
        output_path=str(result.output_path) if result.output_path else "",
        surfaced_log_path=(
            str(result.surfaced_log_path) if result.surfaced_log_path else ""
        ),
    )
    return {
        "ok": True,
        "date": result.date,
        "items_count": len(result.items),
        "ranker_count": result.ranker_count,
        "output_path": str(result.output_path) if result.output_path else "",
    }


async def run_daemon(config: DistillerConfig) -> None:
    """Main loop — one fire per day at the configured slot.

    Mirrors the digest daemon: drift-bounded ``sleep_until`` keeps the
    fire wall-clock-aligned even on WSL2 with monotonic clock skew.
    """
    rd = config.radar_day
    log.info(
        "radar_day.daemon.starting",
        schedule_time=rd.schedule.time,
        tz=rd.schedule.timezone,
        day_of_week=rd.schedule.day_of_week,
        top_n=rd.top_n,
        min_score=rd.min_score,
        vault=str(config.vault.vault_path),
    )
    while True:
        tz = ZoneInfo(rd.schedule.timezone)
        now = datetime.now(tz)
        target = compute_next_fire(rd.schedule, now)
        sleep_seconds = (target - now).total_seconds()
        if sleep_seconds > 0:
            log.info(
                "radar_day.daemon.sleeping",
                next_run=target.isoformat(),
                sleep_seconds=round(sleep_seconds, 1),
                sleep_hours=round(sleep_seconds / 3600, 1),
            )
            actual_seconds = await sleep_until(target)
            log.info(
                "radar_day.daemon.woke",
                intended_seconds=round(sleep_seconds, 1),
                actual_seconds=round(actual_seconds, 1),
                drift_seconds=round(actual_seconds - sleep_seconds, 1),
            )
        try:
            await fire_once(config, now=datetime.now(tz))
        except Exception:  # noqa: BLE001
            log.exception("radar_day.daemon.fire_error")
        # Sleep 90s past fire so the next ``compute_next_fire`` lands
        # on the following day, not the current minute.
        await asyncio.sleep(90)


__all__ = [
    "_resolve_dirs",
    "fire_once",
    "run_daemon",
]
