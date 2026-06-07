"""Document-attachment support for Telegram document messages.

Parallel to :mod:`alfred.telegram.vision` (images) — when the user
forwards a Telegram ``document`` message (a file attached as a file
rather than an image attached as a photo) we want to:

1. Reject anything outside the supported-mime allowlist with an
   explicit user-facing reply (no silent filter-drop — that was the
   2026-06-06 incident this module exists to fix: a PDF arrived,
   PTB had no handler registered for documents, the update was
   silently dropped from every routing path while ``inbound_in_window``
   incremented identically to a noise tick).
2. Download the file bytes via the bot's ``Document.get_file()``
   coroutine.
3. Text-extract via :mod:`pypdf` (PDF-only in c1; the allowlist surface
   is here so adding ``.docx`` / ``.txt`` / etc. is one extra mime + one
   extra extractor branch).
4. Persist the bytes under ``<vault>/inbox/`` for the audit trail.
5. Compose a user-message text that fences the extracted document text
   from the user's caption so the LLM sees the attachment as an
   attachment (not as a continuation of the user's words).

Why text extraction instead of Anthropic's native PDF document block:
the native block is model-family-specific (works on Opus 4.x, but the
shape would change if a future model family swapped the source-block
form). Text extraction is backend-agnostic and works across every
model the talker can route to (Claude, OpenRouter, future locals).
When the model-family quirk surface starts paying off elsewhere
(per ``feedback_sdk_quirk_centralization.md``), we can centralise a
``build_document_block`` here without changing any caller.

Per-instance vault scoping mirrors :mod:`vision`: callers pass
``vault_path`` directly, this module never reads config.

Failure-mode separation: :class:`AttachmentDownloadError` covers the
Telegram fetch (network / PTB shape); :class:`AttachmentExtractError`
covers the PDF-decoding path (corrupted file, empty text, library
quirk). Keeping the two distinct lets the caller emit a more useful
user-facing reply ("couldn't fetch your PDF" vs. "couldn't read your
PDF").
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import get_logger

log = get_logger(__name__)


# Allowlist of document MIME types the bot will process. PDF is the c1
# surface. Adding ``.docx`` / ``.txt`` / ``.md`` later: add the mime
# here AND a branch in :func:`extract_pdf_text` (or sibling functions).
# The on_document handler reads this constant for the rejection check
# so the allowlist lives in exactly one place.
SUPPORTED_DOCUMENT_MIME: set[str] = {"application/pdf"}


# Pathological-forward guard. 10MB lands well above the 99th percentile
# of operational PDFs (Andrew's typical: 0.5–3 MB) and well below the
# point at which text extraction starts blowing memory. Module constant
# rather than config field per the 2026-06-06 c1 decision: lift to
# :class:`TalkerConfig` when a second instance needs a different limit.
MAX_PDF_BYTES: int = 10 * 1024 * 1024  # 10 MiB


# Extracted-text truncation guard. An operations manual at 200 pages
# can extract to ~400 KB of plain text — feeding that verbatim to the
# LLM context window is wasteful and would blow the per-turn budget.
# 50K chars is roughly 10–15K tokens, fits comfortably in any modern
# model's context, and covers the long tail of typical operational PDFs.
# When the truncation fires the handler emits a single log line and
# appends a visible truncation marker so the model knows it's seeing a
# partial document, not the whole thing.
MAX_EXTRACTED_CHARS: int = 50_000


TRUNCATION_MARKER: str = (
    "\n\n[... document truncated; "
    f"only first {MAX_EXTRACTED_CHARS} characters shown ...]"
)


class AttachmentDownloadError(Exception):
    """Raised when fetching a Telegram document fails (network / PTB)."""


class AttachmentExtractError(Exception):
    """Raised when decoding a downloaded document fails.

    Distinct from :class:`AttachmentDownloadError` so the caller can
    emit a more useful user-facing reply — "I couldn't fetch your PDF"
    (network) vs. "I couldn't read your PDF" (decoding).
    """


async def download_document_bytes(document: Any) -> bytes:
    """Download a Telegram ``Document`` to in-memory bytes.

    Mirrors :func:`alfred.telegram.vision.download_photo_bytes`. PTB's
    ``Document.get_file()`` returns a ``File`` object whose
    ``download_as_bytearray()`` coroutine fetches the actual bytes from
    Telegram's servers; we bytes-cast the result so the caller receives
    a plain ``bytes`` object.

    Raises:
        AttachmentDownloadError: Wraps any exception from the PTB /
            HTTP download path so the caller has one error class.
    """
    try:
        tg_file = await document.get_file()
        raw = await tg_file.download_as_bytearray()
        return bytes(raw)
    except Exception as exc:  # noqa: BLE001 — wrap-and-rethrow with one class
        log.warning(
            "talker.attachments.download_failed",
            error=str(exc),
            file_id=getattr(document, "file_id", ""),
            mime_type=getattr(document, "mime_type", ""),
        )
        raise AttachmentDownloadError(
            f"Failed to download Telegram document: {exc!s}"
        ) from exc


def extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from a PDF byte stream via :mod:`pypdf`.

    Iterates pages, joins with double-newlines (so the LLM sees page
    boundaries), and trims to :data:`MAX_EXTRACTED_CHARS` with the
    :data:`TRUNCATION_MARKER` appended when truncation fires.

    Raises:
        AttachmentExtractError: On any pypdf decoding failure, or when
            the extracted text is empty (e.g. a scanned-image PDF with
            no embedded text layer — text extraction can't help us
            there; the caller should emit a "scanned PDF can't be read"
            user-facing reply).
    """
    try:
        # Lazy import: keeps the talker module importable on installs
        # that haven't pulled the ``voice`` extra (the on_document
        # handler's load-time guard will reply "PDF support not
        # available in this install" rather than crashing the daemon
        # at import time).
        import pypdf
    except ImportError as exc:
        log.warning("talker.attachments.pypdf_missing", error=str(exc))
        raise AttachmentExtractError(
            "PDF support is not installed in this build "
            "(pip install -e '.[voice]' to enable)"
        ) from exc

    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages_text: list[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                # Per-page extraction can fail on malformed pages while
                # the rest of the PDF is fine. Log the per-page miss
                # and continue — partial extraction beats total failure.
                log.warning(
                    "talker.attachments.page_extract_failed",
                    error=str(exc),
                )
                continue
            if page_text.strip():
                pages_text.append(page_text)
    except Exception as exc:  # noqa: BLE001 — wrap pypdf's exceptions
        log.warning(
            "talker.attachments.extract_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        raise AttachmentExtractError(
            f"Failed to decode PDF: {exc!s}"
        ) from exc

    full_text = "\n\n".join(pages_text).strip()
    if not full_text:
        # Empty extraction is its own failure mode: typically a scanned
        # image-only PDF with no embedded text layer. The text-extract
        # path can't recover here; OCR would be a separate feature.
        log.info("talker.attachments.empty_extraction")
        raise AttachmentExtractError(
            "No text could be extracted from this PDF "
            "(scanned image-only PDFs need OCR, which isn't enabled)"
        )

    if len(full_text) > MAX_EXTRACTED_CHARS:
        log.info(
            "talker.attachments.text_truncated",
            original_chars=len(full_text),
            kept_chars=MAX_EXTRACTED_CHARS,
        )
        full_text = full_text[:MAX_EXTRACTED_CHARS] + TRUNCATION_MARKER

    return full_text


def _short_id_from_file_unique_id(file_unique_id: str) -> str:
    """Return an 8-char filesystem-safe slug from a Telegram unique id.

    Same logic as :func:`alfred.telegram.vision._short_id_from_file_unique_id`
    — duplicated here rather than imported to keep this module independent
    of vision (a future refactor could lift this to a shared
    ``alfred.telegram._fs_id`` helper; out of scope for c1).
    """
    cleaned = "".join(
        c for c in (file_unique_id or "") if c.isalnum() or c in "_-"
    )
    return cleaned[:8] or "unknown"


def storage_path_for_document(
    vault_path: str | Path,
    file_unique_id: str,
    *,
    extension: str = "pdf",
    when: datetime | None = None,
) -> Path:
    """Return the destination path for a saved document attachment.

    Pattern: ``<vault_path>/inbox/document-<YYYYMMDDTHHMMSSZ>-<short>.<ext>``

    Distinct prefix (``document-`` vs. vision's ``screenshot-``) so a
    vault-walk regex over ``inbox/`` can disambiguate image and
    document attachments by filename alone. ISO-8601 compact form for
    cross-filesystem portability (matches vision's pattern).
    """
    if when is None:
        when = datetime.now(timezone.utc)
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    short = _short_id_from_file_unique_id(file_unique_id)
    name = f"document-{stamp}-{short}.{extension}"
    return Path(vault_path) / "inbox" / name


def save_document_to_inbox(
    document_bytes: bytes,
    vault_path: str | Path,
    file_unique_id: str,
    *,
    extension: str = "pdf",
    when: datetime | None = None,
) -> Path:
    """Persist ``document_bytes`` under the per-instance vault inbox.

    Creates ``<vault_path>/inbox/`` if missing (the scaffold ships it,
    but a fresh vault that hasn't been seeded yet shouldn't crash the
    document handler).

    Returns the absolute path written. Audit-trail value is the whole
    point — without this, the saved-path on the session record points
    at a missing file and the audit trail is broken.

    Persistence failure at the call site is treated as non-fatal
    (same contract as :func:`alfred.telegram.vision.save_image_to_inbox`):
    the handler catches the exception and proceeds with the in-memory
    bytes, logging ``action=continuing_to_llm_in_memory_only``.
    """
    dest = storage_path_for_document(
        vault_path, file_unique_id, extension=extension, when=when,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(document_bytes)
    log.info(
        "talker.attachments.saved",
        path=str(dest),
        bytes=len(document_bytes),
    )
    return dest


def build_document_user_text(
    caption: str,
    extracted_text: str,
    filename: str,
) -> str:
    """Compose the user-message text for a document attachment.

    Shape::

        [PDF attached: <filename>]

        <caption>           (when present)

        --- Document text ---
        <extracted_text>

    The header line names the attachment so the model knows context.
    The caption (if any) follows so user intent is preserved verbatim.
    The document text is fenced with a separator so the model treats it
    as the attached content rather than a continuation of the user's
    words. Empty caption collapses cleanly (no leading blank block).
    """
    safe_filename = filename or "document.pdf"
    header = f"[PDF attached: {safe_filename}]"

    parts: list[str] = [header, ""]
    caption_clean = (caption or "").strip()
    if caption_clean:
        parts.extend([caption_clean, ""])
    parts.extend(["--- Document text ---", extracted_text])
    return "\n".join(parts)


__all__ = [
    "AttachmentDownloadError",
    "AttachmentExtractError",
    "MAX_EXTRACTED_CHARS",
    "MAX_PDF_BYTES",
    "SUPPORTED_DOCUMENT_MIME",
    "TRUNCATION_MARKER",
    "build_document_user_text",
    "download_document_bytes",
    "extract_pdf_text",
    "save_document_to_inbox",
    "storage_path_for_document",
]
