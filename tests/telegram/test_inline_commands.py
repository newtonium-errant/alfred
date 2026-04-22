"""Tests for talker inline slash-commands.

Problem: PTB's ``CommandHandler`` only fires when the message text starts
with ``/command``. "Good. /end" lands on the text MessageHandler and the
session doesn't close. This module covers the detector and the dispatch
wired into :func:`handle_message`.

See ``vault/session/Talker inline commands 2026-04-19.md`` for the
shipped-change context and
``project_talker_inline_commands.md`` memory for the original bug report.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import bot


# --- _detect_inline_command pure-function tests ----------------------------


def test_detect_end_of_line_command() -> None:
    """End-of-line slash token is the primary inline-command shape."""
    assert bot._detect_inline_command("Good. /end") == "end"


def test_detect_end_of_line_with_trailing_whitespace() -> None:
    """Trailing whitespace after the command is tolerated."""
    assert bot._detect_inline_command("Good. /end   ") == "end"


def test_detect_start_of_message_with_prose() -> None:
    """``/opus please`` is start-of-message; inline path catches it too.

    Note: pure ``/opus`` is filtered out of the text MessageHandler by
    ``filters.TEXT & ~filters.COMMAND`` and never reaches the detector.
    ``/opus please`` DOES (it's a command with args) — PTB routes that to
    the CommandHandler though, so this path is mostly belt-and-braces.
    """
    assert bot._detect_inline_command("/opus please") == "opus"


def test_detect_is_case_insensitive() -> None:
    """Both all-caps and mixed case fire (users typing fast on mobile)."""
    assert bot._detect_inline_command("Good. /END") == "end"
    assert bot._detect_inline_command("Good. /End") == "end"


def test_detect_ignores_mid_message_slash() -> None:
    """``maybe I'll /end later`` is prose discussing the command, not invoking it."""
    assert bot._detect_inline_command("maybe I'll /end later") is None


def test_detect_ignores_unknown_commands() -> None:
    """Unknown slash tokens return None so they route to Claude as prose."""
    assert bot._detect_inline_command("what about /nonsense") is None
    assert bot._detect_inline_command("oh no /delete everything") is None


def test_detect_ignores_mid_word_slash() -> None:
    """``foo/end`` is a path-like token, not a command."""
    assert bot._detect_inline_command("look at foo/end please") is None


def test_detect_only_inspects_first_line() -> None:
    """A second-line ``/end`` doesn't silently close the session.

    Multi-line messages are rare but a user dictating a multi-paragraph
    thought and happening to end paragraph 2 with "/end this thread" is
    not trying to close their session. Keep the detector line-local.
    """
    text = "First line of prose.\nAnd a follow up. /end"
    assert bot._detect_inline_command(text) is None


def test_detect_first_line_command_with_later_prose() -> None:
    """``/end\nmaybe later we keep going`` still fires on first line."""
    text = "Good. /end\nand some notes about what I meant"
    assert bot._detect_inline_command(text) == "end"


def test_detect_all_supported_commands() -> None:
    """Every listed inline command is recognised at end-of-line."""
    for name in ["end", "opus", "sonnet", "no_auto_escalate", "status", "start"]:
        assert bot._detect_inline_command(f"Hi there. /{name}") == name, (
            f"inline command {name!r} should be detected"
        )


def test_detect_empty_input() -> None:
    """Empty / whitespace input returns None without crashing."""
    assert bot._detect_inline_command("") is None
    assert bot._detect_inline_command("   ") is None


def test_detect_just_command_as_start() -> None:
    """A message that's just ``/end`` still detects (backwards compat).

    In real usage PTB's CommandHandler fires first for a pure-command
    message, but the detector shouldn't reject the shape — if the
    detector ever runs on a pure command (test paths, future refactor),
    it should still return the command name.
    """
    assert bot._detect_inline_command("/end") == "end"


# --- Regression: punctuation-anchored boundary -----------------------------
#
# 2026-04-21: the previous regex ``(?:^|\s)/(\w+)\s*$`` matched ANY
# whitespace token before the slash. That false-positived on bare prose
# like "the road came to a /end" and "Goodbye /end" — both ended in a
# legitimate-looking ``\s/end\s*$`` shape but were clearly not commands.
# The fix tightens the boundary to either start-of-message or a sentence-
# terminating punctuation char (``.,!?;:``) followed by whitespace.


def test_detect_good_period_end_fires() -> None:
    """``Good. /end`` — the canonical inline-close shape."""
    assert bot._detect_inline_command("Good. /end") == "end"


def test_detect_pure_end_still_fires() -> None:
    """Standalone ``/end`` is still detected (start-of-message form)."""
    assert bot._detect_inline_command("/end") == "end"


def test_detect_mid_sentence_prose_does_not_fire() -> None:
    """``the road came to a /end`` is bare prose — no command intent."""
    assert bot._detect_inline_command("the road came to a /end") is None


def test_detect_goodbye_no_punctuation_does_not_fire() -> None:
    """``Goodbye /end`` lacks sentence-terminating punctuation — stays prose."""
    assert bot._detect_inline_command("Goodbye /end") is None


def test_detect_note_colon_extract_with_arg_fires() -> None:
    """``Note: /extract abc`` — colon counts as a sentence boundary."""
    assert bot._detect_inline_command("Note: /extract abc") == "extract"
    # And the arg is extractable through _parse_short_id_arg.
    assert bot._parse_short_id_arg("Note: /extract abc", None) == "abc"


def test_detect_with_arg_mid_prose_does_not_fire() -> None:
    """Bare-prose with-arg shapes don't fire either (regression for symmetry)."""
    assert bot._detect_inline_command(
        "the file we want to /extract abc123",
    ) is None
    assert bot._detect_inline_command("go faster /speed 1.5") is None


# --- handle_message dispatch integration -----------------------------------
#
# These tests exercise the wiring from ``handle_message`` through
# ``_dispatch_inline_command`` to the actual on_* handler. We use
# MagicMock-ed Update/Context objects so the handlers' own state
# mutations (``state_mgr.pop_active``) fire for real.


def _make_update(text: str, chat_id: int = 1, user_id: int = 1) -> MagicMock:
    """Build a minimal Update mock that passes the talker's allowlist + nulls."""
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.voice = None
    update.message.reply_text = AsyncMock()
    return update


def _make_ctx(config, state_mgr, client) -> MagicMock:
    """Build a Context whose ``application.bot_data`` matches ``build_app``."""
    ctx = MagicMock()
    ctx.application.bot_data = {
        "config": config,
        "state_mgr": state_mgr,
        "anthropic_client": client,
        "system_prompt": "sys",
        "vault_context_str": "",
        "chat_locks": {},
    }
    ctx.bot.send_chat_action = AsyncMock()
    return ctx


def _seed_active_session(state_mgr, chat_id: int, model: str | None = None) -> None:
    """Seed a minimal active session dict that survives ``Session.from_dict``."""
    now = datetime.now(timezone.utc).isoformat()
    # Per-chat session id so sequential closes in the same test don't
    # collide on the vault record filename. close_session derives the
    # short_id from ``session_id.split('-')[0]``, so the leading segment
    # has to differ between sessions.
    state_mgr.set_active(chat_id, {
        "session_id": f"chat{chat_id}-inline-test",
        "chat_id": chat_id,
        "started_at": now,
        "last_message_at": now,
        "model": model or bot._SONNET_MODEL,
        "opening_model": model or bot._SONNET_MODEL,
        "transcript": [],
        "vault_ops": [],
        "_vault_path_root": "",  # filled by specific tests
        "_user_vault_path": "person/Test",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "note",
        "_continues_from": None,
    })
    state_mgr.save()


@pytest.mark.asyncio
async def test_inline_end_closes_session(
    state_mgr, talker_config, fake_client, tmp_path,
) -> None:
    """``Good. /end`` dispatches to on_end → session closes, record written."""
    _seed_active_session(state_mgr, chat_id=1)
    # on_end needs a vault_path_root that exists for the session record.
    active = state_mgr.get_active(1)
    active["_vault_path_root"] = talker_config.vault.path
    state_mgr.set_active(1, active)
    state_mgr.save()

    update = _make_update("Good. /end", chat_id=1, user_id=1)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    await bot.handle_message(update, ctx, text="Good. /end", voice=False)

    # Session was popped from active_sessions.
    assert state_mgr.get_active(1) is None
    # on_end replied with "session closed." prefix.
    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args.args[0]
    assert reply.startswith("session closed."), (
        f"expected close-acknowledge reply, got: {reply!r}"
    )


@pytest.mark.asyncio
async def test_inline_opus_switches_model(
    state_mgr, talker_config, fake_client,
) -> None:
    """End-of-line ``/opus`` after sentence-terminating punctuation flips to Opus."""
    _seed_active_session(state_mgr, chat_id=2, model=bot._SONNET_MODEL)

    update = _make_update("Yes please. /opus", chat_id=2, user_id=1)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    await bot.handle_message(update, ctx, text="Yes please. /opus", voice=False)

    active = state_mgr.get_active(2)
    assert active is not None
    assert active["model"] == bot._OPUS_MODEL
    update.message.reply_text.assert_called_once()
    assert "Opus" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_inline_sonnet_switches_model(
    state_mgr, talker_config, fake_client,
) -> None:
    """End-of-line ``/sonnet`` after prose flips the active session to Sonnet."""
    _seed_active_session(state_mgr, chat_id=3, model=bot._OPUS_MODEL)

    update = _make_update("back to basics, /sonnet", chat_id=3, user_id=1)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    await bot.handle_message(update, ctx, text="back to basics, /sonnet", voice=False)

    active = state_mgr.get_active(3)
    assert active is not None
    assert active["model"] == bot._SONNET_MODEL
    update.message.reply_text.assert_called_once()
    assert "Sonnet" in update.message.reply_text.call_args.args[0]


@pytest.mark.asyncio
async def test_pure_command_still_works(
    state_mgr, talker_config, fake_client,
) -> None:
    """``/end`` alone still closes the session via the inline path.

    In production PTB routes pure commands to CommandHandler before
    handle_message runs, so the inline path doesn't fire. This test
    verifies the inline path WOULD still handle it if ever invoked
    directly — backwards compat for the detector's start-of-message rule.
    """
    _seed_active_session(state_mgr, chat_id=4)
    active = state_mgr.get_active(4)
    active["_vault_path_root"] = talker_config.vault.path
    state_mgr.set_active(4, active)
    state_mgr.save()

    update = _make_update("/end", chat_id=4, user_id=1)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    await bot.handle_message(update, ctx, text="/end", voice=False)

    assert state_mgr.get_active(4) is None


@pytest.mark.asyncio
async def test_ambiguous_mid_message_does_not_fire(
    state_mgr, talker_config, fake_client,
) -> None:
    """``maybe I'll /end later`` stays as prose — session remains open.

    Because the test-fake client has an empty response queue, handle_message
    will try to route this to ``run_turn`` after the inline pre-check fails.
    We short-circuit by mocking conversation.run_turn so the path completes.
    """
    _seed_active_session(state_mgr, chat_id=5)

    update = _make_update("maybe I'll /end later", chat_id=5, user_id=1)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    # Stub run_turn so the test doesn't need to stand up a full LLM fake.
    from alfred.telegram import conversation
    original_run_turn = conversation.run_turn
    try:
        conversation.run_turn = AsyncMock(return_value="you might!")
        await bot.handle_message(
            update, ctx, text="maybe I'll /end later", voice=False,
        )
    finally:
        conversation.run_turn = original_run_turn

    # Session is still active — the message was prose, not a command.
    assert state_mgr.get_active(5) is not None
    # run_turn was actually called (not short-circuited by inline dispatch).
    assert update.message.reply_text.called
    assert update.message.reply_text.call_args.args[0] == "you might!"


@pytest.mark.asyncio
async def test_unknown_inline_slash_ignored(
    state_mgr, talker_config, fake_client,
) -> None:
    """``/nonsense`` is not in the allowlist → prose pipeline runs."""
    _seed_active_session(state_mgr, chat_id=6)

    update = _make_update("try /nonsense", chat_id=6, user_id=1)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    from alfred.telegram import conversation
    original_run_turn = conversation.run_turn
    try:
        conversation.run_turn = AsyncMock(return_value="I don't know that command.")
        await bot.handle_message(update, ctx, text="try /nonsense", voice=False)
    finally:
        conversation.run_turn = original_run_turn

    # Session still active; LLM was asked.
    assert state_mgr.get_active(6) is not None
    assert update.message.reply_text.called


@pytest.mark.asyncio
async def test_inline_case_insensitive_dispatch(
    state_mgr, talker_config, fake_client,
) -> None:
    """``/END`` and ``/End`` both fire the end handler (case-insensitive)."""
    # /END
    _seed_active_session(state_mgr, chat_id=7)
    active = state_mgr.get_active(7)
    active["_vault_path_root"] = talker_config.vault.path
    state_mgr.set_active(7, active)
    state_mgr.save()

    update = _make_update("Good. /END", chat_id=7, user_id=1)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)
    await bot.handle_message(update, ctx, text="Good. /END", voice=False)
    assert state_mgr.get_active(7) is None

    # /End
    _seed_active_session(state_mgr, chat_id=8)
    active = state_mgr.get_active(8)
    active["_vault_path_root"] = talker_config.vault.path
    state_mgr.set_active(8, active)
    state_mgr.save()

    update = _make_update("ok. /End", chat_id=8, user_id=1)
    ctx = _make_ctx(talker_config, state_mgr, fake_client)
    await bot.handle_message(update, ctx, text="ok. /End", voice=False)
    assert state_mgr.get_active(8) is None
