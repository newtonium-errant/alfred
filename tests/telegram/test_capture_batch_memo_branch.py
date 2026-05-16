"""Capture-mode memo branch — ≤1 user message → memo path.

Phase 1 commit 4/5 of the Hypatia Zettelkasten schema cutover.
Per project_hypatia_zettelkasten_redesign.md "LOCKED IMPLEMENTATION
PLAN" → "Auto-maintenance behaviors" → item 10:

    Memo auto-creation: capture session with ≤1 user message at /end →
    branch to memo path; memo/<slug>.md with raw message + session
    pointer. No extraction overhead.

This file pins:
  * _count_user_turns() — turn-count semantics (string content,
    list-of-blocks content, empty turns excluded).
  * _memo_slug_from_text() — filename-safe slug derivation.
  * _memo_body_from_text() — memo template body shape +
    truncation marker.
  * End-to-end branch: process_capture_session with ≤1 user message
    and anchor_scope="hypatia" → memo record created, session updated
    with capture_structured=memo, batch pipeline SKIPPED (no Sonnet
    call fired), follow-up sent.
  * Per-instance gating: anchor_scope="" (Salem) with ≤1 user message
    does NOT branch — continues to batch pipeline (regression guard).
  * Threshold: 2+ user turns does NOT branch (regression guard on
    threshold boundary).
  * Memo-create failure → fallback to batch (failure-isolated).
"""

from __future__ import annotations

from pathlib import Path

import frontmatter
import pytest
import structlog

from alfred.telegram import capture_batch
from tests.telegram.conftest import (
    FakeAnthropicClient, FakeBlock, FakeResponse,
)


# --- _count_user_turns unit tests -----------------------------------------


def test_count_user_turns_zero_when_empty_transcript() -> None:
    assert capture_batch._count_user_turns([]) == 0


def test_count_user_turns_zero_when_all_assistant_turns() -> None:
    transcript = [
        {"role": "assistant", "content": "hi"},
        {"role": "assistant", "content": "anything else?"},
    ]
    assert capture_batch._count_user_turns(transcript) == 0


def test_count_user_turns_one_for_single_user_message() -> None:
    transcript = [
        {"role": "user", "content": "just had a thought about stoicism"},
    ]
    assert capture_batch._count_user_turns(transcript) == 1


def test_count_user_turns_excludes_empty_content_turns() -> None:
    """User turns with empty string content don't count."""
    transcript = [
        {"role": "user", "content": ""},
        {"role": "user", "content": "   "},  # whitespace-only
        {"role": "user", "content": "real content"},
    ]
    assert capture_batch._count_user_turns(transcript) == 1


def test_count_user_turns_handles_list_content() -> None:
    """List-of-blocks content (Anthropic SDK tool-turn shape) counts when a
    text block has content."""
    transcript = [
        {"role": "user", "content": [
            {"type": "text", "text": "from a tool turn"},
        ]},
        {"role": "user", "content": [
            {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
        ]},  # no text block → doesn't count
        {"role": "user", "content": [
            {"type": "text", "text": ""},  # empty text → doesn't count
        ]},
    ]
    assert capture_batch._count_user_turns(transcript) == 1


def test_count_user_turns_two_for_two_user_messages() -> None:
    transcript = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    assert capture_batch._count_user_turns(transcript) == 2


def test_count_user_turns_at_threshold_boundary() -> None:
    """Threshold = 1. Two user messages crosses it."""
    one = [{"role": "user", "content": "alone"}]
    assert capture_batch._count_user_turns(one) <= capture_batch._MEMO_BRANCH_MAX_USER_TURNS
    two = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]
    assert capture_batch._count_user_turns(two) > capture_batch._MEMO_BRANCH_MAX_USER_TURNS


# --- _memo_slug_from_text unit tests --------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("a quick thought about stoicism", "a-quick-thought-about-stoicism"),
    ("Capitalization Doesn't Matter", "capitalization-doesnt-matter"),
    ("punctuation!! gets, stripped.", "punctuation-gets-stripped"),
    ("", "untitled"),
    ("   ", "untitled"),
    ("a", "a"),
])
def test_memo_slug_from_text(text: str, expected: str) -> None:
    assert capture_batch._memo_slug_from_text(text) == expected


def test_memo_slug_caps_at_5_words() -> None:
    """Slug uses first 5 whitespace-delimited tokens."""
    text = "one two three four five six seven eight"
    assert capture_batch._memo_slug_from_text(text) == "one-two-three-four-five"


def test_memo_slug_collapses_dash_runs() -> None:
    """Multiple dashes / consecutive symbols collapse to one dash."""
    text = "a--b...c"
    # Tokens are ["a--b...c"]; non-alphanumeric stripping leaves "abc".
    # Inner punctuation runs collapse — verify we get a clean slug.
    result = capture_batch._memo_slug_from_text(text)
    assert "--" not in result


# --- _memo_body_from_text unit tests --------------------------------------


def test_memo_body_renders_template_shape() -> None:
    """Body has # Memo / # Context / # Tags in order, raw text under
    # Memo."""
    body = capture_batch._memo_body_from_text(
        "an important thought about how I work"
    )
    assert body.index("# Memo") < body.index("# Context")
    assert body.index("# Context") < body.index("# Tags")
    assert "an important thought about how I work" in body


def test_memo_body_strips_whitespace() -> None:
    body = capture_batch._memo_body_from_text("   trimmed text   ")
    assert "trimmed text" in body
    # Leading whitespace was trimmed before insertion.
    lines = [line for line in body.splitlines() if line.strip()]
    assert "trimmed text" in lines


def test_memo_body_truncates_long_text() -> None:
    """Long bodies hit the cap with a truncation marker."""
    long_text = "x" * 5000
    body = capture_batch._memo_body_from_text(long_text)
    assert "truncated" in body.lower()
    # Body is bounded — well under 5000 chars even with scaffolding.
    assert len(body) < 5000


def test_memo_body_no_truncation_on_short_text() -> None:
    body = capture_batch._memo_body_from_text("short")
    assert "truncated" not in body.lower()


# --- End-to-end branch test -----------------------------------------------


def _make_hypatia_vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    for sub in ("session", "memo", "source", "author", "zettel", "note"):
        (vault / sub).mkdir(parents=True)
    return vault


def _write_capture_session(vault: Path, name: str) -> str:
    (vault / "session").mkdir(exist_ok=True, parents=True)
    rel = f"session/{name}.md"
    body = "\n# Transcript\n\n**Andrew** (10:00 · voice): a fleeting thought\n"
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
async def test_memo_branch_creates_record_skipping_batch(tmp_path: Path) -> None:
    """≤1 user message + hypatia scope → memo path. Batch pipeline NOT
    invoked (no Sonnet calls fire)."""
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_capture_session(vault, "capture-2026-05-16-fleeting-aa112233")

    # Fake client with NO pre-canned responses — if the orchestrator
    # tries to fire run_batch_structuring, the fake returns a default
    # "(done)" response. We track .calls instead to confirm no batch
    # call fired.
    client = FakeAnthropicClient([])

    transcript = [
        {"role": "user", "content": "a fleeting thought about stoicism today",
         "_ts": "2026-05-16T10:00:00+00:00"},
    ]

    follow_ups: list[str] = []

    async def _capture_follow_up(text: str) -> None:
        follow_ups.append(text)

    await capture_batch.process_capture_session(
        client=client,
        vault_path=vault,
        session_rel_path=rel,
        transcript=transcript,
        model="claude-sonnet-4-6",
        send_follow_up=_capture_follow_up,
        short_id="aa112233",
        agent_slug="hypatia",
        anchor_scope="hypatia",
    )

    # No Sonnet call fired — batch pipeline skipped entirely.
    assert client.messages.calls == [], (
        f"Memo branch should skip batch — got Sonnet calls: {client.messages.calls}"
    )

    # Memo record exists with the expected shape.
    memo_files = list((vault / "memo").glob("*.md"))
    assert len(memo_files) == 1, (
        f"Expected exactly 1 memo file, got {[f.name for f in memo_files]}"
    )

    memo = frontmatter.load(memo_files[0])
    assert memo["type"] == "memo"
    # Session wikilink set in frontmatter.
    session_link = str(memo["session"])
    assert "session/" in session_link
    assert rel[:-3] in session_link  # path-no-md inside wikilink
    # Raw user text in body.
    assert "a fleeting thought about stoicism today" in memo.content

    # Session record updated.
    sess = frontmatter.load(vault / rel)
    assert sess["capture_structured"] == "memo"
    assert "memo_record" in sess.metadata
    assert "[[memo/" in str(sess["memo_record"])

    # Follow-up sent.
    assert len(follow_ups) == 1
    assert "memo" in follow_ups[0].lower()
    assert "aa112233" in follow_ups[0]


@pytest.mark.asyncio
async def test_memo_branch_does_NOT_trigger_for_salem(tmp_path: Path) -> None:
    """Salem (anchor_scope='') with ≤1 user message continues through the
    batch pipeline. Regression guard — memo branch is Hypatia-only.

    Reason: Salem's scope doesn't carry the ``memo`` create-allowlist
    entry. Adding ``memo`` to Salem is a future decision; until then
    Salem captures continue producing session records via the batch
    pipeline regardless of message count.
    """
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_capture_session(vault, "Voice Session — 2026-05-16 bb223344")

    # Provide a fake response so the batch call succeeds — we expect
    # it to fire (memo branch should NOT trigger for Salem).
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

    transcript = [
        {"role": "user", "content": "single fleeting thought",
         "_ts": "2026-05-16T10:00:00+00:00"},
    ]

    await capture_batch.process_capture_session(
        client=client,
        vault_path=vault,
        session_rel_path=rel,
        transcript=transcript,
        model="claude-sonnet-4-6",
        send_follow_up=None,
        short_id="bb223344",
        agent_slug="salem",
        anchor_scope="",  # Salem default — no memo branch
    )

    # Batch call DID fire.
    assert len(client.messages.calls) == 1, (
        f"Salem path should fire batch — got {len(client.messages.calls)} calls"
    )

    # No memo records created.
    memo_files = list((vault / "memo").glob("*.md"))
    assert memo_files == [], (
        f"Salem should NOT create memo records, got: {[f.name for f in memo_files]}"
    )

    # Session has the batch-path's capture_structured=true (not "memo").
    sess = frontmatter.load(vault / rel)
    assert sess["capture_structured"] == "true"


@pytest.mark.asyncio
async def test_memo_branch_does_NOT_trigger_above_threshold(
    tmp_path: Path,
) -> None:
    """2+ user messages + hypatia scope → batch pipeline. Threshold is
    strictly ≤1 — boundary regression guard.

    Reason: the brief explicitly sets the threshold at ≤1 ("single-
    thought capture"). Two or more user turns indicates the capture
    session warranted multi-turn thinking → structured extraction.
    """
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_capture_session(vault, "capture-2026-05-16-multi-cc334455")

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

    transcript = [
        {"role": "user", "content": "first message",
         "_ts": "2026-05-16T10:00:00+00:00"},
        {"role": "user", "content": "second message about something else",
         "_ts": "2026-05-16T10:01:00+00:00"},
    ]

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
    )

    # Batch fired.
    assert len(client.messages.calls) == 1

    # No memo records.
    memo_files = list((vault / "memo").glob("*.md"))
    assert memo_files == []

    # Session went through normal pipeline.
    sess = frontmatter.load(vault / rel)
    assert sess["capture_structured"] == "true"


# --- Observability — memo log lines ---------------------------------------


@pytest.mark.asyncio
async def test_memo_branch_emits_trigger_log(tmp_path: Path) -> None:
    """``talker.capture.memo_branch_triggered`` log fires when the branch
    activates. Per builder.md pre-commit checklist #9 — log-emission
    tests must drive the production code path.
    """
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_capture_session(vault, "capture-2026-05-16-log-dd445566")

    transcript = [
        {"role": "user", "content": "quick thought",
         "_ts": "2026-05-16T10:00:00+00:00"},
    ]

    with structlog.testing.capture_logs() as captured:
        await capture_batch.process_capture_session(
            client=FakeAnthropicClient([]),
            vault_path=vault,
            session_rel_path=rel,
            transcript=transcript,
            model="claude-sonnet-4-6",
            send_follow_up=None,
            short_id="dd445566",
            agent_slug="hypatia",
            anchor_scope="hypatia",
        )

    trigger_logs = [c for c in captured
                    if c.get("event") == "talker.capture.memo_branch_triggered"]
    assert len(trigger_logs) == 1, (
        f"Expected 1 memo_branch_triggered log, got {len(trigger_logs)}: "
        f"{captured}"
    )
    assert trigger_logs[0]["user_turn_count"] == 1
    assert trigger_logs[0]["anchor_scope"] == "hypatia"
    assert trigger_logs[0]["session_rel_path"] == rel

    done_logs = [c for c in captured
                 if c.get("event") == "talker.capture.memo_done"]
    assert len(done_logs) == 1
    assert done_logs[0]["memo_rel"].startswith("memo/")


# --- Failure-isolation: memo create fails → fall back to batch -----------


@pytest.mark.asyncio
async def test_memo_branch_falls_back_to_batch_on_create_failure(
    tmp_path: Path, monkeypatch,
) -> None:
    """If memo creation fails, the orchestrator falls back to the batch
    pipeline rather than black-holing the session.

    Forced via monkey-patching ``_create_memo_record`` to return None
    (the failure-path signal). The batch path should then run and
    complete normally.
    """
    vault = _make_hypatia_vault(tmp_path)
    rel = _write_capture_session(vault, "capture-2026-05-16-fallback-ee556677")

    # Pre-canned batch response — needs to be available because we
    # expect the orchestrator to fall through.
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

    transcript = [
        {"role": "user", "content": "thought to capture",
         "_ts": "2026-05-16T10:00:00+00:00"},
    ]

    async def _fake_create_memo(*args, **kwargs) -> None:
        return None  # simulate vault_create failure

    monkeypatch.setattr(
        capture_batch, "_create_memo_record", _fake_create_memo,
    )

    with structlog.testing.capture_logs() as captured:
        await capture_batch.process_capture_session(
            client=client,
            vault_path=vault,
            session_rel_path=rel,
            transcript=transcript,
            model="claude-sonnet-4-6",
            send_follow_up=None,
            short_id="ee556677",
            agent_slug="hypatia",
            anchor_scope="hypatia",
        )

    # Memo NOT created.
    memo_files = list((vault / "memo").glob("*.md"))
    assert memo_files == []

    # Batch fired (fall-through).
    assert len(client.messages.calls) == 1

    # Session got the batch path's structured flag.
    sess = frontmatter.load(vault / rel)
    assert sess["capture_structured"] == "true"

    # Fallback log emitted — distinguishes "memo path failed and we
    # recovered" from "memo path never triggered".
    fallback_logs = [c for c in captured
                     if c.get("event") == "talker.capture.memo_branch_fallback_to_batch"]
    assert len(fallback_logs) == 1
