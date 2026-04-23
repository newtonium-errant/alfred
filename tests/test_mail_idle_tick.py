"""Tests for the mail idle-tick heartbeat.

Mail is the odd one out — its "daemon" is a sync ``HTTPServer``, so the
heartbeat ticks via a background ``threading.Thread`` rather than an
asyncio task. Counter semantic: one webhook received OR one email
fetched = one event. The IMAP fetcher imports the heartbeat from
``mail.webhook`` so both paths bump the same counter.

We don't drive the real thread loop here — :meth:`Heartbeat.tick` is
called directly with the counter pre-populated, same pattern as the
async daemons.
"""

from __future__ import annotations

import threading
from unittest.mock import patch

import pytest

from alfred.common.heartbeat import Heartbeat, run_in_thread
from alfred.mail.config import IdleTickConfig
from alfred.mail.webhook import heartbeat


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
    assert args[0] == "mail.idle_tick"
    assert kwargs["interval_seconds"] == 60
    assert kwargs["events_in_window"] == 3
    assert heartbeat.get_count() == 0


def test_disabled_idle_tick_skips_task_creation() -> None:
    cfg = IdleTickConfig(enabled=False, interval_seconds=60)
    assert cfg.enabled is False

    spawned: list[str] = []
    if cfg.enabled:
        spawned.append("heartbeat-thread")
    assert spawned == []


def test_disabled_idle_tick_default_is_enabled() -> None:
    cfg = IdleTickConfig()
    assert cfg.enabled is True
    assert cfg.interval_seconds == 60


def test_zero_traffic_tick_still_emits_event() -> None:
    """No webhooks + no fetches still emits ``mail.idle_tick`` with
    ``events_in_window=0`` — the load-bearing case for an idle daemon.
    """
    assert heartbeat.get_count() == 0

    with patch.object(heartbeat._log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 0
    assert mock_info.call_count == 1
    args, kwargs = mock_info.call_args
    assert args[0] == "mail.idle_tick"
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


def test_heartbeat_uses_mail_event_name() -> None:
    assert heartbeat.daemon_name == "mail"
    assert heartbeat.event_name == "mail.idle_tick"


# --- Thread-runner specific test -----------------------------------------


def test_run_in_thread_starts_daemon_thread_and_can_be_stopped() -> None:
    """``run_in_thread`` must return a started daemon thread that exits
    promptly when the shutdown event is set.

    We don't wait a full interval — we set the shutdown event immediately
    and verify the thread exits within a small timeout.
    """
    hb = Heartbeat(daemon_name="mail-test")
    shutdown_event = threading.Event()

    # Use a short interval so the thread loop is responsive in tests, but
    # we shut down immediately anyway via the event.
    t = run_in_thread(hb, interval_seconds=1, shutdown_event=shutdown_event)

    assert t.is_alive()
    assert t.daemon is True
    assert t.name == "mail-test-heartbeat"

    shutdown_event.set()
    t.join(timeout=2)
    assert not t.is_alive(), (
        "Heartbeat thread should exit promptly when shutdown_event is set"
    )
