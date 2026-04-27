"""Per-call-site regression tests: every Anthropic SDK caller in
``src/alfred/telegram/`` must drop ``temperature`` on Opus 4.x models.

Background: 2026-04-27 Hypatia release blocker. Capture extraction +
batch structuring used the talker's ``config.anthropic.model`` (Opus
on Hypatia) but passed a literal ``temperature=0.2/0.3``, producing
a 400 from Anthropic. Conversation.py already knew the rule;
``_anthropic_compat.messages_create_kwargs`` now centralizes it. These
tests pin every other caller to that helper so the same bug class
can't recur as new tools land.
"""
from __future__ import annotations

import pytest

from alfred.telegram import calibration, capture_batch, capture_extract, router, tts
from alfred.telegram.session_types import ROUTER_MODEL
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- capture_batch.run_batch_structuring ----------------------------------


def _emit_summary_response() -> FakeResponse:
    return FakeResponse(
        content=[
            FakeBlock(
                type="tool_use",
                id="toolu_x",
                name="emit_structured_summary",
                input={
                    "topics": [],
                    "decisions": [],
                    "open_questions": [],
                    "action_items": [],
                    "key_insights": [],
                    "raw_contradictions": [],
                },
            )
        ],
        stop_reason="tool_use",
    )


@pytest.mark.asyncio
async def test_capture_batch_drops_temperature_on_opus() -> None:
    client = FakeAnthropicClient([_emit_summary_response()])
    await capture_batch.run_batch_structuring(client, [], model="claude-opus-4-7")
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-7"
    assert "temperature" not in call


@pytest.mark.asyncio
async def test_capture_batch_keeps_temperature_on_sonnet() -> None:
    client = FakeAnthropicClient([_emit_summary_response()])
    await capture_batch.run_batch_structuring(client, [], model="claude-sonnet-4-6")
    call = client.messages.calls[0]
    assert call["temperature"] == 0.2


# --- capture_extract._call_extract_llm ------------------------------------


@pytest.mark.asyncio
async def test_capture_extract_drops_temperature_on_opus() -> None:
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="(no notes)")])
    ])
    await capture_extract._call_extract_llm(
        client=client,
        model="claude-opus-4-7",
        transcript_text="hi",
        summary_block="",
        max_notes=3,
    )
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-7"
    assert "temperature" not in call


@pytest.mark.asyncio
async def test_capture_extract_keeps_temperature_on_sonnet() -> None:
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="(no notes)")])
    ])
    await capture_extract._call_extract_llm(
        client=client,
        model="claude-sonnet-4-6",
        transcript_text="hi",
        summary_block="",
        max_notes=3,
    )
    call = client.messages.calls[0]
    assert call["temperature"] == 0.3


# --- calibration.propose_updates ------------------------------------------


@pytest.mark.asyncio
async def test_calibration_drops_temperature_on_opus() -> None:
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="[]")])
    ])
    await calibration.propose_updates(
        client=client,
        transcript_text="USER: hi\nASSISTANT: ok",
        current_calibration="(empty)",
        session_type="note",
        source_session_rel="session/T.md",
        model="claude-opus-4-7",
    )
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-7"
    assert "temperature" not in call


@pytest.mark.asyncio
async def test_calibration_keeps_temperature_on_sonnet() -> None:
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="[]")])
    ])
    await calibration.propose_updates(
        client=client,
        transcript_text="USER: hi\nASSISTANT: ok",
        current_calibration="(empty)",
        session_type="note",
        source_session_rel="session/T.md",
        model="claude-sonnet-4-6",
    )
    call = client.messages.calls[0]
    assert call["temperature"] == 0.2


# --- tts.compress_summary_for_tts -----------------------------------------


@pytest.mark.asyncio
async def test_tts_compress_drops_temperature_on_opus() -> None:
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="prose")])
    ])
    await tts.compress_summary_for_tts(
        client=client,
        summary_markdown="## summary",
        model="claude-opus-4-7",
    )
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-7"
    assert "temperature" not in call


@pytest.mark.asyncio
async def test_tts_compress_keeps_temperature_on_sonnet() -> None:
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="prose")])
    ])
    await tts.compress_summary_for_tts(
        client=client,
        summary_markdown="## summary",
        model="claude-sonnet-4-6",
    )
    call = client.messages.calls[0]
    assert call["temperature"] == 0.5


# --- router.classify_opening_cue ------------------------------------------


@pytest.mark.asyncio
async def test_router_keeps_temperature_on_sonnet() -> None:
    """Router model is pinned to Sonnet — but the helper application is
    a contract pin: if ROUTER_MODEL ever flips to Opus, temperature
    will silently drop and the router will keep working."""
    payload = '{"session_type": "note", "continues_from": null, "reasoning": "ok"}'
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text=payload)])
    ])
    await router.classify_opening_cue(
        client=client,
        first_message="hello",
        recent_sessions=[],
        self_name="Salem",
        self_display_name="Salem",
        has_reply_context=False,
    )
    call = client.messages.calls[0]
    assert call["model"] == ROUTER_MODEL
    assert ROUTER_MODEL.startswith("claude-sonnet-")
    assert call["temperature"] == 0.2
