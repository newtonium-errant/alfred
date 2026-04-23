"""Daemon idle-tick heartbeat — a positive "I'm alive, nothing to report" signal.

Background — "intentionally left blank" pattern
-----------------------------------------------
A daemon that emits zero log events for a window can mean any of:
    * didn't run
    * ran with nothing to do
    * ran and crashed silently

Silence is ambiguous. The 2026-04-22 talker investigation cost ~30 minutes
chasing a "structured logging is broken!" hypothesis that turned out to be
"no traffic since 03:36 UTC". A periodic positive idle signal makes the
answer obvious in 5 seconds: heartbeat in the log → daemon is alive;
heartbeat missing → daemon is broken.

This module is the shared, daemon-agnostic implementation. Each daemon
instantiates its own :class:`Heartbeat` with a daemon-specific event
prefix (e.g. ``curator`` → emits ``curator.idle_tick``) and counter
semantic. The talker keeps a thin module-level wrapper around its own
:class:`Heartbeat` instance for backward compatibility — see
``src/alfred/telegram/heartbeat.py``.

Design
------
Each :class:`Heartbeat` owns:
    * A counter (plain ``int``) incremented via :meth:`record_event` from
      the daemon's "something meaningful happened" callsite.
    * A :meth:`tick` method that reads-then-resets the counter atomically
      (single statement, single asyncio loop) and emits one log event of
      shape ``<daemon>.idle_tick interval_seconds=N events_in_window=M``.
    * A :meth:`run` async loop that sleeps on a shutdown event and calls
      :meth:`tick` each interval — wrap in ``asyncio.create_task`` from
      the daemon's lifecycle.

Same asyncio loop on the increment side and the tick side = no thread
safety required. Counter lives on the instance (not module-global) so
multiple daemons in one process — a future possibility if the
orchestrator ever moves from multiprocess to multi-task — don't
collide.

Counter semantic per daemon (from the propagation spec)
-------------------------------------------------------
    * talker      — one inbound (text or voice) message processed
    * curator     — one inbox file processed end-to-end
    * janitor     — one issue fixed (not just scanned)
    * distiller   — one learn record created
    * surveyor    — one record re-embedded
    * instructor  — one directive executed (not poll ticks)
    * mail        — one email fetched OR one webhook received

Out of scope intentionally: brief, bit, daily_sync — clock-aligned
scheduled fires that sleep for hours between runs. The wake event itself
is their natural positive signal; a 60s heartbeat across a 23-hour sleep
would generate ~1,380 noise events for one signal event. Skip them.

Cadence rationale (60s default)
-------------------------------
60s × 24h × 365 = ~525k events/year ≈ 290 KB/day per daemon log.
Negligible vs. an active daemon's log volume, dense enough that an
operator scanning the tail can confirm liveness within a minute. 1Hz
would be 17 MB/day for no additional diagnostic value — tick at the
human-attention timescale, not the machine-monitoring timescale.

Disabled path
-------------
When ``enabled=False`` the daemon never spawns the heartbeat task — no
background work, no log noise. The :meth:`record_event` increment still
runs (cheap ``+= 1``) so the contract is the same either way; it just
never gets read.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import structlog


@dataclass
class IdleTickConfig:
    """Generic per-daemon idle-tick config.

    Each daemon's own ``config.py`` defines its own ``IdleTickConfig``
    dataclass with the same shape (talker already has one); this is the
    canonical reference for the contract. We don't import individual
    daemon configs here to avoid circular dependencies — daemons pass
    ``enabled`` and ``interval_seconds`` to :class:`Heartbeat` directly.
    """

    enabled: bool = True
    interval_seconds: int = 60


class Heartbeat:
    """Per-daemon heartbeat emitter.

    Args:
        daemon_name: The structlog event prefix (e.g. ``"curator"`` →
            emits ``curator.idle_tick``). Used both as the event name
            stem and as the disambiguator if multiple Heartbeat
            instances ever coexist in one process.
        log: The daemon's own structlog ``BoundLogger`` so events land
            in the right log file (curator.log, janitor.log, etc.).
            When None, falls back to a structlog default logger named
            ``alfred.common.heartbeat`` — handy for tests but not what
            production daemons should use.
    """

    def __init__(
        self,
        daemon_name: str,
        log: structlog.stdlib.BoundLogger | None = None,
    ) -> None:
        self.daemon_name = daemon_name
        self._log = log if log is not None else structlog.get_logger(
            "alfred.common.heartbeat"
        )
        self._count: int = 0

    @property
    def event_name(self) -> str:
        """The structlog event name this heartbeat emits."""
        return f"{self.daemon_name}.idle_tick"

    def record_event(self) -> None:
        """Increment the counter by one.

        Called from the daemon's "something meaningful happened"
        callsite — the exact callsite varies per daemon, see the
        per-daemon counter-semantic table in the module docstring.
        Cheap by design (must add no measurable latency to the
        daemon's hot path).
        """
        self._count += 1

    def get_count(self) -> int:
        """Return the current counter without resetting (test/debug helper)."""
        return self._count

    def reset(self) -> None:
        """Reset the counter to zero (test helper).

        Production resets happen inside :meth:`tick`; this is here so
        test setup can isolate cases without relying on a previous
        test's end-state.
        """
        self._count = 0

    def tick(self, interval_seconds: int) -> int:
        """Emit one ``<daemon>.idle_tick`` event and reset the counter.

        Returns the count that was just emitted (mainly so tests can
        assert against the return value alongside the log call).

        Event shape::

            <daemon>.idle_tick interval_seconds=60 events_in_window=N

        ``interval_seconds`` is included for forward-compat: if the
        cadence is ever made adaptive or per-instance, downstream
        consumers don't have to infer it from inter-event timestamps.
        """
        count = self._count
        self._count = 0
        self._log.info(
            self.event_name,
            interval_seconds=interval_seconds,
            events_in_window=count,
        )
        return count

    async def run(
        self,
        interval_seconds: int,
        shutdown_event: asyncio.Event,
    ) -> None:
        """Async loop: tick every ``interval_seconds`` until shutdown.

        Mirrors the sweeper-task pattern in the talker's daemon —
        ``wait_for`` on the shutdown event with a timeout, swallow the
        timeout, run the work, repeat. SIGTERM / SIGINT sets the event
        and the next ``wait_for`` returns immediately, exiting the loop
        cleanly.

        Wraps :meth:`tick` in a try/except so a logging-layer failure
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
                self.tick(interval_seconds)
            except Exception:  # noqa: BLE001
                # Swallow + log so heartbeat keeps firing. If the log
                # call itself is broken we'll see the exception in
                # stderr; the next tick will still fire.
                self._log.exception(f"{self.event_name}.error")

    def snapshot(self) -> dict[str, Any]:
        """Return a small dict of current state — useful for /status probes."""
        return {
            "daemon_name": self.daemon_name,
            "events_in_window": self._count,
        }


async def run_with_polling(
    heartbeat: Heartbeat,
    interval_seconds: int,
    is_shutdown: callable,
) -> None:
    """Alternate run loop for daemons that don't have an asyncio.Event.

    Used by daemons whose shutdown signal is a plain bool / flag (e.g.
    surveyor's ``Daemon._shutdown_requested``). Polls the predicate
    every interval and exits when it returns True.

    Don't use this when an :class:`asyncio.Event` is available —
    :meth:`Heartbeat.run` is more responsive on shutdown because it
    wakes immediately on event set rather than waiting up to one full
    interval.
    """
    while not is_shutdown():
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            return
        if is_shutdown():
            return
        try:
            heartbeat.tick(interval_seconds)
        except Exception:  # noqa: BLE001
            heartbeat._log.exception(f"{heartbeat.event_name}.error")


def run_in_thread(
    heartbeat: Heartbeat,
    interval_seconds: int,
    shutdown_event: "object",
) -> "object":
    """Spawn a daemon thread that ticks every ``interval_seconds``.

    For sync daemons (e.g. mail's ``http.server.HTTPServer.serve_forever``)
    that don't run inside an asyncio loop. ``shutdown_event`` should be a
    :class:`threading.Event` (duck-typed here — accepts any object with
    ``.is_set()`` and ``.wait(timeout)`` methods so callers don't need to
    import threading just for the type hint).

    Returns the started thread so callers can ``.join()`` it on shutdown.
    The thread is marked daemon=True so a hung tick doesn't block process
    exit — the orchestrator SIGTERMs the process if the daemon thread
    refuses to die.
    """
    import threading

    def _loop() -> None:
        while not shutdown_event.is_set():
            # threading.Event.wait returns True if set, False on timeout.
            if shutdown_event.wait(timeout=interval_seconds):
                return
            try:
                heartbeat.tick(interval_seconds)
            except Exception:  # noqa: BLE001
                heartbeat._log.exception(f"{heartbeat.event_name}.error")

    t = threading.Thread(
        target=_loop,
        name=f"{heartbeat.daemon_name}-heartbeat",
        daemon=True,
    )
    t.start()
    return t
