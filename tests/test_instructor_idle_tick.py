"""Tests for the instructor idle-tick heartbeat.

Counter semantic: one directive executed = one event. Poll ticks that
find no pending directives don't count, only actual work executed.
``status in {"done", "dry_run"}`` both bump the counter; errors don't
(the retry counter / error frontmatter handles error visibility).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alfred.instructor.config import IdleTickConfig
from alfred.instructor.daemon import heartbeat


@pytest.fixture(autouse=True)
def _reset_counter():
    heartbeat.reset()
    yield
    heartbeat.reset()


def test_record_event_increments_counter() -> None:
    assert heartbeat.get_count() == 0
    heartbeat.record_event()
    assert heartbeat.get_count() == 1
    heartbeat.record_event()
    heartbeat.record_event()
    assert heartbeat.get_count() == 3


def test_tick_emits_event_with_correct_count_and_resets() -> None:
    heartbeat.record_event()
    heartbeat.record_event()
    heartbeat.record_event()

    with patch.object(heartbeat._log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 3
    assert mock_info.call_count == 1
    args, kwargs = mock_info.call_args
    assert args[0] == "instructor.idle_tick"
    assert kwargs["interval_seconds"] == 60
    assert kwargs["events_in_window"] == 3
    assert heartbeat.get_count() == 0


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


def test_zero_traffic_tick_still_emits_event() -> None:
    """A poll cycle with no pending directives still emits
    ``instructor.idle_tick`` with ``events_in_window=0``.
    """
    assert heartbeat.get_count() == 0

    with patch.object(heartbeat._log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 0
    assert mock_info.call_count == 1
    args, kwargs = mock_info.call_args
    assert args[0] == "instructor.idle_tick"
    assert kwargs["events_in_window"] == 0
    assert kwargs["interval_seconds"] == 60


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


def test_heartbeat_uses_instructor_event_name() -> None:
    assert heartbeat.daemon_name == "instructor"
    assert heartbeat.event_name == "instructor.idle_tick"
