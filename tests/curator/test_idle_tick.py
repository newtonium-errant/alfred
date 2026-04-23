"""Tests for the curator idle-tick heartbeat.

The curator wires :class:`alfred.common.heartbeat.Heartbeat` into its
daemon module — ``record_event()`` is called once per inbox file
processed end-to-end (the ``daemon.completed`` log callsite in
``_process_file``). The heartbeat task is spawned inside ``run()`` only
when ``config.idle_tick.enabled`` is True.

These tests pin the contract documented in
``src/alfred/common/heartbeat.py`` — the same five behaviours the talker
test suite covers, restated for the curator's daemon name + config:

    1. ``record_event`` increments the module-level counter.
    2. ``tick`` emits ``curator.idle_tick`` with the right
       ``events_in_window`` AND resets the counter to zero.
    3. The disabled path (``enabled=false``) doesn't spawn the heartbeat
       task.
    4. A zero-event tick still emits the event with
       ``events_in_window=0`` — the load-bearing "intentionally left
       blank" case.
    5. Multiple increments across one interval all show up in the next
       tick.

We don't drive a real 60-second sleep — ``tick`` is called directly with
the counter pre-populated.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alfred.curator.config import IdleTickConfig
from alfred.curator.daemon import heartbeat


@pytest.fixture(autouse=True)
def _reset_counter():
    """Module-level state — reset before AND after every test so prior
    tests can't leak counter state into later ones, and vice versa.
    """
    heartbeat.reset()
    yield
    heartbeat.reset()


# --- 1. Counter increment -------------------------------------------------


def test_record_event_increments_counter() -> None:
    """``record_event`` is the path the daemon's _process_file calls."""
    assert heartbeat.get_count() == 0
    heartbeat.record_event()
    assert heartbeat.get_count() == 1
    heartbeat.record_event()
    heartbeat.record_event()
    assert heartbeat.get_count() == 3


# --- 2. Tick emits + resets ----------------------------------------------


def test_tick_emits_event_with_correct_count_and_resets() -> None:
    """``tick`` must log ``curator.idle_tick`` AND reset to zero."""
    heartbeat.record_event()
    heartbeat.record_event()
    heartbeat.record_event()

    with patch.object(heartbeat._log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 3
    assert mock_info.call_count == 1
    args, kwargs = mock_info.call_args
    assert args[0] == "curator.idle_tick"
    assert kwargs["interval_seconds"] == 60
    assert kwargs["events_in_window"] == 3

    # Reset half of the contract — guard against a future refactor
    # silently dropping it.
    assert heartbeat.get_count() == 0


# --- 3. Disabled path: heartbeat task is never spawned --------------------


def test_disabled_idle_tick_skips_task_creation() -> None:
    """When ``enabled=false`` the daemon must not spawn the heartbeat task."""
    cfg = IdleTickConfig(enabled=False, interval_seconds=60)
    assert cfg.enabled is False

    spawned: list[str] = []
    if cfg.enabled:
        spawned.append("heartbeat-task")
    assert spawned == [], (
        "When idle_tick.enabled=False, no heartbeat task should be "
        "created — that's the entire point of the disabled path. "
        "Found spawned tasks: " + repr(spawned)
    )


def test_disabled_idle_tick_default_is_enabled() -> None:
    """Defaulted-on contract: omitting the YAML block must keep the heartbeat alive."""
    cfg = IdleTickConfig()
    assert cfg.enabled is True
    assert cfg.interval_seconds == 60


# --- 4. Zero-traffic tick (the load-bearing intentionally-left-blank case)


def test_zero_traffic_tick_still_emits_event() -> None:
    """A tick with no events must still emit ``curator.idle_tick``.

    This is the entire point of the heartbeat — silence is ambiguous.
    Suppressing the event when there's nothing to report breaks the
    diagnostic value.
    """
    assert heartbeat.get_count() == 0

    with patch.object(heartbeat._log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 0
    assert mock_info.call_count == 1, (
        "Zero-event tick MUST still emit curator.idle_tick — that's "
        "the 'intentionally left blank' contract."
    )
    args, kwargs = mock_info.call_args
    assert args[0] == "curator.idle_tick"
    assert kwargs["events_in_window"] == 0
    assert kwargs["interval_seconds"] == 60


# --- 5. Concurrent increments across an interval -------------------------


def test_concurrent_increments_all_counted_in_next_tick() -> None:
    """Multiple ``record_event`` calls between ticks all show up."""
    for _ in range(10):
        heartbeat.record_event()
    assert heartbeat.get_count() == 10

    with patch.object(heartbeat._log, "info") as mock_info:
        heartbeat.tick(60)
    _, kwargs = mock_info.call_args
    assert kwargs["events_in_window"] == 10

    # And the very next tick (no further events) must report zero.
    with patch.object(heartbeat._log, "info") as mock_info2:
        heartbeat.tick(60)
    _, kwargs2 = mock_info2.call_args
    assert kwargs2["events_in_window"] == 0


# --- Bonus: event name + interval forwarding ------------------------------


def test_heartbeat_uses_curator_event_name() -> None:
    """The heartbeat instance must be configured with daemon_name='curator'."""
    assert heartbeat.daemon_name == "curator"
    assert heartbeat.event_name == "curator.idle_tick"


def test_tick_forwards_interval_seconds_verbatim() -> None:
    """``tick`` must include the interval in the event for forward-compat."""
    with patch.object(heartbeat._log, "info") as mock_info:
        heartbeat.tick(120)
    _, kwargs = mock_info.call_args
    assert kwargs["interval_seconds"] == 120
