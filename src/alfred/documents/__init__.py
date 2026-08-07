"""Surface-agnostic document handling (#57).

Extraction lives here rather than under any one consumer, because two now
exist: the Telegram ``on_document`` handler and the web ingest route. Adding a
kind (``.docx``, ``.xlsx``, …) means a module beside :mod:`~alfred.documents.pdf`
and an export here — not another copy inside whichever surface needed it first.
"""

from __future__ import annotations

from .pdf import (
    MAX_EXTRACTED_CHARS,
    MAX_PDF_BYTES,
    REASON_EMPTY_FILE,
    REASON_ENCRYPTED,
    REASON_NO_TEXT_LAYER,
    REASON_SUPPORT_MISSING,
    REASON_UNREADABLE,
    TELEGRAM_LOG_PREFIX,
    TRUNCATION_MARKER,
    DocumentExtractError,
    apply_char_truncation,
    extract_pdf_text,
)

__all__ = [
    "MAX_EXTRACTED_CHARS",
    "MAX_PDF_BYTES",
    "REASON_EMPTY_FILE",
    "REASON_ENCRYPTED",
    "REASON_NO_TEXT_LAYER",
    "REASON_SUPPORT_MISSING",
    "REASON_UNREADABLE",
    "TELEGRAM_LOG_PREFIX",
    "TRUNCATION_MARKER",
    "DocumentExtractError",
    "apply_char_truncation",
    "extract_pdf_text",
]
