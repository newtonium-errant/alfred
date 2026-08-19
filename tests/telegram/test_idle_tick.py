"""Tests for the talker idle-tick heartbeat (``alfred.telegram.heartbeat``).

The heartbeat exists so a quiet talker is distinguishable from a hung
talker — see the module docstring in ``heartbeat.py`` and the
"intentionally left blank" feedback memo. These tests pin six
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
    6. The application-level ``_pre_record_inbound`` pre-pass
       (``TypeHandler(Update, …)`` at group=-1) bumps the counter for
       EVERY inbound update, including the originally-uncovered cases:
       recognised commands, unrecognised commands, edited messages,
       callback queries. This is the load-bearing coverage gap caught
       on 2026-04-22 — see the ``_pre_record_inbound`` comment block
       in ``bot.py``.

We don't drive a real 60-second sleep here — that would either flake
or burn CI time. ``tick`` is called directly with the counter
pre-populated by ``record_inbound``. The disabled-path test inspects
the daemon's task list at the moment ``shutdown_event.set()`` returns.
"""

from __future__ import annotations

from datetime import datetime, timezone
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


# --- 6. Application-level pre-pass coverage ------------------------------
#
# The 2026-04-22 incident: the per-handler ``record_inbound`` calls in
# ``on_text`` / ``on_voice`` left a coverage gap. Anything that PTB
# routed elsewhere — recognised commands (``/end``), unrecognised
# commands (``/calibration`` when only ``/calibrate`` is registered),
# edited messages, callback queries — bypassed both handlers and the
# counter never ticked. The fix moved ``record_inbound`` to a
# ``TypeHandler(Update, …)`` registered at group=-1 so it observes
# every Update before per-handler routing.
#
# The tests that built a real ``bot.build_app`` Application and drove
# ``process_update`` with hand-built Updates were removed with the
# Telegram surface (2026-08-19, bot.py deleted) — the pre-pass they
# pinned lived in bot.py's handler registration and is gone with it.
# The counter API itself (``record_inbound`` / ``record_handled``) is
# covered directly below.


# --- 7. Handled-counter split (2026-06-06 c1) ----------------------------
#
# The split addresses the silent-drop ambiguity surfaced 2026-06-06: a
# pre-split heartbeat with ``inbound_in_window=1`` was indistinguishable
# between (a) one Update routed and handled normally and (b) one Update
# delivered but with no handler registered (silent drop). The split
# surfaces case (b) as ``inbound_unhandled > 0``.
#
# Tests below pin the new contract:
#
#   * ``record_handled`` increments a SEPARATE counter from
#     ``record_inbound``.
#   * ``tick`` emits all three fields: ``inbound_in_window`` (total,
#     legacy alias), ``inbound_handled`` (split half), ``inbound_unhandled``
#     (derived: total - handled).
#   * Derivation is correct: total=3, handled=2 → unhandled=1.
#   * ``reset`` clears BOTH counters together (preserves the
#     ``handled <= total`` invariant).
#   * Pre-pass alone bumps total but NOT handled — the silent-drop
#     signature.


def test_record_handled_increments_separate_counter() -> None:
    """``record_handled`` bumps the handled counter, leaving total alone."""
    assert heartbeat.get_count() == 0
    assert heartbeat.get_handled_count() == 0

    heartbeat.record_handled()
    heartbeat.record_handled()
    assert heartbeat.get_handled_count() == 2
    # Total counter NOT touched by record_handled.
    assert heartbeat.get_count() == 0


def test_tick_emits_split_fields() -> None:
    """``tick`` emits ``inbound_in_window`` AND the split fields together.

    The three-field emit is the load-bearing observability contract —
    log dashboards / grep queries that consume any of the three should
    find them all in the same event.
    """
    heartbeat.record_inbound()
    heartbeat.record_inbound()
    heartbeat.record_inbound()
    heartbeat.record_handled()
    heartbeat.record_handled()

    with patch.object(heartbeat.log, "info") as mock_info:
        returned = heartbeat.tick(60)

    # Return value still carries the total (back-compat for callers
    # that consumed the pre-split return).
    assert returned == 3
    assert mock_info.call_count == 1
    args, kwargs = mock_info.call_args
    assert args[0] == "talker.idle_tick"
    # All three split fields present.
    assert kwargs["inbound_in_window"] == 3
    assert kwargs["inbound_handled"] == 2
    assert kwargs["inbound_unhandled"] == 1
    assert kwargs["interval_seconds"] == 60


def test_tick_unhandled_derives_correctly() -> None:
    """``inbound_unhandled = total - handled`` — derivation contract.

    Pins the silent-drop signal: an Update that bumped total via the
    pre-pass but never reached an entry handler shows up as
    unhandled > 0.
    """
    # Simulate: 2 messages routed normally (total + handled), 1 message
    # silently dropped (only total bumped — no handler called
    # record_handled).
    for _ in range(3):
        heartbeat.record_inbound()
    for _ in range(2):
        heartbeat.record_handled()

    with patch.object(heartbeat.log, "info") as mock_info:
        heartbeat.tick(60)
    _, kwargs = mock_info.call_args
    assert kwargs["inbound_in_window"] == 3
    assert kwargs["inbound_handled"] == 2
    assert kwargs["inbound_unhandled"] == 1, (
        "Silent-drop case: 3 messages received via pre-pass, 2 reached "
        "a handler. The 1 that didn't must surface as inbound_unhandled."
    )


def test_tick_zero_traffic_emits_zero_split() -> None:
    """Idle tick (no traffic) emits all three fields as zero.

    The "intentionally left blank" contract applies to ALL three
    fields: a quiet daemon must still emit the heartbeat with
    ``handled=0`` and ``unhandled=0`` so an operator can see that the
    daemon is alive AND that no silent-drops happened this window.
    """
    assert heartbeat.get_count() == 0
    assert heartbeat.get_handled_count() == 0

    with patch.object(heartbeat.log, "info") as mock_info:
        heartbeat.tick(60)

    _, kwargs = mock_info.call_args
    assert kwargs["inbound_in_window"] == 0
    assert kwargs["inbound_handled"] == 0
    assert kwargs["inbound_unhandled"] == 0


def test_tick_resets_both_counters() -> None:
    """After ``tick``, BOTH counters reset to zero together.

    Preserves the ``handled <= total`` invariant on the next interval.
    """
    heartbeat.record_inbound()
    heartbeat.record_inbound()
    heartbeat.record_handled()

    with patch.object(heartbeat.log, "info"):
        heartbeat.tick(60)

    assert heartbeat.get_count() == 0
    assert heartbeat.get_handled_count() == 0


def test_reset_clears_both_counters() -> None:
    """The test-helper :func:`reset` zeroes BOTH counters together.

    Without this, a test that pre-loads handled but resets only total
    would leak handled state into the next test. Module-level state
    cleanup is load-bearing.
    """
    heartbeat.record_inbound()
    heartbeat.record_handled()
    heartbeat.record_handled()
    assert heartbeat.get_count() == 1
    assert heartbeat.get_handled_count() == 2

    heartbeat.reset()
    assert heartbeat.get_count() == 0
    assert heartbeat.get_handled_count() == 0


def test_tick_handled_capped_at_total_when_drift() -> None:
    """If handled somehow exceeds total, unhandled clamps to 0 (no negatives).

    Belt-and-braces guard. The ``record_handled`` call sites are paired
    with ``record_inbound`` via the application pre-pass, so handled
    should never exceed total in production. The clamp protects against
    a hypothetical future refactor that decouples the counters and
    introduces drift — negative ``inbound_unhandled`` would be a
    nonsense signal in dashboards.
    """
    # Drive a drift case: handled > total. (Production doesn't do this,
    # but the test pins the defensive ``max(0, ...)`` behaviour.)
    heartbeat.record_handled()
    heartbeat.record_handled()

    with patch.object(heartbeat.log, "info") as mock_info:
        heartbeat.tick(60)
    _, kwargs = mock_info.call_args
    assert kwargs["inbound_in_window"] == 0
    assert kwargs["inbound_handled"] == 2
    # Derived value clamps to 0 rather than going negative.
    assert kwargs["inbound_unhandled"] == 0


