"""Integration pins for ``on_voice`` ↔ the STT fallback chain (M1).

Closes the code-reviewer WARN (spec decision #4, resolved → reprompt on
served-empty): the router correctly SERVEs a genuine-silence primary's
empty result (no backup re-spend), but ``on_voice`` must NOT forward that
blank into ``handle_message`` — it reprompts the user to type, the same as
``NoTranscript``. The no-re-spend stays at the router; the
reprompt-on-empty UX decision stays at the bot.

Harness mirrors test_document_handler.py: hand-rolled fakes for the PTB
Voice/Update/Context surfaces, monkeypatch the module symbols, AsyncMock
for the reply path. ``talker_config`` (conftest) allows user_id=1.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from alfred.telegram import heartbeat
from alfred.telegram.stt_backends import NoTranscript, SttResult


@pytest.fixture(autouse=True)
def _reset_counter():
    heartbeat.reset()
    yield
    heartbeat.reset()


class _FakeVoiceFile:
    async def download_as_bytearray(self) -> bytearray:
        return bytearray(b"FAKE-OGG-BYTES")


class _FakeVoice:
    def __init__(self, duration: int = 3) -> None:
        self.duration = duration

    async def get_file(self) -> _FakeVoiceFile:
        return _FakeVoiceFile()


def _build_update_and_ctx(talker_config, *, user_id: int = 1):
    reply = AsyncMock()
    update = type("U", (), {})()
    update.message = type("M", (), {})()
    update.message.voice = _FakeVoice()
    update.message.reply_text = reply
    update.effective_chat = type("C", (), {"id": 1})()
    update.effective_user = type("EU", (), {"id": user_id})()

    ctx = type("Ctx", (), {})()
    ctx.application = type("App", (), {"bot_data": {
        "config": talker_config,
        "state_mgr": None,
        "anthropic_client": None,
        "system_prompt": "",
        "vault_context_str": "",
        "chat_locks": {},
    }})()
    ctx.bot = type("B", (), {})()
    return update, ctx, reply


def _patch_router(monkeypatch, return_value):
    """Patch the router so on_voice gets a deterministic result, and stub
    build_chain (no real backends needed)."""
    from alfred.telegram import bot

    async def _fake_router(*args: Any, **kwargs: Any):
        return return_value

    monkeypatch.setattr(bot.stt_backends, "build_chain", lambda cfg: [])
    monkeypatch.setattr(
        bot.stt_backends, "transcribe_with_fallback", _fake_router,
    )


@pytest.mark.asyncio
async def test_on_voice_served_text_dispatches(talker_config, monkeypatch):
    """Happy path: a served non-empty transcript reaches handle_message."""
    from alfred.telegram import bot

    captured: dict[str, Any] = {}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)
    _patch_router(monkeypatch, SttResult(
        text="walk the dog", backend_id="groq-whisper", tier="comparable",
    ))

    update, ctx, reply = _build_update_and_ctx(talker_config)
    await bot.on_voice(update, ctx)

    assert "kwargs" in captured, "handle_message should have been invoked"
    assert captured["kwargs"]["text"] == "walk the dog"
    assert captured["kwargs"]["voice"] is True
    reply.assert_not_called()


@pytest.mark.asyncio
async def test_on_voice_served_empty_reprompts_not_blank(
    talker_config, monkeypatch,
):
    """THE WARN fix (decision #4): a SERVED empty/whitespace transcript
    (genuine-silence primary, served by the router without re-spend) must
    NOT be forwarded as a blank into handle_message — on_voice reprompts
    the user to type, same as NoTranscript."""
    from alfred.telegram import bot

    handle_called = {"n": 0}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        handle_called["n"] += 1

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)
    # Router SERVEs an empty result (has_speech_signal False → genuine
    # silence; no backup re-spend happened — that's the router's job).
    _patch_router(monkeypatch, SttResult(
        text="", backend_id="groq-whisper", tier="comparable",
        has_speech_signal=False,
    ))

    update, ctx, reply = _build_update_and_ctx(talker_config)
    await bot.on_voice(update, ctx)

    assert handle_called["n"] == 0, (
        "a served-empty transcript must NOT enter handle_message as a blank"
    )
    reply.assert_called_once()
    # The reprompt asks the user to type.
    (msg,), _ = reply.call_args
    assert "type" in msg.lower() or "transcribe" in msg.lower()


@pytest.mark.asyncio
async def test_on_voice_whitespace_only_reprompts(talker_config, monkeypatch):
    """Whitespace-only served text is also treated as empty → reprompt."""
    from alfred.telegram import bot

    handle_called = {"n": 0}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        handle_called["n"] += 1

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)
    _patch_router(monkeypatch, SttResult(
        text="   \n  ", backend_id="deepgram", tier="comparable",
    ))

    update, ctx, reply = _build_update_and_ctx(talker_config)
    await bot.on_voice(update, ctx)

    assert handle_called["n"] == 0
    reply.assert_called_once()


@pytest.mark.asyncio
async def test_on_voice_no_transcript_reprompts(talker_config, monkeypatch):
    """The existing NoTranscript path still reprompts (regression guard —
    kept alongside the new served-empty pin)."""
    from alfred.telegram import bot

    handle_called = {"n": 0}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        handle_called["n"] += 1

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)
    _patch_router(monkeypatch, NoTranscript(reason="all_failed"))

    update, ctx, reply = _build_update_and_ctx(talker_config)
    await bot.on_voice(update, ctx)

    assert handle_called["n"] == 0
    reply.assert_called_once()
