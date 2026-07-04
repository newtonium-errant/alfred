"""Pins for the streaming ``run_turn`` engine (streaming-run-turn sub-arc).

Sub-arc 1 of the Algernon PWA real-time voice loop makes the LLM engine
capable of streaming token/sentence output so a future streaming-TTS consumer
can start speaking on the first sentence — WITHOUT changing any existing
behaviour. The refactor extracts two shared cores from ``run_turn``:

  * ``_prepare_turn``            — user-turn append, capture short-circuit,
                                   system-block + tool-list assembly.
  * ``_process_model_response``  — the tool-use loop + session-record +
                                   escalation side effects (yields tool events
                                   and a terminal continue/finish control).

``run_turn`` (batch) stays on ``client.messages.create`` and consumes the
shared cores — behaviourally byte-identical (the 1500+ existing telegram tests
that mock ``messages.create`` still pass unchanged). ``run_turn_streaming``
drives ``client.messages.stream`` and yields incremental sentence chunks +
tool markers + a terminal ``final`` reply.

The four regression-critical pins (named in the sub-arc brief):

  1. **Batch behavioural-invariance** — over a turn that INCLUDES a mid-turn
     tool call, the batch ``run_turn`` and ``run_turn_streaming`` produce the
     SAME final reply AND the SAME transcript side effects (user turn →
     assistant tool_use → tool_result → assistant text). Proves the batch
     path is unchanged and the streaming path is a faithful mirror.
  2. **Streaming incrementality** — the streaming core yields MULTIPLE ordered
     text chunks that concatenate to the full reply (not one final blob).
  3. **Tool-use interleaving (the crux)** — a turn with a mid-turn tool call
     streams text BEFORE the tool executes AND more text AFTER, across ≥2
     ``messages.stream`` iterations.
  4. **Thinking-not-emitted** — adaptive-thinking deltas never appear in the
     yielded reply text.

The Anthropic streaming client is mocked with a fake ``messages.stream()``
async context manager emitting a scripted event sequence (text deltas →
tool_use block → ``get_final_message()`` with stop_reason=tool_use → a second
stream with more text → final), so the pins are deterministic and never hit
the API — mirroring how the existing suite mocks ``messages.create``.

Log-emission assertions use ``structlog.testing.capture_logs`` per
``feedback_structlog_assertion_patterns.md`` (the streaming loop is async).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import anthropic
import pytest
import structlog

from alfred.telegram import conversation
from alfred.telegram.config import (
    AnthropicConfig,
    InstanceConfig,
    LoggingConfig,
    SessionConfig,
    STTConfig,
    TalkerConfig,
    VaultConfig,
)
from alfred.telegram.conversation import _SentenceChunker
from alfred.telegram.session import Session
from alfred.telegram.state import StateManager


# --- Fixtures --------------------------------------------------------------


def _config(tmp_path: Path) -> TalkerConfig:
    """Salem-shaped single-user config (tool_set == "talker")."""
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir(parents=True, exist_ok=True)
    return TalkerConfig(
        bot_token="x",
        allowed_users=[1],
        primary_users=["person/Test"],
        anthropic=AnthropicConfig(api_key="x", model="claude-opus-4-8"),
        stt=STTConfig(api_key="x", model="whisper-large-v3"),
        session=SessionConfig(state_path=str(tmp_path / "state.json")),
        vault=VaultConfig(path=str(vault_dir)),
        logging=LoggingConfig(file=str(tmp_path / "talker.log")),
        instance=InstanceConfig(name="Salem", tool_set="talker"),
    )


def _session(chat_id: int = 1, session_id: str = "sess-1") -> Session:
    now = datetime(2026, 6, 27, tzinfo=timezone.utc)
    return Session(
        session_id=session_id,
        chat_id=chat_id,
        started_at=now,
        last_message_at=now,
        model="claude-opus-4-8",
    )


# --- Scripted response / stream mocks --------------------------------------


class _TextBlk:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text

    def model_dump(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


class _ToolBlk:
    type = "tool_use"

    def __init__(self, id_: str, name: str, input_: dict[str, Any]) -> None:
        self.id = id_
        self.name = name
        self.input = input_

    def model_dump(self) -> dict[str, Any]:
        return {
            "type": "tool_use",
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


class _FinalMsg:
    """Stands in for a fully-assembled Anthropic ``Message``."""

    def __init__(self, content: list[Any], stop_reason: str) -> None:
        self.content = content
        self.stop_reason = stop_reason


def _text_delta(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="text_delta", text=text),
    )


def _thinking_delta(thinking: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="thinking_delta", thinking=thinking),
    )


def _signature_delta(sig: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="signature_delta", signature=sig),
    )


def _input_json_delta(partial: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial),
    )


def _noise_event(kind: str) -> SimpleNamespace:
    """A non-delta stream event (message_start / content_block_start / …)."""
    return SimpleNamespace(type=kind)


class _FakeStreamCtx:
    """Async context manager mimicking ``client.messages.stream(...)``."""

    def __init__(self, events: list[Any], final: _FinalMsg) -> None:
        self._events = events
        self._final = final

    async def __aenter__(self) -> "_FakeStreamCtx":
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for ev in self._events:
            yield ev

    async def get_final_message(self) -> _FinalMsg:
        return self._final


def _streaming_client(scripts: list[tuple[list[Any], _FinalMsg]]) -> MagicMock:
    """A client whose ``messages.stream(**kw)`` pops one scripted iteration."""
    it = iter(scripts)
    calls: list[dict[str, Any]] = []

    def _stream(**kwargs: Any) -> _FakeStreamCtx:
        calls.append(kwargs)
        events, final = next(it)
        return _FakeStreamCtx(events, final)

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.stream = _stream
    client.messages.stream_calls = calls  # type: ignore[attr-defined]
    return client


def _batch_client(responses: list[_FinalMsg]) -> MagicMock:
    """A client whose ``messages.create`` returns each scripted response."""
    client = MagicMock()
    client.messages = MagicMock()
    client.messages.create = AsyncMock(side_effect=list(responses))
    return client


async def _collect(agen) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    async for ev in agen:
        out.append(ev)
    return out


def _strip_ts(transcript: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the per-turn ``_ts`` wall-clock stamp for cross-run comparison."""
    cleaned: list[dict[str, Any]] = []
    for turn in transcript:
        cleaned.append({k: v for k, v in turn.items() if k != "_ts"})
    return cleaned


# The representative mid-turn-tool-call turn used by the invariance pin:
#   iteration 1: assistant emits pre-tool text + a vault_search tool_use
#   iteration 2: assistant emits the final answer
_PRE_TEXT = "Let me look that up. "
_POST_TEXT = "Found it: the answer is 42."


def _tool_turn_streaming_scripts() -> list[tuple[list[Any], _FinalMsg]]:
    return [
        (
            [
                _noise_event("message_start"),
                _text_delta(_PRE_TEXT),
                _input_json_delta('{"query": "x"}'),
                _noise_event("content_block_stop"),
            ],
            _FinalMsg(
                [_TextBlk(_PRE_TEXT), _ToolBlk("t1", "vault_search", {"query": "x"})],
                "tool_use",
            ),
        ),
        (
            [_text_delta(_POST_TEXT)],
            _FinalMsg([_TextBlk(_POST_TEXT)], "end_turn"),
        ),
    ]


def _tool_turn_batch_responses() -> list[_FinalMsg]:
    return [
        _FinalMsg(
            [_TextBlk(_PRE_TEXT), _ToolBlk("t1", "vault_search", {"query": "x"})],
            "tool_use",
        ),
        _FinalMsg([_TextBlk(_POST_TEXT)], "end_turn"),
    ]


# ---------------------------------------------------------------------------
# Pin 1 — batch behavioural invariance over a mid-turn tool call
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin1_batch_and_streaming_produce_identical_turn(tmp_path, monkeypatch):
    """Over a turn WITH a mid-turn tool call, batch ``run_turn`` and
    ``run_turn_streaming`` yield the SAME reply AND the SAME transcript."""
    monkeypatch.setattr(
        conversation, "_execute_tool", AsyncMock(return_value='{"ok": true}')
    )

    # --- batch path ---
    cfg_b = _config(tmp_path / "b")
    sess_b = _session()
    state_b = StateManager(cfg_b.session.state_path)
    reply_b = await conversation.run_turn(
        client=_batch_client(_tool_turn_batch_responses()),
        state=state_b,
        session=sess_b,
        user_message="what is the answer?",
        config=cfg_b,
        vault_context_str="",
        system_prompt="sys",
    )

    # --- streaming path ---
    cfg_s = _config(tmp_path / "s")
    sess_s = _session()
    state_s = StateManager(cfg_s.session.state_path)
    events = await _collect(
        conversation.run_turn_streaming(
            client=_streaming_client(_tool_turn_streaming_scripts()),
            state=state_s,
            session=sess_s,
            user_message="what is the answer?",
            config=cfg_s,
            vault_context_str="",
            system_prompt="sys",
        )
    )
    finals = [e for e in events if e["type"] == "final"]
    assert len(finals) == 1, f"expected exactly one final event, got {events}"
    reply_s = finals[0]["reply"]

    # Same final reply.
    assert reply_b == _POST_TEXT
    assert reply_s == reply_b, (
        f"streaming reply {reply_s!r} != batch reply {reply_b!r}"
    )

    # Same transcript shape + content (modulo per-turn timestamps).
    tb = _strip_ts(sess_b.transcript)
    ts = _strip_ts(sess_s.transcript)
    assert tb == ts, f"transcript diverged:\n batch={tb}\n stream={ts}"

    # And that transcript is the expected 4-turn shape.
    assert [t["role"] for t in tb] == ["user", "assistant", "user", "assistant"]
    assert tb[1]["content"][1]["type"] == "tool_use"           # mid-turn tool call
    assert tb[2]["content"][0]["type"] == "tool_result"        # its result
    assert tb[2]["content"][0]["tool_use_id"] == "t1"
    assert tb[3]["content"] == [{"type": "text", "text": _POST_TEXT}]

    # The tool actually executed once, on both paths.
    assert conversation._execute_tool.await_count == 2  # once per path


# ---------------------------------------------------------------------------
# Pin 2 — streaming incrementality (multiple chunks, concatenate to reply)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin2_streaming_yields_multiple_chunks_concatenating_to_reply(
    tmp_path,
):
    """A multi-sentence end_turn reply streams as several ordered chunks whose
    concatenation equals the full reply — not one final blob."""
    full = "First sentence here. Second sentence here. Third one."
    scripts = [
        (
            [
                _text_delta("First sentence here. "),
                _text_delta("Second sentence "),
                _text_delta("here. Third one."),
            ],
            _FinalMsg([_TextBlk(full)], "end_turn"),
        ),
    ]
    cfg = _config(tmp_path)
    events = await _collect(
        conversation.run_turn_streaming(
            client=_streaming_client(scripts),
            state=StateManager(cfg.session.state_path),
            session=_session(),
            user_message="tell me three things",
            config=cfg,
            vault_context_str="",
            system_prompt="sys",
        )
    )

    text_chunks = [e["text"] for e in events if e["type"] == "text"]
    finals = [e for e in events if e["type"] == "final"]

    assert len(text_chunks) >= 2, (
        f"expected multiple streamed chunks, got {text_chunks}"
    )
    assert "".join(text_chunks) == full, (
        "streamed chunks must concatenate to the full reply (lossless)"
    )
    assert finals[0]["reply"] == full


# ---------------------------------------------------------------------------
# Pin 3 — tool-use interleaving (the crux): text BEFORE tool + text AFTER
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin3_tool_interleaving_text_before_and_after(tmp_path, monkeypatch):
    """Across ≥2 stream iterations: text is streamed BEFORE the tool executes,
    the tool marker is surfaced mid-stream, then more text is streamed AFTER."""
    monkeypatch.setattr(
        conversation, "_execute_tool", AsyncMock(return_value='{"ok": true}')
    )
    cfg = _config(tmp_path)
    events = await _collect(
        conversation.run_turn_streaming(
            client=_streaming_client(_tool_turn_streaming_scripts()),
            state=StateManager(cfg.session.state_path),
            session=_session(),
            user_message="what is the answer?",
            config=cfg,
            vault_context_str="",
            system_prompt="sys",
        )
    )

    kinds = [e["type"] for e in events]
    tool_idx = kinds.index("tool")
    before = [e for e in events[:tool_idx] if e["type"] == "text"]
    after = [e for e in events[tool_idx + 1:] if e["type"] == "text"]

    # There is a tool event, and it is genuinely mid-stream.
    assert "tool" in kinds
    assert events[tool_idx]["tool"] == "vault_search"
    assert before, "expected streamed text BEFORE the tool executes"
    assert after, "expected streamed text AFTER the tool executes"
    assert "".join(c["text"] for c in before) == _PRE_TEXT
    assert "".join(c["text"] for c in after) == _POST_TEXT

    # Non-vacuous: the tool ran, meaning we crossed ≥2 stream iterations.
    assert conversation._execute_tool.await_count == 1
    assert len(events[-1:]) == 1 and events[-1]["type"] == "final"


@pytest.mark.asyncio
async def test_pin3_demo_prints_interleaved_stream(tmp_path, monkeypatch, capsys):
    """Demo: drive the streaming core over the scripted tool turn and print
    sentence chunks + the tool call as they arrive (run with ``-s`` to watch).

    Doubles as an executable demonstration of incremental output + mid-stream
    tool visibility (the sub-arc DEMO deliverable)."""
    monkeypatch.setattr(
        conversation, "_execute_tool", AsyncMock(return_value='{"ok": true}')
    )
    cfg = _config(tmp_path)
    print("\n--- streaming run_turn demo -------------------------------")
    order: list[str] = []
    async for ev in conversation.run_turn_streaming(
        client=_streaming_client(_tool_turn_streaming_scripts()),
        state=StateManager(cfg.session.state_path),
        session=_session(),
        user_message="what is the answer?",
        config=cfg,
        vault_context_str="",
        system_prompt="sys",
    ):
        if ev["type"] == "text":
            print(f"  [text]  {ev['text']!r}")
            order.append("text")
        elif ev["type"] == "tool":
            print(f"  [tool]  → {ev['tool']} (iteration {ev['iteration']})")
            order.append("tool")
        elif ev["type"] == "final":
            print(f"  [final] {ev['reply']!r}")
            order.append("final")
    print("-----------------------------------------------------------")

    # text before the tool, tool mid-stream, text after, then final.
    assert order == ["text", "tool", "text", "final"]


# ---------------------------------------------------------------------------
# Pin 4 — thinking deltas never appear in the yielded reply text
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pin4_thinking_deltas_not_emitted(tmp_path):
    """Adaptive-thinking (and signature) deltas are consumed but never yielded
    as reply text; only ``text_delta`` content reaches the consumer."""
    secret = "SECRET internal reasoning that must never be spoken"
    visible = "Visible answer only."
    scripts = [
        (
            [
                _noise_event("content_block_start"),
                _thinking_delta(secret),
                _thinking_delta(" more hidden chain of thought"),
                _signature_delta("sig-abc"),
                _noise_event("content_block_stop"),
                _text_delta(visible),
            ],
            _FinalMsg([_TextBlk(visible)], "end_turn"),
        ),
    ]
    cfg = _config(tmp_path)
    events = await _collect(
        conversation.run_turn_streaming(
            client=_streaming_client(scripts),
            state=StateManager(cfg.session.state_path),
            session=_session(),
            user_message="think then answer",
            config=cfg,
            vault_context_str="",
            system_prompt="sys",
        )
    )

    text_chunks = [e["text"] for e in events if e["type"] == "text"]
    final = [e for e in events if e["type"] == "final"][0]

    joined = "".join(text_chunks)
    assert secret not in joined, "thinking content leaked into streamed text"
    assert "hidden chain of thought" not in joined
    assert joined == visible
    assert secret not in final["reply"]
    assert final["reply"] == visible


# ---------------------------------------------------------------------------
# Capture mode + iteration cap + api-error parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_capture_mode_yields_sentinel_no_llm(tmp_path):
    """A capture-type session yields ONLY the capture sentinel as the final
    reply, appends the user turn, and never calls the streaming client."""
    cfg = _config(tmp_path)
    sess = _session()
    client = _streaming_client([])  # no scripts — stream must never be called
    events = await _collect(
        conversation.run_turn_streaming(
            client=client,
            state=StateManager(cfg.session.state_path),
            session=sess,
            user_message="silent capture note",
            config=cfg,
            vault_context_str="",
            system_prompt="sys",
            session_type="capture",
        )
    )
    assert events == [{"type": "final", "reply": conversation.CAPTURE_SENTINEL}]
    assert client.messages.stream_calls == []  # LLM never invoked
    # User turn still recorded so /extract + /brief can see it later.
    assert len(sess.transcript) == 1
    assert sess.transcript[0]["role"] == "user"


@pytest.mark.asyncio
async def test_streaming_iteration_cap(tmp_path, monkeypatch):
    """When the model never ends its turn, the streaming engine hits the
    safety cap, records the warning turn, logs it, and yields the same cap
    reply the batch engine returns."""
    monkeypatch.setattr(conversation, "MAX_TOOL_ITERATIONS", 2)
    monkeypatch.setattr(
        conversation, "_execute_tool", AsyncMock(return_value='{"ok": true}')
    )

    def _always_tool_use(**kwargs):
        return _FakeStreamCtx(
            [_text_delta("looping ")],
            _FinalMsg([_ToolBlk("t1", "vault_search", {"query": "x"})], "tool_use"),
        )

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.stream = _always_tool_use

    cfg = _config(tmp_path)
    sess = _session()
    with structlog.testing.capture_logs() as caplog:
        events = await _collect(
            conversation.run_turn_streaming(
                client=client,
                state=StateManager(cfg.session.state_path),
                session=sess,
                user_message="spin forever",
                config=cfg,
                vault_context_str="",
                system_prompt="sys",
            )
        )

    final = [e for e in events if e["type"] == "final"][0]
    assert final["reply"] == conversation._ITERATION_CAP_WARNING
    # Last transcript turn is the recorded cap warning.
    assert sess.transcript[-1]["role"] == "assistant"
    assert sess.transcript[-1]["content"] == conversation._ITERATION_CAP_WARNING
    cap_logs = [c for c in caplog if c.get("event") == "talker.run_turn.iteration_cap"]
    assert len(cap_logs) == 1
    assert cap_logs[0]["cap"] == 2


@pytest.mark.asyncio
async def test_streaming_api_error_propagates_and_logs(tmp_path):
    """An ``anthropic.APIError`` from the stream propagates (bot.py translates
    it) and is logged as ``talker.api_error`` — parity with the batch engine."""

    class _BoomError(anthropic.APIError):
        def __init__(self) -> None:  # bypass the SDK's arg-heavy __init__
            pass

    class _BoomStream:
        async def __aenter__(self):
            raise _BoomError()

        async def __aexit__(self, *exc):
            return False

    client = MagicMock()
    client.messages = MagicMock()
    client.messages.stream = lambda **kw: _BoomStream()

    cfg = _config(tmp_path)
    with structlog.testing.capture_logs() as caplog:
        with pytest.raises(anthropic.APIError):
            async for _ in conversation.run_turn_streaming(
                client=client,
                state=StateManager(cfg.session.state_path),
                session=_session(),
                user_message="boom",
                config=cfg,
                vault_context_str="",
                system_prompt="sys",
            ):
                pass

    assert any(c.get("event") == "talker.api_error" for c in caplog)


# ---------------------------------------------------------------------------
# _SentenceChunker unit tests (losslessness + boundary behaviour)
# ---------------------------------------------------------------------------


def _feed_all(chunker: _SentenceChunker, pieces: list[str]) -> list[str]:
    out: list[str] = []
    for p in pieces:
        out.extend(chunker.feed(p))
    out.extend(chunker.flush())
    return out


def test_chunker_lossless_across_arbitrary_delta_splits():
    """The concatenation of emitted chunks equals the concatenation of inputs,
    regardless of how the text is split across deltas."""
    full = "Alpha beta. Gamma delta! Epsilon zeta? Final tail with no ender"
    # split mid-word / mid-sentence to stress the buffer
    pieces = ["Alp", "ha be", "ta. Gamma ", "delta! Epsi", "lon zeta? Fin", "al tail with no ender"]
    chunks = _feed_all(_SentenceChunker(), pieces)
    assert "".join(chunks) == full
    assert len(chunks) >= 3  # at least the three complete sentences


def test_chunker_emits_on_sentence_boundaries():
    """Complete sentences flush at ``. `` / ``! `` / ``? ``; the trailing
    partial waits for flush()."""
    chunker = _SentenceChunker()
    first = chunker.feed("Hello there. ")
    assert first == ["Hello there. "]
    # No boundary yet — buffered.
    assert chunker.feed("Still going") == []
    assert chunker.flush() == ["Still going"]


def test_chunker_newline_is_a_boundary():
    chunker = _SentenceChunker()
    assert chunker.feed("line one\nline two") == ["line one\n"]
    assert chunker.flush() == ["line two"]


def test_chunker_trailing_quote_after_ender_is_boundary():
    """An ender followed by a closing quote, then whitespace, is a boundary."""
    chunker = _SentenceChunker()
    out = chunker.feed('She said "hi." Then left.')
    # First sentence includes the closing quote + trailing space.
    assert out[0] == 'She said "hi." '
    assert "".join(out) + "".join(chunker.flush()) == 'She said "hi." Then left.'


def test_chunker_decimal_is_not_a_premature_boundary():
    """``1.5`` must not split at the dot — the dot isn't followed by space."""
    chunker = _SentenceChunker()
    assert chunker.feed("The value is 1.5 exactly.") == []  # no boundary yet
    # boundary appears once trailing whitespace arrives
    assert chunker.feed(" Done.") == ["The value is 1.5 exactly. "]
    assert chunker.flush() == ["Done."]


def test_chunker_empty_feed_is_noop():
    chunker = _SentenceChunker()
    assert chunker.feed("") == []
    assert chunker.flush() == []
