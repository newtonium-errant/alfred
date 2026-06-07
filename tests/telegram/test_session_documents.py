"""Tests for the :class:`Session` ``documents`` field + load-time schema tolerance.

Coverage:

    * ``Session.to_dict`` / ``from_dict`` round-trip with ``documents``
      populated
    * ``Session.from_dict`` schema-tolerance: an unknown extra field in
      the input dict is silently filtered out (per the CLAUDE.md state
      persistence contract)
    * ``Session.from_dict`` back-compat: missing ``documents`` field
      loads with an empty-list default
    * ``Session.from_dict`` back-compat: a pre-document state file
      (omits ``documents``) round-trips cleanly through to_dict/from_dict
      without loss of any other field
    * :func:`append_document` writes a row + persists
    * ``_render_content`` collapses a document content block to
      ``[document]`` (forward-compat — the production path inlines
      extracted text rather than emitting document blocks today)
    * Close-time frontmatter includes ``documents:`` when populated and
      omits when empty
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from alfred.telegram.session import (
    Session,
    _build_session_frontmatter,
    _render_content,
    append_document,
)
from alfred.telegram.state import StateManager


# --- Round-trip + schema tolerance ----------------------------------------


def _make_session(**overrides) -> Session:
    base = dict(
        session_id="sess-1",
        chat_id=1,
        started_at=datetime(2026, 6, 6, 12, 0, 0, tzinfo=timezone.utc),
        last_message_at=datetime(2026, 6, 6, 12, 5, 0, tzinfo=timezone.utc),
        model="claude-opus-4-7",
    )
    base.update(overrides)
    return Session(**base)


def test_session_to_from_dict_preserves_documents() -> None:
    """Document attachments survive a round-trip through state."""
    sess = _make_session()
    sess.documents = [{
        "path": "/vault/inbox/document-20260606T120014Z-abcd1234.pdf",
        "file_unique_id": "abcd1234",
        "bytes": 12345,
        "filename": "report.pdf",
        "mime_type": "application/pdf",
        "turn_index": 0,
        "timestamp": "2026-06-06T12:00:14+00:00",
    }]

    raw = sess.to_dict()
    assert "documents" in raw
    assert len(raw["documents"]) == 1

    rehydrated = Session.from_dict(raw)
    assert len(rehydrated.documents) == 1
    assert rehydrated.documents[0]["filename"] == "report.pdf"
    assert rehydrated.documents[0]["mime_type"] == "application/pdf"
    assert rehydrated.documents[0]["bytes"] == 12345


def test_session_from_dict_pre_document_records_default_empty() -> None:
    """A state file written before ``documents`` shipped loads cleanly.

    Back-compat invariant: an older state file that lacks the
    ``documents`` key must not crash the loader; the field defaults to
    an empty list. Equivalent to the
    ``test_session_from_dict_pre_vision_records_default_empty`` pin in
    test_vision.py.
    """
    pre_document_dict = {
        "session_id": "sess-old",
        "chat_id": 1,
        "started_at": "2026-05-01T12:00:00+00:00",
        "last_message_at": "2026-05-01T12:05:00+00:00",
        "model": "claude-opus-4-7",
        "transcript": [],
        "vault_ops": [],
        "opening_model": "claude-opus-4-7",
        "outbound_failures": [],
        "images": [],
        # NO ``documents`` key — pre-2026-06-06 state shape.
    }
    sess = Session.from_dict(pre_document_dict)
    assert sess.documents == []


def test_session_from_dict_filters_unknown_fields() -> None:
    """Schema-tolerance: unknown keys in input dict are silently filtered.

    The load-time schema-tolerance contract from CLAUDE.md "State
    persistence" — a state file written by a NEWER version with a
    field this version doesn't know about (e.g. a hypothetical future
    ``audio_attachments`` field rolled back from) must not crash the
    loader. Pin the contract.
    """
    forward_compat_dict = {
        "session_id": "sess-future",
        "chat_id": 1,
        "started_at": "2026-08-01T12:00:00+00:00",
        "last_message_at": "2026-08-01T12:05:00+00:00",
        "model": "claude-opus-4-7",
        "transcript": [],
        "vault_ops": [],
        "opening_model": "claude-opus-4-7",
        "outbound_failures": [],
        "images": [],
        "documents": [],
        # Hypothetical future field that this version doesn't know about.
        "audio_attachments": [{"path": "/vault/inbox/voice-xyz.ogg"}],
        # Another future field.
        "vision_provenance_marker": "inf-20260801-salem-xyz",
    }
    # Must not raise on the unknown fields.
    sess = Session.from_dict(forward_compat_dict)
    # Known fields populated correctly.
    assert sess.session_id == "sess-future"
    assert sess.documents == []
    # Unknown fields are not stuck onto the object as attrs (would
    # silently drop on next to_dict — which is correct).
    assert not hasattr(sess, "audio_attachments")
    assert not hasattr(sess, "vision_provenance_marker")


def test_session_from_dict_roundtrip_pre_document_state() -> None:
    """A full pre-document state file round-trips losslessly through.

    Pin the combined back-compat invariant: old state files load,
    serialise back out, and don't gain spurious fields. The new
    ``documents`` field DOES appear on the output (because to_dict
    always emits it), but its value is the default empty list.
    """
    pre_document_dict = {
        "session_id": "sess-old",
        "chat_id": 42,
        "started_at": "2026-05-01T12:00:00+00:00",
        "last_message_at": "2026-05-01T12:05:00+00:00",
        "model": "claude-opus-4-7",
        "transcript": [{"role": "user", "content": "hello"}],
        "vault_ops": [],
        "opening_model": "claude-opus-4-7",
        "outbound_failures": [],
        "images": [],
    }
    sess = Session.from_dict(pre_document_dict)
    re_serialised = sess.to_dict()
    assert re_serialised["documents"] == []
    # Round-trip back: still loads cleanly.
    sess2 = Session.from_dict(re_serialised)
    assert sess2.transcript == [{"role": "user", "content": "hello"}]


# --- append_document -----------------------------------------------------


def test_append_document_writes_row_and_persists(tmp_path: Path) -> None:
    """Each call appends a row carrying path / metadata / turn_index."""
    state_path = tmp_path / "state.json"
    state_mgr = StateManager(state_path)
    state_mgr.load()
    state_mgr.set_active(1, {
        "session_id": "sess-1",
        "chat_id": 1,
        "started_at": "2026-06-06T12:00:00+00:00",
        "last_message_at": "2026-06-06T12:05:00+00:00",
        "model": "claude-opus-4-7",
        "transcript": [{"role": "user", "content": "first"}],
        "vault_ops": [],
        "outbound_failures": [],
        "images": [],
        "documents": [],
    })
    state_mgr.save()

    sess = Session.from_dict(state_mgr.get_active(1))
    append_document(
        state_mgr, sess,
        path="/vault/inbox/document-20260606T120014Z-abcd.pdf",
        file_unique_id="abcd",
        bytes_size=2048,
        filename="report.pdf",
        mime_type="application/pdf",
    )

    # Persisted row on the in-memory session object.
    assert len(sess.documents) == 1
    row = sess.documents[0]
    assert row["path"] == "/vault/inbox/document-20260606T120014Z-abcd.pdf"
    assert row["file_unique_id"] == "abcd"
    assert row["bytes"] == 2048
    assert row["filename"] == "report.pdf"
    assert row["mime_type"] == "application/pdf"
    # P8: the default kind is "pdf" for back-compat with the c1
    # call sites — this test predates the P8 universal-filetype-bundle
    # so it doesn't pass ``kind=`` and still expects the default.
    assert row["kind"] == "pdf"
    # Turn-index is the would-be position of the next user turn (one
    # turn currently in the transcript, so the next is index 1).
    assert row["turn_index"] == 1
    assert "timestamp" in row

    # Persisted to state file (read it back fresh).
    fresh = StateManager(state_path)
    fresh.load()
    active = fresh.get_active(1)
    assert len(active["documents"]) == 1
    assert active["documents"][0]["filename"] == "report.pdf"
    assert active["documents"][0]["kind"] == "pdf"


# --- _render_content forward-compat document branch ----------------------


def test_render_content_collapses_document_block_to_marker() -> None:
    """Document-type content blocks collapse to ``[document]`` in transcripts.

    Forward-compat branch — the production on_document handler inlines
    extracted text into the user message, NOT into a content block. But
    if a future model family round-trips a document block, the
    transcript should render readably rather than falling into the
    catchall ``[document_subtype]`` shape.
    """
    content = [{"type": "document", "source": {"type": "base64", "data": "..."}}]
    rendered = _render_content(content)
    assert rendered == "[document]"


def test_render_content_handles_text_plus_document_mix() -> None:
    """Text + document blocks render with the document marker inline."""
    content = [
        {"type": "text", "text": "See attached:"},
        {"type": "document", "source": {"type": "base64", "data": "..."}},
    ]
    rendered = _render_content(content)
    assert "See attached:" in rendered
    assert "[document]" in rendered


# --- Frontmatter close-time integration ----------------------------------


def test_frontmatter_includes_documents_when_populated() -> None:
    """``documents:`` lands on the session frontmatter when non-empty."""
    sess = _make_session()
    sess.documents = [{
        "path": "/vault/inbox/document-20260606T120014Z-abcd.pdf",
        "file_unique_id": "abcd",
        "bytes": 2048,
        "filename": "report.pdf",
        "mime_type": "application/pdf",
        "turn_index": 0,
        "timestamp": "2026-06-06T12:00:14+00:00",
    }]
    fm = _build_session_frontmatter(
        sess,
        ended_at=datetime(2026, 6, 6, 12, 10, 0, tzinfo=timezone.utc),
        reason="user_end",
        tool_set="",
    )
    assert "documents" in fm
    assert len(fm["documents"]) == 1
    assert fm["documents"][0]["filename"] == "report.pdf"


def test_frontmatter_omits_documents_when_empty() -> None:
    """Empty ``documents`` is omitted from the frontmatter.

    Mirrors the ``images``/``outbound_failures`` shape — empty lists
    don't pollute the record. Pre-document record consumers see no
    shape drift.
    """
    sess = _make_session()
    # documents defaults to empty list.
    fm = _build_session_frontmatter(
        sess,
        ended_at=datetime(2026, 6, 6, 12, 10, 0, tzinfo=timezone.utc),
        reason="user_end",
        tool_set="",
    )
    assert "documents" not in fm


# === P8 — universal filetype bundle ======================================
#
# Tests below pin the ``kind`` field semantics on document rows: the
# field is recorded by :func:`append_document`, round-trips through
# state, backfills for pre-P8 rows on read, and survives the frontmatter
# emit shape.


def test_append_document_with_audio_kind(tmp_path: Path) -> None:
    """``kind="audio"`` is recorded on the row when supplied."""
    state_path = tmp_path / "state.json"
    state_mgr = StateManager(state_path)
    state_mgr.load()
    state_mgr.set_active(1, {
        "session_id": "sess-audio",
        "chat_id": 1,
        "started_at": "2026-06-06T12:00:00+00:00",
        "last_message_at": "2026-06-06T12:05:00+00:00",
        "model": "claude-opus-4-7",
        "transcript": [],
        "vault_ops": [],
        "outbound_failures": [],
        "images": [],
        "documents": [],
    })
    state_mgr.save()

    sess = Session.from_dict(state_mgr.get_active(1))
    from alfred.telegram.session import append_document as _append
    _append(
        state_mgr, sess,
        path="/vault/inbox/audio-20260606T120014Z-aud1.mp3",
        file_unique_id="aud1",
        bytes_size=4096,
        filename="recording.mp3",
        mime_type="audio/mpeg",
        kind="audio",
    )

    assert sess.documents[0]["kind"] == "audio"
    assert sess.documents[0]["mime_type"] == "audio/mpeg"

    # Round-trip through state.
    fresh = StateManager(state_path)
    fresh.load()
    active = fresh.get_active(1)
    assert active["documents"][0]["kind"] == "audio"


def test_session_from_dict_backfills_kind_on_pre_p8_documents() -> None:
    """Pre-P8 document rows (no ``kind`` field) backfill to ``"pdf"`` on read.

    This is the load-time backfill — pre-P8 (c1) sessions only handled
    PDFs, so every row's ``mime_type`` is ``application/pdf``, making
    the kind unambiguous. Doing the backfill on read means consumers
    downstream can rely on the kind field being present without
    per-row defensive ``.get("kind", "pdf")`` calls.
    """
    pre_p8_dict = {
        "session_id": "sess-pre-p8",
        "chat_id": 1,
        "started_at": "2026-06-06T12:00:00+00:00",
        "last_message_at": "2026-06-06T12:05:00+00:00",
        "model": "claude-opus-4-7",
        "transcript": [],
        "vault_ops": [],
        "outbound_failures": [],
        "images": [],
        # Pre-P8 document rows: no ``kind`` field.
        "documents": [
            {
                "path": "/vault/inbox/document-x.pdf",
                "file_unique_id": "x",
                "bytes": 1024,
                "filename": "report.pdf",
                "mime_type": "application/pdf",
                "turn_index": 0,
                "timestamp": "2026-05-15T10:00:00+00:00",
                # NO "kind" key — pre-P8 row.
            },
        ],
    }
    sess = Session.from_dict(pre_p8_dict)
    assert len(sess.documents) == 1
    # Backfilled to "pdf" on load.
    assert sess.documents[0]["kind"] == "pdf"


def test_session_from_dict_does_not_overwrite_kind_when_present() -> None:
    """Backfill only fires when ``kind`` is absent — existing values stay."""
    p8_dict = {
        "session_id": "sess-p8",
        "chat_id": 1,
        "started_at": "2026-06-06T12:00:00+00:00",
        "last_message_at": "2026-06-06T12:05:00+00:00",
        "model": "claude-opus-4-7",
        "transcript": [],
        "vault_ops": [],
        "outbound_failures": [],
        "images": [],
        "documents": [
            {
                "path": "/vault/inbox/audio-y.m4a",
                "file_unique_id": "y",
                "bytes": 2048,
                "filename": "voice.m4a",
                "mime_type": "audio/mp4",
                "kind": "audio",  # explicit; must not be overwritten.
                "turn_index": 0,
                "timestamp": "2026-06-06T12:00:00+00:00",
            },
        ],
    }
    sess = Session.from_dict(p8_dict)
    assert sess.documents[0]["kind"] == "audio"


def test_session_round_trip_with_mixed_kinds() -> None:
    """Multi-kind document list round-trips cleanly (kind preserved per row)."""
    sess = _make_session()
    sess.documents = [
        {
            "path": "/vault/inbox/document-a.pdf",
            "file_unique_id": "a",
            "bytes": 1024,
            "filename": "report.pdf",
            "mime_type": "application/pdf",
            "kind": "pdf",
            "turn_index": 0,
            "timestamp": "2026-06-06T12:00:00+00:00",
        },
        {
            "path": "/vault/inbox/audio-b.mp3",
            "file_unique_id": "b",
            "bytes": 4096,
            "filename": "voice.mp3",
            "mime_type": "audio/mpeg",
            "kind": "audio",
            "turn_index": 1,
            "timestamp": "2026-06-06T12:01:00+00:00",
        },
        {
            "path": "/vault/inbox/document-c.docx",
            "file_unique_id": "c",
            "bytes": 2048,
            "filename": "spec.docx",
            "mime_type": "application/vnd.openxmlformats-officedocument."
            "wordprocessingml.document",
            "kind": "docx",
            "turn_index": 2,
            "timestamp": "2026-06-06T12:02:00+00:00",
        },
    ]
    rehydrated = Session.from_dict(sess.to_dict())
    assert len(rehydrated.documents) == 3
    assert [r["kind"] for r in rehydrated.documents] == ["pdf", "audio", "docx"]
