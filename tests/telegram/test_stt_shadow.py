"""Regression pins for STT shadow-capture (R1-baseline corpus builder).

Run UNCONDITIONALLY (no module-level importorskip — backends are mocked; the
runtime uses httpx directly, not a vendor SDK). Three layers:

  * capture() unit pins — fake SttBackend stubs (no HTTP): results-shaped
    corpus line, served-result REUSE (no double-spend), NoTranscript →
    both-fresh, per-engine error recording, the ISOLATION backstop.
  * on_voice integration pins — a shadow exception must NOT break the served
    turn (the load-bearing isolation property); disabled → exact back-compat.
  * config + divergence pins — STTConfig loads the shadow_capture block (and
    defaults OFF when omitted); divergence() matches the harness formula.

asyncio_mode=auto (pyproject) → bare ``async def test_`` works.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import structlog

from alfred.telegram import heartbeat, stt_shadow
from alfred.telegram.config import (
    STTConfig,
    SttShadowCaptureConfig,
    load_from_unified,
)
from alfred.telegram.stt_backends import (
    STT_ERR_NETWORK,
    STT_ERR_RATE_LIMIT,
    NoTranscript,
    SttError,
    SttResult,
)

import pytest


# ---------------------------------------------------------------------------
# Fakes + helpers
# ---------------------------------------------------------------------------


class _FakeEngine:
    """Controllable SttBackend stub with a call counter (to assert no
    re-spend / fresh-run). Returns a queued SttResult or raises a queued
    exception."""

    def __init__(
        self, backend_id: str, *, result=None, error=None, timeout_s: float = 10.0,
    ) -> None:
        self.backend_id = backend_id
        self.timeout_s = timeout_s
        self._result = result
        self._error = error
        self.calls = 0

    async def transcribe(self, audio, mime, vocab):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._result


class _GatedEngine:
    """An engine whose transcribe blocks on an asyncio.Event until the test
    releases it — lets a test hold a capture task in-flight to assert it is
    strong-referenced (GC-protected) before completion."""

    def __init__(self, backend_id: str, text: str, gate: "asyncio.Event") -> None:
        self.backend_id = backend_id
        self.timeout_s = 10.0
        self._text = text
        self._gate = gate
        self.calls = 0

    async def transcribe(self, audio, mime, vocab):
        self.calls += 1
        await self._gate.wait()
        return _res(self._text, self.backend_id)


def _res(text: str, backend_id: str, *, latency_ms: int = 100) -> SttResult:
    return SttResult(
        text=text, backend_id=backend_id, tier="comparable",
        latency_ms=latency_ms,
    )


def _cfg(tmp_path: Path, *, enabled: bool = True) -> STTConfig:
    return STTConfig(
        vocab_terms=["RRTS"],
        shadow_capture=SttShadowCaptureConfig(
            enabled=enabled, dir=str(tmp_path / "corpus"),
        ),
    )


def _read_corpus(cfg: STTConfig) -> list[dict]:
    jsonl = Path(cfg.shadow_capture.dir) / "corpus.jsonl"
    if not jsonl.exists():
        return []
    return [
        json.loads(line)
        for line in jsonl.read_text().splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# 1. enabled + captured note → ONE results-shaped line + audio file exists
# ---------------------------------------------------------------------------


async def test_capture_writes_one_results_shaped_line(tmp_path, monkeypatch):
    groq = _FakeEngine("groq-whisper", result=_res("walk the dog", "groq-whisper"))
    dg = _FakeEngine("deepgram", result=_res("walk the dawg", "deepgram"))
    monkeypatch.setattr(stt_shadow, "build_chain", lambda cfg: [groq, dg])
    cfg = _cfg(tmp_path)

    with structlog.testing.capture_logs() as cap:
        await stt_shadow.capture(
            b"OGGDATA", "audio/ogg", None, cfg,
            instance_name="Salem", chat_id=42, duration=3,
        )

    rows = _read_corpus(cfg)
    assert len(rows) == 1
    rec = rows[0]
    # Results-shaped + LITERAL harness keys "groq"/"deepgram".
    assert "audio_file" in rec
    assert "groq" in rec and "deepgram" in rec
    assert rec["groq"]["text"] == "walk the dog"
    assert rec["deepgram"]["text"] == "walk the dawg"
    assert rec["groq"]["error"] is None and rec["deepgram"]["error"] is None
    assert "latency_ms" in rec["groq"] and "latency_ms" in rec["deepgram"]
    assert isinstance(rec["divergence"], float)
    assert rec["instance"] == "Salem"
    assert rec["chat_id"] == 42
    assert rec["duration"] == 3
    assert "ts" in rec
    # Audio file exists in the corpus dir, content-addressed by the record.
    audio = Path(cfg.shadow_capture.dir) / rec["audio_file"]
    assert audio.exists()
    assert audio.read_bytes() == b"OGGDATA"
    # served_result=None → both engines ran fresh.
    assert groq.calls == 1 and dg.calls == 1
    # ILB / log-emission discipline: the success path must emit its signal.
    captured = [c for c in cap if c.get("event") == "stt.shadow_captured"]
    assert len(captured) == 1
    assert captured[0]["instance"] == "Salem"
    assert captured[0]["audio_file"] == rec["audio_file"]
    assert captured[0]["divergence"] == rec["divergence"]


# ---------------------------------------------------------------------------
# 2. served Groq SttResult is REUSED (no double-spend)
# ---------------------------------------------------------------------------


async def test_capture_reuses_served_groq_no_respend(tmp_path, monkeypatch):
    served = _res("served text", "groq-whisper", latency_ms=222)
    groq = _FakeEngine("groq-whisper", result=_res("SHOULD NOT RUN", "groq-whisper"))
    dg = _FakeEngine("deepgram", result=_res("deepgram text", "deepgram"))
    monkeypatch.setattr(stt_shadow, "build_chain", lambda cfg: [groq, dg])
    cfg = _cfg(tmp_path)

    await stt_shadow.capture(
        b"X", "audio/ogg", served, cfg,
        instance_name="Salem", chat_id=1, duration=2,
    )

    assert groq.calls == 0, "served Groq result must be reused, not re-spent"
    assert dg.calls == 1, "the non-served engine runs exactly once"
    rec = _read_corpus(cfg)[0]
    # The served result is reused verbatim (text + latency).
    assert rec["groq"]["text"] == "served text"
    assert rec["groq"]["latency_ms"] == 222
    assert rec["groq"]["error"] is None
    assert rec["deepgram"]["text"] == "deepgram text"


# ---------------------------------------------------------------------------
# 3. served NoTranscript (Groq failed live) → BOTH engines fresh
# ---------------------------------------------------------------------------


async def test_capture_no_transcript_runs_both_fresh(tmp_path, monkeypatch):
    groq = _FakeEngine(
        "groq-whisper",
        error=SttError(STT_ERR_RATE_LIMIT, "429", backend_id="groq-whisper"),
    )
    dg = _FakeEngine("deepgram", result=_res("recovered text", "deepgram"))
    monkeypatch.setattr(stt_shadow, "build_chain", lambda cfg: [groq, dg])
    cfg = _cfg(tmp_path)

    # served_result=None mirrors the NoTranscript / degraded case.
    await stt_shadow.capture(
        b"X", "audio/ogg", None, cfg,
        instance_name="Salem", chat_id=1, duration=2,
    )

    assert groq.calls == 1 and dg.calls == 1, "both engines run fresh"
    rec = _read_corpus(cfg)[0]
    assert rec["groq"]["error"] == STT_ERR_RATE_LIMIT
    assert rec["groq"]["text"] == ""
    assert rec["deepgram"]["text"] == "recovered text"
    assert rec["deepgram"]["error"] is None


# ---------------------------------------------------------------------------
# 4a. ISOLATION (per-engine): each engine raising is RECORDED, not fatal
# ---------------------------------------------------------------------------


async def test_capture_records_per_engine_errors_without_raising(
    tmp_path, monkeypatch,
):
    """A shadow engine that raises SttError AND one that raises an arbitrary
    Exception → both recorded in the line; capture does not re-raise and the
    sibling engine's result is preserved."""
    groq = _FakeEngine(
        "groq-whisper",
        error=SttError(STT_ERR_NETWORK, "down", backend_id="groq-whisper"),
    )
    dg = _FakeEngine("deepgram", error=ValueError("boom"))
    monkeypatch.setattr(stt_shadow, "build_chain", lambda cfg: [groq, dg])
    cfg = _cfg(tmp_path)

    # Must not raise.
    await stt_shadow.capture(
        b"X", "audio/ogg", None, cfg,
        instance_name="Salem", chat_id=1, duration=2,
    )

    rec = _read_corpus(cfg)[0]
    assert rec["groq"]["error"] == STT_ERR_NETWORK   # classified SttError
    assert rec["deepgram"]["error"].startswith("unknown:")  # arbitrary exc
    assert rec["groq"]["text"] == "" and rec["deepgram"]["text"] == ""


# ---------------------------------------------------------------------------
# 4b. ISOLATION (top-level): a capture-level failure → swallowed + logged
# ---------------------------------------------------------------------------


async def test_capture_top_level_failure_swallowed_and_logged(
    tmp_path, monkeypatch,
):
    """A failure OUTSIDE the per-engine try (here: build_chain itself raises)
    → capture swallows it, logs stt.shadow_capture_failed, never re-raises."""
    def _boom(cfg):
        raise RuntimeError("chain construction exploded")

    monkeypatch.setattr(stt_shadow, "build_chain", _boom)
    cfg = _cfg(tmp_path)

    with structlog.testing.capture_logs() as cap:
        # Must not raise.
        await stt_shadow.capture(
            b"X", "audio/ogg", None, cfg,
            instance_name="Salem", chat_id=1, duration=2,
        )

    failed = [c for c in cap if c.get("event") == "stt.shadow_capture_failed"]
    assert len(failed) == 1
    assert failed[0]["error_type"] == "RuntimeError"
    assert failed[0]["instance"] == "Salem"
    # No corpus line was written (the failure aborted before the write).
    assert _read_corpus(cfg) == []


# ---------------------------------------------------------------------------
# 4c. ISOLATION (on_voice integration): a shadow exception cannot break the
#     served turn — handle_message still receives the served text.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_counter():
    heartbeat.reset()
    yield
    heartbeat.reset()


class _FakeVoiceFile:
    async def download_as_bytearray(self) -> bytearray:
        return bytearray(b"FAKE-OGG-BYTES")


class _FakeVoice:
    def __init__(self, duration: int = 3) -> None:
        self.duration = duration

    async def get_file(self) -> _FakeVoiceFile:
        return _FakeVoiceFile()


def _build_update_and_ctx(talker_config, *, user_id: int = 1):
    reply = AsyncMock()
    update = type("U", (), {})()
    update.message = type("M", (), {})()
    update.message.voice = _FakeVoice()
    update.message.reply_text = reply
    update.effective_chat = type("C", (), {"id": 1})()
    update.effective_user = type("EU", (), {"id": user_id})()

    ctx = type("Ctx", (), {})()
    ctx.application = type("App", (), {"bot_data": {
        "config": talker_config,
        "state_mgr": None,
        "anthropic_client": None,
        "system_prompt": "",
        "vault_context_str": "",
        "chat_locks": {},
    }})()
    ctx.bot = type("B", (), {})()
    return update, ctx, reply


def _patch_router(monkeypatch, return_value):
    from alfred.telegram import bot

    async def _fake_router(*args: Any, **kwargs: Any):
        return return_value

    monkeypatch.setattr(bot.stt_backends, "build_chain", lambda cfg: [])
    monkeypatch.setattr(
        bot.stt_backends, "transcribe_with_fallback", _fake_router,
    )


async def test_on_voice_shadow_exception_does_not_break_served_turn(
    talker_config, monkeypatch,
):
    """THE load-bearing isolation pin: even when shadow-capture raises, the
    served Groq transcript still reaches handle_message and the user gets no
    error — the capture is fire-and-forget + fully isolated."""
    from alfred.telegram import bot

    captured: dict[str, Any] = {}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)
    _patch_router(monkeypatch, SttResult(
        text="walk the dog", backend_id="groq-whisper", tier="comparable",
    ))

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("shadow disk on fire")

    monkeypatch.setattr(bot.stt_shadow, "capture", _boom)

    update, ctx, reply = _build_update_and_ctx(talker_config)
    with structlog.testing.capture_logs() as cap:
        await bot.on_voice(update, ctx)
        # Drain the fire-and-forget task + its done-callback.
        for _ in range(5):
            await asyncio.sleep(0)

    # The served turn proceeded normally despite the shadow blowing up.
    assert captured.get("kwargs", {}).get("text") == "walk the dog"
    assert captured["kwargs"]["voice"] is True
    reply.assert_not_called()
    # The done-callback surfaced the orphaned task's exception.
    errs = [c for c in cap if c.get("event") == "stt.shadow_capture_task_error"]
    assert len(errs) == 1
    assert errs[0]["error_type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# 5. disabled → no-op (no files, build_chain not called); on_voice back-compat
# ---------------------------------------------------------------------------


async def test_capture_disabled_is_noop(tmp_path, monkeypatch):
    spy = {"n": 0}

    def _spy_build_chain(cfg):
        spy["n"] += 1
        return []

    monkeypatch.setattr(stt_shadow, "build_chain", _spy_build_chain)
    cfg = _cfg(tmp_path, enabled=False)

    with structlog.testing.capture_logs() as cap:
        await stt_shadow.capture(
            b"X", "audio/ogg", None, cfg,
            instance_name="Salem", chat_id=1, duration=2,
        )

    assert spy["n"] == 0, "disabled capture must short-circuit before any work"
    assert not Path(cfg.shadow_capture.dir).exists(), "no corpus dir created"
    # Silent when disabled (event-driven; no idle tick).
    assert not [c for c in cap if c.get("event", "").startswith("stt.shadow")]


async def test_on_voice_disabled_shadow_back_compat(talker_config, monkeypatch):
    """With shadow-capture absent/disabled (the talker_config default), the
    served turn behaves EXACTLY as today and nothing is written."""
    from alfred.telegram import bot

    assert talker_config.stt.shadow_capture.enabled is False  # fixture default

    captured: dict[str, Any] = {}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        captured["kwargs"] = kwargs

    # Spy the real capture path: it should be invoked but be a no-op.
    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)
    _patch_router(monkeypatch, SttResult(
        text="hello there", backend_id="groq-whisper", tier="comparable",
    ))

    update, ctx, reply = _build_update_and_ctx(talker_config)
    await bot.on_voice(update, ctx)
    for _ in range(5):
        await asyncio.sleep(0)

    assert captured["kwargs"]["text"] == "hello there"
    assert captured["kwargs"]["voice"] is True
    reply.assert_not_called()
    # Default dir is relative to cwd; disabled capture never touches it.
    assert not (Path(talker_config.stt.shadow_capture.dir) / "corpus.jsonl").exists()


# ---------------------------------------------------------------------------
# 5b. KEEP-ALIVE (reviewer WARN fix): the reprompt-path capture must NOT be
#     GC-dropped — the task is strong-ref'd while in-flight and completes.
# ---------------------------------------------------------------------------


async def test_reprompt_path_capture_not_gc_dropped(
    talker_config, monkeypatch, tmp_path,
):
    """The exposed path (reviewer WARN): on the REPROMPT branch (NoTranscript /
    served-empty) on_voice replies + RETURNS while the shadow Deepgram call is
    still in flight. Without a strong ref the loop's weak ref could GC the
    task and silently drop the most valuable (failure/silence) divergence
    clips. Pin: the in-flight task is held in ``_SHADOW_TASKS`` until done, and
    the reprompt-path capture COMPLETES (corpus line written, ref discarded)."""
    from alfred.telegram import bot

    talker_config.stt.shadow_capture = SttShadowCaptureConfig(
        enabled=True, dir=str(tmp_path / "corpus"),
    )

    # Reprompt path: router returns NoTranscript → on_voice replies + returns.
    _patch_router(monkeypatch, NoTranscript(reason="all_failed"))

    handle_called = {"n": 0}

    async def _fake_handle_message(*args: Any, **kwargs: Any) -> None:
        handle_called["n"] += 1

    monkeypatch.setattr(bot, "handle_message", _fake_handle_message)

    # Both engines block on the gate so the capture task stays in-flight after
    # on_voice returns — capture runs BOTH fresh (served_result=None).
    gate = asyncio.Event()
    groq = _GatedEngine("groq-whisper", "groq heard this", gate)
    dg = _GatedEngine("deepgram", "deepgram heard this", gate)
    monkeypatch.setattr(stt_shadow, "build_chain", lambda cfg: [groq, dg])

    bot._SHADOW_TASKS.clear()  # isolate the module-level keep-alive set

    update, ctx, reply = _build_update_and_ctx(talker_config)
    await bot.on_voice(update, ctx)

    # Reprompt fired and on_voice returned; the capture is still gated and
    # MUST be strong-referenced (the GC-protection property).
    assert handle_called["n"] == 0
    reply.assert_called_once()
    assert len(bot._SHADOW_TASKS) == 1, "in-flight capture must be strong-ref'd"

    # Release the engines; let the capture finish.
    gate.set()
    for _ in range(20):
        await asyncio.sleep(0)
        if not bot._SHADOW_TASKS:
            break

    # Completed → ref discarded AND the reprompt-path capture was NOT dropped.
    assert bot._SHADOW_TASKS == set(), "completed task must be discarded"
    jsonl = tmp_path / "corpus" / "corpus.jsonl"
    assert jsonl.exists(), "reprompt-path capture must still write its line"
    rows = [json.loads(line) for line in jsonl.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["groq"]["text"] == "groq heard this"
    assert rows[0]["deepgram"]["text"] == "deepgram heard this"
    assert groq.calls == 1 and dg.calls == 1  # both ran fresh (NoTranscript)


# ---------------------------------------------------------------------------
# 6. divergence() matches the harness formula
# ---------------------------------------------------------------------------


def test_divergence_matches_harness_formula():
    # Identical → 0.0 (harness selftest).
    assert stt_shadow.divergence("the cat sat", "the cat sat") == 0.0
    # Disjoint → > 0.5 (harness selftest).
    assert stt_shadow.divergence("the cat sat", "a dog ran") > 0.5
    # Both empty → 0.0.
    assert stt_shadow.divergence("", "") == 0.0
    # One-word substitution out of four → edit_distance 1 / max_len 4 = 0.25.
    # ("rrts"→"rrs"; punctuation stripped, lowercased by _norm).
    d = stt_shadow.divergence(
        "check the RRTS tickets", "check the RRS tickets",
    )
    assert abs(d - 0.25) < 1e-9, d


# ---------------------------------------------------------------------------
# 7. config: STTConfig loads shadow_capture; omitted → OFF default; _build OK
# ---------------------------------------------------------------------------


def test_config_loads_shadow_capture_block():
    cfg = load_from_unified({"telegram": {
        "instance": {"name": "Salem"},
        "stt": {"shadow_capture": {"enabled": True, "dir": "/data/x"}},
    }})
    assert isinstance(cfg.stt.shadow_capture, SttShadowCaptureConfig)
    assert cfg.stt.shadow_capture.enabled is True
    assert cfg.stt.shadow_capture.dir == "/data/x"


def test_config_omitted_shadow_capture_defaults_off():
    cfg = load_from_unified({"telegram": {
        "instance": {"name": "Salem"},
        "stt": {"api_key": "k", "model": "whisper-large-v3"},
    }})
    assert cfg.stt.shadow_capture.enabled is False
    assert cfg.stt.shadow_capture.dir == "./data/stt_corpus"


def test_config_no_stt_block_defaults_off():
    cfg = load_from_unified({"telegram": {"instance": {"name": "Salem"}}})
    assert cfg.stt.shadow_capture.enabled is False
    assert cfg.stt.shadow_capture.dir == "./data/stt_corpus"
