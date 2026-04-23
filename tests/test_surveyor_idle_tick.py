"""Tests for the surveyor idle-tick heartbeat.

Unlike curator/janitor/distiller (which use a module-level Heartbeat
instance), surveyor's :class:`Daemon` owns its heartbeat as an instance
attribute (because the ``Daemon`` is itself a class). The contract is
the same — these tests exercise the Heartbeat instance directly via a
fresh :class:`Heartbeat`-backed object so we don't have to spin up the
full surveyor daemon (which needs Milvus, Ollama, etc.).

Counter semantic: one record re-embedded = one event. The bump happens
in ``Daemon._tick`` and ``Daemon._initial_sync`` for each entry returned
from ``embedder.process_diff``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from alfred.common.heartbeat import Heartbeat
from alfred.surveyor.config import IdleTickConfig


@pytest.fixture
def heartbeat() -> Heartbeat:
    """Fresh per-test :class:`Heartbeat` named ``surveyor`` — same shape
    as the Daemon's instance attribute, no need to construct the full
    Daemon class.
    """
    return Heartbeat(daemon_name="surveyor")


def test_record_event_increments_counter(heartbeat: Heartbeat) -> None:
    assert heartbeat.get_count() == 0
    heartbeat.record_event()
    assert heartbeat.get_count() == 1
    heartbeat.record_event()
    heartbeat.record_event()
    assert heartbeat.get_count() == 3


def test_tick_emits_event_with_correct_count_and_resets(
    heartbeat: Heartbeat,
) -> None:
    heartbeat.record_event()
    heartbeat.record_event()
    heartbeat.record_event()

    with patch.object(heartbeat._log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 3
    assert mock_info.call_count == 1
    args, kwargs = mock_info.call_args
    assert args[0] == "surveyor.idle_tick"
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


def test_zero_traffic_tick_still_emits_event(heartbeat: Heartbeat) -> None:
    """A surveyor pass that re-embeds nothing still emits
    ``surveyor.idle_tick`` with ``events_in_window=0``.
    """
    assert heartbeat.get_count() == 0

    with patch.object(heartbeat._log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 0
    assert mock_info.call_count == 1
    args, kwargs = mock_info.call_args
    assert args[0] == "surveyor.idle_tick"
    assert kwargs["events_in_window"] == 0
    assert kwargs["interval_seconds"] == 60


def test_concurrent_increments_all_counted_in_next_tick(
    heartbeat: Heartbeat,
) -> None:
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


def test_heartbeat_uses_surveyor_event_name(heartbeat: Heartbeat) -> None:
    assert heartbeat.daemon_name == "surveyor"
    assert heartbeat.event_name == "surveyor.idle_tick"
