"""Tests for wk2b commit 4 — /extract <short-id> command.

Covers:
    * ``extract_notes_from_capture`` end-to-end: resolve short-id,
      call extract LLM, create note records, update session
      ``derived_notes`` frontmatter.
    * Idempotency: re-run after success returns existing list with
      ``skipped_reason="already_extracted"``.
    * Missing session → ``skipped_reason="no_session"``.
    * Max-notes cap (default 8) trims LLM output defensively.
    * Inline-command dispatch: ``Note: /extract abc123`` fires the
      handler, the short-id gets parsed from message text.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import bot, capture_batch, capture_extract
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- Helpers --------------------------------------------------------------


def _make_closed_session(state_mgr, short_id: str, rel_path: str) -> None:
    """Seed a closed session entry with ``session_id`` starting with short_id."""
    state_mgr.state.setdefault("closed_sessions", []).append({
        "session_id": f"{short_id}-full-session-uuid",
        "chat_id": 1,
        "started_at": "2026-04-20T10:00:00+00:00",
        "ended_at":   "2026-04-20T10:30:00+00:00",
        "reason": "explicit",
        "record_path": rel_path,
        "message_count": 5,
        "vault_ops": 0,
        "session_type": "capture",
        "continues_from": None,
        "opening_model": "claude-sonnet-4-6",
        "closing_model": "claude-sonnet-4-6",
    })
    state_mgr.save()


def _write_session_record(
    vault_path: Path,
    name: str,
    with_summary: bool = True,
) -> str:
    (vault_path / "session").mkdir(exist_ok=True, parents=True)
    rel = f"session/{name}.md"
    body = (
        "**Andrew** (10:00 · voice): driver retention is the Q2 constraint\n"
        "**Andrew** (10:01 · voice): i should draft a retention SOP\n"
    )
    if with_summary:
        summary = (
            f"{capture_batch.SUMMARY_MARKER_START}\n\n"
            "## Structured Summary\n\n"
            "### Topics\n- driver retention\n\n"
            f"{capture_batch.SUMMARY_MARKER_END}\n\n"
        )
        body = summary + "# Transcript\n\n" + body
    else:
        body = "# Transcript\n\n" + body

    (vault_path / rel).write_text(
        "---\n"
        "type: session\n"
        "status: completed\n"
        f"name: {name}\n"
        "created: '2026-04-20'\n"
        "session_type: capture\n"
        "---\n\n" + body,
        encoding="utf-8",
    )
    return rel


def _tool_use_note_block(
    name: str,
    body: str,
    tier: str = "high",
    quote: str = "driver retention is the Q2 constraint",
) -> FakeBlock:
    return FakeBlock(
        type="tool_use",
        id=f"toolu_{name[:8]}",
        name="create_note",
        input={
            "name": name,
            "body": body,
            "confidence_tier": tier,
            "source_quote": quote,
        },
    )


# --- extract_notes_from_capture end-to-end -------------------------------


@pytest.mark.asyncio
async def test_extract_creates_notes_and_updates_session(
    state_mgr, talker_config, tmp_path,
) -> None:
    rel = _write_session_record(
        Path(talker_config.vault.path),
        "Voice Session — 2026-04-20 1000 abc12345",
    )
    _make_closed_session(state_mgr, "abc12345", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                _tool_use_note_block("Driver Retention Is Q2 Constraint",
                                     "Andrew flagged retention as the"
                                     " limiting factor on growth."),
                _tool_use_note_block("Draft Retention SOP",
                                     "Next step: SOP covering onboarding"
                                     " and compensation.",
                                     tier="medium",
                                     quote="i should draft a retention SOP"),
            ],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=Path(talker_config.vault.path),
        short_id="abc12345",
        model="claude-sonnet-4-6",
    )

    assert result.skipped_reason == ""
    assert len(result.created_paths) == 2
    for p in result.created_paths:
        assert p.startswith("note/")
        assert (Path(talker_config.vault.path) / p).exists()

    # Session record now has derived_notes frontmatter list.
    import frontmatter
    post = frontmatter.load(Path(talker_config.vault.path) / rel)
    derived = post.get("derived_notes") or []
    assert len(derived) == 2
    assert all("[[note/" in d for d in derived)

    # Note bodies carry the required frontmatter + blockquote + attribution.
    first_note = frontmatter.load(
        Path(talker_config.vault.path) / result.created_paths[0]
    )
    assert first_note["created_by_capture"] is True
    assert first_note["confidence_tier"] == "high"
    assert first_note["source_session"].startswith("[[session/")
    assert "> driver retention" in first_note.content
    assert "_Source:" in first_note.content


@pytest.mark.asyncio
async def test_extract_is_idempotent_on_second_run(
    state_mgr, talker_config,
) -> None:
    """Second /extract on the same session returns existing list."""
    rel = _write_session_record(
        Path(talker_config.vault.path),
        "Voice Session — 2026-04-20 1001 def67890",
    )
    _make_closed_session(state_mgr, "def67890", rel)

    # First run.
    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Idem Test Note", "body text")],
            stop_reason="tool_use",
        )
    ])
    first = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=Path(talker_config.vault.path),
        short_id="def67890",
        model="claude-sonnet-4-6",
    )
    assert first.skipped_reason == ""
    assert len(first.created_paths) == 1

    # Second run — no LLM should be called (would consume empty queue).
    client2 = FakeAnthropicClient([])
    second = await capture_extract.extract_notes_from_capture(
        client=client2,
        state=state_mgr,
        vault_path=Path(talker_config.vault.path),
        short_id="def67890",
        model="claude-sonnet-4-6",
    )
    assert second.skipped_reason == "already_extracted"
    assert len(second.created_paths) == 1
    assert client2.messages.calls == []  # no LLM call on the idempotent path


@pytest.mark.asyncio
async def test_extract_missing_session_returns_no_session(
    state_mgr, talker_config,
) -> None:
    client = FakeAnthropicClient([])
    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=Path(talker_config.vault.path),
        short_id="nonexistent",
        model="claude-sonnet-4-6",
    )
    assert result.skipped_reason == "no_session"
    assert result.created_paths == []
    # No LLM was called — we bailed on the short-id lookup.
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_extract_caps_notes_at_max(
    state_mgr, talker_config,
) -> None:
    """If the LLM emits 12 notes with max_notes=3, only 3 are created."""
    rel = _write_session_record(
        Path(talker_config.vault.path),
        "Voice Session — 2026-04-20 1002 cap12345",
    )
    _make_closed_session(state_mgr, "cap12345", rel)

    blocks = [
        _tool_use_note_block(f"Note {i}", f"body {i}") for i in range(12)
    ]
    client = FakeAnthropicClient([
        FakeResponse(content=blocks, stop_reason="tool_use")
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=Path(talker_config.vault.path),
        short_id="cap12345",
        model="claude-sonnet-4-6",
        max_notes=3,
    )
    assert len(result.created_paths) == 3


@pytest.mark.asyncio
async def test_extract_implicit_structuring_when_summary_missing(
    state_mgr, talker_config,
) -> None:
    """If the session has no ALFRED:DYNAMIC summary, batch pass runs first."""
    rel = _write_session_record(
        Path(talker_config.vault.path),
        "Voice Session — 2026-04-20 1003 imp12345",
        with_summary=False,  # no summary yet
    )
    _make_closed_session(state_mgr, "imp12345", rel)

    # Response 1: batch structuring tool_use.
    batch_response = FakeResponse(
        content=[
            FakeBlock(
                type="tool_use",
                id="toolu_batch",
                name="emit_structured_summary",
                input={
                    "topics": ["implicit"],
                    "decisions": [],
                    "open_questions": [],
                    "action_items": [],
                    "key_insights": [],
                    "raw_contradictions": [],
                },
            )
        ],
        stop_reason="tool_use",
    )
    # Response 2: note extraction.
    extract_response = FakeResponse(
        content=[_tool_use_note_block("Implicit Note", "body")],
        stop_reason="tool_use",
    )
    client = FakeAnthropicClient([batch_response, extract_response])

    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=Path(talker_config.vault.path),
        short_id="imp12345",
        model="claude-sonnet-4-6",
    )
    assert len(result.created_paths) == 1

    # Two LLM calls — batch pass + extract.
    assert len(client.messages.calls) == 2

    # Session now has a structured summary.
    raw = (Path(talker_config.vault.path) / rel).read_text(encoding="utf-8")
    assert capture_batch.SUMMARY_MARKER_START in raw


# --- Inline-command dispatch ---------------------------------------------


def test_inline_detect_extract_with_arg() -> None:
    """``Note: /extract abc123`` fires inline detection with extract command."""
    assert bot._detect_inline_command("Note: /extract abc123") == "extract"


def test_inline_detect_extract_at_end_of_line_with_arg() -> None:
    """End-of-line form ``Good. /extract abc123`` detects."""
    assert bot._detect_inline_command("Good. /extract abc12345") == "extract"


def test_parse_short_id_from_command_args() -> None:
    """CommandHandler path: ctx.args list takes priority."""
    assert bot._parse_short_id_arg("/extract abc", ["abc"]) == "abc"


def test_parse_short_id_from_inline_text() -> None:
    """Inline path: None args → regex-parse from the message text."""
    assert bot._parse_short_id_arg("Note: /extract abc123", None) == "abc123"
    # Start-of-message form also resolves through the with-arg fallback.
    assert bot._parse_short_id_arg("/extract abc123 now", None) == "abc123"


def test_parse_short_id_empty_when_no_arg() -> None:
    """Bare /extract with no arg → empty string."""
    assert bot._parse_short_id_arg("/extract", None) == ""
    assert bot._parse_short_id_arg("/extract", []) == ""


@pytest.mark.asyncio
async def test_inline_extract_dispatches_handler(
    state_mgr, talker_config, tmp_path,
) -> None:
    """``Note: /extract abc12345`` routes to on_extract via inline dispatch."""
    rel = _write_session_record(
        Path(talker_config.vault.path),
        "Voice Session — 2026-04-20 1004 inl12345",
    )
    _make_closed_session(state_mgr, "inl12345", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Inline Test Note", "body")],
            stop_reason="tool_use",
        )
    ])

    update = MagicMock()
    update.effective_user.id = 1
    update.effective_chat.id = 1
    update.message.text = "Note: /extract inl12345"
    update.message.voice = None
    update.message.message_id = 99
    update.message.reply_text = AsyncMock()

    ctx = MagicMock()
    ctx.application.bot_data = {
        "config": talker_config,
        "state_mgr": state_mgr,
        "anthropic_client": client,
        "system_prompt": "sys",
        "vault_context_str": "",
        "chat_locks": {},
    }
    ctx.args = None  # inline path has no args
    ctx.bot.send_chat_action = AsyncMock()
    ctx.bot.set_message_reaction = AsyncMock()

    await bot.handle_message(
        update, ctx, text="Note: /extract inl12345", voice=False,
    )

    # Handler replied with extraction outcome.
    update.message.reply_text.assert_called_once()
    reply = update.message.reply_text.call_args.args[0]
    assert "Extracted 1 notes" in reply
    assert "note/" in reply
