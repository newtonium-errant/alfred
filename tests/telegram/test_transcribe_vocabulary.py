"""Tests for Whisper vocabulary biasing via the ``initial_prompt`` mechanism.

Groq's Whisper endpoint is OpenAI-compatible and accepts a ``prompt``
multipart field that biases the decoder toward listed terms. We hardcode a
vocabulary string in ``transcribe.py`` covering Algernon's instance names
(Salem, KAL-LE, Hypatia, ...) plus current-context proper nouns. Without
this bias, captures kept transcribing "Zettelkasten" as "Zeno Kessen" and
similar phonetic drift on out-of-prior terms.

These tests verify the wiring — that the constant is non-trivial, contains
the load-bearing terms, and is actually passed through to the underlying
API call. We do NOT assert anything about Whisper's downstream output
quality (empirical / calibration claim, not a unit-test claim).

Pattern: ``monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)`` to
capture the multipart form data the transcribe module sends, then inspect
``data["prompt"]`` for the constant. Mirrors the pattern used in
``test_tts_brief.py`` for ElevenLabs.
"""

from __future__ import annotations

import httpx
import pytest
import structlog

from alfred.telegram import transcribe
from alfred.telegram.config import STTConfig


# --- Constant shape -------------------------------------------------------


def test_vocabulary_prompt_constant_is_non_empty() -> None:
    """The hardcoded vocabulary must actually have content."""
    assert transcribe._STT_VOCABULARY_PROMPT
    assert len(transcribe._STT_VOCABULARY_PROMPT) > 50


def test_vocabulary_prompt_contains_zettelkasten() -> None:
    """Today's calibration anchor — Zettelkasten kept transcribing as
    'Zeno Kessen' before the bias was wired."""
    assert "Zettelkasten" in transcribe._STT_VOCABULARY_PROMPT


def test_vocabulary_prompt_contains_instance_names() -> None:
    """At least one instance name must be present so cross-instance voice
    addressing ('hey Salem', 'tell KAL-LE') transcribes correctly."""
    vocab = transcribe._STT_VOCABULARY_PROMPT
    instance_names = ["Salem", "KAL-LE", "Hypatia"]
    matches = [name for name in instance_names if name in vocab]
    assert matches, (
        f"vocabulary must contain at least one instance name from "
        f"{instance_names}; got: {vocab!r}"
    )


def test_vocabulary_prompt_contains_marcus_aurelius() -> None:
    """Today's capture context — Marcus Aurelius / Stoicism research thread."""
    assert "Marcus Aurelius" in transcribe._STT_VOCABULARY_PROMPT


# --- Wiring through to the API call --------------------------------------


@pytest.mark.asyncio
async def test_transcribe_passes_vocabulary_prompt_to_groq(monkeypatch) -> None:
    """The constant must reach Groq as the ``prompt`` multipart field.

    Groq's REST API accepts ``prompt`` (matching OpenAI's
    ``/audio/transcriptions`` parameter name). httpx puts the ``data`` dict
    into the multipart form alongside ``files``; we capture the kwargs and
    assert the ``prompt`` field was sent.
    """
    captured: dict = {}

    async def _fake_post(self, url: str, **kwargs) -> httpx.Response:
        captured["url"] = url
        captured["data"] = kwargs.get("data", {})
        captured["files"] = kwargs.get("files", {})
        return httpx.Response(200, json={"text": "hello there"})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    cfg = STTConfig(
        provider="groq",
        api_key="DUMMY_GROQ_TEST_KEY",
        model="whisper-large-v3",
    )
    text = await transcribe.transcribe(b"FAKE-OGG-BYTES", "audio/ogg", cfg)

    assert text == "hello there"
    assert captured["url"] == transcribe._GROQ_ENDPOINT
    assert captured["data"]["model"] == "whisper-large-v3"
    assert captured["data"]["prompt"] == transcribe._STT_VOCABULARY_PROMPT


@pytest.mark.asyncio
async def test_transcribe_prompt_field_is_exactly_the_module_constant(
    monkeypatch,
) -> None:
    """Regression-pin: the wired value must be the module constant, not an
    inlined literal that could silently diverge from the constant on edits.
    If someone refactors and accidentally writes the string twice, this
    catches the drift."""
    captured: dict = {}

    async def _fake_post(self, url: str, **kwargs) -> httpx.Response:
        captured["data"] = kwargs.get("data", {})
        return httpx.Response(200, json={"text": "ok"})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    cfg = STTConfig(
        provider="groq",
        api_key="DUMMY_GROQ_TEST_KEY",
        model="whisper-large-v3",
    )
    await transcribe.transcribe(b"data", "audio/ogg", cfg)

    # Identity-like check: object equality with the constant, not a string
    # that just happens to look the same.
    assert captured["data"]["prompt"] is transcribe._STT_VOCABULARY_PROMPT


@pytest.mark.asyncio
async def test_transcribe_logs_vocab_prompt_chars_on_success(monkeypatch) -> None:
    """Observability: the success log must record ``vocab_prompt_chars`` so
    operators can grep the log to confirm vocabulary bias was active on a
    given call. Per the log-emission-test-pattern discipline — if we don't
    pin the log here, a future refactor can silently drop the field."""

    async def _fake_post(self, url: str, **kwargs) -> httpx.Response:
        return httpx.Response(200, json={"text": "hello"})

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    cfg = STTConfig(
        provider="groq",
        api_key="DUMMY_GROQ_TEST_KEY",
        model="whisper-large-v3",
    )

    with structlog.testing.capture_logs() as captured_logs:
        await transcribe.transcribe(b"data", "audio/ogg", cfg)

    ok_events = [c for c in captured_logs if c.get("event") == "talker.stt.ok"]
    assert len(ok_events) == 1, (
        f"expected exactly one talker.stt.ok emission; got {ok_events!r}"
    )
    evt = ok_events[0]
    assert evt["vocab_prompt_chars"] == len(transcribe._STT_VOCABULARY_PROMPT)
    assert evt["vocab_prompt_chars"] > 0
