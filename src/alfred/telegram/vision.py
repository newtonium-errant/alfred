"""Vision support for Telegram photo messages.

When the user sends a Telegram ``photo`` message we want to:

1. Pick the largest resolution from the ``photo`` array (Telegram orders
   ``PhotoSize`` smallest-to-largest, so the **last** entry is the original).
2. Download the file bytes via the bot's ``get_file()`` URL.
3. Persist the bytes under ``<vault.path>/inbox/`` so:

   * Andrew has an audit trail of every screenshot the bot saw.
   * Distillers / future tools can pull the saved path off the session
     record's ``images`` field for retroactive analysis.
4. Encode the bytes as a base64 image content block in Anthropic's
   multimodal Messages-API shape.

The Anthropic SDK accepts message ``content`` as either a bare string or a
list of typed content blocks (``{"type": "text", ...}`` /
``{"type": "image", ...}``). For this module the **content-block list**
form is canonical — the bot caller passes the list straight through to
``conversation.run_turn``, which threads it onto the user transcript turn.

Per-instance vault scoping: each Telegram instance writes to its own
vault root (see :class:`alfred.telegram.config.VaultConfig`). Salem
lands images in ``alfred/vault/inbox/``, Hypatia in
``library-alexandria/inbox/``, KAL-LE in ``aftermath-alfred/inbox/``.
The caller passes ``vault_path`` directly — this module never reads
config so it stays trivially testable.

Vision-disabled gate: when :class:`VisionConfig` carries
``enabled=False``, the bot's photo handler short-circuits before reaching
this module and replies to the user with a "vision is off" message.
This module always assumes vision is on; the gate is the caller's
responsibility.

See ``project_image_vision_support.md`` for the deferred-Phase-2 plan
this builds out, and ``feedback_sdk_quirk_centralization.md`` for the
"if a model family ever wants a different ``media_type`` string,
centralise here" reminder.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger(__name__)


# Anthropic's vision API accepts ``image/jpeg``, ``image/png``,
# ``image/gif``, ``image/webp`` (per
# https://docs.claude.com/en/docs/build-with-claude/vision). Telegram
# always re-encodes the user-side photo to JPEG for transport, so the
# default media_type below is correct for every Telegram ``photo``
# update — even when the source was a PNG screenshot. Documents
# (forwarded as files, not photos) preserve the original mime; that
# code path is out of scope for this commit but the helper accepts an
# explicit ``media_type`` so adding it later is one-line.
DEFAULT_TELEGRAM_PHOTO_MIME = "image/jpeg"


class VisionDownloadError(Exception):
    """Raised when fetching a Telegram photo fails."""


def select_largest_photo(photo_sizes: list[Any]) -> Any:
    """Return the largest ``PhotoSize`` from a Telegram ``photo`` list.

    Telegram orders the ``photo`` array smallest-to-largest, so the
    canonical pick is ``photo_sizes[-1]``. We defensively re-derive the
    pick by ``width * height`` so a future PTB / Telegram ordering quirk
    can't silently route us to a thumbnail.

    Args:
        photo_sizes: The Telegram ``message.photo`` list — at least one
            ``PhotoSize`` (or PhotoSize-shaped object) with ``width`` /
            ``height`` / ``file_id`` attributes.

    Returns:
        The selected ``PhotoSize`` object.

    Raises:
        VisionDownloadError: If ``photo_sizes`` is empty or None.
    """
    if not photo_sizes:
        raise VisionDownloadError("Empty photo array on Telegram message")
    # Last entry is the canonical largest per Telegram's ordering
    # contract; the max-by-area is belt-and-braces against ordering
    # drift. A tie (rare) breaks toward the last entry, matching
    # Telegram's documented behaviour.
    return max(photo_sizes, key=lambda p: (p.width or 0) * (p.height or 0))


async def download_photo_bytes(photo_size: Any) -> bytes:
    """Download a Telegram ``PhotoSize`` to in-memory bytes.

    PTB's ``PhotoSize.get_file()`` returns a ``File`` object whose
    ``download_as_bytearray()`` coroutine fetches the actual bytes
    from Telegram's servers. We bytes-cast the result so the caller
    receives a plain ``bytes`` object (mirrors the voice-handler
    pattern in :func:`alfred.telegram.bot.on_voice`).

    Raises:
        VisionDownloadError: Wraps any exception from the PTB / HTTP
            download path so the caller has one error class to handle.
    """
    try:
        tg_file = await photo_size.get_file()
        raw = await tg_file.download_as_bytearray()
        return bytes(raw)
    except Exception as exc:  # noqa: BLE001 — wrap-and-rethrow with one class
        log.warning(
            "talker.vision.download_failed",
            error=str(exc),
            file_id=getattr(photo_size, "file_id", ""),
        )
        raise VisionDownloadError(
            f"Failed to download Telegram photo: {exc!s}"
        ) from exc


def build_image_block(
    image_bytes: bytes,
    media_type: str = DEFAULT_TELEGRAM_PHOTO_MIME,
) -> dict[str, Any]:
    """Build an Anthropic vision content block from raw image bytes.

    Shape per Anthropic's vision docs:
    ``{"type": "image", "source": {"type": "base64",
       "media_type": "image/jpeg", "data": "<b64>"}}``

    All three live instances (Salem / Hypatia / KAL-LE) target Opus 4.x,
    which natively accepts this exact shape. If a future model family
    needs a different ``media_type`` mapping or alternate source-block
    structure, **centralise the variant here** rather than at the call
    site (per ``feedback_sdk_quirk_centralization.md``).
    """
    encoded = base64.standard_b64encode(image_bytes).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": encoded,
        },
    }


def build_user_content(
    text: str,
    image_blocks: list[dict[str, Any]] | None = None,
) -> str | list[dict[str, Any]]:
    """Compose a multimodal user message ``content`` field.

    When ``image_blocks`` is empty / None, returns ``text`` as a bare
    string — preserving the wk1 single-modal shape so existing
    transcripts / tests stay byte-identical and
    ``_messages_for_api`` / ``_render_content`` round-trip unchanged.

    When images are present, returns a content-block list with the
    image blocks first followed by a text block. **Image-then-text**
    matches Anthropic's documented best-practice ordering for vision
    (the model "sees" the image before the text question), and it
    stays consistent with how Andrew uses screenshots — the image is
    the subject, the caption is the prompt about it.
    """
    if not image_blocks:
        return text
    blocks: list[dict[str, Any]] = list(image_blocks)
    blocks.append({"type": "text", "text": text})
    return blocks


def _short_id_from_file_unique_id(file_unique_id: str) -> str:
    """Return an 8-char filesystem-safe slug from a Telegram unique id.

    Telegram's ``file_unique_id`` is short and base64-ish — perfectly
    suited to disambiguate two screenshots taken seconds apart in the
    same UTC second. We trim to 8 chars to keep filenames short and
    strip any path-unsafe character defensively.
    """
    cleaned = "".join(
        c for c in (file_unique_id or "") if c.isalnum() or c in "_-"
    )
    return cleaned[:8] or "unknown"


def storage_path(
    vault_path: str | Path,
    file_unique_id: str,
    *,
    extension: str = "jpg",
    when: datetime | None = None,
) -> Path:
    """Return the destination path for a saved screenshot.

    Pattern: ``<vault_path>/inbox/screenshot-<YYYYMMDDTHHMMSSZ>-<short>.<ext>``

    ISO-8601 compact form (no colons / dashes inside the time component)
    keeps the filename portable across filesystems that reject ``:`` —
    matches the canonical ``inf-YYYYMMDD-...`` marker grammar in spirit
    while staying distinct enough that a vault-walk regex won't confuse
    image paths with attribution markers.
    """
    if when is None:
        when = datetime.now(timezone.utc)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    short = _short_id_from_file_unique_id(file_unique_id)
    name = f"screenshot-{stamp}-{short}.{extension}"
    return Path(vault_path) / "inbox" / name


def save_image_to_inbox(
    image_bytes: bytes,
    vault_path: str | Path,
    file_unique_id: str,
    *,
    extension: str = "jpg",
    when: datetime | None = None,
) -> Path:
    """Persist ``image_bytes`` under the per-instance vault inbox.

    Creates ``<vault_path>/inbox/`` if missing (the scaffold ships it,
    but a fresh vault that hasn't been seeded yet shouldn't crash the
    photo handler).

    Returns the absolute path written. Audit-trail value is the whole
    point — without this, the saved-path on the session record points
    at a missing file and the audit trail is broken.
    """
    dest = storage_path(
        vault_path, file_unique_id, extension=extension, when=when,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(image_bytes)
    log.info(
        "talker.vision.saved",
        path=str(dest),
        bytes=len(image_bytes),
    )
    return dest


__all__ = [
    "DEFAULT_TELEGRAM_PHOTO_MIME",
    "VisionDownloadError",
    "build_image_block",
    "build_user_content",
    "download_photo_bytes",
    "save_image_to_inbox",
    "select_largest_photo",
    "storage_path",
]
