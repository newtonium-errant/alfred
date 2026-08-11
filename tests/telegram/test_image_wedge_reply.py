"""#82 WARN-2 — the Telegram reply seam of the honest-error leg is WIRED.

The third of three box-side sites that translate an Anthropic failure into
something an operator reads. `tests/test_image_wedge_guards.py` proves the
classifier returns the right copy, and stays green against a build where no
site calls it; `tests/test_web_routes_chat.py` covers the two web seams. This
covers the one that reaches Andrew's phone.

Telegram wedges the same way the web chat does. Its photos arrive
pre-compressed so an oversized *arrival* is unlikely, but history accumulates
across turns exactly as it does on the web, so a deterministic 400 repeats on
every subsequent turn. "Try again in a moment?" is the same wrong advice there.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest

from alfred.telegram import bot
from alfred.telegram import conversation as conversation_mod

_LIVE_MSG = (
    "messages.11.content.0.image.source.base64: image dimensions exceed max "
    "allowed size for many-image requests: 2000 pixels"
)


class _DimensionAPIError(anthropic.APIError):
    """A real ``anthropic.APIError`` subclass — the handler catches that type.

    ``APIError.__init__`` wants a request object; bypassing it to ``Exception``
    keeps the fixture honest about what matters here (the isinstance check and
    the ``.body`` payload) without constructing an httpx request.
    """

    def __init__(self) -> None:
        Exception.__init__(self, _LIVE_MSG)
        self.message = _LIVE_MSG
        self.body = {"error": {"type": "invalid_request_error", "message": _LIVE_MSG}}


class _PlainAPIError(anthropic.APIError):
    """An unrecognised API error — must keep the generic reply."""

    def __init__(self) -> None:
        Exception.__init__(self, "overloaded_error: try later")
        self.message = "overloaded_error: try later"
        self.body = {"error": {"type": "overloaded_error", "message": "overloaded"}}


def _make_update(text: str, chat_id: int = 501) -> MagicMock:
    update = MagicMock()
    update.message = MagicMock()
    update.message.text = text
    update.message.caption = None
    update.message.reply_text = AsyncMock()
    update.effective_chat = SimpleNamespace(id=chat_id)
    update.effective_user = SimpleNamespace(id=chat_id, username="andrew")
    return update


def _make_ctx(config, state_mgr, client) -> MagicMock:
    ctx = MagicMock()
    ctx.application.bot_data = {
        "config": config,
        "state_mgr": state_mgr,
        "anthropic_client": client,
        "system_prompt": "",
        "vault_context_str": "",
        "chat_locks": {},
    }
    return ctx


async def _drive(update, ctx, exc: Exception) -> None:
    """Run one turn whose run_turn raises ``exc``, restoring the symbol after."""
    async def _boom(**_kwargs):
        raise exc

    orig = conversation_mod.run_turn
    try:
        conversation_mod.run_turn = _boom
        await bot.handle_message(update, ctx, text=update.message.text, voice=False)
    finally:
        conversation_mod.run_turn = orig


def _reply_text(update) -> str:
    assert update.message.reply_text.await_count >= 1, "no reply was sent"
    return " ".join(
        str(c.args[0]) for c in update.message.reply_text.await_args_list if c.args
    )


@pytest.mark.asyncio
async def test_dimension_400_reply_is_actionable_not_try_again(
    state_mgr, talker_config, fake_client,
) -> None:
    """The operator-visible surface: never "try again" for a deterministic 400."""
    update = _make_update("what's on page 3?")
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    await _drive(update, ctx, _DimensionAPIError())

    reply = _reply_text(update).lower()
    assert "try again" not in reply
    assert "new chat" in reply
    # Names the actual cause, so the operator can tell this from an outage.
    assert "image" in reply


@pytest.mark.asyncio
async def test_unrecognised_api_error_keeps_the_generic_reply(
    state_mgr, talker_config, fake_client,
) -> None:
    """The abstain half.

    Without this, classifying EVERY APIError as image_too_large would pass the
    pin above while telling an operator with a transient overload to go start a
    new chat — advice that is wrong in the opposite direction.
    """
    update = _make_update("hello")
    ctx = _make_ctx(talker_config, state_mgr, fake_client)

    await _drive(update, ctx, _PlainAPIError())

    reply = _reply_text(update).lower()
    assert "try again" in reply
    assert "new chat" not in reply
