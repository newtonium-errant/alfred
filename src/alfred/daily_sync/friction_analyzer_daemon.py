"""Friction analyzer daemon — auto-fires the K3 c1 analyzer on a schedule.

Mirrors the radar_day daemon (and the digest daemon's pattern):
load config, compute next fire, drift-bounded ``sleep_until``, fire,
repeat. Default fire 07:30 ADT — 1.5h ahead of KAL-LE's Daily Sync at
09:00 ADT so the friction log is fresh when the section provider
reads it.

The daemon DOES NOT start automatically unless
``daily_sync.friction_analyzer.enabled`` is True. The orchestrator's
``_run_friction_analyzer`` entry exits 78 when disabled.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from alfred.common.scheduled_daemon import run_scheduled_daemon

from .config import DailySyncConfig, FrictionAnalyzerConfig
from .friction_analyzer import run_friction_analysis

log = structlog.get_logger(__name__)


def _resolve_audit_log_path(
    fa_config: FrictionAnalyzerConfig,
    raw_config: dict[str, Any] | None,
) -> Path:
    """Resolve the audit-log path the analyzer should read from.

    Order:
      1. Explicit ``daily_sync.friction_analyzer.audit_log_path`` —
         operator override; wins when set.
      2. ``telegram.bash_exec.audit_path`` from raw_config — production
         path for KAL-LE; the bash_exec audit log is per-instance and
         already configured in ``config.kalle.yaml``.
      3. Default ``./data/bash_exec.jsonl`` — backward-compat fallback
         matching ``BashExecConfig.audit_path``'s default.

    Returns a resolved ``Path`` either way; the analyzer handles a
    missing file as the empty-corpus case.
    """
    if fa_config.audit_log_path:
        return Path(fa_config.audit_log_path).expanduser().resolve()
    if raw_config is not None:
        telegram_raw = raw_config.get("telegram") or {}
        bash_exec_raw = telegram_raw.get("bash_exec") or {}
        bash_exec_path = bash_exec_raw.get("audit_path")
        if isinstance(bash_exec_path, str) and bash_exec_path:
            return Path(bash_exec_path).expanduser().resolve()
    return Path("./data/bash_exec.jsonl").expanduser().resolve()


async def fire_once(
    config: DailySyncConfig,
    *,
    raw_config: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one friction-analysis fire. Returns a summary dict.

    Per ``feedback_intentionally_left_blank.md``: every fire emits a
    structured ``friction_analyzer.scheduled_fire_complete`` log event
    so an empty-audit day is observably distinct from a daemon that
    never ran.

    ``now`` is the wall-clock fire time. Defaults to
    ``datetime.now(ZoneInfo(fa.schedule.timezone))`` — the schedule's
    configured zone, NOT the system locale. Kept as a parameter for
    symmetry with the digest + radar_day daemons (test injection
    point + parallel daemon signatures). ``run_friction_analysis``
    is itself TZ-aware via the ``schedule_timezone`` argument below,
    so ``now`` doesn't need to propagate further into the analysis
    path. Pre-fix the parameter was unused; making it TZ-aware-by-
    default closes the dead-param smell without diverging from the
    digest pattern.
    """
    fa = config.friction_analyzer
    if now is None:
        now = datetime.now(ZoneInfo(fa.schedule.timezone))
    audit_log_path = _resolve_audit_log_path(fa, raw_config)
    log_path = Path(fa.log_path).expanduser().resolve()

    result = run_friction_analysis(
        audit_log_path,
        log_path,
        failed_pattern_threshold=fa.thresholds.failed_pattern_count,
        repeated_pattern_threshold=fa.thresholds.repeated_pattern_count,
        window_hours=fa.thresholds.window_hours,
        schedule_timezone=fa.schedule.timezone,
    )

    # Per-category breakdown for the log line so an operator can see
    # at a glance which detection branch fired.
    by_kind: dict[str, int] = {}
    for ev in result.events:
        kind = str(ev.get("kind") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1

    log.info(
        "friction_analyzer.scheduled_fire_complete",
        day_bucket=result.day_bucket,
        events_count=len(result.events),
        skipped_idempotent=result.skipped,
        audit_entries_scanned=result.audit_entries_scanned,
        audit_entries_in_window=result.audit_entries_in_window,
        by_kind=by_kind,
        audit_log_path=str(audit_log_path),
        friction_log_path=str(log_path),
    )
    return {
        "ok": True,
        "day_bucket": result.day_bucket,
        "events_count": len(result.events),
        "skipped_idempotent": result.skipped,
        "audit_entries_in_window": result.audit_entries_in_window,
        "by_kind": by_kind,
    }


async def run_daemon(
    config: DailySyncConfig,
    raw_config: dict[str, Any] | None = None,
) -> None:
    """Main loop — one fire per day at the configured slot.

    Delegates the canonical sleep/wake/fire/catch loop to
    ``alfred.common.scheduled_daemon.run_scheduled_daemon``; this
    function owns only the daemon-specific ``starting`` log event +
    the ``fire_once`` partial. Drift-bounded ``sleep_until`` + 90s
    post-fire sleep (so the next ``compute_next_fire`` lands on the
    following day rather than the current minute) live inside the
    shared template.
    """
    fa = config.friction_analyzer
    log.info(
        "friction_analyzer.daemon.starting",
        schedule_time=fa.schedule.time,
        tz=fa.schedule.timezone,
        day_of_week=fa.schedule.day_of_week,
        log_path=fa.log_path,
        thresholds={
            "failed_pattern_count": fa.thresholds.failed_pattern_count,
            "repeated_pattern_count": fa.thresholds.repeated_pattern_count,
            "window_hours": fa.thresholds.window_hours,
        },
    )

    async def _fire(now: datetime) -> Any:
        return await fire_once(config, raw_config=raw_config, now=now)

    await run_scheduled_daemon(
        schedule=fa.schedule,
        fire=_fire,
        log_namespace="friction_analyzer.daemon",
        log=log,
    )


__all__ = [
    "_resolve_audit_log_path",
    "fire_once",
    "run_daemon",
]
