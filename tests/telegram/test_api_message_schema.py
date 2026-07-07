"""Regression test for the 2026-04-18 wk3-post-ship hotfix.

Anthropic's Messages API strictly validates message schemas and rejects
extra fields like ``_ts`` or ``_kind`` that wk2 stamps onto transcript
turns for session-record rendering. Without sanitization, the API
returns ``400: messages.0._ts: Extra inputs are not permitted``.

``_messages_for_api`` strips underscore-prefixed metadata keys before
sending to Anthropic. Keep this test pinned to catch any future change
that sends the raw transcript directly.
"""
from __future__ import annotations

import json

import structlog

from alfred.telegram.conversation import _blocks_to_jsonable, _messages_for_api


def test_strips_underscore_prefixed_keys():
    transcript = [
        {"role": "user", "content": "hi", "_ts": "2026-04-18T22:33:01Z", "_kind": "voice"},
        {"role": "assistant", "content": "ready", "_ts": "2026-04-18T22:33:05Z"},
    ]
    cleaned = _messages_for_api(transcript)
    assert cleaned == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ready"},
    ]


def test_preserves_standard_keys_even_with_complex_content():
    """Metadata stripping doesn't damage tool_use blocks.

    Includes a matching tool_result so the dangling-tool_use heal
    (race-fix 2026-05-03) is a no-op — keeps this test focused on
    its original purpose (schema preservation).
    """
    transcript = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "searching..."},
                {"type": "tool_use", "id": "t1", "name": "vault_search", "input": {}},
            ],
            "_ts": "2026-04-18T22:33:05Z",
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "[]"},
            ],
            "_ts": "2026-04-18T22:33:06Z",
        },
    ]
    cleaned = _messages_for_api(transcript)
    assert len(cleaned) == 2
    assert cleaned[0]["role"] == "assistant"
    assert cleaned[0]["content"][0]["type"] == "text"
    assert cleaned[0]["content"][1]["type"] == "tool_use"
    assert "_ts" not in cleaned[0]
    assert cleaned[1]["role"] == "user"
    assert cleaned[1]["content"][0]["type"] == "tool_result"
    assert "_ts" not in cleaned[1]


def test_empty_transcript_returns_empty():
    assert _messages_for_api([]) == []


def test_transcript_without_metadata_unchanged():
    transcript = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert _messages_for_api(transcript) == transcript


def test_does_not_mutate_input():
    transcript = [{"role": "user", "content": "hi", "_ts": "2026-04-18T22:33:01Z"}]
    _messages_for_api(transcript)
    assert "_ts" in transcript[0], "input transcript must not be mutated"


# ---------------------------------------------------------------------------
# Assistant content-block whitelist (2026-07-07 live 400 hotfix)
#
# ``block.model_dump()`` emits OUTPUT-ONLY fields the Messages API rejects when
# a stored assistant turn is REPLAYED as history (400: "Extra inputs are not
# permitted"): TextBlock → citations:null (+ parsed_output on streamed blocks),
# ToolUseBlock → caller. _blocks_to_jsonable must whitelist per block type.
# ---------------------------------------------------------------------------


def test_blocks_to_jsonable_strips_output_only_fields_real_sdk_blocks():
    # Round-trip REAL anthropic SDK blocks (not hand-built dicts) — the class of
    # input the fake test clients never produced, which hid this bug.
    from anthropic.types import TextBlock, ToolUseBlock

    text = TextBlock(type="text", text="hello", citations=None)   # citations:null
    tool = ToolUseBlock(type="tool_use", id="t1", name="vault_search",
                        input={"q": "x"})                          # dumps caller:null
    out = _blocks_to_jsonable([text, tool])
    assert out[0] == {"type": "text", "text": "hello"}             # null citations gone
    assert out[1] == {"type": "tool_use", "id": "t1",
                      "name": "vault_search", "input": {"q": "x"}}  # caller gone
    blob = json.dumps(out)
    assert "citations" not in blob and "caller" not in blob


def test_blocks_to_jsonable_strips_parsed_output_streamed_block():
    # parsed_output only appears on STREAMED text blocks (get_final_message with
    # structured outputs) on newer SDKs — the exact live error field. Simulate
    # via a block whose model_dump carries it (version-independent).
    class _StreamedBlock:
        def model_dump(self):
            return {"type": "text", "text": "reply",
                    "citations": None, "parsed_output": {"k": "v"}}

    out = _blocks_to_jsonable([_StreamedBlock()])
    assert out == [{"type": "text", "text": "reply"}]
    assert "parsed_output" not in json.dumps(out)


def test_blocks_to_jsonable_preserves_thinking_signature():
    # Extended-thinking round-trip breaks if the signature is dropped.
    from anthropic.types import ThinkingBlock

    block = ThinkingBlock(type="thinking", thinking="reasoning", signature="SIG123")
    out = _blocks_to_jsonable([block])
    assert out == [{"type": "thinking", "thinking": "reasoning", "signature": "SIG123"}]


def test_blocks_to_jsonable_unknown_type_reduced_and_logged():
    class _Weird:
        def model_dump(self):
            return {"type": "some_future_block", "text": "x", "extra": 1}

    with structlog.testing.capture_logs() as cap:
        out = _blocks_to_jsonable([_Weird()])
    assert out == [{"type": "some_future_block"}]              # reduced, not raw
    events = [c for c in cap if c.get("event") == "chat.block_type_unknown"]
    assert len(events) == 1 and events[0]["block_type"] == "some_future_block"


def test_messages_for_api_sanitizes_dirty_assistant_history():
    # THE live-400 pin (the multi-turn assert that would have caught it): turn-1's
    # stored assistant reply carries output-only fields from an OLDER build's raw
    # model_dump; on turn-2 it is replayed as history. _messages_for_api must
    # strip them so an already-stored dirty transcript recovers WITHOUT a state
    # wipe (the source fix in _blocks_to_jsonable only cleans NEW stores).
    transcript = [
        {"role": "user", "content": "first", "_ts": "t"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "the answer",
             "citations": None, "parsed_output": {"k": 1}},
            {"type": "tool_use", "id": "t1", "name": "vault_search",
             "input": {}, "caller": None},
        ], "_ts": "t"},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "[]"},
        ], "_ts": "t"},
        {"role": "user", "content": "second"},
    ]
    msgs = _messages_for_api(transcript)
    blob = json.dumps(msgs)
    assert "parsed_output" not in blob
    assert "caller" not in blob
    assert "citations" not in blob                     # null citations dropped
    # Assistant blocks cleaned to input fields; user tool_result untouched.
    assert msgs[1]["content"][0] == {"type": "text", "text": "the answer"}
    assert msgs[1]["content"][1] == {
        "type": "tool_use", "id": "t1", "name": "vault_search", "input": {}}
    assert msgs[2]["content"][0] == {
        "type": "tool_result", "tool_use_id": "t1", "content": "[]"}
