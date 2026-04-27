"""Tests for per-instance session-save naming and frontmatter shape.

The talker writes a ``session/`` record on close. The filename pattern
and frontmatter contract differ by instance ``tool_set``:

- Salem (``"talker"``) and KAL-LE (``"kalle"``) keep the wk1
  ``Voice Session — <date> <time> <short-id>`` pattern.
- Hypatia (``"hypatia"``) uses ``<mode>-<YYYY-MM-DD>-<slug>-<short-id>``
  per ``vault-hypatia/SKILL.md`` and ``~/library-alexandria/CLAUDE.md``,
  and the frontmatter adds ``mode``, ``processed``, ``extracted_to``.

These tests exercise both layers — the pure ``_build_record_name`` /
``_build_session_frontmatter`` helpers AND the full ``close_session``
roundtrip through ``vault_ops.vault_create``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import frontmatter

from alfred.telegram import session as talker_session
from alfred.telegram.session import Session


def _make_session(**overrides) -> Session:
    """Build a Session fixture with deterministic id/timestamp."""
    now = datetime(2026, 4, 26, 21, 36, tzinfo=timezone.utc)
    defaults: dict = {
        "session_id": "17cfdecd-0000-0000-0000-000000000000",
        "chat_id": 1,
        "started_at": now,
        "last_message_at": now,
        "model": "claude-sonnet-4-6",
        "transcript": [
            {"role": "user", "content": "thinking out loud about the Q2 plan"},
            {"role": "assistant", "content": "ok, let's go"},
        ],
        "vault_ops": [],
    }
    defaults.update(overrides)
    return Session(**defaults)  # type: ignore[arg-type]


# --- Filename pattern ---------------------------------------------------


def test_salem_filename_keeps_wk1_voice_session_pattern() -> None:
    """``tool_set="talker"`` → ``Voice Session — <date> <time> <short-id>``.

    Regression guard: Salem's existing convention must not change. Distiller
    queries and `closed_sessions` parsing both expect this exact prefix.
    """
    sess = _make_session()
    name = talker_session._build_record_name(
        sess, tool_set="talker", mode="conversation",
    )
    assert name == "Voice Session — 2026-04-26 2136 17cfdecd"


def test_salem_filename_default_tool_set_unchanged() -> None:
    """Empty ``tool_set`` (legacy/no-config callers) preserves Salem shape.

    Default arg path — ``close_session`` calls without explicit ``tool_set``
    must still produce the wk1 filename so any code path that hasn't been
    threaded yet doesn't silently shift Salem records into Hypatia's shape.
    """
    sess = _make_session()
    name = talker_session._build_record_name(
        sess, tool_set="", mode="conversation",
    )
    assert name.startswith("Voice Session — ")


def test_kalle_filename_uses_voice_session_pattern() -> None:
    """KAL-LE inherits Salem's filename pattern (no separate spec)."""
    sess = _make_session()
    name = talker_session._build_record_name(
        sess, tool_set="kalle", mode="conversation",
    )
    assert name == "Voice Session — 2026-04-26 2136 17cfdecd"


def test_hypatia_filename_uses_mode_prefixed_slug_pattern() -> None:
    """``tool_set="hypatia"`` → ``conversation-<date>-<slug>-<short-id>``."""
    sess = _make_session()
    name = talker_session._build_record_name(
        sess, tool_set="hypatia", mode="conversation",
    )
    # First 5 tokens of "thinking out loud about the Q2 plan" =
    # "thinking out loud about the". "Q2" is dropped by the slug filter
    # because it's only kept after lowercasing — wait, "q2" is alnum
    # so it survives. Let me reconsider: 5 words = "thinking out loud
    # about the".
    assert name == "conversation-2026-04-26-thinking-out-loud-about-the-17cfdecd"


def test_hypatia_capture_filename_uses_capture_prefix() -> None:
    """Capture-mode session lands at ``capture-<date>-<slug>-<short-id>``."""
    sess = _make_session()
    name = talker_session._build_record_name(
        sess, tool_set="hypatia", mode="capture",
    )
    assert name.startswith("capture-2026-04-26-")
    assert name.endswith("-17cfdecd")


def test_hypatia_filename_handles_empty_transcript() -> None:
    """No user turns → ``untitled`` slug rather than crashing."""
    sess = _make_session(transcript=[])
    name = talker_session._build_record_name(
        sess, tool_set="hypatia", mode="conversation",
    )
    assert name == "conversation-2026-04-26-untitled-17cfdecd"


def test_hypatia_filename_strips_punctuation_from_slug() -> None:
    """Punctuation, em-dashes, and unicode get filtered out of the slug."""
    sess = _make_session(transcript=[
        {"role": "user", "content": "Hey, Pat — what's the deal?"},
    ])
    name = talker_session._build_record_name(
        sess, tool_set="hypatia", mode="conversation",
    )
    # First 5 tokens = "hey,", "pat", "—", "what's", "the".
    # After punctuation filtering: "hey", "pat", "", "whats", "the".
    # Empty token collapses; final slug = "hey-pat-whats-the".
    assert name == "conversation-2026-04-26-hey-pat-whats-the-17cfdecd"


# --- Mode mapping --------------------------------------------------------


def test_mode_from_session_type_capture() -> None:
    """``session_type="capture"`` → ``mode="capture"``."""
    assert talker_session._mode_from_session_type("capture") == "capture"


def test_mode_from_session_type_other_types_are_conversation() -> None:
    """All non-capture types collapse to ``conversation``."""
    for t in ("note", "task", "journal", "article", "brainstorm", "", None):
        assert talker_session._mode_from_session_type(t) == "conversation"


# --- Slug helper ---------------------------------------------------------


def test_slug_from_topic_lowercases_and_dashes() -> None:
    assert talker_session._slug_from_topic("Hello World") == "hello-world"


def test_slug_from_topic_caps_at_max_words() -> None:
    text = "one two three four five six seven"
    assert talker_session._slug_from_topic(text) == "one-two-three-four-five"


def test_slug_from_topic_filters_unicode() -> None:
    # Em-dashes and accented characters drop out (ASCII-only).
    assert talker_session._slug_from_topic("café — résumé") == "caf-rsum"


def test_slug_from_topic_empty_returns_untitled() -> None:
    assert talker_session._slug_from_topic("") == "untitled"
    assert talker_session._slug_from_topic("   ") == "untitled"


# --- Frontmatter shape ---------------------------------------------------


def test_hypatia_frontmatter_has_mode_processed_extracted_to() -> None:
    """Hypatia tool_set adds the three SKILL-spec fields.

    ``mode`` from the mode argument; ``processed=True`` for conversations
    (the close-time structuring pass IS the processing); ``extracted_to``
    is an empty placeholder list Hypatia populates post-extraction.
    """
    sess = _make_session()
    ended = datetime(2026, 4, 26, 22, 0, tzinfo=timezone.utc)

    fm = talker_session._build_session_frontmatter(
        sess,
        ended_at=ended,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="note",
        tool_set="hypatia",
        mode="conversation",
    )

    assert fm["mode"] == "conversation"
    assert fm["processed"] is True
    assert fm["extracted_to"] == []


def test_hypatia_frontmatter_includes_duration_minutes() -> None:
    """Hypatia frontmatter records ``duration_minutes`` (rounded).

    Per ~/library-alexandria/CLAUDE.md spec — Bases views ("Stale drafts",
    Daily Sync session-list) read this field. ``ended_at - started_at``
    in minutes, rounded.
    """
    sess = _make_session()  # started_at = 21:36 UTC
    ended = datetime(2026, 4, 26, 22, 0, tzinfo=timezone.utc)  # +24 minutes

    fm = talker_session._build_session_frontmatter(
        sess, ended_at=ended, reason="explicit",
        user_vault_path=None, stt_model_used="",
        tool_set="hypatia", mode="conversation",
    )
    assert fm["duration_minutes"] == 24


def test_salem_frontmatter_no_duration_minutes() -> None:
    """Salem records do not gain ``duration_minutes`` (Hypatia-only)."""
    sess = _make_session()
    ended = datetime(2026, 4, 26, 22, 0, tzinfo=timezone.utc)

    fm = talker_session._build_session_frontmatter(
        sess, ended_at=ended, reason="explicit",
        user_vault_path=None, stt_model_used="",
        tool_set="talker",
    )
    assert "duration_minutes" not in fm


def test_hypatia_capture_frontmatter_processed_false() -> None:
    """Capture-mode session queues at ``processed: false``.

    The "Unprocessed captures" Bases view in
    ``~/library-alexandria/_bases/`` filters on this; if it ever lands
    as ``True``, capture sessions never surface for Andrew's /extract.
    """
    sess = _make_session()
    ended = datetime(2026, 4, 26, 22, 0, tzinfo=timezone.utc)

    fm = talker_session._build_session_frontmatter(
        sess,
        ended_at=ended,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="capture",
        tool_set="hypatia",
        mode="capture",
    )

    assert fm["mode"] == "capture"
    assert fm["processed"] is False
    assert fm["extracted_to"] == []


def test_salem_frontmatter_does_not_have_hypatia_fields() -> None:
    """Regression guard: Salem records keep wk1 shape (no ``mode`` etc).

    Distiller queries against Salem's vault expect ``mode`` / ``processed``
    to be absent on her records. Adding them retroactively would break
    existing surveyor cluster bases.
    """
    sess = _make_session()
    ended = datetime(2026, 4, 26, 22, 0, tzinfo=timezone.utc)

    fm = talker_session._build_session_frontmatter(
        sess,
        ended_at=ended,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="note",
        tool_set="talker",
    )

    assert "mode" not in fm
    assert "processed" not in fm
    assert "extracted_to" not in fm


def test_hypatia_frontmatter_display_name_uses_mode_capitalized() -> None:
    """``name:`` field uses ``Conversation —`` / ``Capture —`` for Hypatia."""
    sess = _make_session()
    ended = datetime(2026, 4, 26, 22, 0, tzinfo=timezone.utc)

    fm_conv = talker_session._build_session_frontmatter(
        sess, ended_at=ended, reason="explicit",
        user_vault_path=None, stt_model_used="",
        tool_set="hypatia", mode="conversation",
    )
    fm_cap = talker_session._build_session_frontmatter(
        sess, ended_at=ended, reason="explicit",
        user_vault_path=None, stt_model_used="",
        tool_set="hypatia", mode="capture",
    )

    assert fm_conv["name"].startswith("Conversation — 2026-04-26 ")
    assert fm_cap["name"].startswith("Capture — 2026-04-26 ")


# --- close_session integration roundtrip --------------------------------


def test_close_session_hypatia_writes_mode_prefixed_record(
    state_mgr, talker_config
) -> None:
    """Full roundtrip: Hypatia close_session lands the right filename + fm.

    Uses the shared ``state_mgr`` + ``talker_config`` fixtures and checks
    both the relative path returned by ``close_session`` and the YAML
    frontmatter on disk. Asserts the load-bearing contract end-to-end.
    """
    chat_id = 99
    now = datetime(2026, 4, 26, 21, 36, tzinfo=timezone.utc)

    active = {
        "session_id": "17cfdecd-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-opus-4-7",
        "transcript": [
            {"role": "user", "content": "drafting the credit union pitch"},
            {"role": "assistant", "content": "let's start with the section structure"},
        ],
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "article",
        "_tool_set": "hypatia",
    }
    state_mgr.set_active(chat_id, active)
    state_mgr.save()

    rel_path = talker_session.close_session(
        state_mgr,
        vault_path_root=talker_config.vault.path,
        chat_id=chat_id,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="article",
        tool_set="hypatia",
    )

    # Filename pattern: conversation-<date>-<slug>-<short-id>.md
    assert rel_path == (
        "session/conversation-2026-04-26-drafting-the-credit-union-pitch-17cfdecd.md"
    )

    record = Path(talker_config.vault.path) / rel_path
    post = frontmatter.load(str(record))
    assert post["mode"] == "conversation"
    assert post["processed"] is True
    assert post["extracted_to"] == []
    # wk2 fields preserved.
    assert post["session_type"] == "article"
    assert post["telegram"]["model"] == "claude-opus-4-7"


def test_close_session_salem_keeps_voice_session_filename(
    state_mgr, talker_config
) -> None:
    """Regression: Salem close_session still writes ``Voice Session — ...``.

    The default ``talker_config`` fixture has ``InstanceConfig.tool_set ==
    "talker"`` — exercise the Salem path end-to-end so a future change
    that defaults differently fails this test loudly.
    """
    chat_id = 100
    now = datetime(2026, 4, 26, 21, 36, tzinfo=timezone.utc)

    active = {
        "session_id": "deadbeef-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-sonnet-4-6",
        "transcript": [{"role": "user", "content": "hi"}],
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "note",
        "_tool_set": "talker",
    }
    state_mgr.set_active(chat_id, active)
    state_mgr.save()

    rel_path = talker_session.close_session(
        state_mgr,
        vault_path_root=talker_config.vault.path,
        chat_id=chat_id,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="note",
        tool_set="talker",
    )

    assert rel_path == "session/Voice Session — 2026-04-26 2136 deadbeef.md"

    record = Path(talker_config.vault.path) / rel_path
    post = frontmatter.load(str(record))
    # Salem records keep wk1 shape — no Hypatia-specific fields.
    assert "mode" not in post.metadata
    assert "processed" not in post.metadata
    assert "extracted_to" not in post.metadata


def test_close_session_kalle_keeps_voice_session_filename(
    state_mgr, talker_config
) -> None:
    """KAL-LE's ``tool_set="kalle"`` also keeps the wk1 filename.

    KAL-LE has its own bash_exec tool surface, but session naming is
    shared with Salem — there's no KAL-LE-specific spec for it. This
    test makes that decision explicit.
    """
    chat_id = 101
    now = datetime(2026, 4, 26, 21, 36, tzinfo=timezone.utc)

    active = {
        "session_id": "cafe0001-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-opus-4-7",
        "transcript": [{"role": "user", "content": "fix the build"}],
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "task",
        "_tool_set": "kalle",
    }
    state_mgr.set_active(chat_id, active)
    state_mgr.save()

    rel_path = talker_session.close_session(
        state_mgr,
        vault_path_root=talker_config.vault.path,
        chat_id=chat_id,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="task",
        tool_set="kalle",
    )

    assert rel_path == "session/Voice Session — 2026-04-26 2136 cafe0001.md"


def test_close_session_hypatia_capture_writes_processed_false(
    state_mgr, talker_config
) -> None:
    """Capture-mode hypatia session lands ``processed: false`` on disk.

    Load-bearing for the "Unprocessed captures" Bases view in
    ``~/library-alexandria/_bases/`` — that view filters on
    ``processed == false`` and surfaces the queue. If processed lands
    as anything else, the queue is empty and Andrew never sees the
    /extract prompt.
    """
    chat_id = 102
    now = datetime(2026, 4, 26, 21, 36, tzinfo=timezone.utc)

    active = {
        "session_id": "babe0002-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-sonnet-4-6",
        "transcript": [{"role": "user", "content": "ok let me think aloud"}],
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "capture",
        "_tool_set": "hypatia",
    }
    state_mgr.set_active(chat_id, active)
    state_mgr.save()

    rel_path = talker_session.close_session(
        state_mgr,
        vault_path_root=talker_config.vault.path,
        chat_id=chat_id,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="capture",
        tool_set="hypatia",
    )

    assert rel_path.startswith("session/capture-2026-04-26-")
    record = Path(talker_config.vault.path) / rel_path
    post = frontmatter.load(str(record))
    assert post["mode"] == "capture"
    assert post["processed"] is False
