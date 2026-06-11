"""Spawn stagger + priority-order pins (O1/O2, 2026-06-11 slow-start fix).

The 2026-06-11 profiling (N=5 restarts, log archaeology) measured the
old 10s serial stagger at ~115s of Salem's ~119s boot — ~97% of total
startup — with the user-facing talker in slot 9 (+82s to Telegram
reachability). These pins make both remediations DELIBERATE-CHANGE-ONLY:

  * SPAWN_STAGGER_SECONDS — a future "bump it back to 10" must be a
    visible diff against this pin, with the herd-vs-boot-time tradeoff
    re-argued in the commit.
  * SPAWN_PRIORITY — talker-first/cloudflared-second is the load-bearing
    contract; the lockstep pin forces every NEW tool to get a deliberate
    slot decision (mirrors the route-smoke register-helper meta-pin).

No orchestrator integration harness here by design — the live restart
measurement is the acceptance test; these pin the pure pieces.
"""

from __future__ import annotations

from alfred.orchestrator import (
    SPAWN_PRIORITY,
    SPAWN_STAGGER_SECONDS,
    TOOL_RUNNERS,
    order_tools,
)


# ---------------------------------------------------------------------------
# O1 — stagger constant
# ---------------------------------------------------------------------------


def test_spawn_stagger_is_two_seconds() -> None:
    """Pin the ratified value (O1). Changing it is allowed — silently is not."""
    assert SPAWN_STAGGER_SECONDS == 2.0


# ---------------------------------------------------------------------------
# O2 — priority order
# ---------------------------------------------------------------------------


def test_spawn_priority_lockstep_with_tool_runners() -> None:
    """Every runnable tool has exactly one deliberate priority slot.

    A tool in TOOL_RUNNERS but missing here would silently sort to the
    tail; a tool here but not runnable is a stale entry. Both fail loud.
    """
    assert set(SPAWN_PRIORITY) == set(TOOL_RUNNERS.keys())
    assert len(SPAWN_PRIORITY) == len(set(SPAWN_PRIORITY))  # no duplicates


def test_spawn_priority_head_is_latency_sensitive_first() -> None:
    """The load-bearing head: talker (user-facing + transport host) first,
    cloudflared (inbound tunnel) second, morning-cadence trio next."""
    assert SPAWN_PRIORITY[:5] == ("talker", "cloudflared", "bit", "brief", "routine")


def test_order_tools_salem_roster() -> None:
    """The Salem-shaped roster (selection-block order, as measured in the
    2026-06-11 profiling) reorders to priority order: talker slot 9 → 1."""
    selection_order = [
        "curator", "janitor", "distiller", "surveyor", "mail", "brief",
        "routine", "bit", "talker", "instructor", "daily_sync",
        "pending_items_pusher", "cloudflared",
    ]
    ordered = order_tools(selection_order)
    assert ordered[0] == "talker"
    assert ordered[1] == "cloudflared"
    assert ordered[2:5] == ["bit", "brief", "routine"]
    # Same membership — ordering only, selection untouched.
    assert sorted(ordered) == sorted(selection_order)
    # Batch daemons follow in SPAWN_PRIORITY order.
    assert ordered[5:] == [
        "curator", "janitor", "distiller", "surveyor", "mail",
        "instructor", "daily_sync", "pending_items_pusher",
    ]


def test_order_tools_stable_for_unknown_tools() -> None:
    """Belt-and-braces: an unlisted tool sorts after every listed one,
    preserving relative selection order (stable sort)."""
    ordered = order_tools(["future_tool_b", "talker", "future_tool_a"])
    assert ordered == ["talker", "future_tool_b", "future_tool_a"]


def test_order_tools_partial_roster() -> None:
    """A light-instance roster (Hypatia-shaped) keeps talker first."""
    ordered = order_tools(["instructor", "talker", "pending_items_pusher"])
    assert ordered == ["talker", "instructor", "pending_items_pusher"]
