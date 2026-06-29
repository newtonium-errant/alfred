"""Tests for the talker tool-execution-loop race fix (P1 from QA 2026-05-03).

Live bug: Hypatia's conversation wedged twice with API 400
``tool_use ids were found without tool_result blocks immediately
after``. Sequence reconstructed from talker_state.json + alfred.log:

  18:49:18 user: "Create the scaffold for it first"
  18:49:xx Hypatia issued 6 tool_use blocks in one assistant turn
           (vault_edit continuity + 4× vault_create + vault_edit)
  18:49:xx tool execution loop ran; one of the calls raised
           (the assistant turn was already persisted by append_turn)
  18:50:xx user: "1. Yes / 2 yes / 3 yes" (next message arrived)
           run_turn appended user turn AFTER the dangling assistant
  18:51:35 next API call: messages = [..., assistant: 6 tool_use,
                                       user: text] → API 400.
  18:52:xx user retried → same wedge → same 400.

Two-layer fix:

  1. Per-tool try/except in run_turn's execution loop. ANY exception
     from ``_execute_tool`` is caught + a synthetic error
     tool_result block is added to ``tool_results``. The loop
     completes and ``append_turn(state, session, "user",
     tool_results)`` lands a well-formed user turn matching every
     tool_use id. asyncio.CancelledError still propagates BUT only
     after flushing partial results so the transcript is well-formed
     when the daemon comes back up.

  2. Defensive heal in ``_messages_for_api``. Walks the transcript
     before sending; for any assistant tool_use missing a matching
     tool_result in the next turn, injects a synthetic tool_result
     ``is_error: True`` block. Seatbelt for already-corrupted state
     (daemon restart mid-loop, manual edits, future code paths
     that strand a tool_use).

Coverage:

  Heal logic (``_messages_for_api`` / ``_heal_dangling_tool_use``):
    * Well-formed transcript passes through unchanged
    * Single dangling tool_use gets a single synthetic tool_result
    * Multiple dangling tool_use ids in one assistant turn → all healed
    * Partial heal: 3 of 6 tool_use already have tool_results, 3 missing
      → only the 3 missing get synthetic results
    * Dangling tool_use followed by a regular text user message
      (the live bug) → synthetic results inserted BEFORE the user
      message in the API payload, preserving the original
      transcript order
    * Multiple dangling assistant turns → all healed independently
    * Heal is idempotent (running it twice = same output)
    * Heal logs ``conversation.transcript_healed`` warning per
      dangling-turn + a summary log

  Per-tool try/except (run_turn):
    * Tool raises generic Exception → loop catches + appends error
      tool_result + continues with next tool
    * Tool raises CancelledError → partial tool_results flushed +
      raised (for daemon shutdown propagation)
    * Mixed: 6 tools, tool 3 raises, others succeed → 6 tool_result
      blocks land in transcript (3 success + 1 error + 2 success
      ordering preserved)

Uses ``structlog.testing.capture_logs`` per
``feedback_structlog_assertion_patterns.md`` (the run_turn loop is
async; the conversation module logs through structlog).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from alfred.telegram import conversation
from alfred.telegram.conversation import (
    _heal_dangling_tool_use,
    _messages_for_api,
)


# ---------------------------------------------------------------------------
# Heal logic — direct unit tests
# ---------------------------------------------------------------------------


def _assistant_with_tool_use(*tool_use_ids: str) -> dict:
    """Build an assistant message with one tool_use block per id."""
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tid, "name": "vault_search", "input": {}}
            for tid in tool_use_ids
        ],
    }


def _user_with_tool_results(*tool_use_ids: str) -> dict:
    """Build a user message with matching tool_result blocks."""
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": "[]"}
            for tid in tool_use_ids
        ],
    }


def test_well_formed_transcript_passes_through_unchanged():
    """Tool_use + matching tool_result + plain text — heal is a no-op."""
    transcript = [
        {"role": "user", "content": "search vault"},
        _assistant_with_tool_use("t1", "t2"),
        _user_with_tool_results("t1", "t2"),
        {"role": "assistant", "content": "found 3 records"},
    ]
    healed = _heal_dangling_tool_use(transcript)
    assert healed == transcript, (
        f"well-formed transcript must pass unchanged. Got: {healed}"
    )


def test_single_dangling_tool_use_gets_synthetic_result():
    """The minimum repro: assistant has tool_use, no following user turn."""
    transcript = [
        {"role": "user", "content": "go"},
        _assistant_with_tool_use("t1"),
    ]
    healed = _heal_dangling_tool_use(transcript)
    assert len(healed) == 3
    assert healed[0] == transcript[0]
    assert healed[1] == transcript[1]
    # Synthetic user message inserted at the end.
    assert healed[2]["role"] == "user"
    blocks = healed[2]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "tool_result"
    assert blocks[0]["tool_use_id"] == "t1"
    assert blocks[0]["is_error"] is True
    assert "Transcript healed" in blocks[0]["content"]


def test_multiple_dangling_tool_uses_in_one_assistant_turn_all_healed():
    """6 tool_use blocks (Hypatia's QA repro), all dangling → 6 results."""
    transcript = [
        _assistant_with_tool_use("t1", "t2", "t3", "t4", "t5", "t6"),
    ]
    healed = _heal_dangling_tool_use(transcript)
    assert len(healed) == 2
    blocks = healed[1]["content"]
    assert len(blocks) == 6
    assert {b["tool_use_id"] for b in blocks} == {
        "t1", "t2", "t3", "t4", "t5", "t6",
    }
    assert all(b["is_error"] for b in blocks)


def test_partial_dangling_only_missing_ids_healed():
    """3 of 6 tool_use already have tool_results — heal only adds the 3 missing."""
    transcript = [
        _assistant_with_tool_use("t1", "t2", "t3", "t4", "t5", "t6"),
        # Only 3 tool_results landed (t1, t3, t5).
        _user_with_tool_results("t1", "t3", "t5"),
    ]
    healed = _heal_dangling_tool_use(transcript)
    # The healer inserts synthetic blocks IMMEDIATELY after the
    # dangling assistant turn — BEFORE the partial user turn — so
    # the API sees a consistent assistant→user pairing for the
    # missing ids before encountering the partial user turn that
    # carries the rest.
    assert len(healed) == 3
    # Position 0: original assistant
    assert healed[0] == transcript[0]
    # Position 1: synthetic user with the 3 missing results
    synthetic = healed[1]["content"]
    assert {b["tool_use_id"] for b in synthetic} == {"t2", "t4", "t6"}
    assert all(b["is_error"] for b in synthetic)
    # Position 2: original partial user with the 3 successful results
    original_partial = healed[2]["content"]
    assert {b["tool_use_id"] for b in original_partial} == {"t1", "t3", "t5"}


def test_dangling_tool_use_followed_by_text_user_message_qa_repro():
    """Direct repro of the live bug: assistant has dangling tool_use,
    next user message is plain text (not tool_results).

    Pre-fix, the API saw:
      [assistant: 6 tool_use] → [user: "1. Yes / 2 yes / 3 yes"]
    and rejected with the 400 error.

    Post-fix, the heal injects a synthetic tool_result user message
    BEFORE the text user message, so the API sees:
      [assistant: 6 tool_use] → [user: 6 synthetic tool_results]
      → [user: "1. Yes / 2 yes / 3 yes"]
    """
    transcript = [
        {"role": "user", "content": "Create the scaffold for it first"},
        _assistant_with_tool_use("t1", "t2", "t3", "t4", "t5", "t6"),
        # Andrew's natural reply that arrived during tool execution.
        {"role": "user", "content": "1. Yes / 2 yes / 3 yes"},
    ]
    healed = _heal_dangling_tool_use(transcript)
    assert len(healed) == 4
    assert healed[0]["content"] == "Create the scaffold for it first"
    assert healed[1]["content"][0]["type"] == "tool_use"  # original assistant
    # Synthetic user with all 6 tool_results inserted BEFORE the
    # original user text message.
    assert healed[2]["role"] == "user"
    synthetic_blocks = healed[2]["content"]
    assert len(synthetic_blocks) == 6
    assert all(b["type"] == "tool_result" for b in synthetic_blocks)
    assert all(b["is_error"] for b in synthetic_blocks)
    # Original user text message preserved as the next turn.
    assert healed[3]["content"] == "1. Yes / 2 yes / 3 yes"


def test_multiple_dangling_assistant_turns_all_healed():
    """Two separate assistant turns each strand their own tool_use."""
    transcript = [
        _assistant_with_tool_use("t1"),
        _assistant_with_tool_use("t2"),
    ]
    healed = _heal_dangling_tool_use(transcript)
    # 4 messages: assistant1, synthetic1, assistant2, synthetic2
    # Wait — the algorithm appends the assistant first, then checks
    # the NEXT message in the original list. After healing assistant1,
    # the original next message is assistant2 (not a user) → healer
    # treats the assistant1 block as dangling and inserts a synthetic
    # user. Then it processes assistant2 the same way.
    assert len(healed) == 4
    assert healed[0]["content"][0]["id"] == "t1"
    assert healed[1]["content"][0]["tool_use_id"] == "t1"
    assert healed[2]["content"][0]["id"] == "t2"
    assert healed[3]["content"][0]["tool_use_id"] == "t2"


def test_heal_is_idempotent():
    """Running the heal twice produces the same output as once."""
    transcript = [
        _assistant_with_tool_use("t1", "t2"),
        {"role": "user", "content": "follow-up"},
    ]
    healed_once = _heal_dangling_tool_use(transcript)
    healed_twice = _heal_dangling_tool_use(healed_once)
    assert healed_once == healed_twice


def test_heal_emits_warning_log_per_dangling_turn():
    """Per ``feedback_intentionally_left_blank.md``: heal must log loudly.

    Operator greps ``transcript_healed`` to spot the gap-then-recovery
    pattern. Uses ``structlog.testing.capture_logs`` per the
    pattern memo.
    """
    from structlog.testing import capture_logs

    transcript = [_assistant_with_tool_use("t1", "t2")]
    with capture_logs() as captured:
        _heal_dangling_tool_use(transcript)

    heal_logs = [
        c for c in captured
        if c.get("event") == "talker.conversation.transcript_healed"
    ]
    assert len(heal_logs) == 1
    assert heal_logs[0]["log_level"] == "warning"
    assert heal_logs[0]["healed_block_count"] == 2
    assert sorted(heal_logs[0]["dangling_tool_use_ids"]) == ["t1", "t2"]

    # Summary log fires too.
    summary_logs = [
        c for c in captured
        if c.get("event") == "talker.conversation.transcript_heal_summary"
    ]
    assert len(summary_logs) == 1
    assert summary_logs[0]["total_healed"] == 2


def test_messages_for_api_combines_strip_and_heal():
    """End-to-end: _messages_for_api both strips _ts and heals dangling.

    Composition contract: the public entry point applies both
    transformations atomically.
    """
    transcript = [
        {"role": "user", "content": "go", "_ts": "2026-05-03T18:49Z"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
            ],
            "_ts": "2026-05-03T18:50Z",
        },
        # Dangling — no tool_result follows.
    ]
    out = _messages_for_api(transcript)
    # 3 messages: user, assistant, synthetic user
    assert len(out) == 3
    # _ts stripped from every message.
    assert all("_ts" not in m for m in out)
    # Synthetic user has tool_result for t1.
    assert out[2]["role"] == "user"
    assert out[2]["content"][0]["tool_use_id"] == "t1"


def test_heal_handles_string_content_messages_safely():
    """Plain-string-content messages (the wk1 shape) don't trip the heal."""
    transcript = [
        {"role": "user", "content": "hello"},  # string content
        {"role": "assistant", "content": "hi"},  # string content
    ]
    healed = _heal_dangling_tool_use(transcript)
    assert healed == transcript


def test_heal_handles_assistant_with_only_text_blocks():
    """Assistant message with text blocks but no tool_use → no heal."""
    transcript = [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "thinking..."}],
        },
    ]
    healed = _heal_dangling_tool_use(transcript)
    assert healed == transcript


# ---------------------------------------------------------------------------
# Per-tool try/except in run_turn — synthesises error tool_result on failure
# ---------------------------------------------------------------------------
#
# These tests exercise the inner-loop catch by patching ``_execute_tool``
# to raise. The expected behaviour: every tool_use block ends up with a
# matching tool_result in the transcript, even when the underlying
# call exploded. Without this, an exception strands the assistant
# turn (already persisted by append_turn) and the next API call wedges.


def _build_run_turn_inputs(tmp_path):
    """Construct minimal run_turn inputs (config / state / session)."""
    from datetime import datetime, timezone
    from alfred.telegram.config import (
        AnthropicConfig, InstanceConfig, LoggingConfig,
        SessionConfig, STTConfig, TalkerConfig, VaultConfig,
    )
    from alfred.telegram.session import Session
    from alfred.telegram.state import StateManager

    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(exist_ok=True)
    for sub in ("session", "task", "note", "project"):
        (vault_dir / sub).mkdir(exist_ok=True)

    config = TalkerConfig(
        bot_token="test-token",
        allowed_users=[1],
        primary_users=["person/Andrew Newton"],
        anthropic=AnthropicConfig(
            api_key="test-key",
            model="claude-sonnet-4-6",
            max_tokens=1024,
            temperature=1.0,
        ),
        stt=STTConfig(api_key="test-stt", model="whisper-large-v3"),
        session=SessionConfig(
            gap_timeout_seconds=1800,
            state_path=str(tmp_path / "state.json"),
        ),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Hypatia", canonical="Hypatia"),
    )
    state_mgr = StateManager(tmp_path / "talker_state.json")
    state_mgr.load()
    now = datetime(2026, 5, 3, 18, 49, tzinfo=timezone.utc)
    session = Session(
        chat_id=1,
        session_id="test-session-123",
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
    )
    return config, state_mgr, session


async def test_run_turn_synthesises_error_tool_result_when_tool_raises(
    tmp_path, monkeypatch,
):
    """Tool raises generic Exception → loop catches + appends error
    tool_result + continues. Transcript stays well-formed.

    The headline behavioral fix for the QA-finding bug.
    """
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)

    # First model call: tool_use response with 3 tool calls.
    # Second model call (after tool_results land): plain text reply.
    tool_use_response = FakeResponse(
        content=[
            FakeBlock(type="tool_use", id="t1", name="vault_search",
                      input={"query": "x"}),
            FakeBlock(type="tool_use", id="t2", name="vault_search",
                      input={"query": "y"}),
            FakeBlock(type="tool_use", id="t3", name="vault_search",
                      input={"query": "z"}),
        ],
        stop_reason="tool_use",
    )
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="ok done")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([tool_use_response, final_response])

    # Patch _execute_tool: t2 explodes; t1 + t3 succeed.
    call_log: list[str] = []

    async def _stub_execute(tool_name, tool_input, *args, **kwargs):
        tid = tool_input.get("query", "")
        call_log.append(tid)
        if tid == "y":
            raise RuntimeError("simulated vault op explosion")
        return '{"result": "ok"}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    result = await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=session,
        user_message="please run those tools",
        config=config,
        vault_context_str="",
        system_prompt="test system",
    )
    assert result == "ok done"

    # All 3 tool calls were attempted (loop didn't bail on t2's exception).
    assert call_log == ["x", "y", "z"]

    # Transcript: user, assistant (3 tool_use), user (3 tool_results), assistant (text)
    assert len(session.transcript) == 4
    tool_results_msg = session.transcript[2]
    assert tool_results_msg["role"] == "user"
    blocks = tool_results_msg["content"]
    assert len(blocks) == 3
    by_id = {b["tool_use_id"]: b for b in blocks}
    # t1, t3 succeeded.
    assert "is_error" not in by_id["t1"] or by_id["t1"].get("is_error") is False
    assert "is_error" not in by_id["t3"] or by_id["t3"].get("is_error") is False
    # t2 surfaces as an error tool_result.
    assert by_id["t2"]["is_error"] is True
    assert "RuntimeError" in by_id["t2"]["content"]
    assert "simulated vault op explosion" in by_id["t2"]["content"]


async def test_run_turn_propagates_cancelled_after_flushing_partial(
    tmp_path, monkeypatch,
):
    """asyncio.CancelledError must propagate (daemon shutdown semantic),
    but the partial tool_results must be flushed first so the
    transcript is well-formed when the daemon comes back up."""
    import asyncio
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)

    tool_use_response = FakeResponse(
        content=[
            FakeBlock(type="tool_use", id="t1", name="vault_search",
                      input={"query": "x"}),
            FakeBlock(type="tool_use", id="t2", name="vault_search",
                      input={"query": "y"}),
        ],
        stop_reason="tool_use",
    )
    client = FakeAnthropicClient([tool_use_response])

    async def _stub_execute(tool_name, tool_input, *args, **kwargs):
        if tool_input.get("query") == "y":
            raise asyncio.CancelledError()
        return '{"result": "ok"}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    with pytest.raises(asyncio.CancelledError):
        await conversation.run_turn(
            client=client,
            state=state_mgr,
            session=session,
            user_message="run them",
            config=config,
            vault_context_str="",
            system_prompt="test",
        )

    # Partial tool_results turn was flushed before re-raise.
    # Transcript: user, assistant (2 tool_use), user (2 partial tool_results)
    assert len(session.transcript) == 3
    tool_results_msg = session.transcript[2]
    assert tool_results_msg["role"] == "user"
    blocks = tool_results_msg["content"]
    # Both ids represented — t1 succeeded, t2 cancelled (with is_error).
    assert len(blocks) == 2
    by_id = {b["tool_use_id"]: b for b in blocks}
    assert by_id["t2"]["is_error"] is True
    assert "cancelled" in by_id["t2"]["content"].lower()


async def test_run_turn_ordering_preserved_when_middle_tool_raises(
    tmp_path, monkeypatch,
):
    """6 tool calls; tool 3 raises. Result blocks appear in original
    tool_use order in the transcript, not "successes first then
    failures". The Anthropic API doesn't require strict ordering but
    deterministic ordering simplifies test assertions + keeps the
    audit trail readable.
    """
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)
    ids = [f"t{i}" for i in range(1, 7)]
    tool_use_response = FakeResponse(
        content=[
            FakeBlock(
                type="tool_use", id=tid, name="vault_search",
                input={"query": tid},
            )
            for tid in ids
        ],
        stop_reason="tool_use",
    )
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="done")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([tool_use_response, final_response])

    async def _stub_execute(tool_name, tool_input, *args, **kwargs):
        if tool_input.get("query") == "t3":
            raise ValueError("boom")
        return '{"ok": true}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=session,
        user_message="run all 6",
        config=config,
        vault_context_str="",
        system_prompt="test",
    )

    tool_results_msg = session.transcript[2]
    blocks = tool_results_msg["content"]
    # Order is preserved: t1, t2, t3, t4, t5, t6.
    assert [b["tool_use_id"] for b in blocks] == ids
    # t3 is the only error.
    error_ids = [b["tool_use_id"] for b in blocks if b.get("is_error")]
    assert error_ids == ["t3"]


async def test_run_turn_failure_log_emitted_with_diagnostic_fields(
    tmp_path, monkeypatch,
):
    """Per ``feedback_intentionally_left_blank.md`` + the
    subprocess-failure-logging convention: log loudly with structured
    fields when a tool raises so the operator can grep
    ``talker.tool.execute_failed`` to spot the regression class."""
    from structlog.testing import capture_logs
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)
    tool_use_response = FakeResponse(
        content=[FakeBlock(
            type="tool_use", id="t1", name="vault_search", input={"query": "x"},
        )],
        stop_reason="tool_use",
    )
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="done")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([tool_use_response, final_response])

    async def _stub_execute(*args, **kwargs):
        raise IOError("disk full")

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    with capture_logs() as captured:
        await conversation.run_turn(
            client=client,
            state=state_mgr,
            session=session,
            user_message="run",
            config=config,
            vault_context_str="",
            system_prompt="test",
        )

    fail_logs = [
        c for c in captured
        if c.get("event") == "talker.tool.execute_failed"
    ]
    assert len(fail_logs) == 1
    log_entry = fail_logs[0]
    assert log_entry["log_level"] == "warning"
    assert log_entry["tool"] == "vault_search"
    assert log_entry["tool_use_id"] == "t1"
    assert log_entry["error_class"] == "OSError"  # IOError is OSError alias
    assert "disk full" in log_entry["error"]


# ---------------------------------------------------------------------------
# Cancellation flushes the FULL tool_use set (P0 from QA 2026-05-04)
# ---------------------------------------------------------------------------
#
# Pre-fix: CancelledError handler appended a synthetic tool_result for
# only the cancelled tool, then re-raised. Tools AFTER the cancelled
# one never got iterated → tool_use ids dangling on the next API call
# → heal fired and the LLM read the heal's "interrupted before
# completing" wording back to Andrew as a NEW symptom (the recurrence
# Andrew saw 8+ times in his Hypatia DJ-tracker conversation between
# 19:28-19:30 UTC despite yesterday's fix).
#
# Post-fix: the cancel handler synthesises tool_results for EVERY
# remaining tool_use id in the assistant turn before re-raising.


async def test_cancellation_mid_loop_synthesizes_all_unprocessed(
    tmp_path, monkeypatch,
):
    """Assistant turn with 5 tool_use blocks; the 2nd raises
    CancelledError. The flushed tool_results must contain ALL 5
    blocks: 1 real success (t1) + 1 cancelled-synthetic (t2) +
    3 unprocessed-synthetic (t3, t4, t5). No dangling ids; the
    next run_turn's heal must NOT fire on this transcript.
    """
    import asyncio
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)
    ids = [f"t{i}" for i in range(1, 6)]
    tool_use_response = FakeResponse(
        content=[
            FakeBlock(
                type="tool_use", id=tid, name="vault_search",
                input={"query": tid},
            )
            for tid in ids
        ],
        stop_reason="tool_use",
    )
    client = FakeAnthropicClient([tool_use_response])

    async def _stub_execute(tool_name, tool_input, *args, **kwargs):
        if tool_input.get("query") == "t2":
            raise asyncio.CancelledError()
        return '{"ok": true}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    with pytest.raises(asyncio.CancelledError):
        await conversation.run_turn(
            client=client,
            state=state_mgr,
            session=session,
            user_message="run all 5",
            config=config,
            vault_context_str="",
            system_prompt="test",
        )

    # Transcript: user, assistant (5 tool_use), user (5 tool_results).
    assert len(session.transcript) == 3
    tool_results_msg = session.transcript[2]
    assert tool_results_msg["role"] == "user"
    blocks = tool_results_msg["content"]
    assert len(blocks) == 5, (
        f"Expected 5 tool_results (1 success + 1 cancelled + "
        f"3 un-iterated), got {len(blocks)}: "
        f"{[b['tool_use_id'] for b in blocks]}"
    )

    by_id = {b["tool_use_id"]: b for b in blocks}
    # t1 succeeded (no is_error, real content).
    assert by_id["t1"].get("is_error", False) is False
    # t2 cancelled (the one that raised).
    assert by_id["t2"]["is_error"] is True
    assert "cancelled" in by_id["t2"]["content"].lower()
    # t3, t4, t5 are un-iterated cancellation synthetics.
    for tid in ("t3", "t4", "t5"):
        assert by_id[tid]["is_error"] is True
        assert "cancelled before this tool ran" in by_id[tid]["content"], (
            f"{tid} should carry the un-iterated-tail wording, "
            f"got: {by_id[tid]['content']!r}"
        )

    # The heal MUST be a no-op on this transcript — every tool_use
    # has a matching tool_result, so _heal_dangling_tool_use returns
    # the input unchanged. This is the load-bearing assertion: the
    # whole point of the cancellation-flush fix is that the next
    # API call doesn't need the heal at all.
    api_messages = _messages_for_api(session.transcript)
    healed = _heal_dangling_tool_use(api_messages)
    assert healed == api_messages, (
        "Heal added blocks even though the cancellation handler "
        "should have flushed a complete tool_results set. The fix "
        "didn't close the dangling-ids gap."
    )


async def test_cancellation_with_no_remaining_tools(tmp_path, monkeypatch):
    """Edge: assistant with 2 tool_use, the 2nd (last) raises
    CancelledError. There are no un-iterated tools after it, so the
    flushed set is exactly 2 results (1 success + 1 cancelled).
    Backward-compat with the existing
    ``test_run_turn_propagates_cancelled_after_flushing_partial``
    behaviour — the new fix must NOT add extra synthetics when
    nothing's un-iterated."""
    import asyncio
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)
    tool_use_response = FakeResponse(
        content=[
            FakeBlock(
                type="tool_use", id="t1", name="vault_search",
                input={"query": "x"},
            ),
            FakeBlock(
                type="tool_use", id="t2", name="vault_search",
                input={"query": "y"},
            ),
        ],
        stop_reason="tool_use",
    )
    client = FakeAnthropicClient([tool_use_response])

    async def _stub_execute(tool_name, tool_input, *args, **kwargs):
        if tool_input.get("query") == "y":
            raise asyncio.CancelledError()
        return '{"ok": true}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    with pytest.raises(asyncio.CancelledError):
        await conversation.run_turn(
            client=client,
            state=state_mgr,
            session=session,
            user_message="run them",
            config=config,
            vault_context_str="",
            system_prompt="test",
        )

    blocks = session.transcript[2]["content"]
    # Exactly 2 — no extra unprocessed synthetics added when there's
    # nothing un-iterated.
    assert len(blocks) == 2
    by_id = {b["tool_use_id"]: b for b in blocks}
    assert "t1" in by_id and "t2" in by_id
    assert by_id["t2"]["is_error"] is True
    assert "cancelled" in by_id["t2"]["content"].lower()
    # The cancelled block must use the original wording (not the
    # un-iterated-tail wording) since it IS the tool that raised.
    assert "cancelled before this tool ran" not in by_id["t2"]["content"]


async def test_cancellation_flush_emits_diagnostic_log(
    tmp_path, monkeypatch,
):
    """The cancel handler must emit
    ``talker.tool.cancellation_flushed_full_set`` with the count of
    un-iterated tools synthesised. Operator greps this to confirm
    the flush ran AND to see how many tools were stranded."""
    import asyncio
    from structlog.testing import capture_logs
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)
    ids = [f"t{i}" for i in range(1, 5)]  # t1..t4
    tool_use_response = FakeResponse(
        content=[
            FakeBlock(
                type="tool_use", id=tid, name="vault_search",
                input={"query": tid},
            )
            for tid in ids
        ],
        stop_reason="tool_use",
    )
    client = FakeAnthropicClient([tool_use_response])

    async def _stub_execute(tool_name, tool_input, *args, **kwargs):
        if tool_input.get("query") == "t2":
            raise asyncio.CancelledError()
        return '{"ok": true}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    with capture_logs() as captured:
        with pytest.raises(asyncio.CancelledError):
            await conversation.run_turn(
                client=client,
                state=state_mgr,
                session=session,
                user_message="run",
                config=config,
                vault_context_str="",
                system_prompt="test",
            )

    flush_logs = [
        c for c in captured
        if c.get("event") == "talker.tool.cancellation_flushed_full_set"
    ]
    assert len(flush_logs) == 1
    entry = flush_logs[0]
    assert entry["log_level"] == "warning"
    assert entry["cancelled_tool_use_id"] == "t2"
    # 4 total in the turn; t1 already resulted; t2 cancelled →
    # remaining un-iterated = [t3, t4].
    assert entry["unprocessed_tool_use_ids"] == ["t3", "t4"]
    assert entry["unprocessed_count"] == 2
    assert entry["total_tool_use_ids_in_turn"] == 4


# ---------------------------------------------------------------------------
# Startup dangling-tool_use detector (P2 from QA 2026-05-04)
# ---------------------------------------------------------------------------
#
# Permanent observability so future occurrences don't need the LLM's
# parroting of the heal's "interrupted before completing" wording as
# the operator-visible tell. Walks active sessions at daemon boot;
# logs a warning per dangling tool_use detected. Per
# ``feedback_intentionally_left_blank.md``: clean state still gets
# a summary info event so the detector running is observable.


class _StubStateManager:
    """Minimal duck-typed StateManager for the detector tests.

    The real ``StateManager`` exposes ``state.state["active_sessions"]``;
    this stub mirrors only that attribute. Keeps the tests free of the
    full state-load + disk-write plumbing.
    """

    def __init__(self, active_sessions: dict[str, dict]):
        self.state = {"active_sessions": active_sessions}


def test_startup_detects_single_dangling_tool_use():
    """One active session whose final assistant turn carries a
    dangling tool_use id (no matching tool_result in the next user
    turn). Warning fires; summary reports 1 session with 1 dangling
    id."""
    from datetime import datetime, timezone
    from structlog.testing import capture_logs
    from alfred.telegram.conversation import (
        detect_dangling_tool_use_at_startup,
    )

    sessions = {
        "1": {
            "session_id": "sess-with-dangling",
            "last_message_at": "2026-05-04T19:28:00+00:00",
            "transcript": [
                {"role": "user", "content": "do the thing"},
                _assistant_with_tool_use("t1"),
                # NO matching user/tool_result turn — this is dangling.
            ],
        },
    }
    state = _StubStateManager(sessions)

    with capture_logs() as captured:
        total = detect_dangling_tool_use_at_startup(
            state, now=datetime(2026, 5, 4, 19, 30, tzinfo=timezone.utc),
        )

    assert total == 1
    warning_logs = [
        c for c in captured
        if c.get("event") == "talker.conversation.startup_dangling_tool_use"
    ]
    assert len(warning_logs) == 1
    entry = warning_logs[0]
    assert entry["log_level"] == "warning"
    assert entry["chat_id"] == "1"
    assert entry["session_id"] == "sess-with-dangling"
    assert entry["assistant_turn_index"] == 1
    assert entry["dangling_tool_use_ids"] == ["t1"]
    assert entry["count_of_dangling_ids"] == 1
    # 2 minutes elapsed (19:30 - 19:28).
    assert entry["time_since_last_message_seconds"] == 120.0


def test_startup_detects_multiple_dangling_in_same_assistant_turn():
    """Assistant turn with 5 tool_use ids; user-side tool_results
    cover only 2 → detector reports the 3 missing as dangling."""
    from datetime import datetime, timezone
    from structlog.testing import capture_logs
    from alfred.telegram.conversation import (
        detect_dangling_tool_use_at_startup,
    )

    sessions = {
        "1": {
            "session_id": "partial-results-sess",
            "last_message_at": "2026-05-04T19:28:00+00:00",
            "transcript": [
                {"role": "user", "content": "go"},
                _assistant_with_tool_use("t1", "t2", "t3", "t4", "t5"),
                _user_with_tool_results("t1", "t3"),  # missing t2/t4/t5
            ],
        },
    }
    state = _StubStateManager(sessions)

    with capture_logs() as captured:
        total = detect_dangling_tool_use_at_startup(
            state, now=datetime(2026, 5, 4, 19, 30, tzinfo=timezone.utc),
        )

    assert total == 3
    warnings = [
        c for c in captured
        if c.get("event") == "talker.conversation.startup_dangling_tool_use"
    ]
    assert len(warnings) == 1
    assert sorted(warnings[0]["dangling_tool_use_ids"]) == ["t2", "t4", "t5"]
    assert warnings[0]["count_of_dangling_ids"] == 3


def test_startup_clean_state_emits_summary_info_no_warning():
    """Per intentionally-left-blank: clean state STILL emits the
    completion summary so the detector running is observable.
    Asserts no warnings fire on a well-formed transcript."""
    from datetime import datetime, timezone
    from structlog.testing import capture_logs
    from alfred.telegram.conversation import (
        detect_dangling_tool_use_at_startup,
    )

    sessions = {
        "1": {
            "session_id": "well-formed-sess",
            "last_message_at": "2026-05-04T19:28:00+00:00",
            "transcript": [
                {"role": "user", "content": "go"},
                _assistant_with_tool_use("t1", "t2"),
                _user_with_tool_results("t1", "t2"),
                {"role": "assistant", "content": [
                    {"type": "text", "text": "done"},
                ]},
            ],
        },
    }
    state = _StubStateManager(sessions)

    with capture_logs() as captured:
        total = detect_dangling_tool_use_at_startup(
            state, now=datetime(2026, 5, 4, 19, 30, tzinfo=timezone.utc),
        )

    assert total == 0
    warnings = [
        c for c in captured
        if c.get("event") == "talker.conversation.startup_dangling_tool_use"
    ]
    assert warnings == []
    # Summary info still fires.
    summary_logs = [
        c for c in captured
        if c.get("event")
        == "talker.conversation.startup_dangling_tool_use_check_complete"
    ]
    assert len(summary_logs) == 1
    summary = summary_logs[0]
    assert summary["log_level"] == "info"
    assert summary["sessions_checked"] == 1
    assert summary["sessions_with_dangling"] == 0
    assert summary["total_dangling_ids"] == 0


def test_startup_no_active_sessions_emits_summary_quietly():
    """Empty active_sessions dict → summary fires with zeros, no
    warnings. Confirms the detector handles the cold-install case."""
    from datetime import datetime, timezone
    from structlog.testing import capture_logs
    from alfred.telegram.conversation import (
        detect_dangling_tool_use_at_startup,
    )

    state = _StubStateManager({})

    with capture_logs() as captured:
        total = detect_dangling_tool_use_at_startup(
            state, now=datetime(2026, 5, 4, 19, 30, tzinfo=timezone.utc),
        )

    assert total == 0
    summary_logs = [
        c for c in captured
        if c.get("event")
        == "talker.conversation.startup_dangling_tool_use_check_complete"
    ]
    assert len(summary_logs) == 1
    assert summary_logs[0]["sessions_checked"] == 0


def test_startup_multiple_sessions_each_with_dangling():
    """Two sessions, each with their own dangling state. Detector
    walks both, fires per-session warnings, summary aggregates."""
    from datetime import datetime, timezone
    from structlog.testing import capture_logs
    from alfred.telegram.conversation import (
        detect_dangling_tool_use_at_startup,
    )

    sessions = {
        "1": {
            "session_id": "sess-a",
            "last_message_at": "2026-05-04T19:00:00+00:00",
            "transcript": [
                {"role": "user", "content": "x"},
                _assistant_with_tool_use("ta1"),
            ],
        },
        "2": {
            "session_id": "sess-b",
            "last_message_at": "2026-05-04T19:25:00+00:00",
            "transcript": [
                {"role": "user", "content": "y"},
                _assistant_with_tool_use("tb1", "tb2"),
                _user_with_tool_results("tb1"),  # tb2 dangling
            ],
        },
    }
    state = _StubStateManager(sessions)

    with capture_logs() as captured:
        total = detect_dangling_tool_use_at_startup(
            state, now=datetime(2026, 5, 4, 19, 30, tzinfo=timezone.utc),
        )

    assert total == 2  # ta1 + tb2
    warnings = [
        c for c in captured
        if c.get("event") == "talker.conversation.startup_dangling_tool_use"
    ]
    assert len(warnings) == 2
    # Find each by chat_id.
    by_chat = {w["chat_id"]: w for w in warnings}
    assert by_chat["1"]["dangling_tool_use_ids"] == ["ta1"]
    assert by_chat["2"]["dangling_tool_use_ids"] == ["tb2"]
    # Summary aggregates.
    summary = next(
        c for c in captured
        if c.get("event")
        == "talker.conversation.startup_dangling_tool_use_check_complete"
    )
    assert summary["sessions_checked"] == 2
    assert summary["sessions_with_dangling"] == 2
    assert summary["total_dangling_ids"] == 2


def test_startup_handles_session_with_no_transcript_field():
    """Defensive: a session dict missing or with empty transcript
    doesn't crash the detector. Counted as 'checked', not as
    'with_dangling'."""
    from datetime import datetime, timezone
    from structlog.testing import capture_logs
    from alfred.telegram.conversation import (
        detect_dangling_tool_use_at_startup,
    )

    sessions = {
        "1": {
            "session_id": "no-transcript",
            "last_message_at": "2026-05-04T19:28:00+00:00",
            # No "transcript" key at all.
        },
        "2": {
            "session_id": "empty-transcript",
            "last_message_at": "2026-05-04T19:28:00+00:00",
            "transcript": [],
        },
    }
    state = _StubStateManager(sessions)

    with capture_logs() as captured:
        total = detect_dangling_tool_use_at_startup(
            state, now=datetime(2026, 5, 4, 19, 30, tzinfo=timezone.utc),
        )

    assert total == 0
    warnings = [
        c for c in captured
        if c.get("event") == "talker.conversation.startup_dangling_tool_use"
    ]
    assert warnings == []
    summary = next(
        c for c in captured
        if c.get("event")
        == "talker.conversation.startup_dangling_tool_use_check_complete"
    )
    assert summary["sessions_checked"] == 2
    assert summary["sessions_with_dangling"] == 0


def test_startup_handles_malformed_last_message_at_gracefully():
    """If last_message_at is malformed, time_since_last_message_seconds
    is None — but the dangling detection still works."""
    from datetime import datetime, timezone
    from structlog.testing import capture_logs
    from alfred.telegram.conversation import (
        detect_dangling_tool_use_at_startup,
    )

    sessions = {
        "1": {
            "session_id": "malformed-ts",
            "last_message_at": "not a real timestamp",
            "transcript": [
                {"role": "user", "content": "x"},
                _assistant_with_tool_use("t1"),
            ],
        },
    }
    state = _StubStateManager(sessions)

    with capture_logs() as captured:
        detect_dangling_tool_use_at_startup(
            state, now=datetime(2026, 5, 4, 19, 30, tzinfo=timezone.utc),
        )

    warnings = [
        c for c in captured
        if c.get("event") == "talker.conversation.startup_dangling_tool_use"
    ]
    assert len(warnings) == 1
    assert warnings[0]["time_since_last_message_seconds"] is None
    # Detection itself still fires.
    assert warnings[0]["dangling_tool_use_ids"] == ["t1"]


# ---------------------------------------------------------------------------
# WARN-2 (2026-05-09): max_tokens-stop with tool_use blocks must execute
# ---------------------------------------------------------------------------
#
# Live bug from Hypatia voice-profile rebuild 2026-05-09 08:00 UTC. Hypatia
# announced "writing all four files simultaneously" then went silent for 7
# minutes. Andrew had to ping "Progress?" before any output landed.
#
# Diagnosis (talker.log + alfred.log + conversation transcript):
#  - Assistant turn at index 7 contained text + 2 tool_use blocks
#    (toolu_019HW... and toolu_01Uep...) for vault_create.
#  - SDK reported stop_reason="max_tokens" because the response hit the
#    4096-token budget mid-stream (long announcement-paragraph + 2 large
#    vault_create body= contents).
#  - Pre-fix dispatch was ``if stop_reason == "tool_use"``, so the loop
#    fell through to the end-turn branch, persisted the tool_use blocks,
#    and returned the partial text.
#  - The 2026-05-03 _messages_for_api heal kicked in on the NEXT user
#    turn and synthesised tool_results so the API didn't 400 — but the
#    actual tool execution NEVER HAPPENED. User-visible: a multi-minute
#    silent gap with no progress signal.
#
# Fix: dispatch on response content (any tool_use blocks present?) rather
# than stop_reason. The well-formed tool_use blocks the model managed to
# emit get executed normally; the loop continues; the next API call lets
# the model finish whatever was truncated.


async def test_run_turn_executes_tool_use_when_stop_reason_max_tokens(
    tmp_path, monkeypatch,
):
    """The Hypatia 2026-05-09 repro: response carries text + tool_use
    blocks but stop_reason == 'max_tokens'. Pre-fix the loop fell
    through to the end-turn branch and the tool_use blocks dangled.
    Post-fix the tool_use blocks execute normally and the loop
    continues.
    """
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)

    # First model call: text + 2 tool_use blocks, stop_reason="max_tokens".
    # This is the EXACT shape Hypatia hit at 11:00 UTC.
    truncated_response = FakeResponse(
        content=[
            FakeBlock(
                type="text",
                text=(
                    "All nine leaves read. Now writing all four files "
                    "simultaneously — three cluster summaries and the "
                    "overall profile."
                ),
            ),
            FakeBlock(
                type="tool_use",
                id="toolu_019HWTw84hGui9S7yoafLoVq",  # real id from incident
                name="vault_create",
                input={"type": "voice-cluster", "name": "x"},
            ),
            FakeBlock(
                type="tool_use",
                id="toolu_01UepN5njTqXhkdNMRKEbnjH",  # real id from incident
                name="vault_create",
                input={"type": "voice-cluster", "name": "y"},
            ),
        ],
        stop_reason="max_tokens",
    )
    # Second call: model finishes with plain text (the 2 tools succeeded
    # so the next turn just confirms).
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="two created, continuing")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([truncated_response, final_response])

    # Stub _execute_tool: track which tool_use_ids actually got executed.
    executed_ids: list[str] = []

    async def _stub_execute(tool_name, tool_input, *args, **kwargs):
        # Record by name (we only have name+input, not the id) — but
        # the test asserts on the transcript's tool_result blocks below
        # which DO carry the tool_use_id, so this is sufficient.
        executed_ids.append(tool_name)
        return '{"path": "voice/cluster/x.md", "warnings": []}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    result = await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=session,
        user_message="generate cluster summaries",
        config=config,
        vault_context_str="",
        system_prompt="test system",
    )

    # Pre-fix: the function would have returned the partial text from
    # the truncated_response and never called the second response.
    # Post-fix: it executes the 2 tool_use blocks, gets results, calls
    # the model again, returns the final text.
    assert result == "two created, continuing"

    # Both tool_use blocks were executed (not dangling).
    assert len(executed_ids) == 2
    assert executed_ids == ["vault_create", "vault_create"]

    # Transcript: user, assistant (text+2 tool_use), user (2 tool_results),
    # assistant (final text). 4 entries total.
    assert len(session.transcript) == 4
    assert session.transcript[0]["role"] == "user"
    assert session.transcript[1]["role"] == "assistant"
    # The assistant turn carries the 2 tool_use ids we expected.
    assistant_blocks = session.transcript[1]["content"]
    assistant_tool_use_ids = {
        b["id"] for b in assistant_blocks
        if b.get("type") == "tool_use"
    }
    assert assistant_tool_use_ids == {
        "toolu_019HWTw84hGui9S7yoafLoVq",
        "toolu_01UepN5njTqXhkdNMRKEbnjH",
    }
    # Tool_results landed for both — no dangling.
    assert session.transcript[2]["role"] == "user"
    tool_results = session.transcript[2]["content"]
    result_ids = {b["tool_use_id"] for b in tool_results}
    assert result_ids == {
        "toolu_019HWTw84hGui9S7yoafLoVq",
        "toolu_01UepN5njTqXhkdNMRKEbnjH",
    }
    # Both succeeded (no is_error flag).
    for b in tool_results:
        assert b.get("is_error", False) is False
    # Final assistant text present.
    assert session.transcript[3]["role"] == "assistant"


async def test_run_turn_logs_nonstandard_stop_with_tool_use_blocks(
    tmp_path, monkeypatch,
):
    """Observability contract: when the loop dispatches into the
    tool-execution branch on a non-tool_use stop_reason (the new
    2026-05-09 path), it MUST emit
    ``talker.run_turn.tool_use_with_nonstandard_stop`` so the operator
    can grep for the truncation pattern.

    Per ``feedback_intentionally_left_blank.md`` — silence is ambiguous;
    every code path that handles a surprising state must say so.
    Per ``feedback_log_emission_test_pattern.md`` — the test must pin
    the log emission, not just the behavior, so future refactors don't
    silently degrade observability.

    Per ``feedback_structlog_assertion_patterns.md`` — async code uses
    ``structlog.testing.capture_logs``.
    """
    import structlog
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)

    truncated_response = FakeResponse(
        content=[
            FakeBlock(type="text", text="thinking..."),
            FakeBlock(
                type="tool_use", id="t1", name="vault_search",
                input={"query": "x"},
            ),
        ],
        stop_reason="max_tokens",
    )
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="done")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([truncated_response, final_response])

    async def _stub_execute(*args, **kwargs):
        return '{"hits": []}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    with structlog.testing.capture_logs() as captured:
        await conversation.run_turn(
            client=client,
            state=state_mgr,
            session=session,
            user_message="search for x",
            config=config,
            vault_context_str="",
            system_prompt="test system",
        )

    matches = [
        c for c in captured
        if c.get("event") == "talker.run_turn.tool_use_with_nonstandard_stop"
    ]
    assert len(matches) == 1, (
        f"expected exactly one nonstandard-stop log; got {len(matches)}: "
        f"{[c.get('event') for c in captured]}"
    )
    log_entry = matches[0]
    assert log_entry["log_level"] == "warning"
    assert log_entry["stop_reason"] == "max_tokens"
    assert log_entry["tool_use_count"] == 1
    # Field presence pin — operator-grep contract.
    assert "iteration" in log_entry
    assert "detail" in log_entry


async def test_run_turn_normal_tool_use_stop_does_not_log_nonstandard(
    tmp_path, monkeypatch,
):
    """Negative pin: when stop_reason IS 'tool_use', the nonstandard-stop
    log MUST NOT fire. Prevents the log from becoming noise on the
    common-case path.
    """
    import structlog
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)

    tool_use_response = FakeResponse(
        content=[
            FakeBlock(
                type="tool_use", id="t1", name="vault_search",
                input={"query": "x"},
            ),
        ],
        stop_reason="tool_use",  # the normal case
    )
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="done")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([tool_use_response, final_response])

    async def _stub_execute(*args, **kwargs):
        return '{"hits": []}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    with structlog.testing.capture_logs() as captured:
        await conversation.run_turn(
            client=client,
            state=state_mgr,
            session=session,
            user_message="search",
            config=config,
            vault_context_str="",
            system_prompt="test system",
        )

    matches = [
        c for c in captured
        if c.get("event") == "talker.run_turn.tool_use_with_nonstandard_stop"
    ]
    assert matches == [], (
        f"nonstandard-stop log fired on the normal stop_reason='tool_use' "
        f"path; this is noise. Captured: {matches}"
    )


# ---------------------------------------------------------------------------
# on_event status callback (web SSE Tier-1 streaming, 2026-06-29)
# ---------------------------------------------------------------------------
#
# Additive optional ``on_event`` kwarg on run_turn fires
# {phase:'tool', tool, iteration} at the existing talker.tool.invoke point
# so a streaming transport can surface "searching the vault…" frames.
# Default None → byte-identical to pre-feature behaviour (covered implicitly
# by every other test in this file, which never passes on_event).


async def test_run_turn_fires_on_event_per_tool_invocation(
    tmp_path, monkeypatch,
):
    """on_event is awaited once per tool_use block, carrying the tool name
    + iteration, at the real tool-invoke point inside the loop."""
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)

    tool_use_response = FakeResponse(
        content=[
            FakeBlock(type="tool_use", id="t1", name="vault_search",
                      input={"query": "x"}),
            FakeBlock(type="tool_use", id="t2", name="vault_create",
                      input={"type": "note", "name": "y"}),
        ],
        stop_reason="tool_use",
    )
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="all done")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([tool_use_response, final_response])

    async def _stub_execute(*args, **kwargs):
        return '{"ok": true}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    events: list[dict] = []

    async def _on_event(ev):
        events.append(ev)

    result = await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=session,
        user_message="do two things",
        config=config,
        vault_context_str="",
        system_prompt="test",
        on_event=_on_event,
    )
    assert result == "all done"
    # One event per tool_use block, in order, carrying the contract shape.
    assert events == [
        {"phase": "tool", "tool": "vault_search", "iteration": 0},
        {"phase": "tool", "tool": "vault_create", "iteration": 0},
    ]


async def test_run_turn_on_event_exception_does_not_wedge_turn(
    tmp_path, monkeypatch,
):
    """A raising on_event (e.g. dropped SSE client) is swallowed — the turn
    still completes server-side (detach-on-disconnect)."""
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)
    tool_use_response = FakeResponse(
        content=[FakeBlock(type="tool_use", id="t1", name="vault_search",
                           input={"query": "x"})],
        stop_reason="tool_use",
    )
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="completed anyway")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([tool_use_response, final_response])

    async def _stub_execute(*args, **kwargs):
        return '{"ok": true}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    async def _boom_on_event(ev):
        raise ConnectionResetError("client gone")

    result = await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=session,
        user_message="search",
        config=config,
        vault_context_str="",
        system_prompt="test",
        on_event=_boom_on_event,
    )
    assert result == "completed anyway"


async def test_run_turn_max_tokens_no_tool_use_falls_through_to_end_turn(
    tmp_path, monkeypatch,
):
    """The text-only max_tokens case: response has only text blocks,
    no tool_use. The loop should fall through to the end-turn branch
    (return the partial text) — NOT enter the tool-execution branch.

    Prevents a regression where the new content-based dispatch
    accidentally triggers on text-only responses.
    """
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)

    text_only_truncated = FakeResponse(
        content=[
            FakeBlock(
                type="text",
                text="This is a long answer that ran out of budget mid-",
            ),
        ],
        stop_reason="max_tokens",
    )
    client = FakeAnthropicClient([text_only_truncated])

    # _execute_tool MUST NOT be called — text-only path doesn't go
    # through tool execution.
    execute_calls: list[Any] = []

    async def _stub_execute(*args, **kwargs):
        execute_calls.append(args)
        return '{"hits": []}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    result = await conversation.run_turn(
        client=client,
        state=state_mgr,
        session=session,
        user_message="please answer at length",
        config=config,
        vault_context_str="",
        system_prompt="test system",
    )

    # The partial reply is returned to the user.
    assert "This is a long answer" in result
    # No tool execution happened.
    assert execute_calls == []
    # Transcript: user + assistant (the partial text). 2 entries.
    assert len(session.transcript) == 2
