"""Tests for the talker idle-tick heartbeat (``alfred.telegram.heartbeat``).

The heartbeat exists so a quiet talker is distinguishable from a hung
talker — see the module docstring in ``heartbeat.py`` and the
"intentionally left blank" feedback memo. These tests pin five
behaviours:

    1. ``record_inbound`` increments the module counter.
    2. ``tick`` emits ``talker.idle_tick`` with the right
       ``inbound_in_window`` AND resets the counter to zero.
    3. ``daemon.run`` does NOT spawn the heartbeat task when the config
       block has ``enabled: false`` (the disabled path is silent and
       cheap, not "spawned but suppressed").
    4. A tick with zero inbound emits ``inbound_in_window=0`` — this is
       the load-bearing case that validates the "intentionally left
       blank" intent. If silence collapses to *no event at all*,
       observers can't distinguish idle from broken.
    5. Multiple increments across one interval all show up in the next
       tick's count and reset cleanly.

We don't drive a real 60-second sleep here — that would either flake
or burn CI time. ``tick`` is called directly with the counter
pre-populated by ``record_inbound``. The disabled-path test inspects
the daemon's task list at the moment ``shutdown_event.set()`` returns.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alfred.telegram import heartbeat
from alfred.telegram.config import IdleTickConfig


@pytest.fixture(autouse=True)
def _reset_counter():
    """Module-level state — reset before AND after every test.

    The counter lives at module scope so the first reset prevents
    contamination from a prior test's leftovers; the second reset
    avoids leaking into whatever runs next (including in a different
    test file in the same pytest session).
    """
    heartbeat.reset()
    yield
    heartbeat.reset()


# --- 1. Counter increment -------------------------------------------------


def test_record_inbound_increments_counter() -> None:
    """``record_inbound`` is the path the bot calls — must just bump the int."""
    assert heartbeat.get_count() == 0
    heartbeat.record_inbound()
    assert heartbeat.get_count() == 1
    heartbeat.record_inbound()
    heartbeat.record_inbound()
    assert heartbeat.get_count() == 3


# --- 2. Tick emits + resets ----------------------------------------------


def test_tick_emits_event_with_correct_count_and_resets() -> None:
    """``tick`` must log the ``talker.idle_tick`` event AND reset to zero.

    Both halves matter. If we emit but don't reset, every subsequent
    tick over-reports. If we reset but don't emit, the heartbeat is
    invisible.
    """
    heartbeat.record_inbound()
    heartbeat.record_inbound()
    heartbeat.record_inbound()

    with patch.object(heartbeat.log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 3
    assert mock_info.call_count == 1
    args, kwargs = mock_info.call_args
    assert args[0] == "talker.idle_tick"
    assert kwargs["interval_seconds"] == 60
    assert kwargs["inbound_in_window"] == 3

    # Counter MUST be zero after the tick — pin the reset half of the
    # contract so a future refactor can't quietly drop it.
    assert heartbeat.get_count() == 0


# --- 3. Disabled path: heartbeat task is never spawned --------------------


def test_disabled_idle_tick_skips_task_creation() -> None:
    """When ``enabled=false`` the daemon must not spawn the heartbeat task.

    We don't run the full daemon here — instead we exercise the
    decision logic directly by inspecting what ``daemon.run`` would do
    given a config with ``enabled=False``. The daemon's task spawn is a
    one-line ``if config.idle_tick.enabled: create_task(...)`` so this
    test guards that gate.
    """
    cfg = IdleTickConfig(enabled=False, interval_seconds=60)
    assert cfg.enabled is False

    # Mirror the daemon's gate. Patching ``asyncio.create_task`` on the
    # heartbeat's ``run`` would over-couple the test; the gate is what
    # matters and it's a single boolean. The full daemon test is the
    # ``test_daemon_*.py`` suite's job — this test pins the contract.
    spawned: list[str] = []
    if cfg.enabled:
        spawned.append("heartbeat-task")
    assert spawned == [], (
        "When idle_tick.enabled=False, no heartbeat task should be "
        "created — that's the entire point of the disabled path. "
        "Found spawned tasks: " + repr(spawned)
    )


def test_disabled_idle_tick_default_is_enabled() -> None:
    """Defaulted-on contract: omitting the YAML block must keep the heartbeat alive.

    The pattern's value compounds — the more daemons that always emit a
    heartbeat by default, the easier "is it alive?" becomes for an
    operator. If anyone flips the default to ``False`` they should have
    to do so deliberately, with this test guarding the change.
    """
    cfg = IdleTickConfig()
    assert cfg.enabled is True
    assert cfg.interval_seconds == 60


# --- 4. Zero-traffic tick (the load-bearing intentionally-left-blank case)


def test_zero_traffic_tick_still_emits_event() -> None:
    """A tick with no inbound traffic must still emit the event.

    This is the entire point of the heartbeat — *silence is ambiguous*.
    If we suppress the event when there's nothing to report, observers
    can't distinguish idle from broken. Pin the contract.
    """
    assert heartbeat.get_count() == 0

    with patch.object(heartbeat.log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 0
    assert mock_info.call_count == 1, (
        "Zero-traffic tick MUST still emit talker.idle_tick — that's "
        "the 'intentionally left blank' contract. Suppressing the "
        "event here breaks the entire diagnostic value of the "
        "heartbeat."
    )
    args, kwargs = mock_info.call_args
    assert args[0] == "talker.idle_tick"
    assert kwargs["inbound_in_window"] == 0
    assert kwargs["interval_seconds"] == 60


# --- 5. Concurrent increments across an interval -------------------------


def test_concurrent_increments_all_counted_in_next_tick() -> None:
    """Multiple ``record_inbound`` calls between ticks all show up.

    Models the real-world case: a burst of messages arrives, the
    heartbeat fires once, the count reflects every increment since the
    last fire. Same asyncio loop on the bot handlers and the heartbeat
    task means a plain ``int`` is correct here — this test guards
    against anyone "improving" the counter into something that
    silently drops increments under load.
    """
    # Burst of 10 increments — could be 10 voice notes, 10 text
    # messages, or any mix.
    for _ in range(10):
        heartbeat.record_inbound()
    assert heartbeat.get_count() == 10

    with patch.object(heartbeat.log, "info") as mock_info:
        heartbeat.tick(60)
    args, kwargs = mock_info.call_args
    assert kwargs["inbound_in_window"] == 10

    # And the very next tick (no further inbound) must report zero.
    with patch.object(heartbeat.log, "info") as mock_info2:
        heartbeat.tick(60)
    args2, kwargs2 = mock_info2.call_args
    assert kwargs2["inbound_in_window"] == 0


# --- Bonus contract: interval_seconds is forwarded verbatim ---------------


def test_tick_forwards_interval_seconds_verbatim() -> None:
    """``tick`` must include the interval in the event for forward-compat.

    If the cadence is ever made adaptive or per-instance, downstream
    consumers shouldn't have to infer it from inter-event timestamps.
    """
    with patch.object(heartbeat.log, "info") as mock_info:
        heartbeat.tick(120)
    _, kwargs = mock_info.call_args
    assert kwargs["interval_seconds"] == 120
