"""``/recap`` slash command tests — queue #10 part 2 (2026-05-18).

Read-only mid-session recap. Spec:

  * ``/recap``         → brief (default)
  * ``/recap brief``   → explicit brief
  * ``/recap verbose`` → full 6-bucket structured summary
  * Any other arg      → help reply, no state change

Capture-session gate:
  * No active session             → "(no active capture session…)" reply
  * Active session, not capture   → same shape (recap is capture-specific)

Read-only contract:
  * Session stays open
  * No vault records created
  * No mutations to active session dict
  * No state.save() calls

LLM-failure isolation:
  * summarize_capture_session_so_far swallows internal LLM errors and
    returns error markdown; handler renders directly. Operator sees
    an error in chat, never a broken bot.
"""

from __future__ import annotations

import copy
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import bot, capture_extract
from tests.telegram.conftest import (
    FakeAnthropicClient, FakeBlock, FakeResponse,
)


# --- Mocks: update + ctx shape (mirror end_zettel_end_note test patterns) -


def _make_update_mock(chat_id: int = 42, user_id: int = 42) -> MagicMock:
    """Telegram-Update-shape mock with the minimum surface the handler
    consults: message.reply_text (awaitable), effective_user.id (for
    _is_allowed), effective_chat.id."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_ctx_mock(
    state_mgr,
    *,
    allowed_user_id: int = 42,
    client: Any = None,
    args: list[str] | None = None,
    model: str = "claude-sonnet-4-6",
) -> MagicMock:
    """Telegram-context-shape mock with bot_data + args.

    ``ctx.args`` is the PTB-parsed arg list (whitespace-split portion
    of the message after the command word). ``/recap`` → [], ``/recap
    brief`` → ["brief"].
    """
    config = MagicMock()
    config.allowed_users = [allowed_user_id]
    config.primary_users = ["person/Andrew Newton"]
    config.anthropic = MagicMock()
    config.anthropic.model = model
    ctx = MagicMock()
    ctx.application.bot_data = {
        bot._KEY_CONFIG: config,
        bot._KEY_STATE: state_mgr,
        bot._KEY_CLIENT: client or FakeAnthropicClient([]),
    }
    ctx.args = args if args is not None else []
    return ctx


def _brief_recap_response(
    topics: list[str] | None = None,
    key_insights: list[str] | None = None,
) -> FakeResponse:
    return FakeResponse(
        content=[
            FakeBlock(
                type="tool_use",
                id="toolu_brief",
                name="emit_brief_recap",
                input={
                    "topics": topics or [],
                    "key_insights": key_insights or [],
                },
            ),
        ],
        stop_reason="tool_use",
    )


def _verbose_summary_response(
    topics: list[str] | None = None,
    key_insights: list[str] | None = None,
) -> FakeResponse:
    return FakeResponse(
        content=[
            FakeBlock(
                type="tool_use",
                id="toolu_verbose",
                name="emit_structured_summary",
                input={
                    "topics": topics or [],
                    "decisions": [],
                    "open_questions": [],
                    "action_items": [],
                    "key_insights": key_insights or [],
                    "raw_contradictions": [],
                },
            ),
        ],
        stop_reason="tool_use",
    )


def _seed_active_capture_session(
    state_mgr,
    chat_id: int = 42,
    transcript: list[dict[str, Any]] | None = None,
    *,
    session_type: str = "capture",
) -> dict[str, Any]:
    """Seed an active capture session in the state manager and return
    the active dict so tests can verify it stays unmutated."""
    active = {
        "session_id": "abc-uuid",
        "chat_id": chat_id,
        "started_at": "2026-05-18T10:00:00+00:00",
        "transcript": transcript or [
            {"role": "user", "content": "I'm reading Meditations",
             "_ts": "2026-05-18T10:00:00+00:00"},
        ],
        "_session_type": session_type,
    }
    state_mgr.state.setdefault("active_sessions", {})[str(chat_id)] = active
    state_mgr.save()
    return active


# --- Capture-session gate ------------------------------------------------


@pytest.mark.asyncio
async def test_recap_no_active_session_replies_help(state_mgr) -> None:
    """No active session → helpful reply, no LLM call."""
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr)
    await bot.on_recap(update, ctx)
    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.call_args.args[0]
    assert "no active capture session" in reply.lower()
    assert "/capture" in reply
    # LLM not invoked.
    client = ctx.application.bot_data[bot._KEY_CLIENT]
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_recap_non_capture_session_replies_help(state_mgr) -> None:
    """Active session exists but session_type is 'note' (regular chat),
    not 'capture' → recap-not-applicable reply."""
    _seed_active_capture_session(
        state_mgr, transcript=[{"role": "user", "content": "x"}],
        session_type="note",
    )
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr)
    await bot.on_recap(update, ctx)
    update.message.reply_text.assert_awaited_once()
    reply = update.message.reply_text.call_args.args[0]
    assert "no active capture session" in reply.lower()
    # LLM not invoked.
    client = ctx.application.bot_data[bot._KEY_CLIENT]
    assert client.messages.calls == []


# --- Default / brief / verbose mode dispatch -----------------------------


@pytest.mark.asyncio
async def test_recap_default_mode_is_brief(state_mgr) -> None:
    """``/recap`` with no args → brief recap."""
    _seed_active_capture_session(state_mgr)
    client = FakeAnthropicClient([
        _brief_recap_response(topics=["stoicism"], key_insights=["control"]),
    ])
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, client=client, args=[])
    await bot.on_recap(update, ctx)
    reply = update.message.reply_text.call_args.args[0]
    assert "## Recap (brief)" in reply
    assert "- stoicism" in reply
    assert "- control" in reply


@pytest.mark.asyncio
async def test_recap_explicit_brief(state_mgr) -> None:
    """``/recap brief`` → brief recap, same shape as default."""
    _seed_active_capture_session(state_mgr)
    client = FakeAnthropicClient([
        _brief_recap_response(topics=["t1"], key_insights=["i1"]),
    ])
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, client=client, args=["brief"])
    await bot.on_recap(update, ctx)
    reply = update.message.reply_text.call_args.args[0]
    assert "## Recap (brief)" in reply
    assert "- t1" in reply


@pytest.mark.asyncio
async def test_recap_verbose(state_mgr) -> None:
    """``/recap verbose`` → 6-bucket structured summary, NOT brief shape."""
    _seed_active_capture_session(state_mgr)
    client = FakeAnthropicClient([
        _verbose_summary_response(topics=["t"], key_insights=["i"]),
    ])
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, client=client, args=["verbose"])
    await bot.on_recap(update, ctx)
    reply = update.message.reply_text.call_args.args[0]
    assert "## Recap (verbose)" in reply
    # All 6 verbose section headings present.
    for heading in [
        "### Topics",
        "### Decisions",
        "### Open Questions",
        "### Action Items",
        "### Key Insights",
        "### Raw Contradictions",
    ]:
        assert heading in reply, f"missing verbose heading: {heading!r}"
    # No re-encounter section (mid-session).
    assert "Re-encounters" not in reply


@pytest.mark.asyncio
async def test_recap_case_insensitive_args(state_mgr) -> None:
    """``/recap VERBOSE`` / ``/recap Brief`` lowercased before dispatch."""
    _seed_active_capture_session(state_mgr)
    client = FakeAnthropicClient([
        _brief_recap_response(topics=["x"]),
    ])
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, client=client, args=["BRIEF"])
    await bot.on_recap(update, ctx)
    reply = update.message.reply_text.call_args.args[0]
    assert "## Recap (brief)" in reply


# --- Garbage args → help reply -------------------------------------------


@pytest.mark.asyncio
async def test_recap_garbage_arg_emits_help(state_mgr) -> None:
    """``/recap medium`` → usage help, no LLM call, no state change."""
    active_before = _seed_active_capture_session(state_mgr)
    transcript_before = copy.deepcopy(active_before["transcript"])
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, args=["medium"])
    await bot.on_recap(update, ctx)
    reply = update.message.reply_text.call_args.args[0]
    assert "usage" in reply.lower()
    assert "/recap" in reply
    # No LLM call fired.
    client = ctx.application.bot_data[bot._KEY_CLIENT]
    assert client.messages.calls == []
    # State unmutated.
    active_after = state_mgr.get_active(42)
    assert active_after is not None
    assert active_after["transcript"] == transcript_before


@pytest.mark.asyncio
async def test_recap_multiple_args_emit_help(state_mgr) -> None:
    """``/recap brief extra`` → usage help (more than 1 arg is garbage)."""
    _seed_active_capture_session(state_mgr)
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, args=["brief", "extra"])
    await bot.on_recap(update, ctx)
    reply = update.message.reply_text.call_args.args[0]
    assert "usage" in reply.lower()


# --- Read-only contract --------------------------------------------------


@pytest.mark.asyncio
async def test_recap_does_not_close_session(state_mgr) -> None:
    """Session stays open after recap — operator continues capturing."""
    _seed_active_capture_session(state_mgr)
    client = FakeAnthropicClient([
        _brief_recap_response(topics=["x"]),
    ])
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, client=client)
    await bot.on_recap(update, ctx)
    # Session still in active_sessions after recap.
    active_after = state_mgr.get_active(42)
    assert active_after is not None
    assert active_after["_session_type"] == "capture"


@pytest.mark.asyncio
async def test_recap_does_not_mutate_active_session_dict(state_mgr) -> None:
    """Active session dict is not modified by the recap handler.

    Snapshot the dict before; compare after. Read-only contract.
    """
    active_before = _seed_active_capture_session(state_mgr)
    snapshot = copy.deepcopy(active_before)
    client = FakeAnthropicClient([
        _brief_recap_response(topics=["x"], key_insights=["y"]),
    ])
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, client=client)
    await bot.on_recap(update, ctx)
    active_after = state_mgr.get_active(42)
    assert active_after == snapshot, (
        f"recap mutated active session dict.\n"
        f"before: {snapshot}\nafter: {active_after}"
    )


@pytest.mark.asyncio
async def test_recap_no_vault_writes(state_mgr, tmp_path, monkeypatch) -> None:
    """Read-only contract: no vault_create / vault_edit fired.

    Monkey-patch the vault ops module's create/edit to fail-loud if
    accidentally called by the recap path.
    """
    _seed_active_capture_session(state_mgr)
    client = FakeAnthropicClient([
        _brief_recap_response(topics=["x"]),
    ])
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, client=client)

    from alfred.vault import ops as _ops

    def _fail_create(*_a, **_kw):
        raise AssertionError("recap should not call vault_create")

    def _fail_edit(*_a, **_kw):
        raise AssertionError("recap should not call vault_edit")

    monkeypatch.setattr(_ops, "vault_create", _fail_create)
    monkeypatch.setattr(_ops, "vault_edit", _fail_edit)

    # Should not raise — recap path does no vault ops.
    await bot.on_recap(update, ctx)
    update.message.reply_text.assert_awaited_once()


# --- Empty-transcript path -----------------------------------------------


@pytest.mark.asyncio
async def test_recap_empty_transcript_returns_placeholder(state_mgr) -> None:
    """0-message capture session → ``(no captures yet)`` placeholder
    reply, no LLM call."""
    _seed_active_capture_session(state_mgr, transcript=[])
    client = FakeAnthropicClient([])  # no responses; LLM shouldn't be called
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, client=client)
    await bot.on_recap(update, ctx)
    reply = update.message.reply_text.call_args.args[0]
    assert "(no captures yet" in reply
    assert client.messages.calls == []


# --- LLM-failure isolation ----------------------------------------------


@pytest.mark.asyncio
async def test_recap_llm_failure_renders_error_in_chat(state_mgr) -> None:
    """LLM call failure → operator sees error markdown in chat, NOT a
    broken bot. summarize_capture_session_so_far swallows the
    exception internally; handler forwards the error string."""
    _seed_active_capture_session(state_mgr)
    # No tool_use block in the response → run_brief_recap_structuring
    # raises RuntimeError, which summarize_capture_session_so_far
    # catches and renders to error markdown.
    client = FakeAnthropicClient([
        FakeResponse(
            content=[FakeBlock(type="text", text="i refuse")],
            stop_reason="end_turn",
        ),
    ])
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr, client=client)
    await bot.on_recap(update, ctx)
    reply = update.message.reply_text.call_args.args[0]
    assert "## Recap (brief)" in reply
    assert "Recap failed" in reply
    # Reply was sent (operator gets the error message in chat).
    update.message.reply_text.assert_awaited_once()


# --- _is_allowed gating --------------------------------------------------


@pytest.mark.asyncio
async def test_recap_disallowed_user_short_circuits(state_mgr) -> None:
    """User ID not in ``config.allowed_users`` → handler returns early,
    no reply, no LLM call."""
    _seed_active_capture_session(state_mgr)
    client = FakeAnthropicClient([])
    update = _make_update_mock(user_id=999)  # NOT in allowed_users
    ctx = _make_ctx_mock(state_mgr, client=client, allowed_user_id=42)
    await bot.on_recap(update, ctx)
    update.message.reply_text.assert_not_called()
    assert client.messages.calls == []


# --- Registration --------------------------------------------------------


def test_recap_handler_registered_in_build_app() -> None:
    """CommandHandler for ``/recap`` is registered in build_app.

    Indirect check via source inspection — building a real app needs
    a full TalkerConfig + token + so on. Reading bot.py source for the
    literal ``CommandHandler("recap"`` substring catches the
    "registration accidentally removed" failure mode.
    """
    import inspect
    src = inspect.getsource(bot)
    assert 'CommandHandler("recap", on_recap)' in src


def test_on_recap_exists_and_is_coroutine() -> None:
    """Public-surface pin: ``on_recap`` is defined and is a coroutine
    function (PTB CommandHandler expects async)."""
    import inspect
    assert callable(bot.on_recap)
    assert inspect.iscoroutinefunction(bot.on_recap)
