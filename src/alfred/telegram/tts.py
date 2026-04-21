"""ElevenLabs Turbo v2.5 text-to-speech for the wk2b ``/brief`` command.

This module wraps the ElevenLabs REST API with httpx (no SDK import
dep) so the call surface stays tiny and testable. Tests mock the
module-level :func:`synthesize` function directly — no httpx transport
mock required.

Telegram voice-message upload uses PTB's ``Bot.send_voice`` with the
audio bytes wrapped as an ``InputFile``. The audio format ElevenLabs
returns for Turbo v2.5 is ``audio/mpeg`` (``.mp3``), which Telegram
accepts both as voice-messages (via ``send_voice``) and as document
uploads (via ``send_document``) for the oversize-fallback path.

Size caps:
    * Telegram voice-message limit: ~50 MB (Bot API), but playback is
      clean up to ~20 MB. We use the 50 MB threshold as the
      hard fallback — above it, we upload as a document instead.
    * A 300-word summary at Turbo v2.5 rates to roughly 2-4 MB of MP3,
      comfortably under any practical cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import httpx

from .config import TtsConfig
from .utils import get_logger

log = get_logger(__name__)


# ElevenLabs voice name → canonical voice id. Source of truth: ElevenLabs
# voice library default voices as of 2026-04. Rachel is the first entry
# in their "library" default list, which has kept the same id since
# v1 of the API (``21m00Tcm4TlvDq8ikWAM``). We store the mapping as a
# small module constant rather than fetching it at call time because:
#   a) the default voices don't change,
#   b) a fetch would add a round-trip per /brief and a failure mode we
#      don't need.
# Unknown names fall through to using the raw string as the voice_id,
# so users who paste an id directly into config also work.
_VOICE_NAME_TO_ID: Final[dict[str, str]] = {
    "Rachel": "21m00Tcm4TlvDq8ikWAM",
    "Adam": "pNInz6obpgDQGcFmaJgB",
    "Antoni": "ErXwobaYiN019PkySvjV",
    "Bella": "EXAVITQu4vr4xnSDxMaL",
    "Clyde": "2EiwWnXFnvU5JabPnv8n",
    "Dave": "CYw3kZ02Hs0563khs1Fj",
    "Domi": "AZnzlk1XvdvUeBnXmlld",
    "Elli": "MF3mGyEYCl7XYWbV9V6O",
    "Josh": "TxGEqnHWrfWFTfGW9XjX",
    "Sam": "yoZ06aMxZJJ28mfd3POQ",
}


# Hard upper bound for Telegram sendVoice. Above this, we upload as a
# document instead (PTB ``send_document``). 50 MB is the Bot API cap.
_VOICE_MAX_BYTES: Final[int] = 50 * 1024 * 1024


# --- Exceptions -----------------------------------------------------------


class TtsError(Exception):
    """Raised when the TTS synthesis call fails."""


class TtsNotConfigured(TtsError):
    """Raised when the caller invokes TTS without a configured ``tts`` section."""


# --- Voice resolution ----------------------------------------------------


def resolve_voice_id(voice_id_or_name: str) -> str:
    """Return the ElevenLabs canonical voice id.

    If the input already looks like an id (no mapping match), it's
    returned unchanged — this lets users paste a raw id into config
    without another indirection. Case-insensitive friendly-name
    matching.
    """
    if not voice_id_or_name:
        return ""
    for name, vid in _VOICE_NAME_TO_ID.items():
        if name.lower() == voice_id_or_name.lower():
            return vid
    return voice_id_or_name


# --- Synthesis ------------------------------------------------------------


async def synthesize(
    text: str, cfg: TtsConfig, *, speed: float | None = None,
) -> bytes:
    """Call ElevenLabs ``text-to-speech/{voice_id}`` and return audio bytes.

    ``cfg`` is a populated :class:`TtsConfig`; caller must check that
    ``tts`` is present on the :class:`TalkerConfig` before calling
    this. Raises :class:`TtsError` on any HTTP failure (the caller
    turns that into a text-fallback reply).

    ``speed`` — optional 0.7-1.2 float. When provided, forwarded to
    ElevenLabs as ``voice_settings.speed``. Callers resolve the
    preference via :func:`speed_pref.resolve_tts_speed` before calling
    so each TTS path consistently respects the user's calibration. When
    ``None``, the setting is omitted and ElevenLabs uses its own
    default (currently 1.0). Range validation is the caller's
    responsibility — this function does NOT clamp or reject out-of-
    range values so the caller can surface meaningful errors earlier.
    """
    if not cfg.api_key:
        raise TtsNotConfigured("elevenlabs api_key is empty")

    voice_id = resolve_voice_id(cfg.voice_id)
    if not voice_id:
        raise TtsError("no voice_id configured")

    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": cfg.api_key,
        "accept": "audio/mpeg",
        "content-type": "application/json",
    }
    voice_settings: dict[str, Any] = {
        # Default voice_settings — mid-range stability/clarity, matches
        # ElevenLabs' own defaults. Exposed config surface stays small
        # (voice, model, word target); tuning stability per-call would
        # be overkill for the wk2b /brief use case.
        "stability": 0.5,
        "similarity_boost": 0.75,
    }
    if speed is not None:
        voice_settings["speed"] = float(speed)
    payload: dict[str, Any] = {
        "text": text,
        "model_id": cfg.model,
        "voice_settings": voice_settings,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise TtsError(f"elevenlabs http error: {exc}") from exc
    if resp.status_code != 200:
        # Surface the body tail for log grepping.
        body = resp.text[:200] if resp.text else ""
        raise TtsError(
            f"elevenlabs returned {resp.status_code}: {body}"
        )
    audio = resp.content
    if not audio:
        raise TtsError("elevenlabs returned empty audio")
    log.info(
        "talker.tts.synthesised",
        voice_id=voice_id,
        model=cfg.model,
        chars=len(text),
        bytes=len(audio),
    )
    return audio


# --- Telegram side-effect helpers ----------------------------------------


@dataclass(frozen=True)
class _TelegramSendResult:
    """Result of a Telegram send. Used by tests to assert behaviour."""

    mode: str  # "voice" or "document"
    size_bytes: int


async def send_voice_to_telegram(
    bot: Any,
    chat_id: int,
    audio_bytes: bytes,
    caption: str = "",
    filename: str = "brief.mp3",
) -> _TelegramSendResult:
    """Upload the audio to Telegram as a voice-message (or document fallback).

    For audio <50 MB we use ``send_voice`` which renders as a voice
    bubble in the Telegram UI. Above 50 MB we fall back to
    ``send_document`` so the user still receives the file — they can
    play it via their device's player.

    ``bot`` is a PTB ``Bot`` instance (``ctx.bot``). We import PTB's
    ``InputFile`` lazily so this module stays importable without the
    telegram SDK on systems where PTB isn't installed (for example,
    the CI-free voice extras path).
    """
    from io import BytesIO
    from telegram import InputFile

    size = len(audio_bytes)
    if size > _VOICE_MAX_BYTES:
        log.info(
            "talker.tts.document_fallback",
            chat_id=chat_id,
            size_bytes=size,
        )
        await bot.send_document(
            chat_id=chat_id,
            document=InputFile(BytesIO(audio_bytes), filename=filename),
            caption=caption or None,
        )
        return _TelegramSendResult(mode="document", size_bytes=size)

    await bot.send_voice(
        chat_id=chat_id,
        voice=InputFile(BytesIO(audio_bytes), filename=filename),
        caption=caption or None,
    )
    return _TelegramSendResult(mode="voice", size_bytes=size)


# --- Summary compression --------------------------------------------------

# Used by /brief to shrink a ``## Structured Summary`` block to ~300 words
# of spoken prose before handing to ElevenLabs. Separate from the
# structuring prompt because the shape is different — one is extractive,
# the other is narrative.
_COMPRESS_PROMPT = """\
Compress the attached structured summary of a Telegram capture session \
into approximately {word_target} words of spoken prose. Constraints:

- Flowing paragraphs, not bullet lists (this text will be spoken aloud).
- Lead with the topics, then decisions + action items, then insights + \
open questions. Skip raw_contradictions unless they're load-bearing.
- No meta-commentary about the summary itself ("Here's a summary..."). \
Start on the content.
- Name the speaker in third person only if helpful; first-person \
reconstruction ("I decided to X") is fine when quoting the user.
- Target length is approximate — a few dozen words over or under is \
acceptable. Stop when content is exhausted.

Structured summary:
---
{summary}
---
"""


async def compress_summary_for_tts(
    client: Any,
    summary_markdown: str,
    model: str,
    word_target: int = 300,
) -> str:
    """Ask Sonnet to compress the summary block to ~word_target words of prose.

    ``summary_markdown`` is the full ``## Structured Summary`` block
    (ALFRED:DYNAMIC markers + sections). We do NOT strip the markers
    before sending — they're stable few-token prefixes and stripping
    costs more complexity than it saves.
    """
    prompt = _COMPRESS_PROMPT.format(
        word_target=word_target, summary=summary_markdown or "(empty)"
    )
    response = await client.messages.create(
        model=model,
        max_tokens=2048,
        temperature=0.5,
        messages=[{"role": "user", "content": prompt}],
    )
    content = getattr(response, "content", None) or []
    parts: list[str] = []
    for block in content:
        btype = getattr(block, "type", None) or (
            block.get("type") if isinstance(block, dict) else None
        )
        if btype != "text":
            continue
        text = getattr(block, "text", None) or (
            block.get("text") if isinstance(block, dict) else ""
        )
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


__all__ = [
    "TtsError",
    "TtsNotConfigured",
    "resolve_voice_id",
    "synthesize",
    "send_voice_to_telegram",
    "compress_summary_for_tts",
]
