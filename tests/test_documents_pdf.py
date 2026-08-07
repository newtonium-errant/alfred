"""#57 — the shared PDF extractor (``alfred.documents.pdf``).

Two things are under test here and they are worth naming separately:

1. **The extraction contract itself** — every failure shape gets its own
   ``reason``, because the ingest route turns those into six distinct operator-
   facing refusals. A test that only asserted "it raised" would be green
   against an extractor that collapsed them all back into one.
2. **The LIFT** — this module moved out of ``alfred.telegram.attachments`` in
   #57 so the web ingest route could share it. The talker's behaviour had to
   survive that move byte-for-byte, so the pins below hold the re-exports, the
   error-class ALIAS, and the truncation default that the talker relies on.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.documents.pdf import (
    MAX_EXTRACTED_CHARS,
    MAX_PDF_BYTES,
    REASON_EMPTY_FILE,
    REASON_ENCRYPTED,
    REASON_NO_TEXT_LAYER,
    REASON_UNREADABLE,
    TRUNCATION_MARKER,
    DocumentExtractError,
    apply_char_truncation,
    extract_pdf_text,
)

from .pdf_fixtures import (
    corrupt_pdf,
    empty_pdf,
    encrypted_pdf,
    not_a_pdf,
    scanned_pdf,
    text_layer_pdf,
)


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


def test_a_text_layer_pdf_extracts_its_text() -> None:
    text = extract_pdf_text(text_layer_pdf(["Alpha line", "Beta line"]))
    assert "Alpha line" in text
    assert "Beta line" in text


def test_pages_are_joined_so_boundaries_survive() -> None:
    """Page joins use a blank line, so the reader can still see where one page
    ended — a detail that matters once a statement runs to several pages."""
    text = extract_pdf_text(text_layer_pdf(["One", "Two", "Three"]))
    assert text.strip() == text, "no leading/trailing whitespace"
    assert "One" in text and "Three" in text


# ---------------------------------------------------------------------------
# the failure taxonomy — each reason DISTINCT
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "make_pdf,expected_reason",
    [
        (scanned_pdf, REASON_NO_TEXT_LAYER),
        (encrypted_pdf, REASON_ENCRYPTED),
        (corrupt_pdf, REASON_UNREADABLE),
        (not_a_pdf, REASON_UNREADABLE),
        (empty_pdf, REASON_EMPTY_FILE),
    ],
)
def test_each_failure_shape_carries_its_own_reason(make_pdf, expected_reason) -> None:
    """The whole point of ``.reason``. Six failures that render identically to
    the operator is the silence the ILB rule exists to prevent — "it didn't
    work" is not a sentence anyone can act on. A build that collapsed these
    into one code would pass a test that only asserted "it raised"."""
    with pytest.raises(DocumentExtractError) as excinfo:
        make_pdf_bytes = make_pdf()
        extract_pdf_text(make_pdf_bytes)
    assert excinfo.value.reason == expected_reason


def test_the_reasons_are_actually_distinct_from_each_other() -> None:
    """Guards the parametrize above from a mutation that makes every REASON_*
    constant the same string — which would leave all five rows green."""
    reasons = {
        REASON_NO_TEXT_LAYER, REASON_ENCRYPTED,
        REASON_UNREADABLE, REASON_EMPTY_FILE,
    }
    assert len(reasons) == 4


def test_a_scanned_pdf_does_not_promise_a_future_capability(tmp_path) -> None:
    """Operator ruling 2026-08-07: the scanned case is a plain refusal. The
    user-facing words must not dangle vision/OCR as something coming soon —
    that was explicitly NOT the copy option chosen."""
    with pytest.raises(DocumentExtractError) as excinfo:
        extract_pdf_text(scanned_pdf())
    message = str(excinfo.value).lower()
    for promise in ("coming soon", "not yet", "will be", "in future", "later"):
        assert promise not in message, f"message dangles a future capability: {promise}"


def test_a_scanned_pdf_logs_its_reason() -> None:
    """Observability pin driving the production path (builder rule 9)."""
    with structlog.testing.capture_logs() as captured:
        with pytest.raises(DocumentExtractError):
            extract_pdf_text(scanned_pdf())
    events = [e for e in captured
              if e.get("event") == "talker.attachments.empty_extraction"]
    assert len(events) == 1
    assert events[0]["reason"] == REASON_NO_TEXT_LAYER


# ---------------------------------------------------------------------------
# truncate-vs-refuse: the two callers' opposite policies
# ---------------------------------------------------------------------------


def test_max_chars_none_does_not_truncate() -> None:
    """The INGEST policy. This route writes a verbatim record, so silently
    shortening a bank statement at 50,000 characters would be data loss inside
    the one route whose promise is that the body is what was uploaded. It
    refuses at its own ceiling instead — so the extractor must hand back
    everything."""
    long_text = "x" * (MAX_EXTRACTED_CHARS + 5_000)
    assert apply_char_truncation(long_text, kind="pdf", max_chars=None) == long_text
    assert TRUNCATION_MARKER not in apply_char_truncation(
        long_text, kind="pdf", max_chars=None,
    )


def test_the_default_still_truncates_at_the_talker_cap() -> None:
    """The TALKER policy, unchanged by the lift. Its extracted text feeds an
    LLM context window, where a partial read plus a visible marker beats
    blowing the per-turn budget."""
    long_text = "y" * (MAX_EXTRACTED_CHARS + 1)
    out = apply_char_truncation(long_text, kind="pdf")
    assert out.endswith(TRUNCATION_MARKER)
    assert len(out) == MAX_EXTRACTED_CHARS + len(TRUNCATION_MARKER)


def test_truncation_is_a_no_op_under_the_cap() -> None:
    assert apply_char_truncation("short", kind="pdf") == "short"


def test_the_log_prefix_is_caller_scoped() -> None:
    """After the lift a second caller shares this module. Its events must not
    claim to be Telegram attachments — an operator grepping
    ``talker.attachments`` should still be reading about the talker."""
    with structlog.testing.capture_logs() as captured:
        apply_char_truncation(
            "z" * (MAX_EXTRACTED_CHARS + 1), kind="pdf",
            log_event_prefix="transport.ingest.pdf",
        )
    names = {e.get("event") for e in captured}
    assert "transport.ingest.pdf.text_truncated" in names
    assert "talker.attachments.text_truncated" not in names


# ---------------------------------------------------------------------------
# the LIFT — the talker's surface must be unchanged
# ---------------------------------------------------------------------------


def test_attachment_extract_error_is_an_alias_not_a_subclass() -> None:
    """Load-bearing, and the reason is subtle: the shared module raises
    ``DocumentExtractError``. Every existing talker handler catches
    ``AttachmentExtractError``. If that name were a SUBCLASS, those handlers
    would silently stop catching — the raise would sail past an ``except`` that
    still looks correct in the diff. Aliasing is what makes them the same
    class, so this identity is the pin."""
    from alfred.telegram import attachments

    assert attachments.AttachmentExtractError is DocumentExtractError

    with pytest.raises(attachments.AttachmentExtractError):
        extract_pdf_text(scanned_pdf())


def test_the_talker_module_still_exposes_the_lifted_names() -> None:
    """The re-exports. ``tests/telegram/test_attachments.py`` reads these off
    the ``attachments`` module, and so does the on_document handler."""
    from alfred.telegram import attachments

    assert attachments.MAX_PDF_BYTES == MAX_PDF_BYTES
    assert attachments.MAX_EXTRACTED_CHARS == MAX_EXTRACTED_CHARS
    assert attachments.TRUNCATION_MARKER == TRUNCATION_MARKER
    assert attachments.extract_pdf_text is extract_pdf_text
    assert attachments.MAX_BYTES_BY_KIND["pdf"] == MAX_PDF_BYTES


def test_the_talker_message_wording_is_unchanged_by_the_lift() -> None:
    """The talker prints ``str(exc)`` to the user verbatim, and that wording
    was reviewed when it shipped. #57 added ``.reason`` ALONGSIDE it rather
    than encoding the distinction into the sentence, precisely so this text
    could stay put."""
    with pytest.raises(DocumentExtractError) as excinfo:
        extract_pdf_text(scanned_pdf())
    assert str(excinfo.value) == (
        "No text could be extracted from this PDF "
        "(scanned image-only PDFs need OCR, which isn't enabled)"
    )

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_pdf_text(corrupt_pdf())
    assert str(excinfo.value).startswith("Failed to decode PDF: ")


def test_the_byte_cap_is_the_ratified_ten_mib() -> None:
    """Ratified 2026-06-06 for Telegram, re-ratified 2026-08-07 for web
    ingest. ONE number across both doors so an operator never has to ask which
    entrance has the smaller cap."""
    assert MAX_PDF_BYTES == 10 * 1024 * 1024
