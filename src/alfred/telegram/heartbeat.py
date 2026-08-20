"""Talker idle-tick heartbeat — a positive "I'm alive, nothing to report" signal.

Background — "intentionally left blank" pattern
-----------------------------------------------
A daemon that emits zero log events for a window can mean any of:
    * didn't run
    * ran with nothing to do
    * ran and crashed silently

Silence is ambiguous, and the 2026-04-22 talker investigation cost ~30
minutes chasing a "structured logging is broken!" hypothesis that turned
out to be "no traffic since 03:36 UTC". A periodic positive idle signal
makes the answer obvious in 5 seconds: the heartbeat is in the log →
daemon is alive; heartbeat is missing → daemon is broken.

T5 trim (2026-08-19) — the counters died with the bot
-----------------------------------------------------
Until the Telegram retirement this module also carried two counters and
their write API (``record_inbound`` from bot.py's group=-1 pre-pass,
``record_handled`` from each entry handler) and the tick emitted a
handled/unhandled split (2026-06-06 c1) whose purpose was surfacing
PTB updates that no handler routed. Every writer lived in ``bot.py``
(deleted in T4); with the bot gone the counters were structurally zero
forever, and a permanently-zero ``inbound_handled``/``inbound_unhandled``
pair would have read as "we are measuring the routed/unrouted split and
it is clean" when nothing can measure it. The write API and the split
fields are deleted; the historical event shape, for log archaeology::

    talker.idle_tick interval_seconds=N
        inbound_in_window=T inbound_handled=H inbound_unhandled=U

The event KEEPS ``inbound_in_window`` — pinned to ``0`` — for two
reasons: operator/dashboard greps on the field name keep working, and
the always-present-field form of the intentionally-left-blank rule
prefers a field carrying an honest empty value ("the Telegram inbound
channel is retired and empty") over a field that vanishes. Web traffic
is observable on the web surfaces' own logs; it was never counted here.

Current event shape::

    talker.idle_tick interval_seconds=N inbound_in_window=0

Cadence rationale (60s)
-----------------------
60s × 24h × 365 = ~525k events/year ≈ 290 KB/day in the talker log.
Negligible vs. an active session's log volume, dense enough that an
operator scanning the tail can confirm liveness within a minute.
1Hz would be 17 MB/day for no additional diagnostic value — tick at
the human-attention timescale, not the machine-monitoring timescale.

Disabled path
-------------
When ``telegram.idle_tick.enabled = false`` the daemon never spawns
the heartbeat task — no background work, no log noise.

(The generic per-daemon heartbeat for curator/janitor/etc. lives in
``alfred.common.heartbeat`` and emits ``events_in_window``; the talker
keeps this thin module for its historical ``inbound_in_window`` field
name and its talker-specific docs.)
"""

from __future__ import annotations

import asyncio

from .utils import get_logger

log = get_logger(__name__)


def tick(interval_seconds: int) -> int:
    """Emit one ``talker.idle_tick`` liveness event.

    Returns the emitted ``inbound_in_window`` value — ``0`` by
    construction since the T5 trim (see module docstring): the counter's
    only writers were bot.py handlers, deleted with the retirement. The
    return value preserves the historical contract for any caller that
    consumed it.

    ``interval_seconds`` is included for forward-compat: if the cadence
    is ever made adaptive or per-instance, downstream consumers don't
    have to infer it from inter-event timestamps.
    """
    log.info(
        "talker.idle_tick",
        interval_seconds=interval_seconds,
        inbound_in_window=0,
    )
    return 0


async def run(
    interval_seconds: int,
    shutdown_event: asyncio.Event,
) -> None:
    """Async loop: tick every ``interval_seconds`` until shutdown.

    Mirrors the sweeper-task pattern in ``daemon.py`` — ``wait_for`` on
    the shutdown event with a timeout, swallow the timeout, run the
    work, repeat. SIGTERM / SIGINT sets the event and the next
    ``wait_for`` returns immediately, exiting the loop cleanly.

    Wraps :func:`tick` in a try/except so a logging-layer failure
    (FileHandler full, etc.) doesn't kill the heartbeat task — the
    whole point of the task is to keep firing through trouble.
    """
    while not shutdown_event.is_set():
        try:
            await asyncio.wait_for(
                shutdown_event.wait(), timeout=interval_seconds
            )
            return  # event set → exit
        except asyncio.TimeoutError:
            pass
        try:
            tick(interval_seconds)
        except Exception:  # noqa: BLE001
            log.exception("talker.idle_tick.error")
