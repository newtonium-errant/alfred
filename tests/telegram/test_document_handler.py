"""Integration tests for :func:`alfred.telegram.bot.on_document`.

Coverage:

    1. PDF document → :func:`handle_message` called with extracted text
       + ``document_metadata`` populated
    2. Non-PDF document → user-facing rejection reply, no
       :func:`handle_message` call
    3. Oversized PDF → user-facing reply, no download
    4. Unauthorized user → silent drop (no reply, no
       ``record_handled`` bump)
    5. Download failure → user-facing reply, no
       :func:`handle_message` call
    6. Extract failure → user-facing reply, no
       :func:`handle_message` call
    7. Save-to-inbox failure → :func:`handle_message` still called
       (audit-trail-non-fatal contract — mirrors the on_photo
       save-failure contract)

The test pattern mirrors :file:`test_vision.py`: hand-rolled fake
objects for the PTB ``Document`` + ``Update`` + ``Context`` surfaces,
``monkeypatch`` to replace the actual module symbols, ``AsyncMock`` for
the reply path.
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from alfred.telegram import attachments, heartbeat


# Module-level state reset between tests: ``heartbeat`` is module-global
# and the handler increments it. Without an autouse reset, count leaks
# from one test into another would mask the unauthorized-silent assertion
# (test #4) — if a prior test left handled=2, test #4's "assert 0" would
# fail for the wrong reason. Mirrors the ``_reset_counter`` fixture in
# ``test_idle_tick.py``.
@pytest.fixture(autouse=True)
def _reset_counter():
    heartbeat.reset()
    yield
    heartbeat.reset()


# --- Fakes for the PTB Document / Update / Context surfaces ---------------


class _FakeDocFile:
    """Stand-in for the ``File`` returned by ``Document.get_file()``."""
    def __init__(self, content: bytes, *, raises: Exception | None = None) -> None:
        self._content = content
        self._raises = raises

    async def download_as_bytearray(self) -> bytearray:
        if self._raises is not None:
            raise self._raises
        return bytearray(self._content)


class _FakeDocument:
    """Minimal stand-in for ``telegram.Document``."""
    def __init__(
        self,
        content: bytes = b"",
        *,
        mime_type: str = "application/pdf",
        file_name: str = "test.pdf",
        file_size: int = 0,
        file_unique_id: str = "uniqid01",
        get_file_raises: Exception | None = None,
    ) -> None:
        self.mime_type = mime_type
        self.file_name = file_name
        self.file_size = file_size or len(content)
        self.file_unique_id = file_unique_id
        self.file_id = f"fid-{file_unique_id}"
        self._file = _FakeDocFile(content, raises=get_file_raises)

    async def get_file(self) -> _FakeDocFile:
        return self._file


def _build_update_and_ctx(
    talker_config,
    document: _FakeDocument | None,
    *,
    caption: str | None = None,
    user_id: int = 1,
):
    """Build a minimal Update + ctx shaped like the production path."""
    reply = AsyncMock()
    update = type("U", (), {})()
    update.message = type("M", (), {})()
    update.message.document = document
    update.message.reply_text = reply
    update.message.caption = caption
    update.effective_chat = type("C", (), {"id": 1})()
    update.effective_user = type("EU", (), {"id": user_id})()

    ctx = type("Ctx", (), {})()
    ctx.application = type("App", (), {"bot_data": {
        "config": talker_config,
        "state_mgr": None,
        "anthropic_client": None,
        "system_prompt": "",
        "vault_context_str": "",
        "chat_locks": {},
    }})()
    ctx.bot = type("B", (), {})()

    return update, ctx, reply


def _make_valid_pdf_bytes() -> bytes:
    """Build a tiny valid PDF with a single readable text line.

    Local helper so this test file is self-contained — the
    test_attachments fixture is session-scoped over there; we want a
    fresh small PDF for handler-level integration tests.
    """
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import (
        DecodedStreamObject,
        DictionaryObject,
        NameObject,
    )
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=612, height=792)

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

    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 50 750 Td (Quarterly report body) Tj ET")
    content_ref = writer._add_object(stream)
    page[NameObject("/Contents")] = content_ref

    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


# --- 1. Happy path: PDF → handle_message with extracted text -------------


@pytest.mark.asyncio
async def test_on_document_pdf_calls_handle_message_with_extracted_text(
    talker_config, monkeypatch,
) -> None:
    """PDF → handle_message receives the extracted text + document_metadata.

    Pins the end-to-end contract: download → extract → save → dispatch.
    """
    from alfred.telegram import bot

    pdf_bytes = _make_valid_pdf_bytes()
    document = _FakeDocument(
        content=pdf_bytes,
        mime_type="application/pdf",
        file_name="report.pdf",
        file_unique_id="uniqdoc1",
    )

    captured: dict[str, Any] = {}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)

    update, ctx, reply = _build_update_and_ctx(
        talker_config, document, caption="Look at this",
    )
    await bot.on_document(update, ctx)

    assert "kwargs" in captured, "handle_message should have been invoked"
    kwargs = captured["kwargs"]
    # Extracted text reaches the user-text arg, with the PDF body inlined.
    assert "Quarterly report body" in kwargs["text"]
    assert "[PDF attached: report.pdf]" in kwargs["text"]
    assert "Look at this" in kwargs["text"]
    # Document metadata is populated with the saved-path audit row.
    metadata = kwargs["document_metadata"]
    assert len(metadata) == 1
    assert metadata[0]["filename"] == "report.pdf"
    assert metadata[0]["mime_type"] == "application/pdf"
    assert metadata[0]["bytes"] == len(pdf_bytes)
    # No user-facing reply on the happy path — handle_message owns the
    # reply via the LLM turn.
    reply.assert_not_awaited()


# --- 2. Non-PDF document → user-facing rejection -------------------------


@pytest.mark.asyncio
async def test_on_document_non_pdf_replies_and_skips_handle_message(
    talker_config, monkeypatch,
) -> None:
    """``.docx`` MIME → user-facing reply, no handle_message call.

    Per ``feedback_intentionally_left_blank.md``: silent filter-drop on
    a non-PDF document is the same bug class this commit closes for
    PDFs (the original 2026-06-06 incident).
    """
    from alfred.telegram import bot

    captured: dict[str, Any] = {}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        captured["called"] = True

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)

    document = _FakeDocument(
        content=b"PK\x03\x04 fake docx bytes",
        mime_type="application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document",
        file_name="proposal.docx",
    )
    update, ctx, reply = _build_update_and_ctx(talker_config, document)

    await bot.on_document(update, ctx)

    # User-facing rejection reply.
    reply.assert_awaited_once()
    reply_text = reply.call_args.args[0]
    assert "PDF" in reply_text
    # handle_message NOT called.
    assert "called" not in captured


@pytest.mark.asyncio
async def test_on_document_empty_mime_replies(
    talker_config, monkeypatch,
) -> None:
    """Empty / missing mime → rejection reply (no silent pass).

    Some Telegram clients omit ``mime_type``; the handler must reject
    rather than treating empty as "probably fine."
    """
    from alfred.telegram import bot

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        pytest.fail("handle_message should not have been called")

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)

    document = _FakeDocument(
        content=b"who knows", mime_type="", file_name="mystery.bin",
    )
    update, ctx, reply = _build_update_and_ctx(talker_config, document)
    await bot.on_document(update, ctx)
    reply.assert_awaited_once()


# --- 3. Oversized PDF → user-facing reply --------------------------------


@pytest.mark.asyncio
async def test_on_document_oversized_pdf_replies(
    talker_config, monkeypatch,
) -> None:
    """PDF over MAX_PDF_BYTES → reject before download.

    Catches pathological forwards before we burn bandwidth + memory.
    """
    from alfred.telegram import bot

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        pytest.fail("handle_message should not have been called for oversized PDF")

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)

    # Set the size field above the cap; the actual content can be tiny
    # (the size check fires on ``document.file_size``, not on a
    # downloaded payload).
    huge = _FakeDocument(
        content=b"%PDF-1.4 stub",
        mime_type="application/pdf",
        file_name="manual.pdf",
        file_size=attachments.MAX_PDF_BYTES + 1,
    )
    update, ctx, reply = _build_update_and_ctx(talker_config, huge)
    await bot.on_document(update, ctx)

    reply.assert_awaited_once()
    reply_text = reply.call_args.args[0]
    # Reply mentions both the user's file size and the limit.
    assert "MB" in reply_text


# --- 4. Unauthorized user → silent drop ----------------------------------


@pytest.mark.asyncio
async def test_on_document_unauthorized_user_silent(
    talker_config, monkeypatch,
) -> None:
    """Non-allowlisted user gets no reply, no download.

    Matches the on_photo / on_voice / on_text unauthorized behaviour.
    The ``record_handled`` counter MUST NOT bump (the message lands
    in the unhandled bucket of the heartbeat split).
    """
    from alfred.telegram import bot, heartbeat

    heartbeat.reset()

    document = _FakeDocument(
        content=b"%PDF-1.4 anything", mime_type="application/pdf",
        file_name="report.pdf",
    )
    # User 99999 is not in talker_config.allowed_users=[1]
    update, ctx, reply = _build_update_and_ctx(
        talker_config, document, user_id=99999,
    )

    await bot.on_document(update, ctx)
    reply.assert_not_awaited()
    # Allowlist-rejected: handled counter MUST NOT have bumped.
    assert heartbeat.get_handled_count() == 0


# --- 5. Download failure → user-facing reply -----------------------------


@pytest.mark.asyncio
async def test_on_document_download_failure_replies(
    talker_config, monkeypatch,
) -> None:
    """Network / PTB download failure → user-facing reply, no dispatch."""
    from alfred.telegram import bot

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        pytest.fail("handle_message should not have been called")

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)

    document = _FakeDocument(
        content=b"%PDF-1.4",
        mime_type="application/pdf",
        file_name="report.pdf",
        get_file_raises=RuntimeError("synthetic network failure"),
    )
    update, ctx, reply = _build_update_and_ctx(talker_config, document)

    await bot.on_document(update, ctx)
    reply.assert_awaited_once()
    reply_text = reply.call_args.args[0]
    assert "fetch" in reply_text.lower() or "PDF" in reply_text


# --- 6. Extract failure → user-facing reply ------------------------------


@pytest.mark.asyncio
async def test_on_document_extract_failure_replies(
    talker_config, monkeypatch,
) -> None:
    """PDF that downloads but fails to parse → user-facing reply.

    Distinct from the download-failure path: the user gets a reply
    that names the decoding failure (so they can act — "share it as
    an image" / "paste the text directly").
    """
    from alfred.telegram import bot

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        pytest.fail("handle_message should not have been called")

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)

    # Valid mime but garbage bytes — passes the MIME gate, fails
    # extraction.
    document = _FakeDocument(
        content=b"this is not actually a PDF",
        mime_type="application/pdf",
        file_name="bad.pdf",
    )
    update, ctx, reply = _build_update_and_ctx(talker_config, document)

    await bot.on_document(update, ctx)
    reply.assert_awaited_once()
    reply_text = reply.call_args.args[0]
    # The reply names "read" or "PDF" and offers a fallback.
    assert "read" in reply_text.lower() or "PDF" in reply_text


# --- 7. Save-to-inbox failure → handle_message still called --------------


@pytest.mark.asyncio
async def test_on_document_save_failure_continues_to_llm(
    talker_config, monkeypatch,
) -> None:
    """Save-to-inbox failure is non-fatal: text still reaches the LLM.

    Mirrors the on_photo save-failure contract. Without this regression
    test, a future refactor could silently flip the "save failure
    aborts conversation" decision.

    Asserts:
        (a) ``handle_message`` is called despite the save failure.
        (b) The extracted text still reaches ``text=`` kwarg.
        (c) ``document_metadata`` is empty — no audit-trail row is
            written for a document we couldn't save (avoids dangling
            -path entries on the session record).
    """
    from alfred.telegram import bot

    def _boom(*_args: Any, **_kwargs: Any) -> Path:
        raise OSError("synthetic disk full")

    monkeypatch.setattr(attachments, "save_document_to_inbox", _boom)

    captured: dict[str, Any] = {}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)

    document = _FakeDocument(
        content=_make_valid_pdf_bytes(),
        mime_type="application/pdf",
        file_name="report.pdf",
    )
    update, ctx, reply = _build_update_and_ctx(talker_config, document)
    await bot.on_document(update, ctx)

    # Conversation proceeded despite save failure.
    assert "kwargs" in captured
    kwargs = captured["kwargs"]
    assert "Quarterly report body" in kwargs["text"]
    # No audit-trail row for an un-saved document (avoids dangling-path
    # entries on the session record).
    assert kwargs["document_metadata"] == []
    # No user-facing reply — handle_message owns it.
    reply.assert_not_awaited()
