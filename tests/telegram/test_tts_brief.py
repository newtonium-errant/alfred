"""Tests for the ElevenLabs TTS synthesis surface (originally wk2b /brief).

Covers what survives the Telegram retirement (2026-08-19 — the /brief
handler, its failure-mode file ``test_tts_failure.py``, and the
``send_voice_to_telegram`` upload leg all died with the surface):
    * ``resolve_voice_id`` friendly-name → id mapping.
    * ``synthesize`` posts to the right URL with the xi-api-key header
      (httpx transport mocked via httpx.MockTransport).
    * ``compress_summary_for_tts`` returns the assistant text.
    * Config shape: TtsConfig defaults, optional absence on TalkerConfig.
"""

from __future__ import annotations

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
