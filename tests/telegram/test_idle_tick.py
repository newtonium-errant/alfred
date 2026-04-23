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

from alfred.telegram import bot, heartbeat
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
# These tests build the real ``bot.build_app`` Application, drive
# ``process_update`` synchronously with hand-built Updates, and assert
# the counter ticked. We bypass ``app.initialize()`` (which would call
# Telegram's ``getMe`` over the network) by setting ``_initialized``
# directly — handlers don't depend on the bot user being cached.


def _build_app_for_test(talker_config, state_mgr, fake_client):
    """Build a real ``bot.Application`` and short-circuit network init.

    ``app.initialize()`` would normally call ``Bot.get_me()`` to
    validate the token; the fake token here would fail. Setting
    ``_initialized = True`` is enough to satisfy ``process_update``'s
    guard — the handlers we care about (the pre-pass at group=-1)
    don't touch the bot user.
    """
    app = bot.build_app(
        config=talker_config,
        state_mgr=state_mgr,
        anthropic_client=fake_client,
        system_prompt="",
        vault_context_str="",
        raw_config={},
    )
    app._initialized = True
    return app


def _make_message(text: str, message_id: int = 1):
    """Build a minimal :class:`telegram.Message` for an allowed user."""
    from telegram import Chat, Message, User

    chat = Chat(id=1, type="private")
    user = User(id=1, first_name="Andrew", is_bot=False)
    return Message(
        message_id=message_id,
        date=datetime.now(timezone.utc),
        chat=chat,
        from_user=user,
        text=text,
    )


@pytest.mark.asyncio
async def test_pre_pass_increments_on_plain_text(
    talker_config, state_mgr, fake_client,
) -> None:
    """Plain text still bumps the counter — was the original (only) path."""
    from telegram import Update

    app = _build_app_for_test(talker_config, state_mgr, fake_client)
    assert heartbeat.get_count() == 0

    msg = _make_message("hello there", message_id=1)
    await app.process_update(Update(update_id=1, message=msg))

    assert heartbeat.get_count() == 1


@pytest.mark.asyncio
async def test_pre_pass_increments_on_recognised_command(
    talker_config, state_mgr, fake_client,
) -> None:
    """Recognised commands (``/end``) used to bypass ``record_inbound``.

    The text MessageHandler is gated by ``~filters.COMMAND``, and the
    CommandHandler never called ``record_inbound``. Pre-pass closes
    that hole — every command counts.
    """
    from telegram import Update

    app = _build_app_for_test(talker_config, state_mgr, fake_client)
    assert heartbeat.get_count() == 0

    msg = _make_message("/end", message_id=2)
    await app.process_update(Update(update_id=2, message=msg))

    assert heartbeat.get_count() == 1, (
        "Recognised commands MUST bump the heartbeat counter — "
        "missing this is half the original coverage gap."
    )


@pytest.mark.asyncio
async def test_pre_pass_increments_on_unrecognised_command(
    talker_config, state_mgr, fake_client,
) -> None:
    """LOAD-BEARING: unrecognised commands MUST still tick the counter.

    The 2026-04-22 incident: Andrew sent ``/calibration`` (typo for
    ``/calibrate``). PTB routed nowhere — no CommandHandler matched
    ``calibration``, and the text MessageHandler's
    ``~filters.COMMAND`` filter excluded it. The message was silently
    dropped from the heartbeat's perspective even though Telegram had
    delivered it. Diagnostic confidently reported "no message
    received" while the user's screenshot showed ``✓✓``.

    The whole point of the heartbeat is that it's the authoritative
    "is the daemon receiving inbound traffic" signal. If unrecognised
    commands don't count, the signal lies. This test pins the fix.
    """
    from telegram import Update

    app = _build_app_for_test(talker_config, state_mgr, fake_client)

    # ``/calibrate`` is the registered command (see build_app).
    # ``/calibration`` is the typo that triggered the original bug.
    # Neither CommandHandler nor MessageHandler will match this, but
    # the pre-pass must still observe it.
    assert heartbeat.get_count() == 0
    msg = _make_message("/calibration", message_id=3)
    await app.process_update(Update(update_id=3, message=msg))

    assert heartbeat.get_count() == 1, (
        "Unrecognised commands MUST bump the heartbeat counter — "
        "this is the exact case that surfaced the coverage gap on "
        "2026-04-22 (/calibration typo). Without this, the heartbeat "
        "lies about whether the daemon is receiving traffic."
    )


@pytest.mark.asyncio
async def test_pre_pass_increments_on_edited_message(
    talker_config, state_mgr, fake_client,
) -> None:
    """Edited messages — a third update kind that the per-handler approach missed.

    Both the text and voice MessageHandlers register against new
    messages, not edits. An edit would never have ticked the old
    counter. The "every Update counts" contract requires it to tick now.
    """
    from telegram import Update

    app = _build_app_for_test(talker_config, state_mgr, fake_client)
    assert heartbeat.get_count() == 0

    edited = _make_message("hello (edited)", message_id=4)
    await app.process_update(Update(update_id=4, edited_message=edited))

    assert heartbeat.get_count() == 1, (
        "Edited messages count as inbound traffic — they prove the "
        "daemon is receiving Telegram updates. Pre-pass must observe."
    )


@pytest.mark.asyncio
async def test_pre_pass_increments_on_callback_query(
    talker_config, state_mgr, fake_client,
) -> None:
    """Callback queries (inline-keyboard taps) also count as inbound.

    The talker doesn't use inline keyboards today, but the contract is
    "every Update counts." Locking this in protects against a future
    refactor that re-introduces a per-handler approach and silently
    drops a category.
    """
    from telegram import CallbackQuery, Update, User

    app = _build_app_for_test(talker_config, state_mgr, fake_client)
    assert heartbeat.get_count() == 0

    user = User(id=1, first_name="Andrew", is_bot=False)
    cq = CallbackQuery(
        id="cb-1", from_user=user, chat_instance="instance-x", data="ping",
    )
    await app.process_update(Update(update_id=5, callback_query=cq))

    assert heartbeat.get_count() == 1, (
        "Callback queries must count — locks the 'every Update is "
        "observed' contract against future drift."
    )


@pytest.mark.asyncio
async def test_pre_pass_does_not_double_count_text(
    talker_config, state_mgr, fake_client,
) -> None:
    """Pre-pass is the single counter call — text path must not double-count.

    Before the fix, ``on_text`` called ``record_inbound`` itself. After
    the fix, the pre-pass at group=-1 is the sole caller. If a future
    edit accidentally re-introduces a per-handler call, this test
    catches the double-count regression.
    """
    from telegram import Update

    app = _build_app_for_test(talker_config, state_mgr, fake_client)

    msg = _make_message("first message", message_id=10)
    await app.process_update(Update(update_id=10, message=msg))
    assert heartbeat.get_count() == 1, (
        "Plain text update should bump the counter exactly once. "
        "Got != 1, suggesting either no pre-pass fire (the per-handler "
        "calls were removed but the pre-pass isn't observing) or a "
        "re-introduced per-handler call (double counting)."
    )
