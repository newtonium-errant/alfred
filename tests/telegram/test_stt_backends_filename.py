"""Prerequisite fix: Groq's Whisper endpoint validates the multipart FILE
EXTENSION, so a WAV must be posted as ``voice.wav`` not ``voice.bin`` (the old
code sent every non-OGG body as ``voice.bin`` → Groq 400). Pins the
mime→extension derivation used by ``GroqWhisperBackend.transcribe``.
"""

from __future__ import annotations

import pytest

import alfred.telegram.stt_backends as sb
from alfred.telegram.stt_backends import GroqWhisperBackend, _groq_filename_for_mime


@pytest.mark.parametrize(
    "mime,expected",
    [
        ("audio/wav", "voice.wav"),
        ("audio/x-wav", "voice.wav"),
        ("audio/wave", "voice.wav"),
        ("audio/webm", "voice.webm"),
        ("audio/mp4", "voice.mp4"),
        ("audio/m4a", "voice.m4a"),
        ("audio/mpeg", "voice.mp3"),
        ("audio/flac", "voice.flac"),
        ("audio/ogg", "voice.ogg"),
        ("application/ogg", "voice.ogg"),
        # codec suffixes are stripped before lookup
        ("audio/ogg; codecs=opus", "voice.ogg"),
        ("audio/wav; rate=16000", "voice.wav"),
        # case-insensitive
        ("AUDIO/WAV", "voice.wav"),
        # an ogg-ish mime not in the map still resolves via the endswith path
        ("audio/x-ogg", "voice.ogg"),
        # genuinely unknown → the safe .bin fallback (unchanged old behavior)
        ("audio/weird", "voice.bin"),
        ("", "voice.bin"),
    ],
)
def test_groq_filename_for_mime(mime: str, expected: str) -> None:
    assert _groq_filename_for_mime(mime) == expected


class _FakeResp:
    status_code = 200
    text = ""

    def json(self) -> dict:
        return {"text": "ok"}


class _FakeClient:
    """Captures the multipart ``files`` kwarg the backend posts."""

    captured: dict = {}

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, *, headers, files, data):
        _FakeClient.captured = dict(files)
        return _FakeResp()


async def test_transcribe_posts_wav_as_voice_wav_not_bin(monkeypatch) -> None:
    """Pins the CALL SITE (transcribe → multipart filename), not just the
    helper: a WAV body must be posted as ``voice.wav`` (reverting the call
    site to ``voice.bin`` fails here)."""
    _FakeClient.captured = {}
    monkeypatch.setattr(sb.httpx, "AsyncClient", _FakeClient)
    backend = GroqWhisperBackend(api_key="DUMMY_GROQ_TEST_KEY")
    await backend.transcribe(b"\x00\x01\x02\x03", "audio/wav", [])
    filename = _FakeClient.captured["file"][0]
    assert filename == "voice.wav"
