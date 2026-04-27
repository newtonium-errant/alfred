"""Tests for per-instance session-save naming and frontmatter shape.

The talker writes a ``session/`` record on close. The filename pattern
follows ``<mode>-<YYYY-MM-DD>-<slug>-<short-id>`` for every registered
instance (``INSTANCE_MODE_PREFIXES`` in ``session.py``); the per-instance
mode set is the only thing that varies. Hypatia additionally adds
``mode``, ``processed``, ``extracted_to`` to the frontmatter — those
fields are tied to her ``/extract`` workflow and stay Hypatia-only.

Legacy / unknown ``tool_set`` (any code path not yet threaded with
the field, plus pre-existing vault records) keeps the wk1
``Voice Session — <date> <time> <short-id>`` pattern. Existing
session files on disk are NEVER renamed — backward compat is
load-bearing.

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


def test_salem_filename_uses_mode_prefixed_pattern() -> None:
    """``tool_set="talker"`` → ``<mode>-<date>-<slug>-<short-id>``.

    Salem migrated off the wk1 ``Voice Session — ...`` shape when the
    per-instance mode-prefixed pattern was generalized across all
    registered instances. Legacy session files on disk keep their
    original filenames — only NEW sessions follow this pattern.
    """
    sess = _make_session()
    name = talker_session._build_record_name(
        sess, tool_set="talker", mode="conversation",
    )
    assert name == "conversation-2026-04-26-thinking-out-loud-about-the-17cfdecd"


def test_salem_filename_default_tool_set_uses_legacy_pattern() -> None:
    """Empty ``tool_set`` (legacy/no-config callers) preserves wk1 shape.

    Default arg path — any code path not yet threaded with ``tool_set``
    must still produce the wk1 filename so callers don't silently shift
    into the new pattern before they've been audited. Pre-existing
    legacy session files (``Voice Session — ...``) on disk also stay
    readable through this branch.
    """
    sess = _make_session()
    name = talker_session._build_record_name(
        sess, tool_set="", mode="conversation",
    )
    assert name.startswith("Voice Session — ")


def test_kalle_filename_uses_mode_prefixed_pattern() -> None:
    """``tool_set="kalle"`` uses her own mode set (``coding`` / ``review``)."""
    sess = _make_session()
    name_coding = talker_session._build_record_name(
        sess, tool_set="kalle", mode="coding",
    )
    name_review = talker_session._build_record_name(
        sess, tool_set="kalle", mode="review",
    )
    assert name_coding == "coding-2026-04-26-thinking-out-loud-about-the-17cfdecd"
    assert name_review == "review-2026-04-26-thinking-out-loud-about-the-17cfdecd"


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


# --- Per-instance mode resolution ---------------------------------------


def test_resolve_mode_salem_text_only_is_conversation() -> None:
    """Salem text-only session (no voice, no capture) → ``conversation``."""
    sess = _make_session(transcript=[
        {"role": "user", "content": "hi", "_kind": "text"},
    ])
    mode = talker_session._resolve_mode_for_instance("talker", sess, "note")
    assert mode == "conversation"


def test_resolve_mode_salem_voice_session_is_voice() -> None:
    """Any voice user turn flips the Salem mode to ``voice``."""
    sess = _make_session(transcript=[
        {"role": "user", "content": "hi", "_kind": "voice"},
    ])
    mode = talker_session._resolve_mode_for_instance("talker", sess, "note")
    assert mode == "voice"


def test_resolve_mode_salem_mixed_text_and_voice_is_voice() -> None:
    """Even one voice user turn alongside text turns → ``voice``."""
    sess = _make_session(transcript=[
        {"role": "user", "content": "first", "_kind": "text"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "second", "_kind": "voice"},
    ])
    mode = talker_session._resolve_mode_for_instance("talker", sess, "note")
    assert mode == "voice"


def test_resolve_mode_salem_capture_wins_over_voice() -> None:
    """``session_type="capture"`` short-circuits the voice check.

    The ``/capture`` opener is an explicit user signal — even if the
    capture happened to be voice-driven, the ``mode`` is ``capture``
    (not ``voice``) so the routing/queue logic stays aligned with the
    session_type.
    """
    sess = _make_session(transcript=[
        {"role": "user", "content": "ramble", "_kind": "voice"},
    ])
    mode = talker_session._resolve_mode_for_instance("talker", sess, "capture")
    assert mode == "capture"


def test_resolve_mode_kalle_default_is_coding() -> None:
    """KAL-LE without an ``alfred reviews`` invocation → ``coding``."""
    sess = _make_session(transcript=[
        {"role": "user", "content": "fix the build"},
    ])
    mode = talker_session._resolve_mode_for_instance("kalle", sess, "task")
    assert mode == "coding"


def test_resolve_mode_kalle_reviews_invocation_is_review() -> None:
    """KAL-LE session that ran ``alfred reviews ...`` → ``review``."""
    sess = _make_session(transcript=[
        {"role": "user", "content": "check the PR review queue"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "running it now"},
                {
                    "type": "tool_use",
                    "id": "toolu_01",
                    "name": "bash_exec",
                    "input": {
                        "command": "alfred reviews list --project alfred",
                        "cwd": "/home/andrew/alfred",
                    },
                },
            ],
        },
    ])
    mode = talker_session._resolve_mode_for_instance("kalle", sess, "task")
    assert mode == "review"


def test_resolve_mode_kalle_reviews_only_in_text_does_not_count() -> None:
    """A literal mention of ``alfred reviews`` in plain text doesn't count.

    Detection is anchored on ``tool_use`` blocks named ``bash_exec`` —
    a transcript that merely *talks about* the command (e.g. KAL-LE
    explaining the workflow) stays in ``coding`` mode.
    """
    sess = _make_session(transcript=[
        {
            "role": "assistant",
            "content": "you can run `alfred reviews list` to check the queue",
        },
    ])
    mode = talker_session._resolve_mode_for_instance("kalle", sess, "task")
    assert mode == "coding"


def test_resolve_mode_hypatia_default_is_conversation() -> None:
    """Hypatia without ``capture`` session_type → ``conversation``."""
    sess = _make_session()
    mode = talker_session._resolve_mode_for_instance("hypatia", sess, "note")
    assert mode == "conversation"


def test_resolve_mode_hypatia_capture_is_capture() -> None:
    """Hypatia ``session_type="capture"`` → ``capture``."""
    sess = _make_session()
    mode = talker_session._resolve_mode_for_instance(
        "hypatia", sess, "capture",
    )
    assert mode == "capture"


def test_resolve_mode_unknown_tool_set_returns_empty() -> None:
    """Unregistered ``tool_set`` returns ``""`` so the wk1 filename is used.

    This is the load-bearing escape hatch: any caller that hasn't been
    threaded with a registered ``tool_set`` keeps writing legacy-shaped
    records, never silently shifting to the new pattern with a wrong
    mode.
    """
    sess = _make_session()
    assert talker_session._resolve_mode_for_instance("", sess, "note") == ""
    assert (
        talker_session._resolve_mode_for_instance("nope", sess, "note") == ""
    )


def test_instance_mode_prefixes_registry_locked() -> None:
    """Lock the per-instance prefix registry — additions need a deliberate edit.

    Reading: any change here is a contract-breaking change (a new
    instance, or a new mode prefix on an existing instance). This test
    is the audit trail — touching it in a PR is a flag for the
    code-reviewer to check the matching SKILL / CLAUDE.md updates.
    """
    assert talker_session.INSTANCE_MODE_PREFIXES == {
        "talker": ["voice", "conversation", "capture"],
        "hypatia": ["conversation", "capture"],
        "kalle": ["coding", "review"],
    }


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


def test_close_session_salem_text_only_uses_conversation_prefix(
    state_mgr, talker_config
) -> None:
    """Salem text-only session lands at ``conversation-<date>-<slug>-<id>``.

    Mode auto-detection: no voice user turns + no ``capture`` session_type
    → ``conversation`` (the instance's first-listed prefix after ``voice``,
    used as the catch-all default).
    """
    chat_id = 100
    now = datetime(2026, 4, 26, 21, 36, tzinfo=timezone.utc)

    active = {
        "session_id": "deadbeef-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-sonnet-4-6",
        # Text-only — no ``_kind="voice"`` markers anywhere.
        "transcript": [{"role": "user", "content": "hi there old friend"}],
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

    assert rel_path == (
        "session/conversation-2026-04-26-hi-there-old-friend-deadbeef.md"
    )

    record = Path(talker_config.vault.path) / rel_path
    post = frontmatter.load(str(record))
    # Salem records do NOT gain Hypatia-specific fields — those stay
    # tied to the ``/extract`` workflow.
    assert "mode" not in post.metadata
    assert "processed" not in post.metadata
    assert "extracted_to" not in post.metadata


def test_close_session_salem_voice_uses_voice_prefix(
    state_mgr, talker_config
) -> None:
    """Salem session with at least one voice user turn → ``voice-...``."""
    chat_id = 110
    now = datetime(2026, 4, 26, 21, 36, tzinfo=timezone.utc)

    active = {
        "session_id": "v0iceabc-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-sonnet-4-6",
        "transcript": [
            {
                "role": "user",
                "content": "rambling about the Q2 plan",
                "_kind": "voice",
            },
        ],
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

    assert rel_path.startswith("session/voice-2026-04-26-rambling-about-the")


def test_close_session_salem_capture_uses_capture_prefix(
    state_mgr, talker_config
) -> None:
    """Salem ``/capture`` session lands at ``capture-...``."""
    chat_id = 111
    now = datetime(2026, 4, 26, 21, 36, tzinfo=timezone.utc)

    active = {
        "session_id": "cap7e000-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-sonnet-4-6",
        "transcript": [{"role": "user", "content": "thinking aloud now"}],
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "capture",
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
        session_type="capture",
        tool_set="talker",
    )

    assert rel_path.startswith("session/capture-2026-04-26-")


def test_close_session_kalle_default_uses_coding_prefix(
    state_mgr, talker_config
) -> None:
    """KAL-LE without an ``alfred reviews`` call → ``coding-...``.

    KAL-LE's first-listed mode is ``coding``; that's the default
    fallback when mode-resolution can't infer a more specific mode.
    """
    chat_id = 101
    now = datetime(2026, 4, 26, 21, 36, tzinfo=timezone.utc)

    active = {
        "session_id": "cafe0001-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-opus-4-7",
        "transcript": [{"role": "user", "content": "fix the build please"}],
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

    assert rel_path == (
        "session/coding-2026-04-26-fix-the-build-please-cafe0001.md"
    )


def test_close_session_kalle_review_uses_review_prefix(
    state_mgr, talker_config
) -> None:
    """KAL-LE session that ran ``alfred reviews`` → ``review-...``."""
    chat_id = 112
    now = datetime(2026, 4, 26, 21, 36, tzinfo=timezone.utc)

    active = {
        "session_id": "rev1ew00-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-opus-4-7",
        # Mid-session tool_use — KAL-LE called ``alfred reviews list``
        # via bash_exec. The block shape mirrors what the SDK actually
        # produces for Anthropic-format tool turns.
        "transcript": [
            {"role": "user", "content": "check the alfred PR feedback"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "let me check"},
                    {
                        "type": "tool_use",
                        "id": "toolu_01",
                        "name": "bash_exec",
                        "input": {
                            "command": "alfred reviews list --project alfred",
                            "cwd": "/home/andrew/alfred",
                        },
                    },
                ],
            },
        ],
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

    assert rel_path.startswith("session/review-2026-04-26-")


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
