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

Implementation note (post-propagation refactor)
-----------------------------------------------
The talker was the first daemon to ship this pattern (commit 5a26d13).
When the pattern was propagated to curator/janitor/distiller/surveyor/
instructor/mail, the core counter+tick logic moved to
``src/alfred/common/heartbeat.py`` as a generic :class:`Heartbeat` class.
This module is now a thin module-level wrapper that exposes the same
function-call surface the talker daemon and tests already used
(``record_inbound``, ``tick``, ``reset``, ``get_count``, ``run``,
``snapshot``) — so the migration is a no-op for talker behaviour.

Event shape stays ``talker.idle_tick interval_seconds=N inbound_in_window=M``.
The ``inbound_in_window`` field name is preserved (rather than renamed
to the generic ``events_in_window``) so existing log-grep queries and
operator muscle memory still work — see :func:`tick` below for the
field translation. Other daemons emit the generic ``events_in_window``
field directly via :class:`alfred.common.heartbeat.Heartbeat`.

Design recap
------------
Two surfaces:
    * :func:`record_inbound` — called from ``bot.on_text`` /
      ``bot.on_voice`` immediately after each ``talker.bot.inbound`` log
      event. Increments a module-level integer.
    * :func:`tick` — called every ``interval_seconds`` from
      ``daemon._heartbeat`` (a sibling asyncio task). Reads the current
      counter, emits ``talker.idle_tick``, resets the counter to zero.

Same asyncio loop = no thread safety required.

Reset semantics
---------------
``tick`` reads-then-resets atomically (single statement, single thread).
The ``inbound_in_window`` field in the emitted event is the count of
``talker.bot.inbound`` events seen since the previous tick — exactly
what an operator needs to distinguish "idle daemon" from "idle but
recently-busy daemon".

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
the heartbeat task — no background work, no log noise. The increment
function still runs (cheap ``+= 1``) so the contract is the same
either way; it just never gets read.
"""

from __future__ import annotations

import asyncio
from typing import Any

from alfred.common.heartbeat import Heartbeat

from .utils import get_logger

log = get_logger(__name__)


# --- Module-level Heartbeat instance ------------------------------------
# The talker has historically exposed a flat module-level API
# (``record_inbound``, ``tick``, etc.) — preserved here as thin wrappers
# around a single shared :class:`Heartbeat`. Same asyncio loop on the
# bot handlers and the heartbeat task means a plain instance attribute
# is correct here — no lock needed.
_hb: Heartbeat = Heartbeat(daemon_name="talker", log=log)


def record_inbound() -> None:
    """Increment the inbound counter by one.

    Called from ``bot.on_text`` and ``bot.on_voice`` immediately after
    the ``talker.bot.inbound`` log event. Cheap by design — must add
    no measurable latency to the message path.
    """
    _hb.record_event()


def get_count() -> int:
    """Return the current counter without resetting (test/debug helper)."""
    return _hb.get_count()


def reset() -> None:
    """Reset the counter to zero (test helper).

    Production resets happen inside :func:`tick`; this is here so test
    setup can isolate cases without relying on the previous test's
    end-state.
    """
    _hb.reset()


def tick(interval_seconds: int) -> int:
    """Emit one ``talker.idle_tick`` event and reset the counter.

    Returns the count that was just emitted (mainly so tests can
    assert against the return value alongside the log call).

    Event shape::

        talker.idle_tick interval_seconds=60 inbound_in_window=N

    ``interval_seconds`` is included for forward-compat: if the cadence
    is ever made adaptive or per-instance, downstream consumers don't
    have to infer it from inter-event timestamps.

    Note on field naming: the talker uses ``inbound_in_window`` (legacy
    field name from the original commit 5a26d13) while
    curator/janitor/etc. use ``events_in_window`` (the generic name).
    Both encode the same thing — count of meaningful events since last
    tick. Talker keeps the legacy name for log-grep continuity.
    """
    # Hand-rolled here (instead of delegating to Heartbeat.tick) so the
    # event field is named ``inbound_in_window`` — the talker's
    # historical contract — rather than the generic
    # ``events_in_window``. Counter handling stays identical.
    count = _hb.get_count()
    _hb.reset()
    log.info(
        "talker.idle_tick",
        interval_seconds=interval_seconds,
        inbound_in_window=count,
    )
    return count


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

    Implemented as a local loop (not a delegate to ``Heartbeat.run``)
    because :func:`tick` here uses the talker-specific
    ``inbound_in_window`` field name, not the generic
    ``events_in_window``.
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


# --- Helper for tests / future introspection -----------------------------


def snapshot() -> dict[str, Any]:
    """Return a small dict of current state — useful for /status probes."""
    return {"inbound_count": _hb.get_count()}
