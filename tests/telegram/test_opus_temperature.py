"""Regression test: Opus 4.x deprecated the ``temperature`` param.

Sonnet, Haiku, and older Claude families still accept ``temperature``.
Sending it on an Opus 4.x call produces a 400 from Anthropic:
``'temperature' is deprecated for this model.``

``run_turn`` must omit ``temperature`` from ``messages.create`` kwargs
when ``session.model`` is an Opus model.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from alfred.telegram import conversation
from alfred.telegram.config import (
    AnthropicConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
from alfred.telegram.session import Session
from alfred.telegram.state import StateManager


def _fake_response(text: str = "ready"):
    """Simple text-block response; plain dicts avoid MagicMock serialization issues."""
    class _Block:
        def __init__(self, t, txt):
            self.type = t
            self.text = txt
        def model_dump(self):
            return {"type": self.type, "text": self.text}
    class _Response:
        stop_reason = "end_turn"
        content = [_Block("text", text)]
    return _Response()


def _fake_config(tmp_path) -> TalkerConfig:
    return TalkerConfig(
        bot_token="x",
        allowed_users=[1],
        primary_users=["person/Test"],
        anthropic=AnthropicConfig(
            api_key="x", model="claude-sonnet-4-6", max_tokens=1024, temperature=0.7
        ),
        stt=STTConfig(provider="groq", api_key="x", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800, state_path=str(tmp_path / "state.json")
        ),
        vault=VaultConfig(path=str(tmp_path), ignore_dirs=[]),
        logging={},
    )


def _fake_session(model: str) -> Session:
    now = datetime(2026, 4, 19, tzinfo=timezone.utc)
    return Session(
        session_id="test",
        chat_id=1,
        started_at=now,
        last_message_at=now,
        model=model,
        transcript=[],
        vault_ops=[],
    )


async def _call_run_turn(model: str, tmp_path):
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(return_value=_fake_response())
    config = _fake_config(tmp_path)
    session = _fake_session(model)
    state = StateManager(config.session.state_path)
    await conversation.run_turn(
        client,
        state,
        session,
        "hello",
        config,
        vault_context_str="",
        system_prompt="sys",
    )
    return client.messages.create.call_args.kwargs


async def test_opus_omits_temperature(tmp_path):
    kwargs = await _call_run_turn("claude-opus-4-7", tmp_path)
    assert "temperature" not in kwargs, "opus should not receive temperature"
    assert kwargs["model"] == "claude-opus-4-7"


async def test_sonnet_includes_temperature(tmp_path):
    kwargs = await _call_run_turn("claude-sonnet-4-6", tmp_path)
    assert kwargs["temperature"] == 0.7


async def test_haiku_includes_temperature(tmp_path):
    kwargs = await _call_run_turn("claude-haiku-4-5-20251001", tmp_path)
    assert kwargs["temperature"] == 0.7


async def test_opus_4_5_also_omits_temperature(tmp_path):
    kwargs = await _call_run_turn("claude-opus-4-5", tmp_path)
    assert "temperature" not in kwargs
