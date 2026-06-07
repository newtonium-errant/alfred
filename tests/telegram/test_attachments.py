"""Unit tests for :mod:`alfred.telegram.attachments` (PDF document handler support).

Coverage:

    * :func:`extract_pdf_text` happy path against a generated fixture
    * :func:`extract_pdf_text` raises ``AttachmentExtractError`` on garbage
    * :func:`extract_pdf_text` truncates at :data:`MAX_EXTRACTED_CHARS`
      with the :data:`TRUNCATION_MARKER` appended
    * :func:`extract_pdf_text` empty-extraction (scanned-only PDF) raises
    * :func:`storage_path_for_document` format + safe-char handling
    * :func:`save_document_to_inbox` writes bytes + creates inbox dir
    * :func:`build_document_user_text` shape with caption / no-caption
    * :data:`SUPPORTED_DOCUMENT_MIME` includes PDF + only PDF (c1 contract)

The generated PDF fixture lives in a session-scoped fixture so the same
2-page in-memory PDF is reused across tests — no on-disk artifact, no
blob in git.
"""
from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.telegram import attachments
from alfred.telegram.attachments import (
    MAX_EXTRACTED_CHARS,
    SUPPORTED_DOCUMENT_MIME,
    TRUNCATION_MARKER,
    AttachmentExtractError,
    build_document_user_text,
    extract_pdf_text,
    save_document_to_inbox,
    storage_path_for_document,
)


# --- Generated PDF fixture ------------------------------------------------


def _make_two_page_pdf(
    page_one: str = "Page one body — quarterly metrics summary.",
    page_two: str = "Page two body — recommendations and next steps.",
) -> bytes:
    """Build a tiny 2-page PDF in memory.

    Uses ``pypdf`` to construct rather than committing a binary fixture
    to the repo. Each page carries identifiable text so the
    extraction-text assertions can verify per-page content survives.
    """
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()

    # Add two blank pages then overwrite their content via PageObject
    # text-insert (pypdf 4.x supports add_blank_page + minimal content
    # streams). We use ``insert_blank_page`` which returns a PageObject.
    page1 = writer.add_blank_page(width=612, height=792)
    page2 = writer.add_blank_page(width=612, height=792)

    # pypdf 4.x doesn't have a high-level "write text to page" API,
    # so we emit a minimal PDF content stream with a single Tj op per
    # page. The resulting PDF is valid and the standard text-extract
    # path can read it.
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    def _text_stream(text: str) -> DecodedStreamObject:
        # Minimal Tj content stream with a Helvetica font reference.
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content = (
            f"BT /F1 12 Tf 50 750 Td ({escaped}) Tj ET"
        ).encode("latin-1")
        stream = DecodedStreamObject()
        stream.set_data(content)
        return stream

    # Attach a minimal Helvetica font resource the content stream can
    # reference. Without a font, pypdf's text extractor produces empty
    # output even from a valid content stream.
    def _attach_font_and_content(page, text: str) -> None:
        font_dict = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        font_ref = writer._add_object(font_dict)

        resources = DictionaryObject({
            NameObject("/Font"): DictionaryObject({
                NameObject("/F1"): font_ref,
            }),
        })
        page[NameObject("/Resources")] = resources

        content_stream = _text_stream(text)
        content_ref = writer._add_object(content_stream)
        page[NameObject("/Contents")] = content_ref

    _attach_font_and_content(page1, page_one)
    _attach_font_and_content(page2, page_two)

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture(scope="session")
def two_page_pdf_bytes() -> bytes:
    """Session-scoped 2-page PDF with identifiable per-page text."""
    return _make_two_page_pdf()


@pytest.fixture(scope="session")
def long_pdf_bytes() -> bytes:
    """A PDF whose extracted text comfortably exceeds MAX_EXTRACTED_CHARS.

    Built by repeating a sentinel string across many pages. The
    extracted length is independent of the PDF's encoded size — we
    care about character count post-extraction, not byte count.
    """
    # Each line is ~100 chars; 1000 lines × 1 line/page = 100k chars
    # extracted, well above the 50k cap.
    long_text = "x" * 100
    # 1000 pages would balloon test time; we get the same trigger from
    # one page with a very long Tj string. PDF content streams have no
    # practical length limit for our purposes.
    huge_line = (long_text + " ") * 600  # ~60k chars on one page
    return _make_two_page_pdf(page_one=huge_line, page_two=huge_line)


# --- SUPPORTED_DOCUMENT_MIME contract ------------------------------------


def test_supported_mime_includes_pdf() -> None:
    """c1 contract: PDF is the only supported mime in the initial ship."""
    assert "application/pdf" in SUPPORTED_DOCUMENT_MIME


def test_supported_mime_is_pdf_only() -> None:
    """Allowlist is exactly {PDF} in c1.

    Pinning the exact set surfaces silent widenings — if a future
    commit adds ``application/msword`` without updating this test,
    the test fails and forces a coordinated update of the prompt-tuner
    capability surface + this test in the same cycle. Mirrors the
    ``allow_body_*`` contract-pin discipline from
    ``feedback_contract_pin_sweep_before_allowlist_widening`` (per the
    builder.md pre-commit checklist).
    """
    assert SUPPORTED_DOCUMENT_MIME == {"application/pdf"}


# --- extract_pdf_text -----------------------------------------------------


def test_extract_pdf_text_returns_per_page_content(
    two_page_pdf_bytes: bytes,
) -> None:
    """Happy path: both pages' text survives extraction."""
    text = extract_pdf_text(two_page_pdf_bytes)
    assert "Page one body" in text
    assert "Page two body" in text


def test_extract_pdf_text_joins_pages_with_double_newline(
    two_page_pdf_bytes: bytes,
) -> None:
    """Page boundaries are preserved as double-newlines in the output."""
    text = extract_pdf_text(two_page_pdf_bytes)
    # Both page markers should appear with a paragraph break between
    # them so the model can detect page boundaries if it cares.
    p1_idx = text.find("Page one body")
    p2_idx = text.find("Page two body")
    assert p1_idx >= 0 and p2_idx >= 0
    assert p2_idx > p1_idx
    # Whitespace between the two pages includes at least one newline.
    between = text[p1_idx:p2_idx]
    assert "\n" in between


def test_extract_pdf_text_garbage_raises() -> None:
    """Non-PDF bytes raise ``AttachmentExtractError``."""
    garbage = b"this is not a PDF, just some random bytes"
    with pytest.raises(AttachmentExtractError):
        extract_pdf_text(garbage)


def test_extract_pdf_text_empty_bytes_raises() -> None:
    """Empty input raises ``AttachmentExtractError`` (not, e.g., IndexError)."""
    with pytest.raises(AttachmentExtractError):
        extract_pdf_text(b"")


def test_extract_pdf_text_truncates_long_text(long_pdf_bytes: bytes) -> None:
    """Text over :data:`MAX_EXTRACTED_CHARS` gets cut + carries the marker.

    The truncation behaviour is load-bearing for context-budget safety:
    a 200-page operations manual must not blow the LLM context window.
    Pin the marker text so a future "improvement" that changes the
    truncation wording is caught.
    """
    text = extract_pdf_text(long_pdf_bytes)
    assert TRUNCATION_MARKER in text
    # Truncation happens at MAX_EXTRACTED_CHARS, then the marker is
    # appended. Total length is the cap plus the marker length.
    assert len(text) == MAX_EXTRACTED_CHARS + len(TRUNCATION_MARKER)


def test_extract_pdf_text_short_text_no_marker(
    two_page_pdf_bytes: bytes,
) -> None:
    """Under-cap text passes through without a truncation marker.

    Symmetric to the truncation pin above — the no-truncation path
    must not accidentally append the marker.
    """
    text = extract_pdf_text(two_page_pdf_bytes)
    assert TRUNCATION_MARKER not in text
    assert len(text) < MAX_EXTRACTED_CHARS


def test_extract_pdf_text_empty_extraction_raises() -> None:
    """A PDF whose extraction yields empty text raises ``AttachmentExtractError``.

    This is the scanned-image-only PDF case: the PDF is valid, but it
    has no embedded text layer. The text-extract path can't recover
    here; the handler emits a user-facing "scanned PDF" reply via the
    raised exception's message.
    """
    pypdf = pytest.importorskip("pypdf")
    # Build a PDF with no content streams — pypdf reads it but
    # extracts nothing.
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    writer.write(buf)

    with pytest.raises(AttachmentExtractError):
        extract_pdf_text(buf.getvalue())


# --- storage_path_for_document -------------------------------------------


def test_storage_path_pattern(tmp_path: Path) -> None:
    """File name follows ``document-<stamp>-<short>.<ext>`` pattern."""
    when = datetime(2026, 6, 6, 13, 57, 14, tzinfo=timezone.utc)
    path = storage_path_for_document(
        tmp_path, "abcdEFGH9999", extension="pdf", when=when,
    )
    assert path.parent == tmp_path / "inbox"
    assert path.name == "document-20260606T135714Z-abcdEFGH.pdf"


def test_storage_path_strips_unsafe_chars(tmp_path: Path) -> None:
    """File-unique IDs with non-safe chars are sanitised in the short-id slug."""
    when = datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc)
    path = storage_path_for_document(
        tmp_path, "abc/../def", extension="pdf", when=when,
    )
    # Slashes and dots get stripped; remaining alphanumerics survive.
    assert "/" not in path.name
    assert ".." not in path.name


def test_storage_path_empty_unique_id(tmp_path: Path) -> None:
    """Empty / fully-invalid unique-id falls back to the ``unknown`` slug."""
    when = datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc)
    path = storage_path_for_document(tmp_path, "", when=when)
    assert "unknown" in path.name


def test_storage_path_distinct_prefix_from_image(tmp_path: Path) -> None:
    """Document filenames use ``document-`` prefix, distinct from images.

    A vault-walk regex over ``inbox/`` can disambiguate image and
    document attachments by filename alone — image side uses
    ``screenshot-``, document side uses ``document-``.
    """
    when = datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc)
    path = storage_path_for_document(tmp_path, "abc", when=when)
    assert path.name.startswith("document-")


# --- save_document_to_inbox ----------------------------------------------


def test_save_document_to_inbox_writes_bytes(tmp_path: Path) -> None:
    """Saved file has the exact byte content we passed in."""
    payload = b"%PDF-1.4 dummy payload bytes"
    saved = save_document_to_inbox(
        payload, tmp_path, "uniqid01",
        when=datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert saved.exists()
    assert saved.read_bytes() == payload


def test_save_document_creates_inbox_dir(tmp_path: Path) -> None:
    """Inbox dir is created on demand — fresh vaults shouldn't fail."""
    vault = tmp_path / "fresh-vault"
    vault.mkdir()
    # No inbox subdir exists yet.
    assert not (vault / "inbox").exists()

    save_document_to_inbox(
        b"%PDF-1.4 payload", vault, "uniqid02",
        when=datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert (vault / "inbox").is_dir()


# --- build_document_user_text --------------------------------------------


def test_build_document_user_text_with_caption() -> None:
    """Caption + filename + fenced text — full shape."""
    out = build_document_user_text(
        caption="Take a look at the action items.",
        extracted_text="Q4 financial review\n\nRevenue up 12%",
        filename="report.pdf",
    )
    assert "[PDF attached: report.pdf]" in out
    assert "Take a look at the action items." in out
    assert "--- Document text ---" in out
    assert "Q4 financial review" in out


def test_build_document_user_text_without_caption() -> None:
    """No caption: shape collapses cleanly with no leading blank block."""
    out = build_document_user_text(
        caption="",
        extracted_text="Document body here",
        filename="manual.pdf",
    )
    assert "[PDF attached: manual.pdf]" in out
    assert "--- Document text ---" in out
    assert "Document body here" in out
    # No double-blank-line orphan from a missing caption.
    assert "\n\n\n" not in out


def test_build_document_user_text_whitespace_caption_treated_as_empty() -> None:
    """Whitespace-only caption is normalised to empty (no orphan block)."""
    out = build_document_user_text(
        caption="   \n\n  ",
        extracted_text="body",
        filename="x.pdf",
    )
    assert "\n\n\n" not in out


def test_build_document_user_text_empty_filename_uses_fallback() -> None:
    """Empty filename falls back to ``document.pdf`` in the header."""
    out = build_document_user_text(
        caption="",
        extracted_text="body",
        filename="",
    )
    assert "[PDF attached: document.pdf]" in out


# --- Module-constant smoke -----------------------------------------------


def test_max_pdf_bytes_is_ten_mib() -> None:
    """10 MiB cap — pin the constant so refactors are visible.

    A future drop to 5 MB or rise to 50 MB is fine, but it should
    require a deliberate test update so the change shows in code review.
    """
    assert attachments.MAX_PDF_BYTES == 10 * 1024 * 1024


def test_max_extracted_chars_default() -> None:
    """50k char extraction cap — pin so accidental change is visible."""
    assert attachments.MAX_EXTRACTED_CHARS == 50_000
