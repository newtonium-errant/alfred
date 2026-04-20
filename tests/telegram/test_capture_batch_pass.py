"""Tests for wk2b commit 3 — async batch structuring pass.

Covers:
    * Happy path: Sonnet emits a tool_use block → ``StructuredSummary``
      parsed, markdown rendered, session record mutated + flag flipped.
    * Missing tool_use block → ``run_batch_structuring`` raises;
      orchestrator writes the failure marker.
    * Schema coercion: non-list bucket values gracefully degrade to [].
    * Idempotent writes: repeat runs replace the existing summary block
      rather than stacking.
    * ``_flatten_transcript`` skips assistant + tool_result turns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alfred.telegram import capture_batch
from alfred.vault import ops
from tests.telegram.conftest import FakeAnthropicClient, FakeBlock, FakeResponse


# --- Helpers --------------------------------------------------------------


def _make_session_record(vault_path: Path, name: str) -> str:
    """Write a minimal session record with a # Transcript body."""
    (vault_path / "session").mkdir(exist_ok=True, parents=True)
    rel = f"session/{name}.md"
    file_path = vault_path / rel
    file_path.write_text(
        "---\n"
        "type: session\n"
        "status: completed\n"
        f"name: {name}\n"
        "created: '2026-04-20'\n"
        "session_type: capture\n"
        "---\n\n"
        "# Transcript\n\n"
        "**Andrew** (10:00 · voice): rambling about Q2 plans\n",
        encoding="utf-8",
    )
    return rel


def _tool_use_response(payload: dict) -> FakeResponse:
    """Return a fake response carrying one tool_use block with ``payload``."""
    return FakeResponse(
        content=[
            FakeBlock(
                type="tool_use",
                id="toolu_xyz",
                name="emit_structured_summary",
                input=payload,
            )
        ],
        stop_reason="tool_use",
    )


# --- run_batch_structuring happy path -------------------------------------


@pytest.mark.asyncio
async def test_run_batch_structuring_parses_tool_use() -> None:
    client = FakeAnthropicClient([
        _tool_use_response({
            "topics": ["Q2 plans", "routing"],
            "decisions": ["switch providers"],
            "open_questions": ["who handles dispatch?"],
            "action_items": ["draft the new SOP"],
            "key_insights": [],
            "raw_contradictions": [],
        })
    ])
    transcript = [
        {"role": "user", "content": "rambling about Q2", "_ts": "2026-04-20T10:00:00+00:00"},
        {"role": "user", "content": "and routing too",   "_ts": "2026-04-20T10:01:00+00:00"},
    ]
    summary = await capture_batch.run_batch_structuring(
        client, transcript, model="claude-sonnet-4-6",
    )

    assert summary.topics == ["Q2 plans", "routing"]
    assert summary.decisions == ["switch providers"]
    assert summary.action_items == ["draft the new SOP"]
    assert summary.key_insights == []

    # Called the right model with tool_choice pinned.
    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-4-6"
    assert call["tool_choice"]["name"] == "emit_structured_summary"


@pytest.mark.asyncio
async def test_run_batch_structuring_missing_tool_use_raises() -> None:
    """No tool_use block in the response → raises RuntimeError."""
    client = FakeAnthropicClient([
        FakeResponse(content=[FakeBlock(type="text", text="nope")]),
    ])
    with pytest.raises(RuntimeError, match="no tool_use block"):
        await capture_batch.run_batch_structuring(
            client, [], model="claude-sonnet-4-6",
        )


@pytest.mark.asyncio
async def test_run_batch_structuring_coerces_non_list_values() -> None:
    """Defensive: string where list expected → empty list, not a crash."""
    client = FakeAnthropicClient([
        _tool_use_response({
            "topics": "not a list",
            "decisions": None,
            "open_questions": [],
            "action_items": ["ok"],
            "key_insights": [],
            "raw_contradictions": [],
        })
    ])
    summary = await capture_batch.run_batch_structuring(
        client, [], model="claude-sonnet-4-6",
    )
    assert summary.topics == []
    assert summary.decisions == []
    assert summary.action_items == ["ok"]


# --- render_summary_markdown ---------------------------------------------


def test_render_summary_markdown_wraps_in_markers_and_has_all_sections() -> None:
    summary = capture_batch.StructuredSummary(
        topics=["A", "B"],
        decisions=["switch"],
        open_questions=[],
        action_items=["do X"],
        key_insights=[],
        raw_contradictions=[],
    )
    md = capture_batch.render_summary_markdown(summary)
    assert md.startswith(capture_batch.SUMMARY_MARKER_START)
    assert md.rstrip().endswith(capture_batch.SUMMARY_MARKER_END)
    assert "## Structured Summary" in md
    # Every heading is present.
    for heading in (
        "### Topics", "### Decisions", "### Open Questions",
        "### Action Items", "### Key Insights", "### Raw Contradictions",
    ):
        assert heading in md
    # Empty buckets render "(none)".
    assert "(none)" in md


# --- write_summary_to_session_record --------------------------------------


@pytest.mark.asyncio
async def test_write_summary_injects_above_transcript_and_flips_flag(
    tmp_path: Path,
) -> None:
    rel = _make_session_record(tmp_path, "Voice Session — 2026-04-20 1000 cap1")
    summary = capture_batch.StructuredSummary(
        topics=["Q2"], action_items=["write SOP"],
    )
    md = capture_batch.render_summary_markdown(summary)

    await capture_batch.write_summary_to_session_record(
        tmp_path, rel, md, "true",
    )

    # Frontmatter flag set.
    import frontmatter
    post = frontmatter.load(tmp_path / rel)
    assert post["capture_structured"] == "true"
    body = post.content

    # Summary block lives ABOVE the transcript heading.
    summary_idx = body.find(capture_batch.SUMMARY_MARKER_START)
    transcript_idx = body.find("# Transcript")
    assert 0 <= summary_idx < transcript_idx
    # Topic content survived the write.
    assert "Q2" in body


@pytest.mark.asyncio
async def test_write_summary_is_idempotent(tmp_path: Path) -> None:
    """Running the write twice produces ONE block, not two."""
    rel = _make_session_record(tmp_path, "Voice Session — 2026-04-20 1001 cap2")
    md1 = capture_batch.render_summary_markdown(
        capture_batch.StructuredSummary(topics=["first pass"])
    )
    md2 = capture_batch.render_summary_markdown(
        capture_batch.StructuredSummary(topics=["second pass"])
    )

    await capture_batch.write_summary_to_session_record(tmp_path, rel, md1, "true")
    await capture_batch.write_summary_to_session_record(tmp_path, rel, md2, "true")

    raw = (tmp_path / rel).read_text(encoding="utf-8")
    # Exactly one start marker.
    assert raw.count(capture_batch.SUMMARY_MARKER_START) == 1
    # Second pass content present, first pass content gone.
    assert "second pass" in raw
    assert "first pass" not in raw


# --- process_capture_session orchestrator --------------------------------


@pytest.mark.asyncio
async def test_process_capture_session_happy_path(tmp_path: Path) -> None:
    """End-to-end: orchestrator runs batch, writes summary, calls follow-up."""
    rel = _make_session_record(tmp_path, "Voice Session — 2026-04-20 1002 cap3")
    client = FakeAnthropicClient([
        _tool_use_response({
            "topics": ["A"],
            "decisions": [],
            "open_questions": [],
            "action_items": [],
            "key_insights": [],
            "raw_contradictions": [],
        })
    ])
    follow_ups: list[str] = []

    async def _sender(text: str) -> None:
        follow_ups.append(text)

    await capture_batch.process_capture_session(
        client=client,
        vault_path=tmp_path,
        session_rel_path=rel,
        transcript=[
            {"role": "user", "content": "hi", "_ts": "2026-04-20T10:00:00+00:00"},
        ],
        model="claude-sonnet-4-6",
        send_follow_up=_sender,
        short_id="cap3",
    )

    import frontmatter
    post = frontmatter.load(tmp_path / rel)
    assert post["capture_structured"] == "true"
    assert "## Structured Summary" in post.content

    # Follow-up was sent with the short-id and /extract hint.
    assert len(follow_ups) == 1
    assert "cap3" in follow_ups[0]
    assert "/extract" in follow_ups[0]


@pytest.mark.asyncio
async def test_process_capture_session_failure_path(tmp_path: Path) -> None:
    """Sonnet error → failure marker written, failure follow-up sent."""
    rel = _make_session_record(tmp_path, "Voice Session — 2026-04-20 1003 cap4")

    class BoomMessages:
        async def create(self, **kwargs):
            raise RuntimeError("rate limited")

    class BoomClient:
        messages = BoomMessages()

    follow_ups: list[str] = []

    async def _sender(text: str) -> None:
        follow_ups.append(text)

    await capture_batch.process_capture_session(
        client=BoomClient(),
        vault_path=tmp_path,
        session_rel_path=rel,
        transcript=[{"role": "user", "content": "hi"}],
        model="claude-sonnet-4-6",
        send_follow_up=_sender,
        short_id="cap4",
    )

    import frontmatter
    post = frontmatter.load(tmp_path / rel)
    assert post["capture_structured"] == "failed"
    assert "Structuring failed" in post.content
    # Failure follow-up surfaces the retry hint.
    assert len(follow_ups) == 1
    assert "Structuring failed" in follow_ups[0]
    assert "cap4" in follow_ups[0]


# --- _flatten_transcript --------------------------------------------------


def test_flatten_transcript_skips_assistant_and_list_content() -> None:
    transcript = [
        {"role": "user", "content": "hello", "_ts": "2026-04-20T10:00:00+00:00"},
        {"role": "assistant", "content": "hi back", "_ts": "2026-04-20T10:00:01+00:00"},
        {"role": "user", "content": [{"type": "tool_result", "content": "..."}]},
        {"role": "user", "content": "real thought", "_ts": "2026-04-20T10:01:00+00:00"},
    ]
    flat = capture_batch._flatten_transcript(transcript)
    assert "hello" in flat
    assert "real thought" in flat
    assert "hi back" not in flat
    # Timestamp formatting: HH:MM prefix.
    assert "[10:00] hello" in flat
    assert "[10:01] real thought" in flat
