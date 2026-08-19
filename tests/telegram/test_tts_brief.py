"""Tests for wk2b commit 5 — ElevenLabs TTS synthesis + /brief command.

Happy-path tests. Failure-mode tests live in ``test_tts_failure.py``.

Covers:
    * ``resolve_voice_id`` friendly-name → id mapping.
    * ``synthesize`` posts to the right URL with the xi-api-key header
      (httpx transport mocked via httpx.MockTransport).
    * ``compress_summary_for_tts`` returns the assistant text.
    * ``send_voice_to_telegram`` uses ``send_voice`` when under 50MB.
    * ``on_brief`` end-to-end: loads session, compresses, synthesises,
      sends voice message.
    * Config shape: TtsConfig defaults, optional absence on TalkerConfig.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from alfred.telegram import tts
from alfred.telegram.config import TtsConfig
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- resolve_voice_id -----------------------------------------------------


def test_resolve_voice_id_maps_rachel_to_canonical_id() -> None:
    assert tts.resolve_voice_id("Rachel") == "21m00Tcm4TlvDq8ikWAM"


def test_resolve_voice_id_is_case_insensitive() -> None:
    assert tts.resolve_voice_id("rachel") == "21m00Tcm4TlvDq8ikWAM"
    assert tts.resolve_voice_id("RACHEL") == "21m00Tcm4TlvDq8ikWAM"


def test_resolve_voice_id_passes_raw_id_through() -> None:
    """Unknown names are returned unchanged — assume it's already an id."""
    raw_id = "someCustomClonedVoiceId12345"
    assert tts.resolve_voice_id(raw_id) == raw_id


# --- synthesize HTTP behaviour -------------------------------------------


@pytest.mark.asyncio
async def test_synthesize_posts_to_elevenlabs_with_correct_headers(
    monkeypatch,
) -> None:
    captured: dict = {}

    async def _fake_post(self, url: str, **kwargs) -> httpx.Response:
        captured["url"] = url
        captured["headers"] = kwargs.get("headers", {})
        captured["json"] = kwargs.get("json", {})
        return httpx.Response(200, content=b"FAKE-MP3-BYTES")

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    cfg = TtsConfig(
        api_key="DUMMY_ELEVENLABS_TEST_KEY",
        model="eleven_turbo_v2_5",
        voice_id="Rachel",
        summary_word_target=300,
    )
    audio = await tts.synthesize("hello world", cfg)
    assert audio == b"FAKE-MP3-BYTES"
    assert captured["url"].endswith("21m00Tcm4TlvDq8ikWAM")
    assert captured["headers"]["xi-api-key"] == "DUMMY_ELEVENLABS_TEST_KEY"
    assert captured["json"]["text"] == "hello world"
    assert captured["json"]["model_id"] == "eleven_turbo_v2_5"


@pytest.mark.asyncio
async def test_synthesize_raises_on_non_200(monkeypatch) -> None:
    async def _fake_post(self, url, **kwargs):
        return httpx.Response(429, text="rate limited")
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    cfg = TtsConfig(api_key="DUMMY_ELEVENLABS_TEST_KEY", voice_id="Rachel")
    with pytest.raises(tts.TtsError, match="429"):
        await tts.synthesize("hi", cfg)


@pytest.mark.asyncio
async def test_synthesize_raises_tts_not_configured_on_empty_key() -> None:
    cfg = TtsConfig(api_key="", voice_id="Rachel")
    with pytest.raises(tts.TtsNotConfigured):
        await tts.synthesize("hi", cfg)


# --- compress_summary_for_tts --------------------------------------------


@pytest.mark.asyncio
async def test_compress_summary_returns_assistant_text() -> None:
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="compressed prose")]),
    ])
    out = await tts.compress_summary_for_tts(
        client=client,
        summary_markdown="## Structured Summary\n- a\n- b",
        model="claude-sonnet-4-6",
        word_target=300,
    )
    assert out == "compressed prose"
    # One call, model threaded through.
    assert client.messages.calls[0]["model"] == "claude-sonnet-4-6"


# --- send_voice_to_telegram ----------------------------------------------


@pytest.mark.asyncio
async def test_send_voice_uses_send_voice_under_cap() -> None:
    bot_mock = MagicMock()
    bot_mock.send_voice = AsyncMock()
    bot_mock.send_document = AsyncMock()

    result = await tts.send_voice_to_telegram(
        bot=bot_mock,
        chat_id=123,
        audio_bytes=b"small" * 100,  # tiny audio
        caption="cap",
        filename="x.mp3",
    )
    assert result.mode == "voice"
    bot_mock.send_voice.assert_called_once()
    bot_mock.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_send_voice_falls_back_to_document_when_oversize() -> None:
    bot_mock = MagicMock()
    bot_mock.send_voice = AsyncMock()
    bot_mock.send_document = AsyncMock()

    huge = b"\x00" * (tts._VOICE_MAX_BYTES + 10)
    result = await tts.send_voice_to_telegram(
        bot=bot_mock,
        chat_id=123,
        audio_bytes=huge,
        caption="huge",
        filename="big.mp3",
    )
    assert result.mode == "document"
    bot_mock.send_document.assert_called_once()
    bot_mock.send_voice.assert_not_called()


# --- /brief command end-to-end --------------------------------------------


# --- Config shape --------------------------------------------------------


def test_tts_config_defaults() -> None:
    cfg = TtsConfig()
    assert cfg.provider == "elevenlabs"
    assert cfg.model == "eleven_turbo_v2_5"
    assert cfg.voice_id == "Rachel"
    assert cfg.summary_word_target == 300


def test_talker_config_tts_defaults_to_none() -> None:
    """tts is optional — TalkerConfig defaults to None so /brief can detect absence."""
    from alfred.telegram.config import InstanceConfig, TalkerConfig
    # ``InstanceConfig.name`` is required (no default) — pass an explicit
    # name so the test stays focused on the tts default rather than
    # tripping the instance-name guard.
    cfg = TalkerConfig(instance=InstanceConfig(name="Salem"))
    assert cfg.tts is None


def test_load_from_unified_picks_up_tts_section() -> None:
    """When config.yaml has a telegram.tts section, it lands on TalkerConfig.tts."""
    from alfred.telegram.config import load_from_unified
    raw = {
        "vault": {"path": "/tmp/vault"},
        "telegram": {
            "bot_token": "x",
            # ``instance.name`` is required as of 2026-04-26 — pin
            # explicit Salem identity so the test stays focused on tts.
            "instance": {"name": "Salem"},
            "tts": {
                "api_key": "DUMMY_ELEVENLABS_TEST_KEY",
                "voice_id": "Rachel",
                "model": "eleven_turbo_v2_5",
                "summary_word_target": 250,
            },
        },
    }
    cfg = load_from_unified(raw)
    assert cfg.tts is not None
    assert cfg.tts.api_key == "DUMMY_ELEVENLABS_TEST_KEY"
    assert cfg.tts.voice_id == "Rachel"
    assert cfg.tts.summary_word_target == 250
