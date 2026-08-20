"""#57 — the shared PDF extractor (``alfred.documents.pdf``).

Two things are under test here and they are worth naming separately:

1. **The extraction contract itself** — every failure shape gets its own
   ``reason``, because the ingest route turns those into six distinct operator-
   facing refusals. A test that only asserted "it raised" would be green
   against an extractor that collapsed them all back into one.
2. **The LIFT** — this module moved out of ``alfred.telegram.attachments`` in
   #57 so the web ingest route could share it. The talker's behaviour had to
   survive that move byte-for-byte, so pins held the re-exports and the
   error-class ALIAS until ``telegram/attachments.py`` (the bot-side wrapper)
   was deleted in T5 (2026-08-19); the wording + cap pins below outlive it
   because the web ingest door consumes them directly.
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


#: Language that turns this refusal into a promise. Mirrors the web suite's
#: ``FUTURE_PROMISE`` (web/tests/ingestRefusalCopy.test.ts) — deliberately, and
#: the alignment is the #68 fix.
#:
#: The list used to be TEMPORAL ONLY ("coming soon", "not yet", …) while the
#: docstring claimed the message "must not dangle vision/OCR". So the pin's name
#: and its docstring described a stricter rule than its assertions enforced, and
#: the message it guarded said "needs OCR, which isn't enabled" — naming the
#: capability outright and passing, because no listed phrase appeared in it.
#: That is the same gap this file's web sibling was created to close: a name
#: that matches the claim is not an assertion that supports it.
#:
#: The BARE NOUNS are the substance. The ruling was not "avoid the future
#: tense", it was "do not point at the vision work at all" — so naming OCR or
#: vision violates it in any tense, including a denial ("isn't enabled", which
#: implies a switch someone could flip).
_FUTURE_PROMISE = (
    "vision",
    "ocr",
    "coming soon",
    "not yet",
    "will be",
    "will be able",
    "in future",
    "for now",
    "later",
    "soon",
)


@pytest.mark.parametrize("promise", _FUTURE_PROMISE)
def test_a_scanned_pdf_does_not_promise_a_future_capability(promise) -> None:
    """Operator ruling 2026-08-07: the scanned case is a plain refusal. The
    user-facing words must not dangle vision/OCR — that was explicitly NOT the
    copy option chosen, and #67 remains deferred and unruled.

    Parametrized so a failure NAMES the offending phrase instead of reporting
    that one of ten substrings was present somewhere.
    """
    with pytest.raises(DocumentExtractError) as excinfo:
        extract_pdf_text(scanned_pdf())
    message = str(excinfo.value).lower()
    assert promise not in message, f"message dangles a future capability: {promise}"


def test_the_scanned_refusal_still_says_what_happened_and_what_to_do() -> None:
    """The other half of the ruling, and the reason the list above is safe.

    A forbidden-word list alone is satisfiable by saying nothing useful — the
    emptiest possible message passes every row of it. So this pins the content:
    the operator learns WHY it failed in the web copy's words ("no selectable
    text" / a scan), and gets the two routes out that exist TODAY with no new
    capability.
    """
    with pytest.raises(DocumentExtractError) as excinfo:
        extract_pdf_text(scanned_pdf())
    message = str(excinfo.value).lower()
    assert "no selectable text" in message
    assert "scan" in message
    assert "paste" in message or "saved as text" in message


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
# (the LIFT re-export pins — deleted in T5 2026-08-19 with
# telegram/attachments.py itself: the alias + re-export surface existed
# for the bot's on_document handler and test_attachments.py, both gone.
# The lifted module below is the ONLY spelling now.)
# ---------------------------------------------------------------------------


def test_the_talker_message_wording_is_unchanged_by_the_lift() -> None:
    """The talker prints ``str(exc)`` to the user verbatim, so the exact
    sentence is pinned — casual edits to operator-facing copy are the thing
    this catches. #57 added ``.reason`` ALONGSIDE the message rather than
    encoding the distinction into the sentence, precisely so the text could be
    changed on purpose without moving the routing.

    UPDATED UNDER #68, in lockstep with the copy change rather than deleted.
    The tripwire is the point: it fired on this very edit, which is how a
    deliberate rewording is meant to feel. The old sentence — "scanned
    image-only PDFs need OCR, which isn't enabled" — named the deferred
    capability outright, and "isn't enabled" implies a switch someone could
    flip. The new one matches the web copy's voice ("no selectable text", a
    scan) and offers only routes that exist today.
    """
    with pytest.raises(DocumentExtractError) as excinfo:
        extract_pdf_text(scanned_pdf())
    assert str(excinfo.value) == (
        "No selectable text in this PDF, so it looks like a scan or a "
        "photo. Try a version saved as text, or paste the text in yourself"
    )

    with pytest.raises(DocumentExtractError) as excinfo:
        extract_pdf_text(corrupt_pdf())
    assert str(excinfo.value).startswith("Failed to decode PDF: ")


def test_the_byte_cap_is_the_ratified_ten_mib() -> None:
    """Ratified 2026-06-06 for Telegram, re-ratified 2026-08-07 for web
    ingest. ONE number across both doors so an operator never has to ask which
    entrance has the smaller cap."""
    assert MAX_PDF_BYTES == 10 * 1024 * 1024
