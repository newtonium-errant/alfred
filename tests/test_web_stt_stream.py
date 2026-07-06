"""Unit tests for ``alfred.web.stt_stream`` + ``alfred.web.stt_deepgram``.

UNCONDITIONAL (no aiortc/av). Pure parsers (build_deepgram_url,
parse_deepgram_message, normalize_stt_settings), the FakeStreamProvider
determinism + finite-script contract, and the error-class re-exports.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.web.config import WebVoiceSttConfig
from alfred.web.stt_deepgram import build_deepgram_url, parse_deepgram_message
from alfred.web.stt_stream import (
    EVENT_FINAL,
    EVENT_PARTIAL,
    EVENT_UTTERANCE_END,
    STT_ERR_AUTH,
    STT_ERR_NETWORK,
    FakeStreamProvider,
    FakeUtterance,
    normalize_stt_settings,
)


# ---------------------------------------------------------------------------
# normalize_stt_settings — clamps (contract §1.5)
# ---------------------------------------------------------------------------


def test_normalize_clamps_endpointing_low() -> None:
    out, warns = normalize_stt_settings(WebVoiceSttConfig(endpointing_ms=5))
    assert out.endpointing_ms == 10
    assert any("endpointing_ms" in w for w in warns)


def test_normalize_clamps_endpointing_high() -> None:
    out, warns = normalize_stt_settings(WebVoiceSttConfig(endpointing_ms=9000))
    assert out.endpointing_ms == 5000


def test_normalize_clamps_utterance_end_to_5000_max() -> None:
    # Contract §1.5: Deepgram's documented max is 5000 (not 10000).
    out, warns = normalize_stt_settings(WebVoiceSttConfig(utterance_end_ms=8000))
    assert out.utterance_end_ms == 5000
    assert any("utterance_end_ms" in w for w in warns)


def test_normalize_clamps_utterance_end_sub_1000_up() -> None:
    out, _ = normalize_stt_settings(WebVoiceSttConfig(utterance_end_ms=400))
    assert out.utterance_end_ms == 1000


def test_normalize_leaves_utterance_end_zero_disabled() -> None:
    out, warns = normalize_stt_settings(WebVoiceSttConfig(utterance_end_ms=0))
    assert out.utterance_end_ms == 0
    assert not any("utterance_end_ms" in w for w in warns)


def test_normalize_clamps_sample_rate_to_16000() -> None:
    out, warns = normalize_stt_settings(WebVoiceSttConfig(sample_rate=48000))
    assert out.sample_rate == 16000
    assert any("sample_rate" in w for w in warns)


def test_normalize_noop_on_valid_config() -> None:
    out, warns = normalize_stt_settings(
        WebVoiceSttConfig(endpointing_ms=300, utterance_end_ms=1000, sample_rate=16000)
    )
    assert warns == []
    assert out.endpointing_ms == 300


def test_normalize_returns_copy_not_mutating_input() -> None:
    src = WebVoiceSttConfig(endpointing_ms=5)
    out, _ = normalize_stt_settings(src)
    assert src.endpointing_ms == 5  # input untouched
    assert out.endpointing_ms == 10


# ---------------------------------------------------------------------------
# build_deepgram_url — pure
# ---------------------------------------------------------------------------


def test_url_interim_results_hardwired_true() -> None:
    url = build_deepgram_url(WebVoiceSttConfig())
    assert "interim_results=true" in url


def test_url_smart_format_from_config() -> None:
    assert "smart_format=true" in build_deepgram_url(WebVoiceSttConfig(smart_format=True))
    assert "smart_format=false" in build_deepgram_url(WebVoiceSttConfig(smart_format=False))


def test_url_omits_utterance_end_when_zero() -> None:
    assert "utterance_end_ms" not in build_deepgram_url(
        WebVoiceSttConfig(utterance_end_ms=0)
    )
    assert "utterance_end_ms=1000" in build_deepgram_url(
        WebVoiceSttConfig(utterance_end_ms=1000)
    )


def test_url_carries_model_and_rate() -> None:
    url = build_deepgram_url(WebVoiceSttConfig(model="nova-3", sample_rate=16000))
    assert "model=nova-3" in url
    assert "sample_rate=16000" in url
    assert "encoding=linear16" in url


# ---------------------------------------------------------------------------
# parse_deepgram_message — pure table
# ---------------------------------------------------------------------------


def _results(transcript: str, *, is_final=False, speech_final=False) -> dict:
    msg = {
        "type": "Results",
        "is_final": is_final,
        "channel": {"alternatives": [{"transcript": transcript}]},
    }
    if speech_final:
        msg["speech_final"] = True
    return msg


def test_parse_interim_is_partial() -> None:
    evs = parse_deepgram_message(_results("hello", is_final=False))
    assert [e.type for e in evs] == [EVENT_PARTIAL]
    assert evs[0].text == "hello"


def test_parse_final_no_speech_final() -> None:
    evs = parse_deepgram_message(_results("hello world", is_final=True))
    assert [e.type for e in evs] == [EVENT_FINAL]


def test_parse_final_with_speech_final_appends_utterance_end() -> None:
    evs = parse_deepgram_message(
        _results("hello world", is_final=True, speech_final=True)
    )
    assert [e.type for e in evs] == [EVENT_FINAL, EVENT_UTTERANCE_END]
    assert evs[1].trigger == "speech_final"


def test_parse_utterance_end_message() -> None:
    evs = parse_deepgram_message({"type": "UtteranceEnd"})
    assert [e.type for e in evs] == [EVENT_UTTERANCE_END]
    assert evs[0].trigger == "utterance_end_fallback"


def test_parse_empty_transcript_yields_nothing() -> None:
    assert parse_deepgram_message(_results("   ", is_final=False)) == []
    assert parse_deepgram_message(_results("", is_final=True)) == []


def test_parse_metadata_and_unknown_yield_nothing() -> None:
    assert parse_deepgram_message({"type": "Metadata"}) == []
    assert parse_deepgram_message({"type": "SpeechStarted"}) == []
    assert parse_deepgram_message({"type": "Results"}) == []  # malformed
    assert parse_deepgram_message({}) == []


# ---------------------------------------------------------------------------
# FakeStreamProvider — deterministic, finite (contract §1.7)
# ---------------------------------------------------------------------------


async def _drain(fp: FakeStreamProvider, feeds: int) -> list:
    import asyncio

    await fp.connect()
    got = []

    async def drain():
        async for e in fp.events():
            got.append((e.type, e.text, e.trigger))

    t = asyncio.ensure_future(drain())
    for _ in range(feeds):
        await fp.feed(b"x" * 3200)
    await asyncio.sleep(0.01)
    await fp.close()
    await t
    return got


async def test_fake_fires_utterance_after_chunk_count() -> None:
    fp = FakeStreamProvider(script=[FakeUtterance(chunks=3, partials=["p"], final="hello")])
    got = await _drain(fp, feeds=3)
    assert (EVENT_PARTIAL, "p", "") in got
    assert (EVENT_FINAL, "hello", "") in got
    assert any(t == EVENT_UTTERANCE_END for t, _, _ in got)


async def test_fake_no_fire_before_chunk_count() -> None:
    fp = FakeStreamProvider(script=[FakeUtterance(chunks=10, final="x")])
    got = await _drain(fp, feeds=5)  # under threshold
    assert got == []


async def test_fake_default_script_is_finite_and_logs_exhausted() -> None:
    fp = FakeStreamProvider(voice_session_id="v1")  # default = 3 utterances
    with structlog.testing.capture_logs() as cap:
        got = await _drain(fp, feeds=80)  # 3*20=60 fires all 3, then idle
    finals = [x for x in got if x[0] == EVENT_FINAL]
    assert len(finals) == 3  # finite — exactly 3, not repeating
    exhausted = [c for c in cap if c.get("event") == "web.voice.stt.script_exhausted"]
    assert len(exhausted) == 1


async def test_fake_finalize_forces_final() -> None:
    import asyncio

    fp = FakeStreamProvider(script=[FakeUtterance(chunks=100, final="forced")])
    await fp.connect()
    got = []

    async def drain():
        async for e in fp.events():
            got.append((e.type, e.text, e.trigger))

    t = asyncio.ensure_future(drain())
    await fp.feed(b"x")  # 1 feed, well under 100
    await fp.finalize()  # force
    await asyncio.sleep(0.01)
    await fp.close()
    await t
    assert (EVENT_FINAL, "forced", "") in got
    assert any(tr == "finalize" for _, _, tr in got)


def test_error_class_reexports() -> None:
    # The batch taxonomy is re-exported so streaming buckets match.
    assert STT_ERR_AUTH == "auth"
    assert STT_ERR_NETWORK == "network"
