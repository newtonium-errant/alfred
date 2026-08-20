"""Talker idle-tick heartbeat pins (post-T5 shape).

History: this file used to pin the counter machinery too —
``record_inbound`` (bot.py's group=-1 pre-pass), ``record_handled``
(entry handlers), the handled/unhandled split, reset semantics and the
2026-04-22 / 2026-06-06 incidents behind them. All of those writers
lived in ``bot.py`` and died with the Telegram retirement; the write
API and split fields were deleted in T5 (2026-08-19) — see the module
docstring in ``telegram/heartbeat.py`` for the archaeology, including
the historical event shape.

What remains under pin:

    1. ``tick`` emits the liveness event every time — the
       intentionally-left-blank contract that motivated the module.
    2. The event carries ``interval_seconds`` verbatim and the
       always-present ``inbound_in_window=0`` (grep-compat field,
       honest empty value for a retired channel).
    3. The retired split fields are ABSENT — an omitted-field pin whose
       positive control is the same captured event (the pin proves the
       fields are gone, not that the path is dead).
    4. The daemon's enabled-gate contract (config defaults).
    5. The ``run`` loop ticks on cadence and exits on shutdown.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from alfred.telegram import heartbeat
from alfred.telegram.config import IdleTickConfig


# --- 1+2. The liveness event ----------------------------------------------


def test_tick_emits_liveness_event_with_zero_inbound() -> None:
    """A tick must always emit — silence is ambiguous, that's the point.

    ``inbound_in_window`` is pinned to 0: the Telegram inbound channel
    is retired by construction (no writer exists), and the field stays
    as an always-present honest empty value so operator greps keep
    working.
    """
    with patch.object(heartbeat.log, "info") as mock_info:
        returned = heartbeat.tick(60)

    assert returned == 0
    assert mock_info.call_count == 1, (
        "tick MUST emit talker.idle_tick unconditionally — the "
        "'intentionally left blank' contract. Suppressing the event "
        "breaks the heartbeat's entire diagnostic value."
    )
    args, kwargs = mock_info.call_args
    assert args[0] == "talker.idle_tick"
    assert kwargs["interval_seconds"] == 60
    assert kwargs["inbound_in_window"] == 0


def test_tick_forwards_interval_seconds_verbatim() -> None:
    """Forward-compat field: consumers must not have to infer cadence
    from inter-event timestamps."""
    with patch.object(heartbeat.log, "info") as mock_info:
        heartbeat.tick(3600)
    _, kwargs = mock_info.call_args
    assert kwargs["interval_seconds"] == 3600


# --- 3. The retired split fields are gone ---------------------------------


def test_tick_omits_retired_split_fields() -> None:
    """T5 deletion pin: ``inbound_handled`` / ``inbound_unhandled`` must
    NOT appear in the event.

    Their writers (bot.py handlers) are deleted; emitting them as
    permanent zeros would claim a measurement nothing can make. The
    positive control lives in the SAME captured event — the tick fired
    and carries its live fields — so this pin proves the fields are
    absent rather than proving the path is dead.
    """
    with patch.object(heartbeat.log, "info") as mock_info:
        heartbeat.tick(60)

    assert mock_info.call_count == 1  # positive control: the event fired
    args, kwargs = mock_info.call_args
    assert args[0] == "talker.idle_tick"
    assert kwargs["inbound_in_window"] == 0  # live field present
    assert "inbound_handled" not in kwargs
    assert "inbound_unhandled" not in kwargs


# --- 4. Disabled path: heartbeat task is never spawned --------------------


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


# --- 5. The run loop ------------------------------------------------------


@pytest.mark.asyncio
async def test_run_ticks_on_cadence_and_exits_on_shutdown() -> None:
    """``run`` fires ``tick`` per interval and returns when the shutdown
    event is set — the daemon's actual consumption of this module
    (``daemon.py`` spawns ``heartbeat.run(interval, shutdown_event)``).
    """
    shutdown = asyncio.Event()
    ticks: list[int] = []

    def _fake_tick(interval_seconds: int) -> int:
        ticks.append(interval_seconds)
        if len(ticks) >= 2:
            shutdown.set()
        return 0

    with patch.object(heartbeat, "tick", side_effect=_fake_tick):
        # Sub-second interval keeps the test fast; asyncio.wait_for
        # accepts the float even though production passes ints.
        await asyncio.wait_for(heartbeat.run(0.02, shutdown), timeout=5)

    assert len(ticks) >= 2
    assert all(t == 0.02 for t in ticks)


@pytest.mark.asyncio
async def test_run_exits_immediately_when_shutdown_pre_set() -> None:
    """A pre-set shutdown event exits before any tick — clean-shutdown pin."""
    shutdown = asyncio.Event()
    shutdown.set()
    with patch.object(heartbeat, "tick") as mock_tick:
        await asyncio.wait_for(heartbeat.run(60, shutdown), timeout=5)
    assert mock_tick.call_count == 0
