"""Tests for Phase 2 deferred-enhancement #1 — substance-slug derivation.

The talker derives a 3-5 word topic slug from the closed-session
transcript and renames the just-written session record so the filename
reflects what the session was *about* (not the opening greeting).
Behaviour is gated by ``telegram.session.derive_slug_from_substance``
(default off; on for Hypatia in Phase 2).

These tests cover:

- The pure helpers: ``_extract_substance_text`` (greeting strip +
  user-turn collection), ``is_substantive`` (length / turn-count gate),
  ``_normalize_substance_slug`` (LLM-output cleanup).
- The async derivation entry point: ``derive_slug_from_substance_async``
  with a fake Anthropic client (success + LLM-error + parse-error).
- The rename step: ``apply_substance_slug`` (filename + frontmatter
  mutation + closed_sessions state update).
- Failure isolation: derivation error → original filename preserved.
- The post-close hook: ``maybe_apply_substance_slug`` end-to-end.
- Config gate: ``derive_slug_from_substance: false`` → no rename.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frontmatter
import pytest

from alfred.telegram import session as talker_session


# ---------------------------------------------------------------------------
# Fake Anthropic client — minimal enough for slug derivation
# ---------------------------------------------------------------------------


@dataclass
class _FakeTextBlock:
    type: str = "text"
    text: str = ""


@dataclass
class _FakeResponse:
    content: list[_FakeTextBlock] = field(default_factory=list)
    stop_reason: str = "end_turn"


class _FakeMessages:
    def __init__(self, response_text: str = "", *, raise_exc: Exception | None = None) -> None:
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        if self.raise_exc is not None:
            raise self.raise_exc
        return _FakeResponse(content=[_FakeTextBlock(text=self.response_text)])


class _FakeClient:
    def __init__(self, response_text: str = "", *, raise_exc: Exception | None = None) -> None:
        self.messages = _FakeMessages(response_text, raise_exc=raise_exc)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_extract_substance_strips_trivial_opener() -> None:
    """A greeting at the head of the first user turn is dropped.

    "Are you awake?" alone shouldn't drive the slug — the next user
    turn carries the actual topic. The trivial-opener filter is
    case-insensitive and tolerates trailing punctuation.
    """
    transcript = [
        {"role": "user", "content": "Are you awake?"},
        {"role": "assistant", "content": "yes, what's up"},
        {
            "role": "user",
            "content": "I want to draft the Komal Gupta termination response letter today",
        },
    ]
    substance = talker_session._extract_substance_text(transcript)
    assert "draft the Komal Gupta termination response letter" in substance
    assert "are you awake" not in substance.lower()


def test_extract_substance_only_strips_first_turn() -> None:
    """Trivial-opener strip applies only to the first user turn.

    A mid-conversation "hi" is real content (e.g. "say hi to Sarah for me")
    and shouldn't be stripped.
    """
    transcript = [
        {"role": "user", "content": "I want to draft an email"},
        {"role": "assistant", "content": "ok"},
        # Mid-session — even a literal "hi" stays.
        {"role": "user", "content": "hi"},
    ]
    substance = talker_session._extract_substance_text(transcript)
    assert "draft an email" in substance
    assert substance.count("hi") >= 1


def test_extract_substance_handles_list_content_blocks() -> None:
    """User turns with list-of-blocks ``content`` are tolerated.

    The Anthropic SDK uses list-of-blocks shape for tool turns. Pure
    text in a single-element list should be picked up. Tool blocks
    are dropped (no "type": "text").
    """
    transcript = [
        {
            "role": "user",
            "content": [{"type": "text", "text": "drafting the substack essay on rural transport"}],
        },
    ]
    substance = talker_session._extract_substance_text(transcript)
    assert "rural transport" in substance


def test_is_substantive_below_turn_threshold_returns_false() -> None:
    """A 2-turn session is below the gate even if the content is long."""
    long_text = "x" * 400
    transcript = [
        {"role": "user", "content": long_text},
        {"role": "assistant", "content": "ok"},
    ]
    assert talker_session.is_substantive(transcript) is False


def test_is_substantive_below_char_threshold_returns_false() -> None:
    """5 short pings don't pass the char threshold either."""
    transcript = [
        {"role": "user", "content": "yo"},
        {"role": "assistant", "content": "yo"},
        {"role": "user", "content": "you up"},
        {"role": "assistant", "content": "yes"},
        {"role": "user", "content": "ok cool"},
    ]
    assert talker_session.is_substantive(transcript) is False


def test_is_substantive_meets_both_thresholds_returns_true() -> None:
    """A real conversation with enough turns + chars passes the gate."""
    long_text = (
        "I want to draft the Komal Gupta termination response letter today. "
        "She's challenging the dismissal under EI provisions. "
        "I need to think through the chronology carefully."
    )
    transcript = [
        {"role": "user", "content": long_text},
        {"role": "assistant", "content": "ok let's start with the chronology"},
        {"role": "user", "content": "the dismissal happened on April 1"},
    ]
    assert talker_session.is_substantive(transcript) is True


def test_normalize_substance_slug_strips_quotes_and_caps() -> None:
    """LLM responses with quotes / capitals get cleaned up to filename-safe form."""
    raw = '"Komal Gupta Termination Response"\n\nNote: this is a slug.'
    cleaned = talker_session._normalize_substance_slug(raw)
    assert cleaned == "komal-gupta-termination-response"


def test_normalize_substance_slug_caps_at_max_words() -> None:
    """5-word cap is honoured — extra words are dropped."""
    raw = "one two three four five six seven"
    assert talker_session._normalize_substance_slug(raw) == "one-two-three-four-five"


def test_normalize_substance_slug_untitled_returns_empty() -> None:
    """The model emitting the literal 'untitled' token signals fall-through.

    The slug-derivation prompt instructs the model to emit ``untitled``
    when it can't extract a clear topic. The normalizer maps that back
    to ``""`` so the caller knows to keep the opening-text slug.
    """
    assert talker_session._normalize_substance_slug("untitled") == ""


def test_normalize_substance_slug_empty_input_returns_empty() -> None:
    """Empty / whitespace-only LLM output → ``""``."""
    assert talker_session._normalize_substance_slug("") == ""
    assert talker_session._normalize_substance_slug("   \n  ") == ""


# ---------------------------------------------------------------------------
# Async derivation entry point
# ---------------------------------------------------------------------------


def test_derive_slug_from_substance_async_returns_clean_slug() -> None:
    """Happy path: LLM returns 3-5 word slug, function returns cleaned form."""
    client = _FakeClient(response_text="komal gupta termination response")
    transcript = [
        {
            "role": "user",
            "content": (
                "I want to draft the Komal Gupta termination response letter today. "
                "She is challenging the dismissal under EI provisions. "
                "I need to think through the chronology carefully and then "
                "structure the response around the documented warnings."
            ),
        },
        {"role": "assistant", "content": "ok let's start with the chronology"},
        {"role": "user", "content": "the dismissal happened on April 1 2026"},
    ]
    slug = asyncio.run(talker_session.derive_slug_from_substance_async(
        client, "claude-sonnet-4-6", transcript,
    ))
    assert slug == "komal-gupta-termination-response"
    # Confirm the derivation actually called the LLM.
    assert len(client.messages.calls) == 1
    # Temperature shim was applied — sonnet accepts it, opus would drop it.
    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-4-6"
    assert "temperature" in call


def test_derive_slug_from_substance_async_uses_transcript_framing() -> None:
    """The user message wraps content in <transcript> tags, system labels-not-continues.

    Load-bearing fix: without the ``<transcript>`` tag wrapping +
    "you are LABELLING it, not continuing it" instruction, Opus tends
    to respond to the transcript content as if it were addressed to
    it. This test asserts both pieces of the framing are present so
    a future refactor doesn't quietly drop them.
    """
    client = _FakeClient(response_text="komal gupta termination response")
    transcript = [
        {
            "role": "user",
            "content": (
                "I want to draft the Komal Gupta termination response letter today. "
                "She is challenging the dismissal under EI provisions. "
                "I need to think through the chronology carefully and then "
                "structure the response around the documented warnings."
            ),
        },
        {"role": "assistant", "content": "ok let's start with the chronology"},
        {"role": "user", "content": "the dismissal happened on April 1 2026"},
    ]
    asyncio.run(talker_session.derive_slug_from_substance_async(
        client, "claude-sonnet-4-6", transcript,
    ))
    call = client.messages.calls[0]
    user_message = call["messages"][0]["content"]
    assert "<transcript>" in user_message
    assert "</transcript>" in user_message
    # System-prompt instruction: model must label, not continue.
    system_blocks = call["system"]
    system_text = "".join(b.get("text", "") for b in system_blocks)
    assert "LABELLING" in system_text


def test_derive_slug_from_substance_async_below_gate_skips_llm() -> None:
    """Non-substantive transcript short-circuits — no LLM call made."""
    client = _FakeClient(response_text="should not be used")
    transcript = [
        {"role": "user", "content": "yo"},
        {"role": "assistant", "content": "yo"},
    ]
    slug = asyncio.run(talker_session.derive_slug_from_substance_async(
        client, "claude-sonnet-4-6", transcript,
    ))
    assert slug == ""
    assert client.messages.calls == []


def test_derive_slug_from_substance_async_llm_error_returns_empty() -> None:
    """LLM exception → ``""`` (failure-isolated, no propagation)."""
    client = _FakeClient(raise_exc=RuntimeError("rate limited"))
    transcript = [
        {"role": "user", "content": "x" * 250},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "and another long line " * 5},
    ]
    slug = asyncio.run(talker_session.derive_slug_from_substance_async(
        client, "claude-sonnet-4-6", transcript,
    ))
    assert slug == ""


def test_derive_slug_from_substance_async_empty_response_returns_empty() -> None:
    """LLM returning empty text → ``""``."""
    client = _FakeClient(response_text="")
    transcript = [
        {"role": "user", "content": "x" * 250},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "and another long line " * 5},
    ]
    slug = asyncio.run(talker_session.derive_slug_from_substance_async(
        client, "claude-sonnet-4-6", transcript,
    ))
    assert slug == ""


def test_derive_slug_from_substance_async_drops_temperature_for_opus() -> None:
    """Opus 4.x rejects temperature — the shim drops it.

    Hypatia uses claude-opus-4-7 as her default model. The
    ``messages_create_kwargs`` shim must drop ``temperature`` for
    opus targets so the slug-derivation call doesn't 400.
    """
    client = _FakeClient(response_text="vac unit economics model")
    transcript = [
        {
            "role": "user",
            "content": (
                "I'm modeling VAC unit economics for the next quarter. "
                "Per-engagement costs are higher than I expected so I "
                "need to revisit the rate card and rebuild the assumptions."
            ),
        },
        {"role": "assistant", "content": "let's walk through the cost structure"},
        {"role": "user", "content": "engagement number one was 12 hours over 3 weeks"},
    ]
    asyncio.run(talker_session.derive_slug_from_substance_async(
        client, "claude-opus-4-7", transcript,
    ))
    call = client.messages.calls[0]
    assert call["model"] == "claude-opus-4-7"
    assert "temperature" not in call


# ---------------------------------------------------------------------------
# Rename step — apply_substance_slug
# ---------------------------------------------------------------------------


def _write_session_record(
    vault_path: Path,
    rel_path: str,
    *,
    name: str = "Conversation — 2026-04-27 are you awake",
    extra_fm: dict[str, Any] | None = None,
) -> Path:
    """Helper: write a minimal session record at ``rel_path``."""
    fm: dict[str, Any] = {
        "type": "session",
        "name": name,
        "session_type": "note",
    }
    if extra_fm:
        fm.update(extra_fm)
    post = frontmatter.Post("# Transcript\n\n**Andrew** (10:00): test\n", **fm)
    full = vault_path / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
    return full


def test_apply_substance_slug_renames_file_and_updates_frontmatter(
    state_mgr, talker_config,
) -> None:
    """Happy path: file renamed, frontmatter ``name`` rewritten, state updated."""
    vault = Path(talker_config.vault.path)
    old_rel = "session/conversation-2026-04-27-are-you-awake-73fe87fa.md"
    _write_session_record(vault, old_rel)

    # Seed a closed_sessions entry so the state-update branch runs.
    state_mgr.append_closed({
        "session_id": "73fe87fa-0000-0000-0000-000000000000",
        "chat_id": 1,
        "record_path": old_rel,
    })
    state_mgr.save()

    new_rel = talker_session.apply_substance_slug(
        state_mgr,
        vault_path_root=str(vault),
        rel_path=old_rel,
        new_slug="komal-gupta-termination-response",
        session_id="73fe87fa-0000-0000-0000-000000000000",
    )

    assert new_rel == (
        "session/conversation-2026-04-27-komal-gupta-termination-response-73fe87fa.md"
    )
    # File rename happened.
    assert not (vault / old_rel).exists()
    assert (vault / new_rel).exists()
    # Frontmatter ``name`` rewritten.
    post = frontmatter.load(str(vault / new_rel))
    assert post.metadata["name"] == (
        "Conversation — 2026-04-27 komal-gupta-termination-response"
    )
    assert post.metadata["substance_slug_derived"] is True
    # State entry follows the new path.
    closed = state_mgr.state["closed_sessions"]
    assert closed[0]["record_path"] == new_rel
    assert closed[0]["substance_slug_derived"] is True


def test_apply_substance_slug_legacy_filename_passthrough(
    state_mgr, talker_config,
) -> None:
    """Legacy ``Voice Session — ...`` paths are NOT renamed.

    Backward compat is load-bearing — only files matching the
    per-instance ``<mode>-<date>-<slug>-<short>`` shape get the
    new slug. Anything else falls through with the original path.
    """
    vault = Path(talker_config.vault.path)
    old_rel = "session/Voice Session — 2026-04-27 1500 73fe87fa.md"
    _write_session_record(vault, old_rel, name="Voice Session — 2026-04-27 1500")

    new_rel = talker_session.apply_substance_slug(
        state_mgr,
        vault_path_root=str(vault),
        rel_path=old_rel,
        new_slug="komal-gupta-termination",
        session_id="73fe87fa",
    )

    assert new_rel == old_rel
    # Original file untouched.
    assert (vault / old_rel).exists()


def test_apply_substance_slug_collision_passthrough(
    state_mgr, talker_config,
) -> None:
    """Destination-exists collision keeps the original filename."""
    vault = Path(talker_config.vault.path)
    old_rel = "session/conversation-2026-04-27-are-you-awake-73fe87fa.md"
    target_rel = "session/conversation-2026-04-27-already-here-73fe87fa.md"
    _write_session_record(vault, old_rel)
    _write_session_record(vault, target_rel, name="Conversation — 2026-04-27 already here")

    new_rel = talker_session.apply_substance_slug(
        state_mgr,
        vault_path_root=str(vault),
        rel_path=old_rel,
        new_slug="already-here",
        session_id="73fe87fa-0000",
    )
    assert new_rel == old_rel
    assert (vault / old_rel).exists()
    assert (vault / target_rel).exists()


def test_apply_substance_slug_empty_slug_passthrough(
    state_mgr, talker_config,
) -> None:
    """Empty ``new_slug`` short-circuits — no rename attempted."""
    vault = Path(talker_config.vault.path)
    old_rel = "session/conversation-2026-04-27-are-you-awake-73fe87fa.md"
    _write_session_record(vault, old_rel)

    new_rel = talker_session.apply_substance_slug(
        state_mgr,
        vault_path_root=str(vault),
        rel_path=old_rel,
        new_slug="",
        session_id="73fe87fa-0000",
    )
    assert new_rel == old_rel
    assert (vault / old_rel).exists()


def test_apply_substance_slug_missing_source_passthrough(
    state_mgr, talker_config,
) -> None:
    """Source file not on disk → graceful pass-through."""
    vault = Path(talker_config.vault.path)
    old_rel = "session/conversation-2026-04-27-no-such-file-73fe87fa.md"
    new_rel = talker_session.apply_substance_slug(
        state_mgr,
        vault_path_root=str(vault),
        rel_path=old_rel,
        new_slug="some-slug",
        session_id="73fe87fa-0000",
    )
    assert new_rel == old_rel


def test_apply_substance_slug_frontmatter_rewrite_failure_passthrough(
    state_mgr, talker_config, monkeypatch, capsys,
) -> None:
    """Frontmatter rewrite failure → original file preserved, warning logged.

    Exercises the ``stage="frontmatter_rewrite"`` branch in
    ``apply_substance_slug``: ``frontmatter.dumps`` raising forces the
    function to bail BEFORE the rename step. The original file must
    stay on disk at the original path, the function must return the
    original ``rel_path``, and the warning must surface stage info so
    operators can grep for it in the talker log.
    """
    vault = Path(talker_config.vault.path)
    old_rel = "session/conversation-2026-04-27-are-you-awake-73fe87fa.md"
    _write_session_record(vault, old_rel)

    def _boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("synthetic frontmatter dumps failure")

    monkeypatch.setattr("frontmatter.dumps", _boom)

    new_rel = talker_session.apply_substance_slug(
        state_mgr,
        vault_path_root=str(vault),
        rel_path=old_rel,
        new_slug="komal-gupta-termination-response",
        session_id="73fe87fa-0000",
    )

    # No rename occurred — original path returned, original file still on disk.
    assert new_rel == old_rel
    assert (vault / old_rel).exists()
    assert not (
        vault / "session/conversation-2026-04-27-komal-gupta-termination-response-73fe87fa.md"
    ).exists()
    # Warning fired with the right stage tag — structlog routes warnings
    # to stdout in test config; we grep the captured output.
    captured = capsys.readouterr()
    log_text = captured.out + captured.err
    assert "talker.session.substance_slug_failed" in log_text
    assert "stage=frontmatter_rewrite" in log_text


# ---------------------------------------------------------------------------
# End-to-end hook — maybe_apply_substance_slug
# ---------------------------------------------------------------------------


def test_maybe_apply_substance_slug_disabled_passthrough(
    state_mgr, talker_config,
) -> None:
    """``enabled=False`` → no LLM call, no rename, original path returned."""
    vault = Path(talker_config.vault.path)
    old_rel = "session/conversation-2026-04-27-are-you-awake-73fe87fa.md"
    _write_session_record(vault, old_rel)
    client = _FakeClient(response_text="should not be used")
    transcript = [
        {"role": "user", "content": "x" * 250},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "y " * 100},
    ]

    new_rel = asyncio.run(talker_session.maybe_apply_substance_slug(
        state_mgr,
        enabled=False,
        client=client,
        model="claude-sonnet-4-6",
        vault_path_root=str(vault),
        rel_path=old_rel,
        transcript=transcript,
        session_id="73fe87fa-0000",
    ))
    assert new_rel == old_rel
    assert client.messages.calls == []
    assert (vault / old_rel).exists()


def test_maybe_apply_substance_slug_no_client_passthrough(
    state_mgr, talker_config,
) -> None:
    """``client=None`` → graceful pass-through (defensive guard)."""
    vault = Path(talker_config.vault.path)
    old_rel = "session/conversation-2026-04-27-are-you-awake-73fe87fa.md"
    _write_session_record(vault, old_rel)
    transcript = [
        {"role": "user", "content": "x" * 250},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "y " * 100},
    ]
    new_rel = asyncio.run(talker_session.maybe_apply_substance_slug(
        state_mgr,
        enabled=True,
        client=None,
        model="claude-sonnet-4-6",
        vault_path_root=str(vault),
        rel_path=old_rel,
        transcript=transcript,
        session_id="73fe87fa-0000",
    ))
    assert new_rel == old_rel


def test_maybe_apply_substance_slug_end_to_end(
    state_mgr, talker_config,
) -> None:
    """Full path: enabled + substantive + LLM responds → rename happens."""
    vault = Path(talker_config.vault.path)
    old_rel = "session/conversation-2026-04-27-are-you-awake-73fe87fa.md"
    _write_session_record(vault, old_rel)
    state_mgr.append_closed({
        "session_id": "73fe87fa-0000-0000-0000-000000000000",
        "chat_id": 1,
        "record_path": old_rel,
    })
    state_mgr.save()
    client = _FakeClient(response_text="komal gupta termination response")
    transcript = [
        {"role": "user", "content": "Are you awake?"},
        {"role": "assistant", "content": "yes"},
        {
            "role": "user",
            "content": (
                "I want to draft the Komal Gupta termination response letter "
                "today. She is challenging the dismissal under EI provisions. "
                "I need to think through the documented chronology carefully "
                "and structure the response around the warning letters."
            ),
        },
    ]

    new_rel = asyncio.run(talker_session.maybe_apply_substance_slug(
        state_mgr,
        enabled=True,
        client=client,
        model="claude-sonnet-4-6",
        vault_path_root=str(vault),
        rel_path=old_rel,
        transcript=transcript,
        session_id="73fe87fa-0000-0000-0000-000000000000",
    ))
    assert new_rel == (
        "session/conversation-2026-04-27-komal-gupta-termination-response-73fe87fa.md"
    )
    assert not (vault / old_rel).exists()
    assert (vault / new_rel).exists()


def test_maybe_apply_substance_slug_llm_failure_preserves_filename(
    state_mgr, talker_config,
) -> None:
    """LLM raises → original filename preserved, no exception bubbles up.

    Failure-isolation is the load-bearing contract here. If the LLM
    times out / rate-limits / errors, the close flow has already
    succeeded — the rename is a polish step, not load-bearing.
    """
    vault = Path(talker_config.vault.path)
    old_rel = "session/conversation-2026-04-27-are-you-awake-73fe87fa.md"
    _write_session_record(vault, old_rel)
    client = _FakeClient(raise_exc=RuntimeError("rate limited"))
    transcript = [
        {
            "role": "user",
            "content": (
                "I want to draft the Komal Gupta termination response letter. "
                "She is challenging the dismissal under EI provisions."
            ),
        },
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "the dismissal happened on April 1 2026"},
    ]

    new_rel = asyncio.run(talker_session.maybe_apply_substance_slug(
        state_mgr,
        enabled=True,
        client=client,
        model="claude-sonnet-4-6",
        vault_path_root=str(vault),
        rel_path=old_rel,
        transcript=transcript,
        session_id="73fe87fa-0000",
    ))
    assert new_rel == old_rel
    assert (vault / old_rel).exists()


def test_maybe_apply_substance_slug_below_gate_passthrough(
    state_mgr, talker_config,
) -> None:
    """Trivial transcript → no LLM call, original path returned.

    Saves a useless LLM hit on "are you awake?" / "ping" / etc. — the
    is_substantive gate runs before the network call.
    """
    vault = Path(talker_config.vault.path)
    old_rel = "session/conversation-2026-04-27-yo-73fe87fa.md"
    _write_session_record(vault, old_rel)
    client = _FakeClient(response_text="should not be used")
    transcript = [{"role": "user", "content": "yo"}]

    new_rel = asyncio.run(talker_session.maybe_apply_substance_slug(
        state_mgr,
        enabled=True,
        client=client,
        model="claude-sonnet-4-6",
        vault_path_root=str(vault),
        rel_path=old_rel,
        transcript=transcript,
        session_id="73fe87fa-0000",
    ))
    assert new_rel == old_rel
    assert client.messages.calls == []


# ---------------------------------------------------------------------------
# Integration roundtrip — close_session followed by maybe_apply_substance_slug
# ---------------------------------------------------------------------------


def test_close_session_then_substance_slug_roundtrip(
    state_mgr, talker_config,
) -> None:
    """Full integration: close_session writes record → substance-slug renames.

    Exercises the actual end-to-end shape: close_session lands the
    record at opening-text slug → maybe_apply_substance_slug pulls it
    forward to the topic-derived slug. Asserts both paths exist /
    don't-exist on disk in the expected sequence.
    """
    chat_id = 142
    now = datetime(2026, 4, 27, 21, 36, tzinfo=timezone.utc)

    active = {
        "session_id": "73fe87fa-0000-0000-0000-000000000000",
        "chat_id": chat_id,
        "started_at": now.isoformat(),
        "last_message_at": now.isoformat(),
        "model": "claude-opus-4-7",
        "transcript": [
            {"role": "user", "content": "Are you awake?"},
            {"role": "assistant", "content": "yes"},
            {
                "role": "user",
                "content": (
                    "I want to draft the Komal Gupta termination response "
                    "letter today. She is challenging the dismissal under "
                    "EI provisions. Need to think through the documented "
                    "chronology carefully and structure the response around "
                    "the warning letters."
                ),
            },
            {"role": "assistant", "content": "let's start with the chronology"},
        ],
        "vault_ops": [],
        "_vault_path_root": talker_config.vault.path,
        "_user_vault_path": "person/Andrew Newton",
        "_stt_model_used": "whisper-large-v3",
        "_session_type": "note",
        "_tool_set": "hypatia",
    }
    state_mgr.set_active(chat_id, active)
    state_mgr.save()
    transcript_snap = list(active["transcript"])
    session_id_snap = active["session_id"]

    rel_path = talker_session.close_session(
        state_mgr,
        vault_path_root=talker_config.vault.path,
        chat_id=chat_id,
        reason="explicit",
        user_vault_path="person/Andrew Newton",
        stt_model_used="whisper-large-v3",
        session_type="note",
        tool_set="hypatia",
    )
    # Opening-text slug as today (close_session is unchanged — it
    # uses ``_first_user_text``, which picks "Are you awake?").
    assert rel_path == (
        "session/conversation-2026-04-27-are-you-awake-73fe87fa.md"
    )

    # Substance-slug pass.
    client = _FakeClient(response_text="komal gupta termination response")
    new_rel = asyncio.run(talker_session.maybe_apply_substance_slug(
        state_mgr,
        enabled=True,
        client=client,
        model="claude-opus-4-7",
        vault_path_root=talker_config.vault.path,
        rel_path=rel_path,
        transcript=transcript_snap,
        session_id=session_id_snap,
    ))
    assert new_rel == (
        "session/conversation-2026-04-27-komal-gupta-termination-response-73fe87fa.md"
    )
    assert not (Path(talker_config.vault.path) / rel_path).exists()
    record = Path(talker_config.vault.path) / new_rel
    assert record.exists()
    post = frontmatter.load(str(record))
    assert "komal-gupta-termination-response" in post.metadata["name"]
    # Hypatia-specific fields preserved through the rename.
    assert post.metadata["mode"] == "conversation"
    assert post.metadata["processed"] is True
