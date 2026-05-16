"""Tests for the shared ``alfred.common.scheduled_daemon`` template.

Three production daemons (digest / radar_day / friction_analyzer)
delegate their loop to ``run_scheduled_daemon``. These tests cover
the template directly with a minimal stub ``fire`` callable; per-
daemon tests at:

* ``tests/test_radar_day_daemon.py``
* ``tests/test_daily_sync/test_friction_analyzer.py``
* (digest has no daemon-loop test)

continue to cover the daemons' ``fire_once`` BODIES — the work
each daemon does. This file covers the LOOP — what the template
owns now.

Schedule → sleep_until → fire → log → repeat is the nominal cycle.
Exception in ``fire`` must be caught and logged so a single failure
can't kill the loop. Cancellation must propagate cleanly so the
orchestrator's SIGTERM path is unchanged.

Per ``feedback_structlog_assertion_patterns.md``: async + structlog
code uses ``structlog.testing.capture_logs`` (NOT caplog). The
template runs in asyncio and emits via ``structlog.get_logger``;
``capture_logs`` is the working assertion path.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import structlog

from alfred.common.schedule import ScheduleConfig
from alfred.common.scheduled_daemon import run_scheduled_daemon


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_real_sleep(monkeypatch):
    """Monkeypatch ``sleep_until`` (drift-bounded chunked wall-clock
    waiter) AND ``asyncio.sleep`` (post-fire delay) to no-ops so the
    loop iterates at full speed under test.

    ``run_scheduled_daemon`` imports ``sleep_until`` at module-load
    time, so we patch the binding inside the template's namespace —
    NOT ``alfred.common.schedule.sleep_until`` directly. Same trap
    surfaced in ``test_schedule.py``'s sleep_until tests; matching
    that pattern.
    """
    async def _no_sleep_until(target):
        # Return a tiny float so the ``actual_seconds`` log field
        # has a sensible non-zero value for grep tests. The real
        # function returns wall-clock elapsed seconds.
        return 0.0

    async def _no_asyncio_sleep(seconds):
        return None

    monkeypatch.setattr(
        "alfred.common.scheduled_daemon.sleep_until",
        _no_sleep_until,
    )
    monkeypatch.setattr(
        "alfred.common.scheduled_daemon.asyncio.sleep",
        _no_asyncio_sleep,
    )


def _schedule_far_future() -> ScheduleConfig:
    """A daily schedule whose ``time`` is far enough into the future
    that ``compute_next_fire`` returns a target with positive
    sleep_seconds — exercises the ``sleeping`` / ``woke`` log path."""
    return ScheduleConfig(time="23:59", timezone="America/Halifax")


# ---------------------------------------------------------------------------
# Headline cycle — schedule → sleep_until → fire → log
# ---------------------------------------------------------------------------


async def test_one_full_cycle_emits_sleeping_woke_and_fires():
    """A single loop iteration emits the three load-bearing events
    in order: ``sleeping`` → ``woke`` → fire callable invoked."""
    fire_calls: list[datetime] = []

    async def _fire(now: datetime) -> Any:
        fire_calls.append(now)
        # After one fire, cancel the loop so the test ends.
        raise asyncio.CancelledError

    schedule = _schedule_far_future()
    with structlog.testing.capture_logs() as captured:
        with pytest.raises(asyncio.CancelledError):
            await run_scheduled_daemon(
                schedule=schedule,
                fire=_fire,
                log_namespace="test_ns",
            )

    # Fire was called exactly once.
    assert len(fire_calls) == 1
    # Fire received a tz-aware datetime in the schedule's timezone.
    assert fire_calls[0].tzinfo is not None

    events = [c["event"] for c in captured]
    # The sleeping → woke pair fired before the fire. Find them in order.
    assert "test_ns.sleeping" in events
    assert "test_ns.woke" in events
    # Sleeping must come before woke.
    assert events.index("test_ns.sleeping") < events.index("test_ns.woke")


async def test_log_namespace_parameterizes_all_events():
    """Custom ``log_namespace`` shows up in every emitted event so
    operator's grep-per-daemon stays clean."""
    iteration_count = 0

    async def _fire(now: datetime) -> Any:
        nonlocal iteration_count
        iteration_count += 1
        if iteration_count >= 1:
            raise asyncio.CancelledError

    with structlog.testing.capture_logs() as captured:
        with pytest.raises(asyncio.CancelledError):
            await run_scheduled_daemon(
                schedule=_schedule_far_future(),
                fire=_fire,
                log_namespace="custom_namespace",
            )

    captured_events = [c.get("event", "") for c in captured]
    # Every event emitted by the template starts with the namespace.
    template_events = [
        e for e in captured_events if e.startswith("custom_namespace.")
    ]
    assert len(template_events) >= 2  # sleeping + woke at minimum
    for event in template_events:
        assert event.startswith("custom_namespace.")


# ---------------------------------------------------------------------------
# Exception handling — fire() raises → caught → loop continues
# ---------------------------------------------------------------------------


async def test_fire_exception_caught_and_logged_loop_continues():
    """A non-Cancelled exception from ``fire`` is logged at exception
    level under ``<ns>.fire_error`` and the loop continues to the next
    cycle. Asserts that fire_error fires AND a second cycle starts."""
    fire_calls: list[int] = []

    async def _fire(now: datetime) -> Any:
        fire_calls.append(len(fire_calls))
        if len(fire_calls) == 1:
            # First iteration raises a normal exception.
            raise RuntimeError("simulated fire failure")
        # Second iteration cancels.
        raise asyncio.CancelledError

    with structlog.testing.capture_logs() as captured:
        with pytest.raises(asyncio.CancelledError):
            await run_scheduled_daemon(
                schedule=_schedule_far_future(),
                fire=_fire,
                log_namespace="test_ns",
            )

    # Loop iterated TWICE — exception didn't kill it.
    assert len(fire_calls) == 2

    # fire_error event was emitted on the first iteration's failure.
    fire_errors = [c for c in captured if c.get("event") == "test_ns.fire_error"]
    assert len(fire_errors) == 1
    # The captured log has log_level=error (structlog convention for
    # ``log.exception``) — exact key depends on capture_logs shape.
    assert fire_errors[0].get("log_level") == "error"


async def test_cancellation_propagates_to_caller():
    """``asyncio.CancelledError`` raised inside fire propagates out so
    the orchestrator's SIGTERM-driven cancellation path works.
    (RuntimeError stays inside; CancelledError must escape.)"""
    async def _fire(now: datetime) -> Any:
        raise asyncio.CancelledError

    # CancelledError escapes — the loop does NOT catch it.
    with pytest.raises(asyncio.CancelledError):
        await run_scheduled_daemon(
            schedule=_schedule_far_future(),
            fire=_fire,
            log_namespace="test_ns",
        )


# ---------------------------------------------------------------------------
# Knobs — post_fire_sleep_seconds + log= override
# ---------------------------------------------------------------------------


async def test_post_fire_sleep_seconds_passed_to_asyncio_sleep(monkeypatch):
    """The ``post_fire_sleep_seconds`` knob reaches asyncio.sleep so
    the daily_sync / bit migrations can use 60 instead of 90."""
    sleep_calls: list[float] = []

    async def _capturing_sleep(seconds):
        sleep_calls.append(seconds)
        return None

    # Re-patch (autouse fixture already patched once — overwrite it).
    monkeypatch.setattr(
        "alfred.common.scheduled_daemon.asyncio.sleep",
        _capturing_sleep,
    )

    async def _fire(now: datetime) -> Any:
        # First fire — let post-fire sleep happen, then cancel via
        # the next iteration.
        if len(sleep_calls) >= 1:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_scheduled_daemon(
            schedule=_schedule_far_future(),
            fire=_fire,
            log_namespace="test_ns",
            post_fire_sleep_seconds=42.5,
        )

    # The captured asyncio.sleep call(s) honoured the knob.
    assert sleep_calls
    assert sleep_calls[0] == 42.5


async def test_caller_supplied_log_used_instead_of_default():
    """When caller passes ``log=``, the template uses it. This lets
    each daemon's ``log = structlog.get_logger(__name__)`` continue
    to tag events with the daemon's module name."""
    # Use a stub logger that records calls so we can assert routing.
    stub_calls: list[tuple[str, str, dict]] = []

    class _StubLogger:
        def info(self, event, **kwargs):
            stub_calls.append(("info", event, kwargs))

        def exception(self, event, **kwargs):
            stub_calls.append(("exception", event, kwargs))

    stub = _StubLogger()

    async def _fire(now: datetime) -> Any:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_scheduled_daemon(
            schedule=_schedule_far_future(),
            fire=_fire,
            log_namespace="test_ns",
            log=stub,  # type: ignore[arg-type]
        )

    # The stub logger received the events directly.
    sleeping = [c for c in stub_calls if c[1] == "test_ns.sleeping"]
    assert len(sleeping) == 1


# ---------------------------------------------------------------------------
# fire receives tz-aware now matching the schedule's timezone
# ---------------------------------------------------------------------------


async def test_fire_receives_tz_aware_now_in_schedule_timezone():
    """The template hands ``fire`` a tz-aware datetime in the
    schedule's configured timezone — same contract as the
    pre-extraction shape (``datetime.now(tz)``)."""
    captured_now: list[datetime] = []

    async def _fire(now: datetime) -> Any:
        captured_now.append(now)
        raise asyncio.CancelledError

    schedule = ScheduleConfig(time="23:59", timezone="America/Halifax")
    with pytest.raises(asyncio.CancelledError):
        await run_scheduled_daemon(
            schedule=schedule,
            fire=_fire,
            log_namespace="test_ns",
        )

    assert len(captured_now) == 1
    now = captured_now[0]
    assert now.tzinfo is not None
    # tzinfo string-equals the schedule's tz (zoneinfo equality is
    # strict — comparing the resolved name works).
    assert str(now.tzinfo) == "America/Halifax"


# ---------------------------------------------------------------------------
# record_error_callback — Arc B P3 prerequisite for per-sub-daemon
# ``last_error`` capture. Helper-only ship; no callers wire it yet.
# ---------------------------------------------------------------------------


async def test_record_error_callback_invoked_on_tick_failure():
    """When the swallow path fires AND a callback is provided, the
    callback receives the standard ``f"{type(exc).__name__}: {exc}"``
    string exactly once."""
    captured_calls: list[str] = []

    def _record(msg: str) -> None:
        captured_calls.append(msg)

    fire_calls: list[int] = []

    async def _fire(now: datetime) -> Any:
        fire_calls.append(len(fire_calls))
        if len(fire_calls) == 1:
            raise RuntimeError("simulated fire failure")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await run_scheduled_daemon(
            schedule=_schedule_far_future(),
            fire=_fire,
            log_namespace="test_ns",
            record_error_callback=_record,
        )

    # Callback invoked exactly once, with the standard format.
    assert captured_calls == ["RuntimeError: simulated fire failure"]
    # Loop still iterated past the failure — second fire was reached.
    assert len(fire_calls) == 2


async def test_record_error_callback_none_preserves_existing_behavior():
    """Omitting ``record_error_callback`` (the default) leaves the
    pre-callback flow byte-for-byte unchanged: existing
    ``log.exception(<ns>.fire_error)`` still fires; the loop
    continues; no new log events appear."""
    fire_calls: list[int] = []

    async def _fire(now: datetime) -> Any:
        fire_calls.append(len(fire_calls))
        if len(fire_calls) == 1:
            raise RuntimeError("simulated fire failure")
        raise asyncio.CancelledError

    with structlog.testing.capture_logs() as captured:
        with pytest.raises(asyncio.CancelledError):
            await run_scheduled_daemon(
                schedule=_schedule_far_future(),
                fire=_fire,
                log_namespace="test_ns",
                # record_error_callback omitted — default None.
            )

    # Existing fire_error log still emitted exactly once.
    fire_errors = [
        c for c in captured if c.get("event") == "test_ns.fire_error"
    ]
    assert len(fire_errors) == 1
    assert fire_errors[0].get("log_level") == "error"

    # The callback-failure event must NOT appear when no callback is set.
    callback_failures = [
        c
        for c in captured
        if c.get("event") == "scheduled_daemon.record_error_callback_failed"
    ]
    assert callback_failures == []

    # Loop still iterated past the failure.
    assert len(fire_calls) == 2


async def test_record_error_callback_exception_does_not_break_swallow():
    """If the callback itself raises, the swallow path continues:
    the callback failure gets its own structured log event, the
    original ``log.exception`` for the underlying ``fire`` exception
    still fires, the loop continues, and the original exception is
    NOT propagated to the caller."""
    callback_invocations: list[str] = []

    def _raising_callback(msg: str) -> None:
        callback_invocations.append(msg)
        raise RuntimeError("callback exploded")

    fire_calls: list[int] = []

    async def _fire(now: datetime) -> Any:
        fire_calls.append(len(fire_calls))
        if len(fire_calls) == 1:
            raise ValueError("simulated fire failure")
        raise asyncio.CancelledError

    with structlog.testing.capture_logs() as captured:
        with pytest.raises(asyncio.CancelledError):
            await run_scheduled_daemon(
                schedule=_schedule_far_future(),
                fire=_fire,
                log_namespace="test_ns",
                record_error_callback=_raising_callback,
            )

    # The callback WAS invoked (and raised internally).
    assert callback_invocations == ["ValueError: simulated fire failure"]

    # The callback-failure log event fired exactly once.
    callback_failures = [
        c
        for c in captured
        if c.get("event") == "scheduled_daemon.record_error_callback_failed"
    ]
    assert len(callback_failures) == 1
    assert callback_failures[0].get("log_level") == "error"

    # The ORIGINAL fire_error log still fires — original exception not lost.
    fire_errors = [
        c for c in captured if c.get("event") == "test_ns.fire_error"
    ]
    assert len(fire_errors) == 1
    assert fire_errors[0].get("log_level") == "error"

    # Loop continued past the failure (second iteration reached + cancelled).
    assert len(fire_calls) == 2


# ---------------------------------------------------------------------------
# Idempotent re-import (ensures the module loads cleanly under
# multi-process orchestrator + test re-import patterns).
# ---------------------------------------------------------------------------


def test_module_exports_run_scheduled_daemon_only():
    """``__all__`` exposes only the public surface — guards against
    a future addition that accidentally promotes a private helper."""
    import alfred.common.scheduled_daemon as mod
    assert mod.__all__ == ["run_scheduled_daemon"]
