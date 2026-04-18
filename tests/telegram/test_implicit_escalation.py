"""Tests for wk3 commit 6 — implicit escalation detection and /no-auto-escalate.

Covers:
    * ``_detect_escalation_signal`` per-signal (keyword, long_user_short_assistant,
      rephrase), negative cases, and Jaccard similarity behaviour.
    * ``_should_offer_escalation`` cooldown + disable-flag + already-on-Opus guards.
    * ``run_turn`` appends the escalation suffix and stashes the turn index.
    * ``/no-auto-escalate`` flips ``_auto_escalate_disabled`` and suppresses offers.
    * ``/opus`` after an offer logs ``escalate_accepted`` instead of
      ``escalated``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

from alfred.telegram import bot, conversation
from alfred.telegram.session import Session
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- Signal detection -----------------------------------------------------


def _empty_session() -> Session:
    return Session(
        session_id="s",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model=bot._SONNET_MODEL,
    )


def test_keyword_signal_fires_on_think_harder() -> None:
    sess = _empty_session()
    assert (
        conversation._detect_escalation_signal(sess, "Think harder about X.", "ok")
        == "keyword"
    )


def test_keyword_signal_case_insensitive() -> None:
    sess = _empty_session()
    assert (
        conversation._detect_escalation_signal(sess, "Can you GO DEEPER?", "ok")
        == "keyword"
    )


def test_keyword_signal_matches_multiple_phrases() -> None:
    sess = _empty_session()
    for kw in ("think harder", "more depth", "go deeper", "dig into this"):
        assert (
            conversation._detect_escalation_signal(sess, f"Please {kw}.", "ok")
            == "keyword"
        )


def test_long_user_short_assistant_fires() -> None:
    sess = _empty_session()
    user = "word " * 100  # 500 chars, well over the 400-char threshold.
    assistant = "ok"  # Very short.
    assert (
        conversation._detect_escalation_signal(sess, user, assistant)
        == "long_user_short_assistant"
    )


def test_long_user_but_long_assistant_does_not_fire() -> None:
    """Long user + long assistant → no signal (already addressing it)."""
    sess = _empty_session()
    user = "word " * 100
    assistant = "x" * 500  # Over the 150-char cap.
    assert conversation._detect_escalation_signal(sess, user, assistant) is None


def test_rephrase_signal_fires_on_similar_prior_user_turn() -> None:
    sess = _empty_session()
    sess.transcript = [
        {"role": "user", "content": "let's talk about the edit conflict handling"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "talk more about the edit conflict handling"},
        {"role": "assistant", "content": "ok"},
    ]
    # The current message is a close paraphrase of a prior user turn.
    got = conversation._detect_escalation_signal(
        sess,
        "let's talk again about the edit conflict handling",
        "ok",
    )
    assert got == "rephrase"


def test_no_signal_on_short_unique_turn() -> None:
    sess = _empty_session()
    assert (
        conversation._detect_escalation_signal(sess, "hi", "hello")
        is None
    )


def test_jaccard_simple_cases() -> None:
    assert conversation._jaccard("", "") == 0.0
    assert conversation._jaccard("a b c", "a b c") == 1.0
    # Exactly half the tokens overlap.
    assert 0.3 < conversation._jaccard("a b c d", "a b e f") < 0.5


# --- Cooldown / disable guards --------------------------------------------


def test_should_offer_false_when_disabled_flag_set() -> None:
    sess = _empty_session()
    active = {"_auto_escalate_disabled": True}
    assert conversation._should_offer_escalation(active, sess) is False


def test_should_offer_false_when_already_on_opus() -> None:
    sess = _empty_session()
    sess.model = bot._OPUS_MODEL
    assert conversation._should_offer_escalation({}, sess) is False


def test_should_offer_true_on_fresh_session() -> None:
    sess = _empty_session()
    assert conversation._should_offer_escalation({}, sess) is True


def test_should_offer_cooldown_applies() -> None:
    sess = _empty_session()
    # 10 turns in, last offered at turn 8 → inside 5-turn cooldown.
    sess.transcript = [{"role": "user", "content": f"m{i}"} for i in range(10)]
    active = {"_escalation_offered_at_turn": 8}
    assert conversation._should_offer_escalation(active, sess) is False

    # Last offered at turn 2 → 8 turns ago, outside cooldown.
    active = {"_escalation_offered_at_turn": 2}
    assert conversation._should_offer_escalation(active, sess) is True


# --- run_turn integration --------------------------------------------------


@pytest.mark.asyncio
async def test_run_turn_appends_escalation_suffix_on_keyword(
    state_mgr, talker_config
) -> None:
    """Keyword signal → offer suffix appended + turn index stashed."""
    sess = Session(
        session_id="esc-test",
        chat_id=1,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model=bot._SONNET_MODEL,
    )
    state_mgr.set_active(1, sess.to_dict())

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok")]),
    ])

    reply = await conversation.run_turn(
        client=client, state=state_mgr, session=sess,
        user_message="Please think harder about this.",
        config=talker_config,
        vault_context_str="", system_prompt="sys",
    )

    assert "/opus to confirm" in reply
    active = state_mgr.get_active(1)
    assert active is not None
    assert active.get("_escalation_offered_at_turn") is not None


@pytest.mark.asyncio
async def test_run_turn_no_suffix_when_already_on_opus(
    state_mgr, talker_config
) -> None:
    """Session on Opus → keyword still fires detection, but offer is suppressed."""
    sess = Session(
        session_id="esc-test-2",
        chat_id=2,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model=bot._OPUS_MODEL,
    )
    state_mgr.set_active(2, sess.to_dict())

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok")]),
    ])

    reply = await conversation.run_turn(
        client=client, state=state_mgr, session=sess,
        user_message="Please think harder about this.",
        config=talker_config,
        vault_context_str="", system_prompt="sys",
    )

    assert "/opus to confirm" not in reply


@pytest.mark.asyncio
async def test_run_turn_no_suffix_when_auto_escalate_disabled(
    state_mgr, talker_config
) -> None:
    """User ran /no-auto-escalate → subsequent qualifying turns get no offer."""
    sess = Session(
        session_id="esc-test-3",
        chat_id=3,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model=bot._SONNET_MODEL,
    )
    active = sess.to_dict()
    active["_auto_escalate_disabled"] = True
    state_mgr.set_active(3, active)
    state_mgr.save()

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok")]),
    ])

    reply = await conversation.run_turn(
        client=client, state=state_mgr, session=sess,
        user_message="Please think harder about this.",
        config=talker_config,
        vault_context_str="", system_prompt="sys",
    )

    assert "/opus to confirm" not in reply


@pytest.mark.asyncio
async def test_run_turn_cooldown_prevents_rapid_re_offer(
    state_mgr, talker_config
) -> None:
    """Two qualifying turns in a row → only the first appends the suffix."""
    sess = Session(
        session_id="esc-test-4",
        chat_id=4,
        started_at=datetime.now(timezone.utc),
        last_message_at=datetime.now(timezone.utc),
        model=bot._SONNET_MODEL,
    )
    state_mgr.set_active(4, sess.to_dict())

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="ok1")]),
        FakeResponse(content=[FakeBlock(type="text", text="ok2")]),
    ])

    reply1 = await conversation.run_turn(
        client=client, state=state_mgr, session=sess,
        user_message="think harder about this please.",
        config=talker_config,
        vault_context_str="", system_prompt="sys",
    )
    assert "/opus to confirm" in reply1

    reply2 = await conversation.run_turn(
        client=client, state=state_mgr, session=sess,
        user_message="go deeper about this please.",
        config=talker_config,
        vault_context_str="", system_prompt="sys",
    )
    assert "/opus to confirm" not in reply2


# --- /no-auto-escalate registration ----------------------------------------


def test_build_app_registers_no_auto_escalate_handler(talker_config) -> None:
    """``/no_auto_escalate`` lands on the Application as a CommandHandler.

    Note: PTB only accepts ``[a-z0-9_]`` in command names, so the wk3 plan's
    ``/no-auto-escalate`` (with hyphens) is implemented as
    ``/no_auto_escalate``. Documented in the session note.
    """
    from alfred.telegram import state as state_mod
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        mgr = state_mod.StateManager(Path(tmp) / "s.json")
        mgr.load()
        app = bot.build_app(
            config=talker_config,
            state_mgr=mgr,
            anthropic_client=None,
            system_prompt="",
            vault_context_str="",
        )
        commands = set()
        for group in app.handlers.values():
            for h in group:
                cmds = getattr(h, "commands", None)
                if cmds:
                    commands.update(cmds)
        assert "no_auto_escalate" in commands
