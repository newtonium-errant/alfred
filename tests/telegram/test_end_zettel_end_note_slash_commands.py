"""``/end-zettel`` + ``/end-note`` slash command tests.

Phase 1.x (2026-05-16). Operator override path for the three-tier
discriminator. Spec:

  * ``/end-zettel`` closes the capture session AND forces zettel/
    extraction target (overrides source-anchored default — operator
    can elevate an unanchored capture).
  * ``/end-note`` closes the capture session AND forces note/
    extraction target (overrides source-anchored default — operator
    can demote an anchored capture).
  * ``/end`` (plain) uses the source-anchored default discriminator.

The override is stamped on the active session dict
(``_extract_target_override``), survives the
:func:`alfred.telegram.session._snapshot_for_post_close` snapshot,
gets forwarded through
:func:`alfred.telegram.capture_batch.process_capture_session` to the
session record's ``capture_extract_target_override:`` frontmatter
field, and is honoured by the deferred ``/extract`` call.

Tests in this file cover:
  * ``_snapshot_for_post_close`` captures the override field.
  * ``_stamp_extract_target_override_on_active`` writes the override
    to state and handles no-active-session gracefully.
  * ``process_capture_session`` writes the override to session
    frontmatter when passed via kwarg.
  * End-to-end through ``extract_notes_from_capture``: the persisted
    override drives the discriminator at extract-time.

PTB CommandHandler regex contract: only ``[a-z0-9_]`` allowed in
command names, so the actual registrations are ``end_zettel`` /
``end_note`` (mirrors the ``/method_source`` decision).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import frontmatter
import pytest

from alfred.telegram import bot, capture_batch, capture_extract, session
from tests.telegram.conftest import (
    FakeAnthropicClient, FakeBlock, FakeResponse,
)


# --- _snapshot_for_post_close — capture override field ------------------


def test_snapshot_captures_extract_target_override() -> None:
    """The snapshot helper picks up ``_extract_target_override`` from
    the active dict so it survives the close pop."""
    active = {
        "session_id": "abc-uuid",
        "transcript": [{"role": "user", "content": "hi"}],
        "_vault_path_root": "/tmp/vault",
        "_extract_target_override": "zettel",
    }
    snap = session._snapshot_for_post_close(active)
    assert snap["extract_target_override"] == "zettel"


def test_snapshot_extract_override_defaults_to_empty_string() -> None:
    """When the active dict has no override (plain /end path), the
    snapshot field is the empty string — not None."""
    active = {
        "session_id": "abc-uuid",
        "transcript": [],
        "_vault_path_root": "/tmp/vault",
    }
    snap = session._snapshot_for_post_close(active)
    assert snap["extract_target_override"] == ""


def test_snapshot_extract_override_coerces_non_string() -> None:
    """Defensive: a stray non-string value coerces to its str form
    (or empty if None / 0)."""
    active = {
        "session_id": "abc-uuid",
        "transcript": [],
        "_vault_path_root": "/tmp/vault",
        "_extract_target_override": None,  # treated as ""
    }
    snap = session._snapshot_for_post_close(active)
    assert snap["extract_target_override"] == ""


# --- _stamp_extract_target_override_on_active ----------------------------


def _make_update_mock(chat_id: int = 42, user_id: int = 42) -> MagicMock:
    """Build a Telegram-Update-shape mock with the minimum surface the
    stamp helper consults: ``message.reply_text`` (awaitable),
    ``effective_user.id`` (for _is_allowed), ``effective_chat.id``."""
    update = MagicMock()
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    update.effective_chat = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    return update


def _make_ctx_mock(state_mgr, *, allowed_user_id: int = 42) -> MagicMock:
    """Build a Telegram-context-shape mock with the bot_data the stamp
    helper consults: config (allowed_users + primary_users), state_mgr."""
    config = MagicMock()
    config.allowed_users = [allowed_user_id]
    config.primary_users = ["person/Andrew Newton"]
    ctx = MagicMock()
    ctx.application.bot_data = {
        bot._KEY_CONFIG: config,
        bot._KEY_STATE: state_mgr,
    }
    return ctx


@pytest.mark.asyncio
async def test_stamp_override_writes_to_active(state_mgr) -> None:
    """The helper writes the override to the active dict + persists."""
    # Seed an active session.
    state_mgr.state.setdefault("active_sessions", {})["42"] = {
        "session_id": "abc-uuid",
        "chat_id": 42,
        "started_at": "2026-05-16T10:00:00+00:00",
        "transcript": [{"role": "user", "content": "hi"}],
    }
    state_mgr.save()

    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr)

    applied = await bot._stamp_extract_target_override_on_active(
        update, ctx, override="zettel",
    )
    assert applied is True

    active = state_mgr.get_active(42)
    assert active is not None
    assert active["_extract_target_override"] == "zettel"


@pytest.mark.asyncio
async def test_stamp_override_no_active_session_replies(state_mgr) -> None:
    """No active session → False return + 'no active session' reply."""
    update = _make_update_mock()
    ctx = _make_ctx_mock(state_mgr)

    applied = await bot._stamp_extract_target_override_on_active(
        update, ctx, override="zettel",
    )
    assert applied is False
    update.message.reply_text.assert_awaited_once_with("no active session.")


# --- process_capture_session writes override to frontmatter --------------


def _make_hypatia_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("session", "zettel", "note", "source", "author", "memo"):
        (vault / sub).mkdir(parents=True)
    return vault


def _write_capture_session(vault: Path, name: str) -> str:
    (vault / "session").mkdir(exist_ok=True, parents=True)
    rel = f"session/{name}.md"
    body = "\n# Transcript\n\n**Andrew** (10:00): some thoughts\n"
    (vault / rel).write_text(
        "---\n"
        "type: session\n"
        f"name: {name}\n"
        "created: '2026-05-16'\n"
        "session_type: capture\n"
        "---\n" + body,
        encoding="utf-8",
    )
    return rel


@pytest.mark.asyncio
async def test_process_capture_session_writes_override_to_frontmatter(
    tmp_path: Path,
) -> None:
    """When ``extract_target_override`` is passed as kwarg,
    process_capture_session writes it to the session record's
    ``capture_extract_target_override:`` frontmatter field via
    extra_fields."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_capture_session(
        vault, "capture-2026-05-16-override-zettel-aa112233",
    )

    # 2 user messages → batch path (memo branch needs ≤1).
    transcript = [
        {"role": "user", "content": "first thought",
         "_ts": "2026-05-16T10:00:00+00:00"},
        {"role": "user", "content": "second related thought",
         "_ts": "2026-05-16T10:01:00+00:00"},
    ]

    client = FakeAnthropicClient([
        FakeResponse(
            content=[FakeBlock(
                type="tool_use", id="t1", name="emit_structured_summary",
                input={
                    "topics": [], "decisions": [], "open_questions": [],
                    "action_items": [], "key_insights": [],
                    "raw_contradictions": [],
                },
            )],
            stop_reason="tool_use",
        )
    ])

    await capture_batch.process_capture_session(
        client=client,
        vault_path=vault,
        session_rel_path=rel,
        transcript=transcript,
        model="claude-sonnet-4-6",
        send_follow_up=None,
        short_id="aa112233",
        agent_slug="hypatia",
        anchor_scope="hypatia",
        extract_target_override="zettel",
    )

    sess = frontmatter.load(vault / rel)
    assert sess["capture_extract_target_override"] == "zettel"


@pytest.mark.asyncio
async def test_process_capture_session_no_override_omits_field(
    tmp_path: Path,
) -> None:
    """When override is empty (plain /end), the field is NOT written
    to the session record (operator's intent is "use default")."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_capture_session(
        vault, "capture-2026-05-16-no-override-bb223344",
    )

    transcript = [
        {"role": "user", "content": "first",
         "_ts": "2026-05-16T10:00:00+00:00"},
        {"role": "user", "content": "second",
         "_ts": "2026-05-16T10:01:00+00:00"},
    ]

    client = FakeAnthropicClient([
        FakeResponse(
            content=[FakeBlock(
                type="tool_use", id="t1", name="emit_structured_summary",
                input={
                    "topics": [], "decisions": [], "open_questions": [],
                    "action_items": [], "key_insights": [],
                    "raw_contradictions": [],
                },
            )],
            stop_reason="tool_use",
        )
    ])

    await capture_batch.process_capture_session(
        client=client,
        vault_path=vault,
        session_rel_path=rel,
        transcript=transcript,
        model="claude-sonnet-4-6",
        send_follow_up=None,
        short_id="bb223344",
        agent_slug="hypatia",
        anchor_scope="hypatia",
        # extract_target_override omitted — defaults to ""
    )

    sess = frontmatter.load(vault / rel)
    assert "capture_extract_target_override" not in sess.metadata


@pytest.mark.asyncio
async def test_process_capture_session_garbage_override_omits_field(
    tmp_path: Path,
) -> None:
    """Defensive: a non-canonical override value (e.g. "garbage")
    silently drops — only ``zettel``/``note`` reach the frontmatter."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_capture_session(
        vault, "capture-2026-05-16-garbage-cc334455",
    )

    transcript = [
        {"role": "user", "content": "a",
         "_ts": "2026-05-16T10:00:00+00:00"},
        {"role": "user", "content": "b",
         "_ts": "2026-05-16T10:01:00+00:00"},
    ]

    client = FakeAnthropicClient([
        FakeResponse(
            content=[FakeBlock(
                type="tool_use", id="t1", name="emit_structured_summary",
                input={
                    "topics": [], "decisions": [], "open_questions": [],
                    "action_items": [], "key_insights": [],
                    "raw_contradictions": [],
                },
            )],
            stop_reason="tool_use",
        )
    ])

    await capture_batch.process_capture_session(
        client=client,
        vault_path=vault,
        session_rel_path=rel,
        transcript=transcript,
        model="claude-sonnet-4-6",
        send_follow_up=None,
        short_id="cc334455",
        agent_slug="hypatia",
        anchor_scope="hypatia",
        extract_target_override="garbage",  # invalid
    )

    sess = frontmatter.load(vault / rel)
    assert "capture_extract_target_override" not in sess.metadata


# --- End-to-end: stamped override drives discriminator at /extract time --


def _make_closed_session(state_mgr, short_id: str, rel_path: str) -> None:
    state_mgr.state.setdefault("closed_sessions", []).append({
        "session_id": f"{short_id}-uuid",
        "chat_id": 1,
        "started_at": "2026-05-16T10:00:00+00:00",
        "ended_at":   "2026-05-16T10:30:00+00:00",
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


def _tool_use_note_block(name: str, body: str) -> FakeBlock:
    return FakeBlock(
        type="tool_use",
        id=f"toolu_{name[:8]}",
        name="create_note",
        input={
            "name": name,
            "body": body,
            "confidence_tier": "high",
            "source_quote": "test quote",
        },
    )


@pytest.mark.asyncio
async def test_end_to_end_end_zettel_override_drives_extract(
    state_mgr, tmp_path,
) -> None:
    """Full path:
      1. ``process_capture_session`` writes override=zettel to frontmatter
      2. ``extract_notes_from_capture`` reads the field, drives
         discriminator → zettel/

    Even though the session is UNanchored (which would normally route
    to note/), the override forces zettel/.
    """
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_capture_session(
        vault, "capture-end-zettel-e2e-dd445566",
    )

    transcript = [
        {"role": "user", "content": "first",
         "_ts": "2026-05-16T10:00:00+00:00"},
        {"role": "user", "content": "second",
         "_ts": "2026-05-16T10:01:00+00:00"},
    ]

    # Step 1: process_capture_session with override.
    batch_client = FakeAnthropicClient([
        FakeResponse(
            content=[FakeBlock(
                type="tool_use", id="t1", name="emit_structured_summary",
                input={
                    "topics": ["x"], "decisions": [], "open_questions": [],
                    "action_items": [], "key_insights": [],
                    "raw_contradictions": [],
                },
            )],
            stop_reason="tool_use",
        )
    ])

    await capture_batch.process_capture_session(
        client=batch_client,
        vault_path=vault,
        session_rel_path=rel,
        transcript=transcript,
        model="claude-sonnet-4-6",
        send_follow_up=None,
        short_id="dd445566",
        agent_slug="hypatia",
        anchor_scope="hypatia",
        extract_target_override="zettel",
    )

    # Verify the field landed.
    sess = frontmatter.load(vault / rel)
    assert sess["capture_extract_target_override"] == "zettel"

    # Step 2: /extract reads the field via the discriminator's
    # frontmatter-fallback path (NO explicit kwarg here).
    _make_closed_session(state_mgr, "dd445566", rel)
    extract_client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Forced Zettel", "Body.")],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=extract_client,
        state=state_mgr,
        vault_path=vault,
        short_id="dd445566",
        model="claude-sonnet-4-6",
        agent_slug="hypatia",
        anchor_scope="hypatia",
        # NO operator_override kwarg — must be read from frontmatter.
    )

    assert result.skipped_reason == ""
    assert len(result.created_paths) == 1
    # Override on the unanchored session drove the discriminator to
    # zettel/ despite no source/author wikilinks.
    assert result.created_paths[0].startswith("zettel/"), (
        f"override path failed — got {result.created_paths[0]}"
    )


@pytest.mark.asyncio
async def test_end_to_end_end_note_override_drives_extract(
    state_mgr, tmp_path,
) -> None:
    """Mirror: override=note drives extract to note/ even if the
    session is source-anchored."""
    vault = _make_hypatia_vault(tmp_path)
    # Build an anchored session via direct write (skipping
    # process_capture_session for cleaner test isolation).
    rel = "session/capture-end-note-e2e-ee556677.md"
    (vault / "session").mkdir(exist_ok=True, parents=True)
    (vault / rel).write_text(
        "---\n"
        "type: session\n"
        "name: capture-end-note-e2e-ee556677\n"
        "created: '2026-05-16'\n"
        "session_type: capture\n"
        'source: "[[source/Test]]"\n'
        'author: "[[author/X, Y]]"\n'
        'capture_extract_target_override: "note"\n'
        "---\n\n"
        f"{capture_batch.SUMMARY_MARKER_START}\n## Structured Summary\n"
        f"### Topics\n- topic\n{capture_batch.SUMMARY_MARKER_END}\n\n"
        "# Transcript\n\n**Andrew** (10:00): a thought\n",
        encoding="utf-8",
    )
    _make_closed_session(state_mgr, "ee556677", rel)

    client = FakeAnthropicClient([
        FakeResponse(
            content=[_tool_use_note_block("Demoted To Note", "Body.")],
            stop_reason="tool_use",
        )
    ])

    result = await capture_extract.extract_notes_from_capture(
        client=client,
        state=state_mgr,
        vault_path=vault,
        short_id="ee556677",
        model="claude-sonnet-4-6",
        agent_slug="hypatia",
        anchor_scope="hypatia",
    )

    assert result.skipped_reason == ""
    assert result.created_paths[0].startswith("note/"), (
        f"override 'note' on anchored session failed — got "
        f"{result.created_paths[0]}"
    )


# --- Slash-command handlers exist + are registered -----------------------


def test_on_end_zettel_exists() -> None:
    """Public-surface pin: the slash-command handlers exist as coroutines."""
    assert callable(bot.on_end_zettel)
    assert callable(bot.on_end_note)


def test_end_zettel_end_note_registered_in_build_app() -> None:
    """The CommandHandler registrations for /end_zettel and /end_note
    are present in build_app.

    Indirect check via source inspection — building a real app needs
    a full TalkerConfig + token + so on, which is overkill for a
    registration pin. Reading the bot.py source for the literal
    ``CommandHandler("end_zettel"`` substring catches the
    "registration accidentally removed" failure mode.
    """
    import inspect
    src = inspect.getsource(bot)
    assert 'CommandHandler("end_zettel", on_end_zettel)' in src
    assert 'CommandHandler("end_note", on_end_note)' in src
