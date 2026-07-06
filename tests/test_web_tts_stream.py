"""Unit tests for ``alfred.web.tts_stream`` — the TTS provider seam.

UNCONDITIONAL (no aiortc/av/aiohttp). FakeTTSProvider determinism + the
finite/bounded per-turn contract, scripted-error injection, fatal-is-last +
events() termination, and the pure ``normalize_tts_settings`` clamp matrix.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from alfred.web.config import WebVoiceTtsConfig
from alfred.web.tts_stream import (
    EVENT_AUDIO,
    EVENT_ERROR,
    EVENT_TURN_DONE,
    TTS_ERR_AUTH,
    TTS_ERR_NETWORK,
    FakeTTSProvider,
    TTSEvent,
    normalize_tts_settings,
    rate_from_output_format,
)


async def _run_turn(fp: FakeTTSProvider, *, feed: str, tid: str = "t1") -> list:
    got: list[TTSEvent] = []

    async def drain():
        async for e in fp.events():
            got.append(e)

    task = asyncio.ensure_future(drain())
    await fp.begin_turn(tid)
    if feed:
        await fp.feed_text(feed)
    await fp.end_of_reply()
    await asyncio.sleep(0.01)
    await fp.close()
    await task
    return got


# ---------------------------------------------------------------------------
# FakeTTSProvider — determinism + bounded
# ---------------------------------------------------------------------------


async def test_fake_emits_audio_then_turn_done() -> None:
    got = await _run_turn(FakeTTSProvider(rate=24000), feed="hello")
    assert got[-1].type == EVENT_TURN_DONE
    assert any(e.type == EVENT_AUDIO for e in got)
    assert all(e.turn_id == "t1" for e in got)


async def test_fake_is_deterministic() -> None:
    a = await _run_turn(FakeTTSProvider(rate=24000), feed="a decent length reply")
    b = await _run_turn(FakeTTSProvider(rate=24000), feed="a decent length reply")
    audio_a = [e.pcm for e in a if e.type == EVENT_AUDIO]
    audio_b = [e.pcm for e in b if e.type == EVENT_AUDIO]
    assert audio_a == audio_b            # same chars → identical PCM
    assert len(audio_a) >= 1


async def test_fake_chunk_count_matches_chars() -> None:
    # audio_ms = clamp(chars*50, 200, 5000); n_chunks = ceil(audio_ms/250).
    fp = FakeTTSProvider(rate=24000, ms_per_char=50, chunk_ms=250, max_turn_ms=5000)
    got = await _run_turn(fp, feed="x" * 20)   # 20*50 = 1000 ms → 4 chunks
    audio = [e for e in got if e.type == EVENT_AUDIO]
    assert len(audio) == math.ceil(1000 / 250)
    # each chunk = 24000 * 250 / 1000 = 6000 samples = 12000 bytes s16.
    assert all(len(e.pcm) == 12000 for e in audio)


async def test_fake_bounded_by_max_turn_ms() -> None:
    fp = FakeTTSProvider(rate=24000, ms_per_char=50, chunk_ms=250, max_turn_ms=1000)
    got = await _run_turn(fp, feed="x" * 500)   # would be 25 s → capped at 1 s
    audio = [e for e in got if e.type == EVENT_AUDIO]
    assert len(audio) == 4               # 1000 ms / 250 ms, not 100


async def test_fake_zero_feed_turn_is_turn_done_only() -> None:
    got = await _run_turn(FakeTTSProvider(), feed="")
    assert [e.type for e in got] == [EVENT_TURN_DONE]


async def test_fake_scripted_error_is_fatal_last() -> None:
    err = TTSEvent(type=EVENT_ERROR, reason=TTS_ERR_AUTH, detail="401", fatal=True)
    fp = FakeTTSProvider(scripted_errors={0: err})
    got: list[TTSEvent] = []

    async def drain():
        async for e in fp.events():
            got.append(e)

    task = asyncio.ensure_future(drain())
    await fp.begin_turn("t1")
    await fp.feed_text("hi")
    await fp.end_of_reply()
    await asyncio.wait_for(task, 1.0)    # events() ends itself after fatal
    assert len(got) == 1
    assert got[0].type == EVENT_ERROR and got[0].fatal is True
    assert got[0].reason == TTS_ERR_AUTH


async def test_fake_cancel_suppresses_turn_events() -> None:
    fp = FakeTTSProvider(rate=24000)
    got: list[TTSEvent] = []

    async def drain():
        async for e in fp.events():
            got.append(e)

    task = asyncio.ensure_future(drain())
    await fp.begin_turn("t1")
    await fp.feed_text("hello there")
    await fp.cancel_turn()               # abort before end_of_reply
    await fp.end_of_reply()              # suppressed
    await asyncio.sleep(0.01)
    await fp.close()
    await task
    assert got == []                     # no audio / turn_done for a cancelled turn


async def test_fake_close_idempotent() -> None:
    fp = FakeTTSProvider()
    await fp.begin_turn("t1")
    await fp.close()
    await fp.close()                     # second is a no-op, no raise


# ---------------------------------------------------------------------------
# normalize_tts_settings — clamps (contract §1.13 / §1.5)
# ---------------------------------------------------------------------------


def test_normalize_bad_output_format_coalesces() -> None:
    out, warns = normalize_tts_settings(WebVoiceTtsConfig(output_format="mp3_44100"))
    assert out.output_format == "pcm_24000"
    assert any("output_format" in w for w in warns)


def test_normalize_keeps_valid_output_format() -> None:
    out, warns = normalize_tts_settings(WebVoiceTtsConfig(output_format="pcm_44100"))
    assert out.output_format == "pcm_44100"
    assert warns == []


def test_normalize_clamps_chars() -> None:
    lo, _ = normalize_tts_settings(WebVoiceTtsConfig(max_tts_chars_per_turn=10))
    hi, _ = normalize_tts_settings(WebVoiceTtsConfig(max_tts_chars_per_turn=999999))
    assert lo.max_tts_chars_per_turn == 200
    assert hi.max_tts_chars_per_turn == 20000


def test_normalize_clamps_buffer_seconds() -> None:
    lo, _ = normalize_tts_settings(WebVoiceTtsConfig(max_buffer_seconds=1))
    hi, _ = normalize_tts_settings(WebVoiceTtsConfig(max_buffer_seconds=9999))
    assert lo.max_buffer_seconds == 5
    assert hi.max_buffer_seconds == 120


def test_normalize_clamps_inactivity() -> None:
    lo, _ = normalize_tts_settings(WebVoiceTtsConfig(inactivity_timeout_s=1))
    hi, _ = normalize_tts_settings(WebVoiceTtsConfig(inactivity_timeout_s=9999))
    assert lo.inactivity_timeout_s == 20
    assert hi.inactivity_timeout_s == 180


def test_normalize_returns_copy() -> None:
    src = WebVoiceTtsConfig(max_buffer_seconds=1)
    out, _ = normalize_tts_settings(src)
    assert src.max_buffer_seconds == 1   # input untouched
    assert out.max_buffer_seconds == 5


def test_rate_from_output_format() -> None:
    assert rate_from_output_format("pcm_24000") == 24000
    assert rate_from_output_format("pcm_44100") == 44100
    assert rate_from_output_format("garbage") == 24000   # safe default


def test_error_class_reexports() -> None:
    assert TTS_ERR_AUTH == "auth"
    assert TTS_ERR_NETWORK == "network"
