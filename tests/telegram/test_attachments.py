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
    page_one: str = "Page one body - quarterly metrics summary.",
    page_two: str = "Page two body - recommendations and next steps.",
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
    """PDF must be in the allowlist (was c1's ship; P8 keeps it)."""
    assert "application/pdf" in SUPPORTED_DOCUMENT_MIME
    assert SUPPORTED_DOCUMENT_MIME["application/pdf"] == "pdf"


def test_supported_mime_full_p8_contract() -> None:
    """Allowlist contract pin — P8 universal filetype bundle.

    Pinning the exact dict surfaces silent widenings — if a future
    commit adds ``application/vnd.ms-excel`` (.xls) without updating
    this test, the test fails and forces a coordinated update of (a)
    the prompt-tuner capability surface, (b) this test, (c) the
    human-readable labels dict, (d) MAX_BYTES_BY_KIND, and (e) the
    on_document dispatch tree. Mirrors the ``allow_body_*``
    contract-pin discipline from
    ``feedback_contract_pin_sweep_before_allowlist_widening`` (per the
    builder.md pre-commit checklist).

    Per ``feedback_universal_filetype_support.md`` (operator-ratified
    2026-06-06): the allowlist is shared across all instances —
    Salem / Hypatia / KAL-LE / V.E.R.A. / STAY-C all get every type.
    No per-instance gate.
    """
    assert SUPPORTED_DOCUMENT_MIME == {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document": "docx",
        "text/plain": "text",
        "text/markdown": "text",
        "text/csv": "csv",
        "text/calendar": "ics",
        "audio/mpeg": "audio",
        "audio/mp4": "audio",
        "audio/x-m4a": "audio",
        "audio/wav": "audio",
        "audio/x-wav": "audio",
        "audio/ogg": "audio",
    }


def test_supported_mime_kinds_are_unique_set() -> None:
    """Every kind tag has corresponding entries in dispatch tables.

    Catches a partial-add: someone adds a new MIME → kind mapping
    above but forgets to add the kind to ``MAX_BYTES_BY_KIND`` or
    ``_HUMAN_LABELS_BY_KIND``. The contract is "if it's a value in
    SUPPORTED_DOCUMENT_MIME, it must be a key in both downstream
    tables."
    """
    kinds = set(SUPPORTED_DOCUMENT_MIME.values())
    # Every kind must have a size cap.
    for k in kinds:
        assert k in attachments.MAX_BYTES_BY_KIND, (
            f"kind {k!r} is in SUPPORTED_DOCUMENT_MIME but missing "
            "from MAX_BYTES_BY_KIND"
        )
    # Every kind must have a human label (else _supported_types_human
    # would silently drop it from the user-facing rejection text).
    for k in kinds:
        assert k in attachments._HUMAN_LABELS_BY_KIND, (
            f"kind {k!r} is in SUPPORTED_DOCUMENT_MIME but missing "
            "from _HUMAN_LABELS_BY_KIND"
        )


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
    """Caption + filename + fenced text — full shape (PDF kind)."""
    out = build_document_user_text(
        caption="Take a look at the action items.",
        extracted_text="Q4 financial review\n\nRevenue up 12%",
        filename="report.pdf",
        kind="pdf",
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
        kind="pdf",
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
        kind="pdf",
    )
    assert "\n\n\n" not in out


def test_build_document_user_text_empty_filename_uses_fallback() -> None:
    """Empty filename falls back to ``document.pdf`` in the header."""
    out = build_document_user_text(
        caption="",
        extracted_text="body",
        filename="",
        kind="pdf",
    )
    assert "[PDF attached: document.pdf]" in out


# --- build_document_user_text — per-kind banners + fences ----------------


def test_build_document_user_text_docx_banner() -> None:
    """DOCX kind produces ``[DOCX attached: ...]`` banner."""
    out = build_document_user_text(
        caption="",
        extracted_text="paragraph 1\n\nparagraph 2",
        filename="proposal.docx",
        kind="docx",
    )
    assert "[DOCX attached: proposal.docx]" in out
    assert "--- Document text ---" in out


def test_build_document_user_text_text_banner() -> None:
    """Text kind produces ``[Text file attached: ...]`` banner."""
    out = build_document_user_text(
        caption="",
        extracted_text="raw text body",
        filename="notes.txt",
        kind="text",
    )
    assert "[Text file attached: notes.txt]" in out
    assert "--- Document text ---" in out


def test_build_document_user_text_csv_banner() -> None:
    """CSV kind produces ``[CSV attached: ...]`` banner."""
    out = build_document_user_text(
        caption="",
        extracted_text="| a | b |\n| --- | --- |\n| 1 | 2 |",
        filename="data.csv",
        kind="csv",
    )
    assert "[CSV attached: data.csv]" in out
    assert "--- Document text ---" in out


def test_build_document_user_text_ics_banner_and_fence() -> None:
    """ICS uses Events fence label (not Document text)."""
    out = build_document_user_text(
        caption="",
        extracted_text="Event: Lunch\nStarts: 2026-06-10T12:00",
        filename="invite.ics",
        kind="ics",
    )
    assert "[Calendar invite attached: invite.ics]" in out
    # ICS gets its own fence label so the LLM treats it as structured.
    assert "--- Events ---" in out
    assert "--- Document text ---" not in out


def test_build_document_user_text_audio_banner_and_fence() -> None:
    """Audio uses Transcript fence label (not Document text)."""
    out = build_document_user_text(
        caption="",
        extracted_text="Hello, this is a voice note from the field.",
        filename="recording.m4a",
        kind="audio",
    )
    assert "[Audio transcript: recording.m4a]" in out
    assert "--- Transcript ---" in out
    assert "--- Document text ---" not in out


def test_build_document_user_text_unknown_kind_defensive_fallback() -> None:
    """Unknown kind falls back to ``Document attached`` + ``Document text``.

    Defensive — the on_document dispatcher only calls with known kinds
    (from :data:`SUPPORTED_DOCUMENT_MIME.values()`), but a future
    refactor that misses adding a kind to the banner map shouldn't
    crash the user-facing path.
    """
    out = build_document_user_text(
        caption="",
        extracted_text="body",
        filename="weird.xyz",
        kind="future_unknown_kind",
    )
    assert "[Document attached: weird.xyz]" in out
    assert "--- Document text ---" in out


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


# === P8 — universal filetype bundle ======================================
#
# Tests below cover the five new extractors (.docx, text, .csv, .ics,
# audio), the per-kind size caps, the storage helpers for audio, the
# dispatch-table pins (MAX_BYTES_BY_KIND, _HUMAN_LABELS_BY_KIND,
# extension_for_kind), and the supported-types human-readable helper.


# --- Per-kind size caps --------------------------------------------------


def test_max_bytes_by_kind_contract() -> None:
    """Pin per-kind caps so silent changes show in code review."""
    assert attachments.MAX_BYTES_BY_KIND == {
        "pdf": 10 * 1024 * 1024,
        "docx": 10 * 1024 * 1024,
        "text": 5 * 1024 * 1024,
        "csv": 5 * 1024 * 1024,
        "ics": 1 * 1024 * 1024,
        "audio": 25 * 1024 * 1024,
    }


def test_max_audio_bytes_matches_groq_whisper_sync_cap() -> None:
    """Groq Whisper sync endpoint rejects above 25 MiB. Pin the constant."""
    assert attachments.MAX_AUDIO_BYTES == 25 * 1024 * 1024


# --- extract_docx_text ---------------------------------------------------


def _make_docx_bytes(paragraphs: list[str], tables: list[list[list[str]]] | None = None) -> bytes:
    """Build a tiny .docx with the given paragraphs + tables.

    Uses python-docx to construct in memory rather than committing a
    binary fixture. Each paragraph is one ``add_paragraph`` call; each
    table is rendered via ``add_table`` with the given rows of cells.
    """
    docx_mod = pytest.importorskip("docx")
    doc = docx_mod.Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    if tables:
        for table_rows in tables:
            if not table_rows:
                continue
            n_cols = max(len(r) for r in table_rows)
            tbl = doc.add_table(rows=len(table_rows), cols=n_cols)
            for r_idx, row in enumerate(table_rows):
                for c_idx, val in enumerate(row):
                    tbl.cell(r_idx, c_idx).text = val
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_extract_docx_text_paragraphs() -> None:
    """Happy path: paragraphs come out joined with double-newlines."""
    docx_bytes = _make_docx_bytes(
        paragraphs=["First paragraph here.", "Second paragraph follows."],
    )
    text = attachments.extract_docx_text(docx_bytes)
    assert "First paragraph here." in text
    assert "Second paragraph follows." in text
    # Paragraphs separated by a blank line.
    p1_idx = text.find("First paragraph")
    p2_idx = text.find("Second paragraph")
    assert p2_idx > p1_idx
    assert "\n\n" in text[p1_idx:p2_idx]


def test_extract_docx_text_renders_tables() -> None:
    """Tables render as ``[Table N]`` markers + pipe-separated rows."""
    docx_bytes = _make_docx_bytes(
        paragraphs=["Quarterly report."],
        tables=[[
            ["Region", "Revenue", "Growth"],
            ["North", "1.2M", "12%"],
            ["South", "0.9M", "8%"],
        ]],
    )
    text = attachments.extract_docx_text(docx_bytes)
    assert "[Table 1]" in text
    assert "Region | Revenue | Growth" in text
    assert "North | 1.2M | 12%" in text


def test_extract_docx_text_mixed_paragraphs_and_tables() -> None:
    """Paragraphs + tables both surface in one extraction.

    Note: python-docx exposes ``.paragraphs`` and ``.tables`` as
    separate lists, so the extractor renders all paragraphs first,
    then all tables. Interleaving precision is out of c1 scope (per
    extractor docstring).
    """
    docx_bytes = _make_docx_bytes(
        paragraphs=["Intro paragraph."],
        tables=[[["A", "B"], ["1", "2"]]],
    )
    text = attachments.extract_docx_text(docx_bytes)
    assert "Intro paragraph." in text
    assert "[Table 1]" in text


def test_extract_docx_text_empty_docx_raises() -> None:
    """A .docx with no paragraphs and no tables raises ExtractError."""
    docx_bytes = _make_docx_bytes(paragraphs=[])
    with pytest.raises(attachments.AttachmentExtractError):
        attachments.extract_docx_text(docx_bytes)


def test_extract_docx_text_garbage_raises() -> None:
    """Non-.docx bytes raise ExtractError on open."""
    garbage = b"not a docx, just some bytes"
    with pytest.raises(attachments.AttachmentExtractError):
        attachments.extract_docx_text(garbage)


# --- extract_text_decoded ------------------------------------------------


def test_extract_text_decoded_utf8_plain() -> None:
    """Plain UTF-8 bytes decode cleanly."""
    raw = "Hello, this is plain UTF-8 text.\nSecond line here.".encode("utf-8")
    out = attachments.extract_text_decoded(raw)
    assert "Hello, this is plain UTF-8 text." in out
    assert "Second line here." in out


def test_extract_text_decoded_utf8_bom_stripped() -> None:
    """UTF-8 BOM (``\\xef\\xbb\\xbf``) is stripped before decoding."""
    raw = b"\xef\xbb\xbf" + "Content after BOM.".encode("utf-8")
    out = attachments.extract_text_decoded(raw)
    # BOM character must not appear in output.
    assert "﻿" not in out
    assert out.startswith("Content after BOM.")


def test_extract_text_decoded_utf16_le_bom() -> None:
    """UTF-16 LE BOM (``\\xff\\xfe``) triggers UTF-16 LE decoding."""
    raw = b"\xff\xfe" + "LE encoded text".encode("utf-16-le")
    out = attachments.extract_text_decoded(raw)
    assert out == "LE encoded text"


def test_extract_text_decoded_utf16_be_bom() -> None:
    """UTF-16 BE BOM (``\\xfe\\xff``) triggers UTF-16 BE decoding."""
    raw = b"\xfe\xff" + "BE encoded text".encode("utf-16-be")
    out = attachments.extract_text_decoded(raw)
    assert out == "BE encoded text"


def test_extract_text_decoded_invalid_utf8_falls_back_to_replace() -> None:
    """Bad UTF-8 bytes get the ``errors=replace`` fallback path.

    Replacement characters (U+FFFD) leak into the output; the
    conversation continues rather than dropping the message. The log
    line ``utf8_decode_fallback`` is the operator-visible signal that
    the fallback fired.
    """
    # Construct bytes that fail strict UTF-8 decoding: a stray
    # continuation byte without a leading byte.
    raw = b"hello\xc3world"  # 0xC3 starts a 2-byte sequence; next byte must be continuation
    out = attachments.extract_text_decoded(raw)
    # The replacement char shows up where the bad byte was.
    assert "hello" in out
    assert "world" in out
    # The U+FFFD replacement character is present.
    assert "�" in out


def test_extract_text_decoded_empty_raises() -> None:
    """Empty bytes raise ExtractError (consistent with other extractors)."""
    with pytest.raises(attachments.AttachmentExtractError):
        attachments.extract_text_decoded(b"")


def test_extract_text_decoded_whitespace_only_raises() -> None:
    """All-whitespace text raises ExtractError after .strip()."""
    with pytest.raises(attachments.AttachmentExtractError):
        attachments.extract_text_decoded(b"   \n\n  \t  ")


# --- extract_csv_text ----------------------------------------------------


def test_extract_csv_text_basic_table() -> None:
    """Basic CSV renders as a Markdown table with header + separator."""
    csv_bytes = b"name,amount,date\nAlice,100,2026-06-01\nBob,200,2026-06-02\n"
    text = attachments.extract_csv_text(csv_bytes)
    # Markdown table shape.
    assert "| name | amount | date |" in text
    assert "| --- | --- | --- |" in text
    assert "| Alice | 100 | 2026-06-01 |" in text
    assert "| Bob | 200 | 2026-06-02 |" in text


def test_extract_csv_text_ragged_rows_padded() -> None:
    """Rows with fewer cells than the header get right-padded with empty cells."""
    csv_bytes = b"a,b,c\n1,2,3\n4,5\n"
    text = attachments.extract_csv_text(csv_bytes)
    # The short row renders with an empty trailing cell.
    assert "| 4 | 5 |  |" in text


def test_extract_csv_text_truncates_at_max_rows() -> None:
    """CSV over MAX_CSV_ROWS gets row-truncated + a marker."""
    lines = ["col1,col2"]
    for i in range(attachments.MAX_CSV_ROWS + 50):
        lines.append(f"row{i},val{i}")
    csv_bytes = ("\n".join(lines)).encode("utf-8")
    text = attachments.extract_csv_text(csv_bytes)
    assert "CSV truncated" in text
    # First row preserved.
    assert "| row0 | val0 |" in text
    # Last kept row is at index MAX_CSV_ROWS - 2 (header + N-1 data rows).
    last_kept_idx = attachments.MAX_CSV_ROWS - 2
    assert f"| row{last_kept_idx} | val{last_kept_idx} |" in text


def test_extract_csv_text_pipe_in_cell_escaped() -> None:
    """Cells containing ``|`` get backslash-escaped so the table parses."""
    csv_bytes = b'a,b\n"x|y",z\n'
    text = attachments.extract_csv_text(csv_bytes)
    assert r"x\|y" in text


def test_extract_csv_text_empty_raises() -> None:
    """Empty CSV bytes raise ExtractError."""
    with pytest.raises(attachments.AttachmentExtractError):
        attachments.extract_csv_text(b"")


def test_extract_csv_text_only_blank_rows_raises() -> None:
    """All-empty CSV raises ExtractError after trailing-blank strip."""
    with pytest.raises(attachments.AttachmentExtractError):
        attachments.extract_csv_text(b",,,\n,,,\n")


# --- extract_ics_text ----------------------------------------------------


def _make_ics_bytes(events: list[dict[str, str]]) -> bytes:
    """Build a minimal .ics with the given VEVENTs.

    Each event dict supplies SUMMARY / DTSTART / DTEND / LOCATION /
    DESCRIPTION as raw strings (caller's responsibility to format
    DTSTART / DTEND correctly).
    """
    icalendar = pytest.importorskip("icalendar")
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Algernon Test//EN")
    cal.add("version", "2.0")
    for ev in events:
        event = icalendar.Event()
        for key, val in ev.items():
            event.add(key.lower(), val)
        cal.add_component(event)
    return cal.to_ical()


def test_extract_ics_text_single_event() -> None:
    """One VEVENT renders with summary + start + end."""
    from datetime import datetime as _dt, timezone as _tz
    ics_bytes = _make_ics_bytes([{
        "SUMMARY": "Standup",
        "DTSTART": _dt(2026, 6, 10, 10, 0, 0, tzinfo=_tz.utc),
        "DTEND": _dt(2026, 6, 10, 10, 30, 0, tzinfo=_tz.utc),
        "LOCATION": "Conference Room A",
    }])
    text = attachments.extract_ics_text(ics_bytes)
    assert "Event: Standup" in text
    assert "Starts:" in text
    assert "Ends:" in text
    assert "Location: Conference Room A" in text
    assert "2026-06-10" in text


def test_extract_ics_text_multiple_events_separated() -> None:
    """Multiple VEVENTs are separated by ``---`` lines."""
    from datetime import datetime as _dt, timezone as _tz
    ics_bytes = _make_ics_bytes([
        {
            "SUMMARY": "Event A",
            "DTSTART": _dt(2026, 6, 10, 10, 0, 0, tzinfo=_tz.utc),
            "DTEND": _dt(2026, 6, 10, 11, 0, 0, tzinfo=_tz.utc),
        },
        {
            "SUMMARY": "Event B",
            "DTSTART": _dt(2026, 6, 11, 14, 0, 0, tzinfo=_tz.utc),
            "DTEND": _dt(2026, 6, 11, 15, 0, 0, tzinfo=_tz.utc),
        },
    ])
    text = attachments.extract_ics_text(ics_bytes)
    assert "Event: Event A" in text
    assert "Event: Event B" in text
    assert "---" in text


def test_extract_ics_text_all_day_event() -> None:
    """All-day events render with the ``All-day on <date>`` shape."""
    from datetime import date as _date
    ics_bytes = _make_ics_bytes([{
        "SUMMARY": "Holiday",
        "DTSTART": _date(2026, 7, 4),
        "DTEND": _date(2026, 7, 5),  # All-day events have end = next day
    }])
    text = attachments.extract_ics_text(ics_bytes)
    assert "Event: Holiday" in text
    assert "All-day on 2026-07-04" in text


def test_extract_ics_text_with_description() -> None:
    """Event DESCRIPTION lands in the output."""
    from datetime import datetime as _dt, timezone as _tz
    ics_bytes = _make_ics_bytes([{
        "SUMMARY": "Planning",
        "DTSTART": _dt(2026, 6, 10, 10, 0, 0, tzinfo=_tz.utc),
        "DTEND": _dt(2026, 6, 10, 11, 0, 0, tzinfo=_tz.utc),
        "DESCRIPTION": "Q3 roadmap review with stakeholders",
    }])
    text = attachments.extract_ics_text(ics_bytes)
    assert "Description: Q3 roadmap review with stakeholders" in text


def test_extract_ics_text_no_vevents_raises() -> None:
    """Calendar with no VEVENTs raises ExtractError (VTODO-only case)."""
    # A calendar with only a VTODO.
    icalendar = pytest.importorskip("icalendar")
    cal = icalendar.Calendar()
    cal.add("prodid", "-//Test//EN")
    cal.add("version", "2.0")
    todo = icalendar.Todo()
    todo.add("summary", "Just a todo")
    cal.add_component(todo)
    ics_bytes = cal.to_ical()
    with pytest.raises(attachments.AttachmentExtractError):
        attachments.extract_ics_text(ics_bytes)


def test_extract_ics_text_garbage_raises() -> None:
    """Non-ics bytes raise ExtractError."""
    with pytest.raises(attachments.AttachmentExtractError):
        attachments.extract_ics_text(b"this is not a calendar")


# --- extract_audio_transcript --------------------------------------------


@pytest.mark.asyncio
async def test_extract_audio_transcript_happy_path(monkeypatch) -> None:
    """Audio bytes route through transcribe.transcribe; output is the text."""
    from alfred.telegram import transcribe as transcribe_mod

    captured: dict[str, object] = {}

    async def _fake_transcribe(audio_bytes, mime, config):
        captured["audio_bytes"] = audio_bytes
        captured["mime"] = mime
        captured["config"] = config
        return "Hello, this is the transcribed audio body."

    monkeypatch.setattr(transcribe_mod, "transcribe", _fake_transcribe)

    payload = b"fake-mp3-bytes"
    out = await attachments.extract_audio_transcript(
        payload, "audio/mpeg", stt_config="DUMMY_STT_CONFIG",
    )
    assert out == "Hello, this is the transcribed audio body."
    assert captured["audio_bytes"] == payload
    assert captured["mime"] == "audio/mpeg"
    assert captured["config"] == "DUMMY_STT_CONFIG"


@pytest.mark.asyncio
async def test_extract_audio_transcript_transcribe_error_wraps(monkeypatch) -> None:
    """``TranscribeError`` becomes ``AttachmentExtractError`` for the handler."""
    from alfred.telegram import transcribe as transcribe_mod

    async def _boom(*args, **kwargs):
        raise transcribe_mod.TranscribeError("synthetic API failure")

    monkeypatch.setattr(transcribe_mod, "transcribe", _boom)

    with pytest.raises(attachments.AttachmentExtractError) as exc_info:
        await attachments.extract_audio_transcript(
            b"bytes", "audio/mp4", stt_config=None,
        )
    assert "synthetic API failure" in str(exc_info.value)


@pytest.mark.asyncio
async def test_extract_audio_transcript_not_implemented_wraps(monkeypatch) -> None:
    """``NotImplementedError`` (unsupported STT provider) wraps cleanly."""
    from alfred.telegram import transcribe as transcribe_mod

    async def _boom(*args, **kwargs):
        raise NotImplementedError("provider 'foo' not supported")

    monkeypatch.setattr(transcribe_mod, "transcribe", _boom)

    with pytest.raises(attachments.AttachmentExtractError) as exc_info:
        await attachments.extract_audio_transcript(
            b"bytes", "audio/wav", stt_config=None,
        )
    assert "not configured" in str(exc_info.value).lower()


@pytest.mark.asyncio
async def test_extract_audio_transcript_empty_text_raises(monkeypatch) -> None:
    """Whisper returning whitespace-only triggers ExtractError."""
    from alfred.telegram import transcribe as transcribe_mod

    async def _empty(*args, **kwargs):
        return "   \n\n   "

    monkeypatch.setattr(transcribe_mod, "transcribe", _empty)

    with pytest.raises(attachments.AttachmentExtractError):
        await attachments.extract_audio_transcript(
            b"bytes", "audio/ogg", stt_config=None,
        )


# --- Audio storage helpers -----------------------------------------------


def test_storage_path_for_audio_pattern(tmp_path: Path) -> None:
    """Audio storage path: ``audio-<stamp>-<short>.<ext>``."""
    when = datetime(2026, 6, 6, 13, 57, 14, tzinfo=timezone.utc)
    path = attachments.storage_path_for_audio(
        tmp_path, "audidXYZ", extension="m4a", when=when,
    )
    assert path.parent == tmp_path / "inbox"
    assert path.name == "audio-20260606T135714Z-audidXYZ.m4a"


def test_storage_path_for_audio_distinct_prefix_from_document(tmp_path: Path) -> None:
    """``audio-`` vs. ``document-`` filename prefixes for disambiguation."""
    when = datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc)
    audio = attachments.storage_path_for_audio(tmp_path, "uid", when=when)
    doc = attachments.storage_path_for_document(tmp_path, "uid", when=when)
    assert audio.name.startswith("audio-")
    assert doc.name.startswith("document-")


def test_save_audio_to_inbox_writes_bytes(tmp_path: Path) -> None:
    """Audio bytes land on disk with the correct extension."""
    payload = b"\xff\xfb\x90\x00 fake mp3 frames"
    saved = attachments.save_audio_to_inbox(
        payload, tmp_path, "uidaudio", extension="mp3",
        when=datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert saved.exists()
    assert saved.read_bytes() == payload
    assert saved.name.endswith(".mp3")


def test_save_audio_creates_inbox_dir(tmp_path: Path) -> None:
    """Audio save creates the inbox dir on demand."""
    vault = tmp_path / "fresh"
    vault.mkdir()
    assert not (vault / "inbox").exists()
    attachments.save_audio_to_inbox(
        b"x", vault, "uid", extension="wav",
        when=datetime(2026, 6, 6, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert (vault / "inbox").is_dir()


# --- extension_for_kind --------------------------------------------------


def test_extension_for_kind_pdf() -> None:
    assert attachments.extension_for_kind("pdf") == "pdf"


def test_extension_for_kind_docx() -> None:
    assert attachments.extension_for_kind("docx") == "docx"


def test_extension_for_kind_text_plain_defaults_txt() -> None:
    assert attachments.extension_for_kind("text", "text/plain") == "txt"


def test_extension_for_kind_text_markdown_uses_md() -> None:
    assert attachments.extension_for_kind("text", "text/markdown") == "md"


def test_extension_for_kind_csv() -> None:
    assert attachments.extension_for_kind("csv") == "csv"


def test_extension_for_kind_ics() -> None:
    assert attachments.extension_for_kind("ics") == "ics"


def test_extension_for_kind_audio_mpeg_is_mp3() -> None:
    assert attachments.extension_for_kind("audio", "audio/mpeg") == "mp3"


def test_extension_for_kind_audio_mp4_is_m4a() -> None:
    assert attachments.extension_for_kind("audio", "audio/mp4") == "m4a"


def test_extension_for_kind_audio_x_m4a_is_m4a() -> None:
    assert attachments.extension_for_kind("audio", "audio/x-m4a") == "m4a"


def test_extension_for_kind_audio_wav() -> None:
    assert attachments.extension_for_kind("audio", "audio/wav") == "wav"


def test_extension_for_kind_unknown_kind_returns_bin() -> None:
    """Defensive fallback — never crash, always return something writeable."""
    assert attachments.extension_for_kind("future_unknown") == "bin"


# --- _supported_types_human ----------------------------------------------


def test_supported_types_human_lists_all_kinds() -> None:
    """The user-facing types string includes every active kind label.

    Derived dynamically so a future widening of
    SUPPORTED_DOCUMENT_MIME updates the rejection-reply text by
    extending the constant — no scattered string sweep.
    """
    text = attachments._supported_types_human()
    # Each human label from the active kinds appears.
    for kind in set(SUPPORTED_DOCUMENT_MIME.values()):
        label = attachments._HUMAN_LABELS_BY_KIND[kind]
        assert label in text, f"label {label!r} for kind {kind!r} missing from supported-types text"


def test_supported_types_human_uses_oxford_and() -> None:
    """For 3+ items, the list uses ``, and`` to join the final entry."""
    text = attachments._supported_types_human()
    # P8 has 6 distinct kinds → comma-list with ", and" before the last.
    assert ", and " in text


# --- Truncation helper shared semantics ----------------------------------


def test_apply_char_truncation_no_op_under_cap() -> None:
    """Text under the cap passes through unchanged."""
    text = "short body" * 10
    assert len(text) < attachments.MAX_EXTRACTED_CHARS
    assert attachments._apply_char_truncation(text, kind="pdf") == text


def test_apply_char_truncation_over_cap_appends_marker() -> None:
    """Over-cap text gets truncated + ``TRUNCATION_MARKER``."""
    text = "x" * (attachments.MAX_EXTRACTED_CHARS + 100)
    out = attachments._apply_char_truncation(text, kind="docx")
    assert out.endswith(attachments.TRUNCATION_MARKER)
    assert len(out) == attachments.MAX_EXTRACTED_CHARS + len(attachments.TRUNCATION_MARKER)
