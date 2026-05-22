"""Tests for the talker dispatcher's truncated tool_use detection
(Layer 2 of the Hypatia 2026-05-21 essay-planning fix).

When the Anthropic SDK delivers a tool_use block whose ``input`` was
max_tokens-truncated mid-emission (the prefix arrived; the action
params didn't), the dispatcher MUST:

1. Detect the signature (``stop_reason == "max_tokens"`` AND
   tool_input has identifier keys but no action keys).
2. Emit ``talker.tool.input_truncated`` structured log so the
   operator can grep for the truncation pattern.
3. Synthesize an error tool_result with an actionable message so the
   model can retry with a smaller payload.
4. NOT invoke the downstream op (which would either crash with a
   confusing "missing field" error or — pre-fix — silently no-op).

The detector is currently wired for ``vault_edit`` (the documented
incident); the helper is structured to extend to other tool surfaces
exhibiting the same failure mode.

Per ``feedback_intentionally_left_blank.md`` — silence is ambiguous.
Per ``feedback_log_emission_test_pattern.md`` — log emission must be
pinned alongside behavior. Per ``feedback_structlog_assertion_patterns
.md`` — async code uses ``structlog.testing.capture_logs``.
"""

from __future__ import annotations

import pytest

from alfred.telegram import conversation
from alfred.telegram.conversation import _detect_truncated_tool_input


# ---------------------------------------------------------------------------
# _detect_truncated_tool_input — pure-function unit tests
# ---------------------------------------------------------------------------


class TestDetectTruncatedToolInput:
    def test_vault_edit_only_path_with_max_tokens_detected(self):
        """The exact Hypatia 2026-05-21 signature: vault_edit input
        carries only ``path`` (identifier key) on a max_tokens
        stop_reason — detector fires."""
        diag = _detect_truncated_tool_input(
            "vault_edit",
            {"path": "session/whatever.md"},
            "max_tokens",
        )
        assert diag is not None
        assert diag["tool_name"] == "vault_edit"
        assert diag["received_keys"] == ["path"]
        assert "body_append" in diag["expected_action_keys"]
        assert "body_replace" in diag["expected_action_keys"]
        assert diag["stop_reason"] == "max_tokens"

    def test_vault_edit_with_action_key_not_detected(self):
        """Well-formed vault_edit input (has at least one action
        key) → detector does NOT fire even on max_tokens."""
        diag = _detect_truncated_tool_input(
            "vault_edit",
            {"path": "x.md", "body_append": "stuff"},
            "max_tokens",
        )
        assert diag is None

    def test_vault_edit_only_path_with_tool_use_stop_not_detected(self):
        """Conservative trigger: detector only fires on
        ``stop_reason == "max_tokens"``. A normal ``tool_use`` stop
        with only ``path`` indicates the model deliberately chose
        that shape — let the runtime gate in vault_edit surface it
        (it raises, but as the no-op gate, not as a truncation
        diagnosis)."""
        diag = _detect_truncated_tool_input(
            "vault_edit",
            {"path": "x.md"},
            "tool_use",
        )
        assert diag is None

    def test_vault_edit_only_path_with_end_turn_not_detected(self):
        """Mirror of the above — end_turn is not the truncation
        signature."""
        diag = _detect_truncated_tool_input(
            "vault_edit",
            {"path": "x.md"},
            "end_turn",
        )
        assert diag is None

    def test_vault_edit_empty_input_not_detected(self):
        """No identifier key either → not the prefix-arrived,
        action-truncated signature. The downstream op will surface
        the missing-field error normally."""
        diag = _detect_truncated_tool_input(
            "vault_edit",
            {},
            "max_tokens",
        )
        assert diag is None

    def test_vault_edit_only_set_fields_not_detected(self):
        """Has action key (set_fields), no identifier (path missing).
        Not the truncation signature — the model chose to emit
        action-without-identifier (which the downstream op will
        reject for its own reasons). Detector stays conservative."""
        diag = _detect_truncated_tool_input(
            "vault_edit",
            {"set_fields": {"status": "active"}},
            "max_tokens",
        )
        assert diag is None

    def test_unknown_tool_not_detected(self):
        """Tools without a configured truncation signature are
        passed through to the downstream dispatcher unchanged."""
        diag = _detect_truncated_tool_input(
            "vault_search",
            {"query": "x"},
            "max_tokens",
        )
        assert diag is None

    def test_non_dict_input_not_detected(self):
        """Defensive: non-dict input shouldn't crash the detector."""
        diag = _detect_truncated_tool_input(
            "vault_edit",
            None,  # type: ignore[arg-type]
            "max_tokens",
        )
        assert diag is None
        diag = _detect_truncated_tool_input(
            "vault_edit",
            "not a dict",  # type: ignore[arg-type]
            "max_tokens",
        )
        assert diag is None


# ---------------------------------------------------------------------------
# End-to-end: run_turn with truncated tool_use input
# ---------------------------------------------------------------------------


def _build_run_turn_inputs(tmp_path):
    """Construct minimal run_turn inputs — mirrors the helper in
    test_conversation_tool_loop_race.py."""
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
    now = datetime(2026, 5, 21, 1, 30, tzinfo=timezone.utc)
    session = Session(
        chat_id=1,
        session_id="test-session-truncation",
        started_at=now,
        last_message_at=now,
        model="claude-sonnet-4-6",
    )
    return config, state_mgr, session


async def test_run_turn_truncated_vault_edit_skips_dispatch_and_logs(
    tmp_path, monkeypatch,
):
    """End-to-end repro of the Hypatia 2026-05-21 failure.

    The model emits a vault_edit tool_use with only ``path`` (action
    params truncated by max_tokens), then a second well-formed
    vault_edit. Post-fix:
      * First tool_use: detector fires, dispatch SKIPPED, error
        tool_result synthesized with truncation diagnosis.
      * Second tool_use: detector does NOT fire (has body_append),
        dispatch runs normally.
      * Log ``talker.tool.input_truncated`` fires once with correct
        fields.
      * Transcript stays well-formed (no dangling tool_use ids).
    """
    import structlog
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)

    # First model call: text + 2 tool_use blocks, stop_reason="max_tokens".
    # Block 1: ONLY ``path`` — the truncation signature.
    # Block 2: well-formed (has body_append).
    truncated_response = FakeResponse(
        content=[
            FakeBlock(
                type="text",
                text=(
                    "Writing the essay sections now — three vault_edit "
                    "calls coming."
                ),
            ),
            FakeBlock(
                type="tool_use",
                id="toolu_truncated_01",
                name="vault_edit",
                input={"path": "session/essay-planning.md"},
            ),
            FakeBlock(
                type="tool_use",
                id="toolu_wellformed_02",
                name="vault_edit",
                input={
                    "path": "session/essay-planning.md",
                    "body_append": "Small chunk that fit.",
                },
            ),
        ],
        stop_reason="max_tokens",
    )
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="ok continuing")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([truncated_response, final_response])

    # Stub _execute_tool: it must be called for the well-formed block
    # only (the truncated block is short-circuited by the detector).
    executed_inputs: list[dict] = []

    async def _stub_execute(tool_name, tool_input, *args, **kwargs):
        executed_inputs.append(dict(tool_input))
        return '{"path": "session/essay-planning.md", "fields_changed": ["body"]}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    with structlog.testing.capture_logs() as captured:
        result = await conversation.run_turn(
            client=client,
            state=state_mgr,
            session=session,
            user_message="write the essay sections",
            config=config,
            vault_context_str="",
            system_prompt="test system",
        )

    assert result == "ok continuing"

    # Only the well-formed tool_use reached _execute_tool — the
    # truncated one was short-circuited by the detector.
    assert len(executed_inputs) == 1
    assert executed_inputs[0].get("body_append") == "Small chunk that fit."

    # Log pin: talker.tool.input_truncated fired exactly once with the
    # truncated block's id + the right diagnostic fields.
    matches = [
        c for c in captured
        if c.get("event") == "talker.tool.input_truncated"
    ]
    assert len(matches) == 1, (
        f"expected exactly one input_truncated log; got {len(matches)}: "
        f"{[c.get('event') for c in captured]}"
    )
    log_entry = matches[0]
    assert log_entry["log_level"] == "warning"
    assert log_entry["tool"] == "vault_edit"
    assert log_entry["tool_use_id"] == "toolu_truncated_01"
    assert log_entry["received_keys"] == ["path"]
    assert log_entry["stop_reason"] == "max_tokens"
    assert "body_append" in log_entry["expected_action_keys"]
    assert "detail" in log_entry

    # Transcript pairing pin: BOTH tool_use ids got tool_result blocks
    # (truncated one got the synthetic error; well-formed one got the
    # stubbed result). No dangling.
    assert session.transcript[1]["role"] == "assistant"
    assistant_tool_use_ids = {
        b["id"] for b in session.transcript[1]["content"]
        if b.get("type") == "tool_use"
    }
    assert session.transcript[2]["role"] == "user"
    tool_result_ids = {
        b["tool_use_id"] for b in session.transcript[2]["content"]
    }
    assert assistant_tool_use_ids == tool_result_ids
    assert tool_result_ids == {
        "toolu_truncated_01", "toolu_wellformed_02",
    }

    # The synthetic error block carries is_error=True + an actionable
    # message naming the root cause + expected action keys.
    truncated_result = next(
        b for b in session.transcript[2]["content"]
        if b["tool_use_id"] == "toolu_truncated_01"
    )
    assert truncated_result.get("is_error") is True
    content = truncated_result["content"]
    assert "max_tokens" in content
    assert "truncat" in content.lower()
    # The error message must name body_append so the model knows what
    # action it likely meant to call.
    assert "body_append" in content


async def test_run_turn_truncated_detector_does_not_fire_on_normal_tool_use(
    tmp_path, monkeypatch,
):
    """Negative pin: when stop_reason is ``"tool_use"`` (normal stop),
    even a vault_edit with only ``path`` must NOT trigger the
    truncation detector. The runtime gate inside vault_edit will catch
    the no-op separately — but the dispatcher logs would mis-attribute
    the cause if the detector mis-fired here.
    """
    import structlog
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)

    # vault_edit with only ``path`` BUT stop_reason="tool_use".
    # Detector should NOT fire; dispatch should proceed; the runtime
    # gate inside vault_edit will then raise the no-op VaultError,
    # which _execute_tool wraps as an error tool_result.
    bad_response = FakeResponse(
        content=[
            FakeBlock(
                type="tool_use",
                id="toolu_no_op_01",
                name="vault_edit",
                input={"path": "session/x.md"},
            ),
        ],
        stop_reason="tool_use",
    )
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="acknowledged")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([bad_response, final_response])

    # Stub _execute_tool — we just need to confirm it was called
    # (i.e. the detector did NOT short-circuit).
    invoked = []

    async def _stub_execute(tool_name, tool_input, *args, **kwargs):
        invoked.append(dict(tool_input))
        return '{"error": "downstream no-op gate"}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    with structlog.testing.capture_logs() as captured:
        await conversation.run_turn(
            client=client,
            state=state_mgr,
            session=session,
            user_message="probe",
            config=config,
            vault_context_str="",
            system_prompt="test system",
        )

    # _execute_tool WAS called (dispatch proceeded normally).
    assert len(invoked) == 1
    # Detector log MUST NOT fire on the normal tool_use stop.
    truncation_logs = [
        c for c in captured
        if c.get("event") == "talker.tool.input_truncated"
    ]
    assert len(truncation_logs) == 0


async def test_run_turn_well_formed_max_tokens_tool_use_does_not_fire(
    tmp_path, monkeypatch,
):
    """Negative pin: max_tokens stop with a well-formed tool_use
    (action keys present) must NOT trigger the truncation detector.
    This is the 2026-05-09 fix's bread-and-butter path — long
    well-formed body_append, model hit max_tokens, blocks land
    correctly — we just continue the loop normally."""
    import structlog
    from tests.telegram.conftest import (
        FakeAnthropicClient, FakeBlock, FakeResponse,
    )

    config, state_mgr, session = _build_run_turn_inputs(tmp_path)

    well_formed_response = FakeResponse(
        content=[
            FakeBlock(
                type="tool_use",
                id="toolu_well_01",
                name="vault_edit",
                input={
                    "path": "session/x.md",
                    "body_append": "Large but complete body.",
                },
            ),
        ],
        stop_reason="max_tokens",
    )
    final_response = FakeResponse(
        content=[FakeBlock(type="text", text="finished")],
        stop_reason="end_turn",
    )
    client = FakeAnthropicClient([well_formed_response, final_response])

    invoked = []

    async def _stub_execute(tool_name, tool_input, *args, **kwargs):
        invoked.append(dict(tool_input))
        return '{"path": "session/x.md", "fields_changed": ["body"]}'

    monkeypatch.setattr(conversation, "_execute_tool", _stub_execute)

    with structlog.testing.capture_logs() as captured:
        await conversation.run_turn(
            client=client,
            state=state_mgr,
            session=session,
            user_message="add content",
            config=config,
            vault_context_str="",
            system_prompt="test system",
        )

    # _execute_tool WAS called — dispatch proceeded normally.
    assert len(invoked) == 1
    assert invoked[0].get("body_append") == "Large but complete body."
    # Detector log MUST NOT fire.
    truncation_logs = [
        c for c in captured
        if c.get("event") == "talker.tool.input_truncated"
    ]
    assert len(truncation_logs) == 0
