"""Shared scheduled-daemon loop for the canonical fire-once daemons.

Three production daemons share a byte-identical run-loop after
939fb15 brought them all into the same TZ-aware ``fire_once`` shape:

* ``alfred.digest.daemon`` (weekly digest fire)
* ``alfred.distiller.radar_day_daemon`` (daily radar fire)
* ``alfred.daily_sync.friction_analyzer_daemon`` (daily friction
  analysis fire)

This module owns the loop. Each daemon supplies its own ``fire_once``
body + log namespace + a small ``starting`` payload via
:func:`run_scheduled_daemon`; everything else (sleep / wake / fire /
catch-and-continue / post-fire wait) is shared.

What stays in each daemon
-------------------------
* The ``fire_once`` body — the actual work (digest assembly / radar
  ranking / friction analysis). Different signature per daemon —
  passed in as an opaque callable so the template doesn't care.
* Daemon-specific config types.
* Daemon-specific log namespaces (``digest.daemon.*`` /
  ``radar_day.daemon.*`` / ``friction_analyzer.daemon.*``). The
  template passes ``log_namespace`` so events grep cleanly per daemon.
* The ``run_daemon`` entry signature each tool's orchestrator imports.
  ``run_daemon`` becomes a thin shim that emits the daemon-specific
  ``.starting`` event + delegates to :func:`run_scheduled_daemon`.

Out of scope (for now)
----------------------
Two daemons share parts of this shape but have load-bearing
idiosyncrasies — migration deferred until those converge:

* ``alfred.daily_sync.daemon`` — has same-day dedup via state-file
  check + 60s post-fire sleep + ``today=`` not ``now=`` fire shape.
* ``alfred.bit.daemon`` — has its own schedule-config adapter +
  StateManager init + 60s post-fire + ``run_bit_once`` not
  ``fire_once``.

When those converge, this module's API can grow optional knobs
(skip predicate, post-fire-sleep override). Premature genericization
would invent abstractions that don't pay rent today; revisit when
the second consumer of any new knob lands. Per-daemon followups
filed alongside this commit.

Why function-based not class-based
----------------------------------
Existing daemons are functions (``async def run_daemon(...)``), not
classes. Wrapping them in a base class would invent a hierarchy
where there isn't one, and force every test that imports
``run_daemon`` to grow class-instantiation boilerplate. A shared
function preserves the existing public surface byte-for-byte.

Per ``feedback_intentionally_left_blank.md``: the loop emits its
``.starting`` log unconditionally (caller's payload), and re-emits
``.sleeping`` / ``.woke`` on every cycle, so an idle daemon stays
distinguishable from a stuck one in operator logs.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import structlog

from alfred.common.schedule import (
    ScheduleConfig,
    compute_next_fire,
    sleep_until,
)


# Default seconds to sleep AFTER a fire so the next ``compute_next_fire``
# call lands on the following day/week rather than re-resolving to the
# minute we just fired in. 90 matches the canonical three daemons
# (digest / radar_day / friction_analyzer); 60 is what daily_sync /
# bit use, and is exposed as a knob for when those migrate.
_POST_FIRE_SLEEP_SECONDS_DEFAULT = 90.0


async def run_scheduled_daemon(
    *,
    schedule: ScheduleConfig,
    fire: Callable[[datetime], Awaitable[Any]],
    log_namespace: str,
    log: structlog.BoundLogger | None = None,
    post_fire_sleep_seconds: float = _POST_FIRE_SLEEP_SECONDS_DEFAULT,
) -> None:
    """Run the canonical scheduled-fire loop until cancelled.

    Args:
        schedule: ``ScheduleConfig`` (time + timezone + optional
            day_of_week) — passed straight to ``compute_next_fire``.
        fire: async callable that does one fire's worth of work.
            Receives a tz-aware ``datetime`` (the moment-after-wake)
            and returns whatever the work returns; the template
            ignores the return value but logs exceptions. Each daemon
            wraps its own ``fire_once`` here, partial-applying any
            extra args (config / raw / etc.).
        log_namespace: dotted prefix for the four log events emitted
            per cycle: ``<ns>.sleeping``, ``<ns>.woke``,
            ``<ns>.fire_error`` (on exception). Convention is
            ``<tool>.daemon`` (e.g. ``radar_day.daemon``).
        log: structlog logger to emit through. Defaults to
            ``structlog.get_logger(log_namespace)``. Allowing a
            caller-supplied logger keeps the per-daemon
            ``log = structlog.get_logger(__name__)`` idiom working
            so events still tag with the daemon's module name.
        post_fire_sleep_seconds: how long to sleep after each fire
            before re-computing next-fire. Default 90 matches the
            canonical three daemons. Knob exposed for the daily_sync /
            bit migrations once their idiosyncrasies settle.

    Returns:
        Never — runs until cancelled (orchestrator sends SIGTERM /
        ``asyncio.Task.cancel``). The loop never exits cleanly on
        its own; this matches the pre-extraction shape.

    Raises:
        Nothing the caller cares about — exceptions from ``fire``
        are caught + logged via ``log.exception(<ns>.fire_error)``
        so one failed fire can't kill the daemon. Cancellation
        (``asyncio.CancelledError``) propagates so the orchestrator's
        shutdown path works unchanged.
    """
    if log is None:
        log = structlog.get_logger(log_namespace)

    while True:
        tz = ZoneInfo(schedule.timezone)
        now = datetime.now(tz)
        target = compute_next_fire(schedule, now)
        sleep_seconds = (target - now).total_seconds()

        if sleep_seconds > 0:
            log.info(
                f"{log_namespace}.sleeping",
                next_run=target.isoformat(),
                sleep_seconds=round(sleep_seconds, 1),
                sleep_hours=round(sleep_seconds / 3600, 1),
            )
            actual_seconds = await sleep_until(target)
            log.info(
                f"{log_namespace}.woke",
                intended_seconds=round(sleep_seconds, 1),
                actual_seconds=round(actual_seconds, 1),
                drift_seconds=round(actual_seconds - sleep_seconds, 1),
            )

        try:
            await fire(datetime.now(tz))
        except Exception:  # noqa: BLE001
            log.exception(f"{log_namespace}.fire_error")

        # Sleep past the fire so the next ``compute_next_fire`` lands
        # on the next scheduled slot, not the current minute.
        await asyncio.sleep(post_fire_sleep_seconds)


__all__ = [
    "run_scheduled_daemon",
]
