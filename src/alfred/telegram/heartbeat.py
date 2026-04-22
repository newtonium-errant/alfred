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

Design
------
Two surfaces:
    * :func:`record_inbound` — called from ``bot.on_text`` /
      ``bot.on_voice`` immediately after each ``talker.bot.inbound`` log
      event. Increments a module-level integer.
    * :func:`tick` — called every ``interval_seconds`` from
      ``daemon._heartbeat`` (a sibling asyncio task). Reads the current
      counter, emits ``talker.idle_tick``, resets the counter to zero.

Same asyncio loop = no thread safety required; a plain ``int`` works.
The counter is module-level (process-global, not bound to a specific
``Application`` instance) for two reasons:
    1. ``bot.on_text`` / ``bot.on_voice`` already run in the talker's
       single asyncio loop, so there is nothing to multiplex by.
    2. Tests can pre-populate the counter via :func:`record_inbound`
       and call :func:`tick` directly — no need to spin up a fake
       daemon harness.

Reset semantics
---------------
``tick`` reads-then-resets atomically (single statement, single thread).
The "inbound_in_window" field in the emitted event is the count of
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

from .utils import get_logger

log = get_logger(__name__)


# --- Module-level counter ------------------------------------------------
# Incremented by ``record_inbound`` from the bot's text + voice handlers.
# Read + reset by ``tick`` from the heartbeat task. Same asyncio loop on
# both sides, so no lock needed — see the module docstring.
_inbound_count: int = 0


def record_inbound() -> None:
    """Increment the inbound counter by one.

    Called from ``bot.on_text`` and ``bot.on_voice`` immediately after
    the ``talker.bot.inbound`` log event. Cheap by design — must add
    no measurable latency to the message path.
    """
    global _inbound_count
    _inbound_count += 1


def get_count() -> int:
    """Return the current counter without resetting (test/debug helper)."""
    return _inbound_count


def reset() -> None:
    """Reset the counter to zero (test helper).

    Production resets happen inside :func:`tick`; this is here so test
    setup can isolate cases without relying on the previous test's
    end-state.
    """
    global _inbound_count
    _inbound_count = 0


def tick(interval_seconds: int) -> int:
    """Emit one ``talker.idle_tick`` event and reset the counter.

    Returns the count that was just emitted (mainly so tests can
    assert against the return value alongside the log call).

    Event shape::

        talker.idle_tick interval_seconds=60 inbound_in_window=N

    ``interval_seconds`` is included for forward-compat: if the cadence
    is ever made adaptive or per-instance, downstream consumers don't
    have to infer it from inter-event timestamps.
    """
    global _inbound_count
    count = _inbound_count
    _inbound_count = 0
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
            # Swallow + log so heartbeat keeps firing. If the log call
            # itself is broken we'll see the exception in stderr; the
            # next tick will still fire.
            log.exception("talker.idle_tick.error")


# --- Helper for tests / future introspection -----------------------------


def snapshot() -> dict[str, Any]:
    """Return a small dict of current state — useful for /status probes."""
    return {"inbound_count": _inbound_count}
