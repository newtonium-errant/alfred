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
This module is now a thin module-level wrapper around the shared class
plus a talker-specific second counter (handled vs. total) — the surface
(``record_inbound``, ``tick``, ``reset``, ``get_count``, ``run``,
``snapshot``) stays backward-compatible.

Event shape (post 2026-06-06 c1 — handled/unhandled split)::

    talker.idle_tick interval_seconds=N
        inbound_in_window=T inbound_handled=H inbound_unhandled=U

where ``T`` is the legacy total-inbound count (every Update PTB saw),
``H`` is how many of those a handler actually processed, and
``U = T - H`` is the silent-drop count — Updates Telegram delivered
but no handler routed (e.g. a forwarded PDF before the document
handler shipped; a future filetype that PTB delivers but the bot
hasn't registered a handler for yet).

The ``inbound_in_window`` field name is preserved (rather than renamed
to the generic ``events_in_window``) so existing log-grep queries and
operator muscle memory still work. Other daemons emit the generic
``events_in_window`` field directly via
:class:`alfred.common.heartbeat.Heartbeat`; the split fields are
talker-specific (they make sense for a daemon with a distinct
"received vs. handled" distinction; curator / janitor / etc. process
every event they observe — there's no unhandled bucket).

Why the split (2026-06-06 incident)
-----------------------------------
A pre-split heartbeat with ``inbound_in_window=1`` was ambiguous: a
silent-drop (a PDF arrived → no handler was registered for
``filters.Document`` → the application-level pre-pass still bumped
the counter via the group=-1 ``TypeHandler``) looked identical in
logs to a healthy short-of-quiet tick. The split surfaces the
silent-drop case explicitly: ``inbound_unhandled > 0`` means
messages came in but nothing routed them — an operator-visible
signal that says "register a handler for that update type." See
``bot.py`` block-comment above ``_pre_record_inbound`` for the
routing-incident context.

Design recap
------------
Three surfaces:
    * :func:`record_inbound` — called from the application-level
      ``_pre_record_inbound`` pre-pass (group=-1 ``TypeHandler``).
      Increments the **total** counter. Sees every Update PTB
      delivers, including ones no handler routes.
    * :func:`record_handled` — called from each entry handler
      (``bot.on_text`` / ``bot.on_voice`` / ``bot.on_photo`` /
      ``bot.on_document``) as the first line after the allowlist gate.
      Increments the **handled** counter. ``handled`` is incremented
      for every inbound that reached a registered handler, including
      ones the handler chose to reject for application reasons
      (vision-disabled photo replies, oversized PDFs, non-PDF
      documents) — all of those routed to a handler and replied to
      the user, which is the operationally-meaningful definition of
      "handled." Allowlist-rejected (unauthorized) messages are NOT
      counted as handled — they bail before the ``record_handled``
      call, intentionally landing in ``unhandled`` so a misconfigured
      allowlist surfaces in the heartbeat. See the on_text /
      on_voice / on_photo / on_document handler comments at the
      counter site for the rationale.
    * :func:`tick` — called every ``interval_seconds`` from
      ``daemon._heartbeat`` (a sibling asyncio task). Reads both
      counters, emits ``talker.idle_tick`` with all three fields,
      resets both to zero.

Same asyncio loop = no thread safety required.

Reset semantics
---------------
``tick`` reads-then-resets atomically (single statement pair, single
thread). Both counters reset together so the next interval's
``handled <= total`` invariant always holds.

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
functions still run (cheap ``+= 1``) so the contract is the same
either way; they just never get read.
"""

from __future__ import annotations

import asyncio
from typing import Any

from alfred.common.heartbeat import Heartbeat

from .utils import get_logger

log = get_logger(__name__)


# --- Module-level counters ----------------------------------------------
# The talker has historically exposed a flat module-level API
# (``record_inbound``, ``tick``, etc.) — preserved here as thin wrappers
# around a single shared :class:`Heartbeat` for the total counter, plus a
# parallel module-level integer for the handled counter. Same asyncio
# loop on the bot handlers and the heartbeat task means plain
# integers/instance attrs are correct here — no lock needed.
#
# The handled counter intentionally lives at module level rather than
# being added to ``Heartbeat`` because the handled/unhandled split is
# talker-specific. Adding a second counter to the generic class would
# either (a) inflate every daemon's event shape with an always-zero
# ``handled`` field, or (b) require a per-daemon "split-mode" flag —
# neither pays off when only the talker has a meaningful unhandled
# bucket. Keep the talker-specific concern in the talker-specific
# module.
_hb: Heartbeat = Heartbeat(daemon_name="talker", log=log)
_handled_count: int = 0


def record_inbound() -> None:
    """Increment the total-inbound counter by one.

    Called from the application-level ``_pre_record_inbound`` pre-pass
    in ``bot.build_app`` (registered as ``TypeHandler(Update, ...)`` at
    group=-1). Sees every Update PTB delivers, including ones no
    handler ends up routing. Cheap by design — must add no measurable
    latency to the message path.
    """
    _hb.record_event()


def record_handled() -> None:
    """Increment the handled-inbound counter by one.

    Called as the first line of each entry handler (``bot.on_text`` /
    ``bot.on_voice`` / ``bot.on_photo`` / ``bot.on_document``) AFTER
    the allowlist gate. The handled counter increments for every
    inbound that reached a registered handler, including ones the
    handler chose to reject for application reasons (vision-disabled
    photo replies, oversized PDFs, non-PDF documents) — those routed
    successfully and replied to the user, which is the
    operationally-meaningful definition of "handled."

    Allowlist-rejected messages bail BEFORE this call so they
    intentionally land in the unhandled bucket — a misconfigured
    allowlist that's silently dropping Andrew's messages will show as
    ``inbound_unhandled > 0`` in the heartbeat, which is the whole
    point of the split.

    Cheap by design — same hot-path constraints as
    :func:`record_inbound`.
    """
    global _handled_count
    _handled_count += 1


def get_count() -> int:
    """Return the current total counter without resetting (test/debug)."""
    return _hb.get_count()


def get_handled_count() -> int:
    """Return the current handled counter without resetting (test/debug)."""
    return _handled_count


def reset() -> None:
    """Reset BOTH counters to zero (test helper).

    Production resets happen inside :func:`tick`; this is here so test
    setup can isolate cases without relying on the previous test's
    end-state. Always resets both counters together to preserve the
    ``handled <= total`` invariant.
    """
    global _handled_count
    _hb.reset()
    _handled_count = 0


def tick(interval_seconds: int) -> int:
    """Emit one ``talker.idle_tick`` event and reset both counters.

    Returns the **total** count that was just emitted (mainly so tests
    can assert against the return value alongside the log call).
    Returning total preserves the pre-split return-value contract —
    callers that consumed the legacy return value see no change.

    Event shape::

        talker.idle_tick interval_seconds=60
            inbound_in_window=T inbound_handled=H inbound_unhandled=U

    where ``T`` is total inbound, ``H`` is how many reached a handler,
    and ``U = T - H`` is the silent-drop count. ``inbound_in_window``
    is preserved as the legacy alias for ``T`` (log-grep continuity);
    new dashboards / queries should prefer the split fields.

    ``interval_seconds`` is included for forward-compat: if the cadence
    is ever made adaptive or per-instance, downstream consumers don't
    have to infer it from inter-event timestamps.

    Note on field naming: the talker uses ``inbound_in_window`` (legacy
    field name from the original commit 5a26d13) while
    curator/janitor/etc. use ``events_in_window`` (the generic name).
    Both encode the same thing — count of meaningful events since last
    tick. Talker keeps the legacy name for log-grep continuity.
    """
    global _handled_count
    # Hand-rolled here (instead of delegating to Heartbeat.tick) so the
    # event field is named ``inbound_in_window`` — the talker's
    # historical contract — and so the handled/unhandled split fires in
    # the same log line. Counter handling stays atomic (single thread,
    # single asyncio loop).
    total = _hb.get_count()
    handled = _handled_count
    # The unhandled count is derived rather than maintained as its own
    # counter: that keeps the ``handled + unhandled == total`` invariant
    # mechanical rather than relying on every code path to bump exactly
    # one of two counters. ``max(0, …)`` is belt-and-braces against a
    # hypothetical future bug where handled outpaces total (e.g.
    # record_handled called without a preceding record_inbound).
    unhandled = max(0, total - handled)
    _hb.reset()
    _handled_count = 0
    log.info(
        "talker.idle_tick",
        interval_seconds=interval_seconds,
        inbound_in_window=total,
        inbound_handled=handled,
        inbound_unhandled=unhandled,
    )
    return total


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
    ``inbound_in_window`` / ``inbound_handled`` / ``inbound_unhandled``
    field names, not the generic ``events_in_window``.
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
    """Return a small dict of current state — useful for /status probes.

    Includes both counters (total + handled) so a ``/status`` consumer
    can surface the split if needed. The dict shape is intentionally
    additive: pre-split consumers reading ``inbound_count`` still see
    the total they used to see.
    """
    return {
        "inbound_count": _hb.get_count(),
        "inbound_handled_count": _handled_count,
    }
