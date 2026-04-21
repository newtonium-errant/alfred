"""Failure-mode tests for wk2b commit 5 — TTS + /brief graceful degradation.

Covers:
    * /brief with no tts section → "not configured" reply, no LLM call.
    * /brief with tts section but ElevenLabs API down → falls back to a
      text reply with the compressed prose.
    * /brief with missing session → "not found" reply.
    * /brief with no structured summary → runs batch pass implicitly.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import bot, capture_batch, tts
from alfred.telegram.config import TtsConfig
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


def _make_update_and_ctx(state_mgr, talker_config, client, short_id: str):
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.id = 1
    update.message.text = f"/brief {short_id}"
    update.message.message_id = 1
    update.message.reply_text = AsyncMock()

    ctx = MagicMock()
    ctx.application.bot_data = {
        "config": talker_config,
        "state_mgr": state_mgr,
        "anthropic_client": client,
        "system_prompt": "sys",
        "vault_context_str": "",
        "chat_locks": {},
    }
    ctx.args = [short_id]
    ctx.bot.send_voice = AsyncMock()
    ctx.bot.send_document = AsyncMock()
    return update, ctx


@pytest.mark.asyncio
async def test_brief_replies_not_configured_when_tts_missing(
    state_mgr, talker_config,
) -> None:
    """tts section absent on TalkerConfig → "not configured" reply."""
    talker_config.tts = None
    client = FakeAnthropicClient([])
    update, ctx = _make_update_and_ctx(state_mgr, talker_config, client, "any")

    await bot.on_brief(update, ctx)

    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args.args[0]
    assert "not configured" in reply.lower()
    # No LLM was called — we bailed before the compress/synthesize path.
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_brief_falls_back_to_text_when_tts_api_errors(
    state_mgr, talker_config, monkeypatch,
) -> None:
    """ElevenLabs API down → reply with the compressed prose as text."""
    # Seed a session with a summary.
    vault_path = Path(talker_config.vault.path)
    (vault_path / "session").mkdir(exist_ok=True)
    rel = "session/Voice Session — 2026-04-20 1000 fal12345.md"
    body = (
        f"{capture_batch.SUMMARY_MARKER_START}\n\n## Structured Summary\n"
        "### Topics\n- a\n\n"
        f"{capture_batch.SUMMARY_MARKER_END}\n\n# Transcript\n\n"
    )
    (vault_path / rel).write_text(
        "---\ntype: session\nstatus: completed\n"
        "name: Voice Session — 2026-04-20 1000 fal12345\n"
        "created: '2026-04-20'\nsession_type: capture\n---\n\n" + body,
        encoding="utf-8",
    )
    state_mgr.state.setdefault("closed_sessions", []).append({
        "session_id": "fal12345-uuid",
        "record_path": rel,
        "session_type": "capture",
        "started_at": "2026-04-20T10:00:00+00:00",
        "ended_at":   "2026-04-20T10:30:00+00:00",
        "reason": "explicit",
        "message_count": 3,
        "vault_ops": 0,
        "continues_from": None,
        "opening_model": "claude-sonnet-4-6",
        "closing_model": "claude-sonnet-4-6",
        "chat_id": 1,
    })
    state_mgr.save()

    talker_config.tts = TtsConfig(
        api_key="DUMMY_ELEVENLABS_TEST_KEY", voice_id="Rachel",
    )

    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="prose text here")])
    ])

    async def _boom(text: str, cfg, *, speed=None):
        raise tts.TtsError("elevenlabs 429")
    monkeypatch.setattr(tts, "synthesize", _boom)

    update, ctx = _make_update_and_ctx(
        state_mgr, talker_config, client, "fal12345",
    )
    await bot.on_brief(update, ctx)

    # Voice/document were NOT sent.
    ctx.bot.send_voice.assert_not_called()
    ctx.bot.send_document.assert_not_called()
    # Text reply carries the compressed prose.
    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args.args[0]
    assert "synthesis failed" in reply.lower()
    assert "prose text here" in reply


@pytest.mark.asyncio
async def test_brief_missing_session_returns_no_session_reply(
    state_mgr, talker_config,
) -> None:
    talker_config.tts = TtsConfig(api_key="DUMMY_ELEVENLABS_TEST_KEY", voice_id="Rachel")
    client = FakeAnthropicClient([])
    update, ctx = _make_update_and_ctx(
        state_mgr, talker_config, client, "nope1234",
    )

    await bot.on_brief(update, ctx)

    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args.args[0]
    assert "no session" in reply.lower()
    # No LLM call — short-id resolution failed before the pipeline.
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_brief_runs_batch_pass_implicitly_when_summary_missing(
    state_mgr, talker_config, monkeypatch,
) -> None:
    """Session without ALFRED:DYNAMIC block triggers batch structuring."""
    vault_path = Path(talker_config.vault.path)
    (vault_path / "session").mkdir(exist_ok=True)
    rel = "session/Voice Session — 2026-04-20 1001 nob12345.md"
    # Note: NO summary block in body.
    (vault_path / rel).write_text(
        "---\ntype: session\nstatus: completed\n"
        "name: Voice Session — 2026-04-20 1001 nob12345\n"
        "created: '2026-04-20'\nsession_type: capture\n---\n\n"
        "# Transcript\n\n**Andrew** (10:00 · voice): some rambling\n",
        encoding="utf-8",
    )
    state_mgr.state.setdefault("closed_sessions", []).append({
        "session_id": "nob12345-uuid",
        "record_path": rel,
        "session_type": "capture",
        "started_at": "2026-04-20T10:00:00+00:00",
        "ended_at":   "2026-04-20T10:30:00+00:00",
        "reason": "explicit",
        "message_count": 1,
        "vault_ops": 0,
        "continues_from": None,
        "opening_model": "claude-sonnet-4-6",
        "closing_model": "claude-sonnet-4-6",
        "chat_id": 1,
    })
    state_mgr.save()

    talker_config.tts = TtsConfig(api_key="DUMMY_ELEVENLABS_TEST_KEY", voice_id="Rachel")

    # Three LLM calls: batch structuring → compress → (synth is monkeypatched).
    client = FakeAnthropicClient([
        # 1: batch structuring tool_use
        FakeResponse(
            content=[
                FakeBlock(
                    type="tool_use", id="toolu_batch",
                    name="emit_structured_summary",
                    input={
                        "topics": ["x"], "decisions": [], "open_questions": [],
                        "action_items": [], "key_insights": [],
                        "raw_contradictions": [],
                    },
                )
            ],
            stop_reason="tool_use",
        ),
        # 2: compress step text response
        FakeResponse(content=[FakeBlock(type="text", text="prose 300 words")]),
    ])

    async def _fake_synth(text: str, cfg, *, speed=None):
        return b"FAKE-MP3"
    monkeypatch.setattr(tts, "synthesize", _fake_synth)

    update, ctx = _make_update_and_ctx(
        state_mgr, talker_config, client, "nob12345",
    )
    await bot.on_brief(update, ctx)

    # Session now has a structured summary written.
    raw = (vault_path / rel).read_text(encoding="utf-8")
    assert capture_batch.SUMMARY_MARKER_START in raw

    # Voice was sent successfully.
    ctx.bot.send_voice.assert_called_once()
    # Two LLM calls (batch + compress).
    assert len(client.messages.calls) == 2
