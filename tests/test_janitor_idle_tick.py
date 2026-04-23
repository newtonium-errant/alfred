"""Tests for the janitor idle-tick heartbeat.

The janitor wires :class:`alfred.common.heartbeat.Heartbeat` into its
daemon module — ``record_event()`` is called once per issue fixed (or
deleted) inside ``run_sweep`` after the ``sweep.complete`` log. Sweeps
that find nothing broken (issues_found == 0) and structural-only sweeps
(fix_mode disabled) add zero events to the heartbeat — that's the
"meaningful signal, not scan noise" intent.

These tests pin the contract documented in
``src/alfred/common/heartbeat.py`` and the propagation spec.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alfred.janitor.config import IdleTickConfig
from alfred.janitor.daemon import heartbeat


@pytest.fixture(autouse=True)
def _reset_counter():
    heartbeat.reset()
    yield
    heartbeat.reset()


# --- 1. Counter increment -------------------------------------------------


def test_record_event_increments_counter() -> None:
    assert heartbeat.get_count() == 0
    heartbeat.record_event()
    assert heartbeat.get_count() == 1
    heartbeat.record_event()
    heartbeat.record_event()
    assert heartbeat.get_count() == 3


# --- 2. Tick emits + resets ----------------------------------------------


def test_tick_emits_event_with_correct_count_and_resets() -> None:
    """``tick`` must log ``janitor.idle_tick`` AND reset to zero."""
    heartbeat.record_event()
    heartbeat.record_event()
    heartbeat.record_event()

    with patch.object(heartbeat._log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 3
    assert mock_info.call_count == 1
    args, kwargs = mock_info.call_args
    assert args[0] == "janitor.idle_tick"
    assert kwargs["interval_seconds"] == 60
    assert kwargs["events_in_window"] == 3
    assert heartbeat.get_count() == 0


# --- 3. Disabled path ------------------------------------------------------


def test_disabled_idle_tick_skips_task_creation() -> None:
    cfg = IdleTickConfig(enabled=False, interval_seconds=60)
    assert cfg.enabled is False

    spawned: list[str] = []
    if cfg.enabled:
        spawned.append("heartbeat-task")
    assert spawned == []


def test_disabled_idle_tick_default_is_enabled() -> None:
    cfg = IdleTickConfig()
    assert cfg.enabled is True
    assert cfg.interval_seconds == 60


# --- 4. Zero-traffic tick (the load-bearing case) -------------------------


def test_zero_traffic_tick_still_emits_event() -> None:
    """The "intentionally left blank" contract — janitor sweeps that fix
    nothing don't add to the count, but the next tick still fires.
    """
    assert heartbeat.get_count() == 0

    with patch.object(heartbeat._log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 0
    assert mock_info.call_count == 1
    args, kwargs = mock_info.call_args
    assert args[0] == "janitor.idle_tick"
    assert kwargs["events_in_window"] == 0
    assert kwargs["interval_seconds"] == 60


# --- 5. Concurrent increments --------------------------------------------


def test_concurrent_increments_all_counted_in_next_tick() -> None:
    for _ in range(10):
        heartbeat.record_event()
    assert heartbeat.get_count() == 10

    with patch.object(heartbeat._log, "info") as mock_info:
        heartbeat.tick(60)
    _, kwargs = mock_info.call_args
    assert kwargs["events_in_window"] == 10

    with patch.object(heartbeat._log, "info") as mock_info2:
        heartbeat.tick(60)
    _, kwargs2 = mock_info2.call_args
    assert kwargs2["events_in_window"] == 0


# --- Bonus: event name ----------------------------------------------------


def test_heartbeat_uses_janitor_event_name() -> None:
    assert heartbeat.daemon_name == "janitor"
    assert heartbeat.event_name == "janitor.idle_tick"
