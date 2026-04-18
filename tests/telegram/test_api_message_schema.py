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

from alfred.telegram.conversation import _messages_for_api


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
    transcript = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "searching..."},
                {"type": "tool_use", "id": "t1", "name": "vault_search", "input": {}},
            ],
            "_ts": "2026-04-18T22:33:05Z",
        },
    ]
    cleaned = _messages_for_api(transcript)
    assert len(cleaned) == 1
    assert cleaned[0]["role"] == "assistant"
    assert cleaned[0]["content"][0]["type"] == "text"
    assert cleaned[0]["content"][1]["type"] == "tool_use"
    assert "_ts" not in cleaned[0]


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
