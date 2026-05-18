"""Mid-session recap tests — queue #10 (2026-05-18).

Covers:
  * ``BriefRecap`` dataclass shape
  * ``run_brief_recap_structuring`` — LLM-call sibling of
    ``run_batch_structuring`` returning 2 buckets instead of 6
  * ``render_recap_markdown`` — brief vs verbose rendering shape
    (no ALFRED:DYNAMIC markers; Telegram-reply formatted)
  * ``summarize_capture_session_so_far`` — orchestrator that picks
    brief vs verbose, returns markdown string, handles empty
    transcript + LLM errors gracefully

Mock the LLM via the existing FakeAnthropicClient pattern; verify
that the recap path is read-only (no vault writes, no state
mutations).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from alfred.telegram import capture_batch, capture_extract
from tests.telegram.conftest import (
    FakeAnthropicClient, FakeBlock, FakeResponse,
)


# --- BriefRecap dataclass ------------------------------------------------


def test_brief_recap_dataclass_shape() -> None:
    """``BriefRecap`` has exactly two fields: topics + key_insights."""
    recap = capture_batch.BriefRecap()
    assert recap.topics == []
    assert recap.key_insights == []
    # Fields are list[str].
    recap2 = capture_batch.BriefRecap(
        topics=["one", "two"], key_insights=["insight"],
    )
    assert recap2.topics == ["one", "two"]
    assert recap2.key_insights == ["insight"]


def test_brief_recap_is_frozen() -> None:
    """``BriefRecap`` is frozen — accidental mutation raises."""
    recap = capture_batch.BriefRecap(topics=["t1"], key_insights=[])
    with pytest.raises(Exception):
        recap.topics = ["mutated"]  # type: ignore[misc]


# --- run_brief_recap_structuring -----------------------------------------


def _brief_recap_response(
    topics: list[str], key_insights: list[str],
) -> FakeResponse:
    """Build a FakeResponse carrying a brief-recap tool_use block."""
    return FakeResponse(
        content=[
            FakeBlock(
                type="tool_use",
                id="toolu_brief",
                name="emit_brief_recap",
                input={
                    "topics": topics,
                    "key_insights": key_insights,
                },
            ),
        ],
        stop_reason="tool_use",
    )


@pytest.mark.asyncio
async def test_run_brief_recap_returns_brief_recap_dataclass() -> None:
    """LLM tool_use response → ``BriefRecap`` dataclass with the two
    bucket values from the tool input."""
    client = FakeAnthropicClient([
        _brief_recap_response(
            topics=["stoicism", "fencing"],
            key_insights=["control is foundational"],
        ),
    ])
    transcript = [
        {"role": "user", "content": "I'm reading meditations",
         "_ts": "2026-05-18T10:00:00+00:00"},
    ]
    result = await capture_batch.run_brief_recap_structuring(
        client=client, transcript=transcript, model="claude-sonnet-4-6",
    )
    assert isinstance(result, capture_batch.BriefRecap)
    assert result.topics == ["stoicism", "fencing"]
    assert result.key_insights == ["control is foundational"]


@pytest.mark.asyncio
async def test_run_brief_recap_handles_empty_buckets() -> None:
    """Empty buckets in the LLM response → empty lists, not None."""
    client = FakeAnthropicClient([
        _brief_recap_response(topics=[], key_insights=[]),
    ])
    transcript = [{"role": "user", "content": "thinking"}]
    result = await capture_batch.run_brief_recap_structuring(
        client=client, transcript=transcript, model="claude-sonnet-4-6",
    )
    assert result.topics == []
    assert result.key_insights == []


@pytest.mark.asyncio
async def test_run_brief_recap_raises_when_no_tool_use_block() -> None:
    """LLM response without a tool_use block → RuntimeError. The
    caller (``summarize_capture_session_so_far``) catches this and
    renders an operator-facing error."""
    client = FakeAnthropicClient([
        FakeResponse(
            content=[FakeBlock(type="text", text="not a tool call")],
            stop_reason="end_turn",
        ),
    ])
    transcript = [{"role": "user", "content": "x"}]
    with pytest.raises(RuntimeError, match="no tool_use block"):
        await capture_batch.run_brief_recap_structuring(
            client=client, transcript=transcript, model="claude-sonnet-4-6",
        )


@pytest.mark.asyncio
async def test_run_brief_recap_coerces_non_string_items() -> None:
    """Defensive coercion — non-string items in the tool input list
    are filtered out (parity with ``run_batch_structuring``)."""
    client = FakeAnthropicClient([
        FakeResponse(
            content=[
                FakeBlock(
                    type="tool_use",
                    id="toolu_brief",
                    name="emit_brief_recap",
                    input={
                        "topics": ["valid", None, "", 42],
                        "key_insights": "not a list",  # wrong type
                    },
                ),
            ],
            stop_reason="tool_use",
        ),
    ])
    transcript = [{"role": "user", "content": "x"}]
    result = await capture_batch.run_brief_recap_structuring(
        client=client, transcript=transcript, model="claude-sonnet-4-6",
    )
    # ``None`` + ``""`` filtered out by the ``if x`` falsy check; 42
    # coerced to "42" by ``str(x).strip()``. Empty strings drop because
    # ``if x`` is False on "".
    assert "valid" in result.topics
    # Non-list ``key_insights`` → coerced to empty list.
    assert result.key_insights == []


# --- render_recap_markdown — brief ---------------------------------------


def test_render_recap_brief_shape() -> None:
    """Brief mode renders ``## Recap (brief)`` + 2 H3 sections."""
    recap = capture_batch.BriefRecap(
        topics=["topic 1", "topic 2"],
        key_insights=["insight 1"],
    )
    md = capture_batch.render_recap_markdown(recap, mode="brief")
    assert "## Recap (brief)" in md
    assert "### Topics" in md
    assert "### Key Insights" in md
    assert "- topic 1" in md
    assert "- topic 2" in md
    assert "- insight 1" in md


def test_render_recap_brief_empty_buckets_emit_none_placeholder() -> None:
    """Empty bucket renders ``(none)`` per intentionally-left-blank
    discipline — operator sees ``nothing has surfaced`` explicitly
    rather than missing-heading silence."""
    recap = capture_batch.BriefRecap(topics=[], key_insights=[])
    md = capture_batch.render_recap_markdown(recap, mode="brief")
    assert "### Topics" in md
    assert "### Key Insights" in md
    assert "(none)" in md
    # Heading still appears for empty bucket.
    topics_idx = md.index("### Topics")
    none_after_topics = md.index("(none)", topics_idx)
    assert none_after_topics > topics_idx


def test_render_recap_brief_no_alfred_dynamic_markers() -> None:
    """Recap output is for Telegram chat, NOT vault embedding. The
    ``<!-- ALFRED:DYNAMIC -->`` marker wrap that ``render_summary_markdown``
    applies is absent here — distinct rendering contract."""
    recap = capture_batch.BriefRecap(topics=["x"], key_insights=[])
    md = capture_batch.render_recap_markdown(recap, mode="brief")
    assert "ALFRED:DYNAMIC" not in md
    assert "<!--" not in md


def test_render_recap_brief_rejects_structured_summary_input() -> None:
    """Brief mode requires ``BriefRecap``; passing a
    ``StructuredSummary`` raises (operator-facing handler is the only
    caller; it dispatches on mode and supplies the right shape)."""
    summary = capture_batch.StructuredSummary(topics=["x"])
    with pytest.raises(ValueError, match="expects BriefRecap"):
        capture_batch.render_recap_markdown(summary, mode="brief")


# --- render_recap_markdown — verbose -------------------------------------


def test_render_recap_verbose_shape() -> None:
    """Verbose mode renders ``## Recap (verbose)`` + all 6 H3 sections."""
    summary = capture_batch.StructuredSummary(
        topics=["t1"],
        decisions=["d1"],
        open_questions=["q1"],
        action_items=["a1"],
        key_insights=["i1"],
        raw_contradictions=["c1"],
    )
    md = capture_batch.render_recap_markdown(summary, mode="verbose")
    assert "## Recap (verbose)" in md
    for heading in [
        "### Topics",
        "### Decisions",
        "### Open Questions",
        "### Action Items",
        "### Key Insights",
        "### Raw Contradictions",
    ]:
        assert heading in md, f"missing verbose heading: {heading!r}"
    for bullet in ["- t1", "- d1", "- q1", "- a1", "- i1", "- c1"]:
        assert bullet in md


def test_render_recap_verbose_no_re_encounters_section() -> None:
    """Verbose recap does NOT include the ``### Re-encounters`` section
    that the end-of-session ``render_summary_markdown`` adds. Recap is
    mid-session; the re-encounter vault scan is a post-close
    operation that needs the full session record + closed-session
    state."""
    summary = capture_batch.StructuredSummary(topics=["t"])
    md = capture_batch.render_recap_markdown(summary, mode="verbose")
    assert "### Re-encounters" not in md
    assert "Re-encounters" not in md


def test_render_recap_verbose_rejects_brief_recap_input() -> None:
    """Verbose mode requires ``StructuredSummary``; passing a
    ``BriefRecap`` raises."""
    recap = capture_batch.BriefRecap(topics=["x"])
    with pytest.raises(ValueError, match="expects StructuredSummary"):
        capture_batch.render_recap_markdown(recap, mode="verbose")


def test_render_recap_rejects_invalid_mode() -> None:
    """Mode must be exactly ``'brief'`` or ``'verbose'``."""
    recap = capture_batch.BriefRecap()
    with pytest.raises(ValueError, match="must be 'brief' or 'verbose'"):
        capture_batch.render_recap_markdown(recap, mode="medium")


# --- summarize_capture_session_so_far ------------------------------------


@pytest.mark.asyncio
async def test_summarize_brief_returns_markdown() -> None:
    """Brief mode → 2-section markdown via the brief-recap LLM call."""
    client = FakeAnthropicClient([
        _brief_recap_response(
            topics=["stoicism"], key_insights=["dichotomy of control"],
        ),
    ])
    transcript = [
        {"role": "user", "content": "I'm reading Marcus Aurelius",
         "_ts": "2026-05-18T10:00:00+00:00"},
    ]
    md = await capture_extract.summarize_capture_session_so_far(
        client=client, transcript=transcript, model="claude-sonnet-4-6",
        mode="brief",
    )
    assert "## Recap (brief)" in md
    assert "- stoicism" in md
    assert "- dichotomy of control" in md


@pytest.mark.asyncio
async def test_summarize_default_mode_is_brief() -> None:
    """``mode`` defaults to ``"brief"`` — operator's ``/recap`` with no
    args lands here."""
    client = FakeAnthropicClient([
        _brief_recap_response(topics=["x"], key_insights=[]),
    ])
    transcript = [
        {"role": "user", "content": "thinking aloud"},
    ]
    md = await capture_extract.summarize_capture_session_so_far(
        client=client, transcript=transcript, model="claude-sonnet-4-6",
        # no ``mode`` kwarg
    )
    assert "## Recap (brief)" in md


@pytest.mark.asyncio
async def test_summarize_verbose_returns_6_section_markdown() -> None:
    """Verbose mode → full 6-bucket structured summary via the
    end-of-session extraction call, rendered without re-encounter
    section."""
    structured_response = FakeResponse(
        content=[
            FakeBlock(
                type="tool_use",
                id="toolu_verbose",
                name="emit_structured_summary",
                input={
                    "topics": ["stoicism"],
                    "decisions": ["read more"],
                    "open_questions": ["why now"],
                    "action_items": ["log this"],
                    "key_insights": ["control"],
                    "raw_contradictions": [],
                },
            ),
        ],
        stop_reason="tool_use",
    )
    client = FakeAnthropicClient([structured_response])
    transcript = [
        {"role": "user", "content": "I'm reading Marcus Aurelius",
         "_ts": "2026-05-18T10:00:00+00:00"},
    ]
    md = await capture_extract.summarize_capture_session_so_far(
        client=client, transcript=transcript, model="claude-sonnet-4-6",
        mode="verbose",
    )
    assert "## Recap (verbose)" in md
    assert "### Topics" in md
    assert "### Decisions" in md
    assert "### Open Questions" in md
    assert "### Action Items" in md
    assert "### Key Insights" in md
    assert "### Raw Contradictions" in md
    # No re-encounter section in recap.
    assert "Re-encounters" not in md


@pytest.mark.asyncio
async def test_summarize_empty_transcript_returns_placeholder() -> None:
    """0-message session → explicit ``(no captures yet)`` placeholder.

    Per ``feedback_intentionally_left_blank.md``: silence is ambiguous
    (operator sees nothing → can't tell if the recap is broken or
    they haven't said anything yet). Explicit placeholder
    distinguishes the two.
    """
    # FakeAnthropicClient with no pre-canned responses — should NOT be
    # invoked at all because the empty-transcript early exit
    # short-circuits before the LLM call.
    client = FakeAnthropicClient([])
    md = await capture_extract.summarize_capture_session_so_far(
        client=client, transcript=[], model="claude-sonnet-4-6",
        mode="brief",
    )
    assert "(no captures yet" in md
    assert "## Recap (brief)" in md
    # Verify the LLM was NOT called (no message history).
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_summarize_whitespace_only_transcript_returns_placeholder() -> None:
    """Transcript with only whitespace-content turns → same
    ``(no captures yet)`` placeholder."""
    client = FakeAnthropicClient([])
    transcript = [
        {"role": "user", "content": "   "},
        {"role": "user", "content": ""},
    ]
    md = await capture_extract.summarize_capture_session_so_far(
        client=client, transcript=transcript, model="claude-sonnet-4-6",
        mode="brief",
    )
    assert "(no captures yet" in md
    assert client.messages.calls == []


@pytest.mark.asyncio
async def test_summarize_verbose_empty_transcript_returns_placeholder() -> None:
    """Empty transcript + verbose mode → verbose-labeled placeholder."""
    client = FakeAnthropicClient([])
    md = await capture_extract.summarize_capture_session_so_far(
        client=client, transcript=[], model="claude-sonnet-4-6",
        mode="verbose",
    )
    assert "## Recap (verbose)" in md
    assert "(no captures yet" in md


@pytest.mark.asyncio
async def test_summarize_rejects_invalid_mode() -> None:
    """Mode must be one of the canonical values; anything else raises
    BEFORE the LLM call. Caller (the bot handler) validates operator's
    argument and converts garbage to a help message — but the function
    itself is defensive."""
    client = FakeAnthropicClient([])
    with pytest.raises(ValueError, match="must be 'brief' or 'verbose'"):
        await capture_extract.summarize_capture_session_so_far(
            client=client, transcript=[{"role": "user", "content": "x"}],
            model="claude-sonnet-4-6", mode="medium",
        )


@pytest.mark.asyncio
async def test_summarize_llm_error_returns_error_message() -> None:
    """LLM call failure → human-readable error string, NEVER raises.

    The chat handler doesn't need a try/except — the operator sees an
    error message in chat, not a broken bot.
    """
    # FakeAnthropicClient configured to return a text response (no
    # tool_use block) → run_brief_recap_structuring raises RuntimeError
    # inside the orchestrator, which catches it and returns an error
    # markdown.
    client = FakeAnthropicClient([
        FakeResponse(
            content=[FakeBlock(type="text", text="i refuse")],
            stop_reason="end_turn",
        ),
    ])
    transcript = [{"role": "user", "content": "x"}]
    md = await capture_extract.summarize_capture_session_so_far(
        client=client, transcript=transcript, model="claude-sonnet-4-6",
        mode="brief",
    )
    assert "## Recap (brief)" in md
    assert "Recap failed" in md
    assert "/end" in md  # operator-actionable next step


@pytest.mark.asyncio
async def test_summarize_does_not_mutate_transcript() -> None:
    """The recap path is read-only — the input transcript dict isn't
    modified."""
    client = FakeAnthropicClient([
        _brief_recap_response(topics=["x"], key_insights=[]),
    ])
    original = [
        {"role": "user", "content": "thought one"},
        {"role": "user", "content": "thought two"},
    ]
    # Deep-snapshot the input (list-of-dicts).
    snapshot = [dict(t) for t in original]
    await capture_extract.summarize_capture_session_so_far(
        client=client, transcript=original, model="claude-sonnet-4-6",
        mode="brief",
    )
    # Original list-of-dicts is unchanged.
    assert original == snapshot


# --- Constants ------------------------------------------------------------


def test_recap_mode_constants_exposed() -> None:
    """The module exposes ``RECAP_MODE_BRIEF`` + ``RECAP_MODE_VERBOSE``
    string constants for the handler to reference instead of duplicate-
    typing the literal strings."""
    assert capture_extract.RECAP_MODE_BRIEF == "brief"
    assert capture_extract.RECAP_MODE_VERBOSE == "verbose"
