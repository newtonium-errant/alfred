"""PDF text extraction, shared by every surface that accepts a document (#57).

## Why this module exists

The extractor shipped 2026-06-06 inside :mod:`alfred.telegram.attachments`,
because the Telegram ``on_document`` handler was the only consumer. #57 adds a
second one — the web ingest route — and a transport module importing from
``telegram`` would be backwards: the talker is one surface among several, not a
library. So the PDF half was LIFTED here and ``attachments`` now imports it
back. The lift is deliberately minimal and behaviour-preserving; see below.

## The two callers want different things, and the difference is load-bearing

**The talker TRUNCATES. The ingest route REFUSES.** Same extractor, opposite
policy, and neither is a bug:

* the talker feeds extracted text into an LLM context window, so a 200-page
  manual is truncated to :data:`MAX_EXTRACTED_CHARS` with a visible marker —
  a partial read is useful and the marker keeps the model honest about it;
* the ingest route writes a VERBATIM vault record. Silently truncating a bank
  statement at 50,000 characters would be data loss inside a route whose entire
  promise is that the body is byte-for-byte what the operator uploaded. So it
  passes ``max_chars=None`` and enforces its own ceiling as a REFUSAL.

Hence ``max_chars`` is a parameter rather than a constant read inside. The
DEFAULT is the talker's, so its call site is unchanged.

## Why extraction rather than the model's native PDF block

Recorded when the extractor first shipped and unchanged: Anthropic's native PDF
document block is model-family-specific, whereas text extraction is
backend-agnostic and works across every model the talker can route to. #57
re-confirmed the trade and kept it.

## Scanned PDFs

A PDF with no text layer raises with reason :data:`REASON_NO_TEXT_LAYER`. That
is a REFUSAL, not a gap awaiting a fallback — operator-ruled 2026-08-07. Vision
for scanned documents is a separate, deferred item; nothing here should grow a
branch toward it, and the user-facing wording deliberately does not promise one.
"""

from __future__ import annotations

import io

import structlog

log = structlog.get_logger(__name__)

# --- caps -------------------------------------------------------------------

#: Byte ceiling for an uploaded PDF. Ratified 2026-06-06 for the Telegram
#: handler and re-ratified 2026-08-07 for web ingest — ONE number across both
#: surfaces so an operator never has to ask which door has the smaller cap.
#: ``web/lib/algernon/schemas.ts`` mirrors it and a drift pin holds the pair.
MAX_PDF_BYTES: int = 10 * 1024 * 1024     # 10 MiB

#: The talker's extracted-text truncation cap (NOT a refusal ceiling, and NOT
#: the ingest route's — see the module docstring). ~10-15K tokens.
MAX_EXTRACTED_CHARS: int = 50_000

TRUNCATION_MARKER: str = (
    "\n\n[... document truncated; "
    f"only first {MAX_EXTRACTED_CHARS} characters shown ...]"
)

#: Log-event namespace of the ORIGINAL call site. Kept as the default so the
#: talker's events (``talker.attachments.extract_failed`` &c.) are unchanged by
#: the lift — an operator grep written against them still works. A new caller
#: passes its own prefix rather than emitting events that name a subsystem it
#: has nothing to do with.
TELEGRAM_LOG_PREFIX = "talker.attachments"


# --- failure taxonomy -------------------------------------------------------

#: No text layer — typically a scan or a photo saved as a PDF. Terminal here.
REASON_NO_TEXT_LAYER = "pdf_no_text_layer"
#: Password-protected / not decryptable.
REASON_ENCRYPTED = "pdf_encrypted"
#: Truncated, malformed, or not a PDF at all.
REASON_UNREADABLE = "pdf_unreadable"
#: Zero bytes.
REASON_EMPTY_FILE = "pdf_empty_file"
#: ``pypdf`` absent from the install.
REASON_SUPPORT_MISSING = "pdf_support_missing"


class DocumentExtractError(Exception):
    """Extraction failed, with a MACHINE-READABLE reason.

    ``reason`` is the #57 addition and it is purely additive: the message text
    is unchanged from the pre-lift extractor, because the talker surfaces
    ``str(exc)`` directly to the user and that wording was reviewed. The route
    needs to tell an encrypted PDF from a corrupt one from a scan — six
    refusals that look identical to the operator are the shape the ILB rule
    exists to prevent — so the distinction rides alongside the message rather
    than inside it.

    (``alfred.telegram.attachments.AttachmentExtractError`` was an ALIAS of
    this class so the bot-side handlers kept catching across the #57 lift;
    the wrapper module and its alias were deleted with the Telegram
    retirement, T5 2026-08-19 — this class is the only spelling now.)
    """

    def __init__(self, message: str, *, reason: str = REASON_UNREADABLE) -> None:
        super().__init__(message)
        self.reason = reason


def apply_char_truncation(
    text: str,
    *,
    kind: str,
    max_chars: int | None = MAX_EXTRACTED_CHARS,
    log_event_prefix: str = TELEGRAM_LOG_PREFIX,
) -> str:
    """Apply a character cap with a visible marker. ``None`` disables it.

    Shared across every extractor (PDF, DOCX, text, CSV, ICS, audio
    transcript). When truncation fires, one log line carries the kind so
    per-type truncation rates stay visible ("audio transcripts truncate 10% of
    the time" is operationally useful). The marker text is identical for every
    kind so the prompt template never branches on type.

    ``max_chars=None`` returns the text untouched — the verbatim-ingest case,
    which must refuse rather than silently shorten.
    """
    if max_chars is None or len(text) <= max_chars:
        return text
    log.info(
        f"{log_event_prefix}.text_truncated",
        kind=kind,
        original_chars=len(text),
        kept_chars=max_chars,
    )
    return text[:max_chars] + TRUNCATION_MARKER


def extract_pdf_text(
    pdf_bytes: bytes,
    *,
    max_chars: int | None = MAX_EXTRACTED_CHARS,
    log_event_prefix: str = TELEGRAM_LOG_PREFIX,
) -> str:
    """Extract text from a PDF byte stream via :mod:`pypdf`.

    Iterates pages, joins with double-newlines (so page boundaries survive),
    and applies ``max_chars`` (see :func:`apply_char_truncation`).

    A per-page failure is logged and SKIPPED rather than fatal: one malformed
    page in an otherwise-readable document should not cost the other 199.

    Raises:
        DocumentExtractError: with ``.reason`` set to one of the ``REASON_*``
            constants. The message wording is unchanged from the pre-#57
            extractor because the talker shows it to the user verbatim.
    """
    try:
        # Lazy import so the module stays importable on installs without the
        # ``voice`` extra; the caller's load-time guard reports it as a missing
        # capability instead of crashing a daemon at import time.
        import pypdf
        from pypdf.errors import EmptyFileError, FileNotDecryptedError
    except ImportError as exc:
        log.warning(f"{log_event_prefix}.pypdf_missing", error=str(exc))
        raise DocumentExtractError(
            "PDF support is not installed in this build "
            "(pip install -e '.[voice]' to enable)",
            reason=REASON_SUPPORT_MISSING,
        ) from exc

    try:
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages_text: list[str] = []
        for page in reader.pages:
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    f"{log_event_prefix}.page_extract_failed", error=str(exc),
                )
                continue
            if page_text.strip():
                pages_text.append(page_text)
    except Exception as exc:  # noqa: BLE001 — wrap pypdf's exceptions
        # The MESSAGE is unchanged from the pre-lift extractor; only the reason
        # is new. Measured against pypdf 6.13.0 (#57 recon): a 0-byte file
        # raises EmptyFileError, an encrypted one reaches FileNotDecryptedError
        # on first page access, and truncated / non-PDF input raises
        # PdfStreamError. Classified by exception type rather than by an
        # early ``is_encrypted`` probe precisely so the control flow — and
        # therefore the message the talker prints — stays identical.
        reason = REASON_UNREADABLE
        if isinstance(exc, FileNotDecryptedError):
            reason = REASON_ENCRYPTED
        elif isinstance(exc, EmptyFileError):
            reason = REASON_EMPTY_FILE
        log.warning(
            f"{log_event_prefix}.extract_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            reason=reason,
        )
        raise DocumentExtractError(
            f"Failed to decode PDF: {exc!s}", reason=reason,
        ) from exc

    full_text = "\n\n".join(pages_text).strip()
    if not full_text:
        # Its own failure mode: a scanned, image-only PDF. Text extraction
        # cannot recover here and — operator-ruled 2026-08-07 — does not fall
        # back to anything. The wording promises no future capability.
        #
        # #68 — realigned to the web copy, which is the voice reference for this
        # refusal. The old text said the file "needs OCR, which isn't enabled",
        # and "isn't enabled" is a promise wearing a denial: it names a
        # capability and implies a switch someone could flip. #67 (vision for
        # scanned PDFs) is DEFERRED AND UNRULED, so that sentence was
        # advertising an outcome nobody has approved.
        #
        # This message is operator-facing on BOTH paths — the talker replies
        # with it verbatim ("sorry, couldn't read your pdf file — {exc}."), and
        # the ingest route relays it as the error `detail`. Nothing MATCHES on
        # it: routing is by ``reason`` (``_PDF_REASON_STATUS`` keys off
        # ``exc.reason``), verified before this edit, so the words are free to
        # change and the failure shape is not.
        #
        # Stays distinct from its siblings — docx says "image-only or embedded
        # objects", text says "empty after decode", csv says "no rows" — because
        # the whole point of per-kind messages is that the operator can tell
        # which one he hit.
        log.info(f"{log_event_prefix}.empty_extraction", reason=REASON_NO_TEXT_LAYER)
        raise DocumentExtractError(
            "No selectable text in this PDF, so it looks like a scan or a "
            "photo. Try a version saved as text, or paste the text in yourself",
            reason=REASON_NO_TEXT_LAYER,
        )

    return apply_char_truncation(
        full_text, kind="pdf", max_chars=max_chars,
        log_event_prefix=log_event_prefix,
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
