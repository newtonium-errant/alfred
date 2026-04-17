"""Speech-to-text for Telegram voice messages.

Wk1 supports Groq's OpenAI-compatible Whisper endpoint only. ElevenLabs and
any other provider will raise :class:`NotImplementedError` — the provider
check is deliberately explicit so misconfiguration fails loudly rather than
silently falling through.

Shape:
    * ``transcribe(audio_bytes, mime, config) -> str`` — single async call,
      returns the transcript text on success.
    * Raises :class:`TranscribeError` on API errors or empty transcripts
      (Groq returns ``{"text": ""}`` for silent audio; passing an empty
      string to Claude is worse than telling the user "I couldn't hear you").

The bot handler catches :class:`TranscribeError` and surfaces a friendly
user message. Other exceptions propagate.
"""

from __future__ import annotations

import httpx

from .config import STTConfig
from .utils import get_logger

log = get_logger(__name__)

_GROQ_ENDPOINT = "https://api.groq.com/openai/v1/audio/transcriptions"
_TIMEOUT_SECONDS = 30.0


class TranscribeError(Exception):
    """Raised when transcription fails (API error, empty transcript, etc)."""


async def transcribe(audio_bytes: bytes, mime: str, config: STTConfig) -> str:
    """Transcribe ``audio_bytes`` via the configured STT provider.

    Args:
        audio_bytes: Raw audio payload (Telegram voice notes arrive as OGG Opus).
        mime: MIME type for the multipart filename hint (``"audio/ogg"``).
        config: :class:`STTConfig` with ``provider``, ``api_key``, ``model``.

    Returns:
        The transcript text (non-empty).

    Raises:
        TranscribeError: API error, HTTP non-2xx, or empty transcript.
        NotImplementedError: For any provider other than ``"groq"``.
    """
    provider = (config.provider or "").lower()
    if provider != "groq":
        raise NotImplementedError(
            f"STT provider {config.provider!r} not supported in wk1"
        )

    if not config.api_key:
        raise TranscribeError("STT api_key is empty")

    # Groq expects multipart with a filename on the file part. The extension
    # is mostly cosmetic for Whisper — any audio container Whisper understands
    # works regardless of the filename — but we set a sensible default.
    filename = "voice.ogg" if mime.endswith("ogg") else "voice.bin"
    files = {"file": (filename, audio_bytes, mime)}
    data = {"model": config.model}
    headers = {"Authorization": f"Bearer {config.api_key}"}

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _GROQ_ENDPOINT, headers=headers, files=files, data=data
            )
    except httpx.HTTPError as exc:
        log.warning("talker.stt.http_error", error=str(exc))
        raise TranscribeError(f"STT HTTP error: {exc}") from exc

    if resp.status_code >= 400:
        body_tail = resp.text[:300] if resp.text else ""
        log.warning(
            "talker.stt.api_error",
            status=resp.status_code,
            body_tail=body_tail,
        )
        raise TranscribeError(
            f"STT API {resp.status_code}: {body_tail or '(no body)'}"
        )

    try:
        payload = resp.json()
    except ValueError as exc:
        raise TranscribeError(f"STT non-JSON response: {exc}") from exc

    text = (payload.get("text") or "").strip()
    if not text:
        # Guard the silent-audio case — see module docstring.
        raise TranscribeError("empty transcription")

    log.info("talker.stt.ok", chars=len(text), model=config.model)
    return text
