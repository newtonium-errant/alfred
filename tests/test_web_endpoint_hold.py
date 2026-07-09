"""Tests for adaptive turn-end (endpointing) — Increment 1.

Two layers:
  * PURE classify_tail / normalize (fast, no loop).
  * The worker hold/commit STATE MACHINE — the load-bearing pins: concurrency
    (resume-cancel, hold_gen supersede, hold never blocks consumption, the
    stream is not killed across a hold), zero-latency default, snapshot/buffer
    alignment, bypass rails, default-off byte-identical, per-utterance state
    reset (no bleed), barge interaction, and privacy (features-only sink).

All UNCONDITIONAL (no av/aiortc). The worker's internal decision methods are
driven directly for determinism; the concurrency/stream pins drive the full
``_pump_events`` loop against a provider whose ``events()`` faithfully parks on
``queue.get()`` with NO CancelledError handling (matching the real
``DeepgramStreamProvider`` — the lifecycle dimension that bit us at activation).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
import structlog

from alfred.web.endpoint_hold import (
    COMMIT,
    HOLD,
    EndpointHoldSettings,
    classify_tail,
    normalize_endpoint_hold_settings,
)
from alfred.web.config import WebVoiceEndpointHoldConfig
from alfred.web.stt_stream import (
    EVENT_FINAL,
    EVENT_PARTIAL,
    EVENT_UTTERANCE_END,
    STTEvent,
    STTStreamProvider,
)
from alfred.web.voice_stt import VoiceSttWorker


# ---------------------------------------------------------------------------
# Pure classify_tail
# ---------------------------------------------------------------------------

_ON = EndpointHoldSettings(enabled=True)


@pytest.mark.parametrize("text,expected", [
    ("move it to friday.", COMMIT),          # terminal punct → complete
    ("yes it is", COMMIT),                    # no signal, no punct → snappy commit
    ("i want to talk about it and", HOLD),    # trailing conjunction
    ("so the plan is um", HOLD),              # trailing filler
    ("put it in the", HOLD),                  # dangling article
    ("cancel the 3pm i mean", HOLD),          # two-token filler phrase (kept)
    ("i was thinking you know", COMMIT),      # "you know" DROPPED from the contract
    ("and then we are done.", COMMIT),        # punct VETOES a mid-word conj
    ("let's move on", COMMIT),                # "on" not dangling; apostrophe safe
    ('he said "go."', COMMIT),                # veto reads RAW: strip " then . vetoes
    ("leave it as is", COMMIT),               # "is" excluded from DANGLING (copula)
    ("i'll think about that", COMMIT),        # "that" DROPPED from CONJUNCTIONS
])
def test_classify_tail_decisions(text, expected) -> None:
    assert classify_tail(text, "", _ON).decision == expected


@pytest.mark.parametrize("text,expected", [
    # The FROZEN contract §4 worked-walkthrough table — pinned verbatim so the
    # shipped code matches the reviewed examples (feedback_worked_example_accuracy).
    ("move it to the", HOLD),                 # last=the ∈ DANGLING (word-finding)
    ("let me", HOLD),                          # last-two 'let me' ∈ FILLERS_MULTI
    ("I need to call him because", HOLD),      # last=because ∈ CONJUNCTIONS
    ("send it to Bob and, um", HOLD),          # last=um (comma edge-stripped) ∈ FILLERS
    ("yes", COMMIT),                           # no punct, no signal → snappy commit
    ("move it to Friday.", COMMIT),            # trailing . → veto
    ("I'll think about that", COMMIT),         # 'that' EXCLUDED (that-trap avoided)
    ("leave it as is", COMMIT),                # 'is' EXCLUDED (copula-final avoided)
    ("so anyway", COMMIT),                     # trailing 'so' is NOT the tail token
    ("get back to", HOLD),                     # last='to' ∈ DANGLING (infinitive)
    ("what do you mean", COMMIT),              # 'you mean' ≠ FILLERS_MULTI 'i mean'
])
def test_contract_worked_examples(text, expected) -> None:
    assert classify_tail(text, "", _ON).decision == expected


def test_classify_tail_features_shape() -> None:
    f = classify_tail("talk about it and", "", _ON).features
    assert f == {
        "trailing_is_conjunction": True, "trailing_is_filler": False,
        "trailing_is_dangling": False, "ends_with_terminal_punct": False,
        "n_tokens": 4,
    }


def test_classify_tail_toggles_disable_category() -> None:
    off_conj = EndpointHoldSettings(enabled=True, hold_on_conjunction=False)
    r = classify_tail("talk about it and", "", off_conj)
    assert r.decision == COMMIT
    assert r.signal_category is None          # no hold → no attribution
    # the feature is still REPORTED (telemetry) even when the toggle is off
    assert r.features["trailing_is_conjunction"] is True


@pytest.mark.parametrize("text,category", [
    ("talk about it and", "conjunction"),
    ("so the plan is um", "filler"),
    ("put it in the", "dangling"),
    ("move it to friday.", None),          # commit (punct veto) → no attribution
    ("yes it is", None),                    # commit (no signal) → no attribution
])
def test_classify_tail_signal_category_attribution(text, category) -> None:
    # The LOCKED contract fact: classify_tail exposes signal_category.
    assert classify_tail(text, "", _ON).signal_category == category


def test_normalize_clamps() -> None:
    cfg = WebVoiceEndpointHoldConfig(
        enabled=True, base_extend_ms=9000, max_total_hold_ms=100)
    ns, warns = normalize_endpoint_hold_settings(cfg)
    assert ns.base_extend_ms == 1500          # [0,1500]
    assert ns.max_total_hold_ms == 1500       # clamped to [base,3000] = [1500,3000]
    assert len(warns) == 2


# ---------------------------------------------------------------------------
# Worker test doubles
# ---------------------------------------------------------------------------


class _ParkingProvider(STTStreamProvider):
    """events() parks on ``queue.get()`` with NO CancelledError handling —
    faithful to the real DeepgramStreamProvider generator (stt_deepgram.py:419).
    A wait_for-timeout-cancel on this would close it; the design must not."""

    provider_id = "parking"

    def __init__(self) -> None:
        self.q: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None: ...
    async def feed(self, chunk: bytes) -> None: ...
    async def finalize(self) -> None: ...

    async def close(self) -> None:
        self.q.put_nowait(None)

    async def events(self):
        while True:
            ev = await self.q.get()   # bare await — no CancelledError catch
            if ev is None:
                return
            yield ev

    def emit(self, ev: STTEvent) -> None:
        self.q.put_nowait(ev)


class _FakeTrack:
    """Yields its frames then raises (reader ends; the pump consumes
    provider-emitted events). Mirrors test_web_voice_stt_worker.py."""

    def __init__(self, frames=None) -> None:
        self._frames = list(frames or [])

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        raise RuntimeError("end-of-track")


def _worker(on_utt, *, endpoint=_ON, telemetry=None, shadow=None,
            on_partial=None, min_chars=3, provider=None):
    return VoiceSttWorker(
        provider=provider or _ParkingProvider(),
        voice_session_id="v1",
        on_utterance=on_utt,
        on_partial=on_partial,
        min_utterance_chars=min_chars,
        resample_fn=lambda f: [f] if isinstance(f, (bytes, bytearray)) else [],
        hello_gate=False,
        shadow_capture=shadow,
        endpoint_settings=endpoint,
        endpoint_telemetry=telemetry,
    )


async def _collect(lst):
    async def _on(t):
        lst.append(t)
    return _on


# ---------------------------------------------------------------------------
# Zero-latency default + hold arming (direct-drive, deterministic)
# ---------------------------------------------------------------------------


async def test_zero_latency_complete_thought_no_timer_armed() -> None:
    got: list[str] = []
    w = _worker(await _collect(got))
    w._buffer = ["move it to friday."]
    with structlog.testing.capture_logs() as cap:
        await w._on_utterance_end("speech_final")
    assert got == ["move it to friday."]          # committed SAME tick
    assert w._hold_task is None                    # NO timer armed
    assert not [c for c in cap if c.get("event") == "web.voice.stt.endpoint_hold_armed"]


async def test_mid_thought_arms_hold_then_commits_on_expiry() -> None:
    got: list[str] = []
    w = _worker(await _collect(got),
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=30))
    w._buffer = ["tell me about it and"]
    await w._on_utterance_end("speech_final")
    assert w._hold_task is not None                # armed, NOT yet fired
    assert got == []
    await asyncio.sleep(0.05)                       # let the timer expire
    assert got == ["tell me about it and"]         # committed on expiry
    assert w._hold_task is None


# ---------------------------------------------------------------------------
# CONCURRENCY pins
# ---------------------------------------------------------------------------


async def test_resume_during_hold_cancels_and_folds_single_fire() -> None:
    got: list[str] = []
    w = _worker(await _collect(got),
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=200))
    w._buffer = ["talk about it and"]
    await w._on_utterance_end("speech_final")      # HOLD armed
    assert w._hold_task is not None
    # Resume: a new final arrives → folds into the SAME buffer + cancels the hold.
    w._buffer.append("the weather today")
    w._note_resume()
    assert w._hold_task is None                     # hold cancelled by resume
    assert w._resumed_within_hold is True
    # Next EOU on the folded buffer → single commit of the whole thing.
    await w._on_utterance_end("speech_final")
    assert got == ["talk about it and the weather today"]   # ONE fire, folded


async def test_hold_gen_supersede_stale_timer_no_double_fire() -> None:
    got: list[str] = []
    w = _worker(await _collect(got),
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=200))
    w._buffer = ["so anyway and"]
    await w._on_utterance_end("speech_final")
    stale_gen = w._hold_gen
    w._note_resume()                                # cancels + bumps hold_gen
    assert w._hold_gen != stale_gen
    # The stale timer firing must be a no-op (superseded).
    await w._commit_held(stale_gen)
    assert got == []                                # NO double fire
    w._cancel_hold()


async def test_hold_does_not_block_event_consumption() -> None:
    """A partial arriving during a hold is still processed (pump not stalled) —
    this is also the barge Stage-A guarantee (partials keep flowing)."""
    got: list[str] = []
    partials: list[str] = []
    prov = _ParkingProvider()
    w = _worker(await _collect(got), on_partial=await _collect(partials),
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=300),
                provider=prov)
    w.start(_FakeTrack([]))                                   # start the pump (parking prov)
    prov.emit(STTEvent(type=EVENT_FINAL, text="describe it and"))
    prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
    await asyncio.sleep(0.02)
    assert w._hold_task is not None                 # holding
    # A partial during the hold → still forwarded (consumption not blocked),
    # and it counts as resume (cancels the hold).
    prov.emit(STTEvent(type=EVENT_PARTIAL, text="the plan"))
    await asyncio.sleep(0.02)
    assert partials == ["the plan"]                 # processed DURING the hold
    assert w._hold_task is None                     # resume cancelled it
    await w.aclose()


async def test_stream_survives_a_hold_no_generator_kill() -> None:
    """feedback_real_provider_integration_gate pin: the hold must NOT be a
    wait_for-on-generator (which closes the un-CancelledError events() and kills
    STT). Drive a real pump: hold+commit one utterance, then a SECOND utterance
    must still flow — proving the generator survived."""
    got: list[str] = []
    prov = _ParkingProvider()
    w = _worker(await _collect(got),
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=20),
                provider=prov)
    w.start(_FakeTrack([]))
    prov.emit(STTEvent(type=EVENT_FINAL, text="first part and"))
    prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
    await asyncio.sleep(0.05)                       # hold expires → commit #1
    prov.emit(STTEvent(type=EVENT_FINAL, text="second utterance here."))
    prov.emit(STTEvent(type=EVENT_UTTERANCE_END, trigger="speech_final"))
    await asyncio.sleep(0.03)
    assert got == ["first part and", "second utterance here."]   # both flowed
    await w.aclose()


async def test_cumulative_ceiling_force_commits(monkeypatch) -> None:
    got: list[str] = []
    w = _worker(await _collect(got), endpoint=EndpointHoldSettings(
        enabled=True, base_extend_ms=500, max_total_hold_ms=1500))
    clock = {"t": 0.0}
    monkeypatch.setattr(w, "_now", lambda: clock["t"])
    w._buffer = ["and"]
    await w._on_utterance_end("speech_final")       # first_hold_at=0, armed
    assert w._hold_task is not None
    w._note_resume()                                # cancel + fold
    clock["t"] = 2.0                                # 2000ms > 1500ms ceiling
    w._buffer.append("so um and")
    with structlog.testing.capture_logs() as cap:
        await w._on_utterance_end("speech_final")   # ceiling → force-commit
    assert got == ["and so um and"]                 # committed despite the signal
    assert w._hold_task is None                     # no new timer
    assert [c for c in cap if c.get("event") == "web.voice.stt.endpoint_hold_ceiling"]


async def test_late_final_during_held_commit_await_survives() -> None:
    """endpoint-hold #1 — late-final drop race. _commit_held runs in the DETACHED
    timer task; during its ``await on_utterance`` the pump can append a NEW final
    to the SAME buffer (its EVENT_FINAL branch). The reset must NOT clear that
    late final — its own utterance_end has not fired yet. It is carried forward
    and commits on its own EOU.

    Negative check: revert the carry-forward and _reset_utt clears the whole
    buffer → the late final is DROPPED → the buffer/second-commit asserts fail.
    The on_utterance callback appends the late final at exactly the await window,
    deterministically modelling the pump's FINAL-branch append during the timer
    commit (where _note_resume is a no-op — hold_task is already None)."""
    got: list[str] = []
    injected = {"done": False}

    async def on_utt(text: str) -> None:
        got.append(text)
        if not injected["done"]:
            injected["done"] = True
            # Resumed final appended to the SAME buffer DURING the commit await;
            # its own EVENT_UTTERANCE_END has NOT arrived yet (the drop window).
            w._buffer.append("wait one more thing.")

    w = _worker(on_utt,
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=20))
    w._buffer = ["book the flight and"]
    await w._on_utterance_end("speech_final")          # HOLD armed
    assert w._hold_task is not None
    await asyncio.sleep(0.05)                            # timer → _commit_held
    assert got == ["book the flight and"]               # the held text committed
    # The late final appended DURING the commit await survived the reset.
    assert w._buffer == ["wait one more thing."], "late final dropped by reset"
    assert w._first_hold_at is None and w._ever_held is False  # per-utt state reset
    # And it commits on its OWN utterance_end (carried forward, not lost).
    await w._on_utterance_end("speech_final")
    assert got == ["book the flight and", "wait one more thing."]


async def test_late_final_carry_forward_noop_on_inline_commit() -> None:
    """The carry-forward must be a NO-OP on the inline (pump-driven) commit path:
    the pump is blocked on the on_utterance await, so nothing appends and the
    whole buffer is cleared exactly as before — byte-identical to today."""
    got: list[str] = []
    w = _worker(await _collect(got))                     # default-off = inline path
    w._buffer = ["just commit this now"]
    await w._on_utterance_end("speech_final")
    assert got == ["just commit this now"]
    assert w._buffer == []                               # fully cleared (no carry)


# ---------------------------------------------------------------------------
# Snapshot / buffer alignment (held audio stays aligned with held text)
# ---------------------------------------------------------------------------


async def test_held_committed_pcm_matches_folded_audio() -> None:
    got: list[str] = []
    seen: dict = {}

    def shadow(pcm, text, dur):
        seen["pcm"] = pcm
        seen["text"] = text

    w = _worker(await _collect(got), shadow=shadow,
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=200))
    # Simulate the sender teeing PCM as speech arrives.
    w._utt_pcm.extend(b"A" * 3200)
    w._buffer = ["describe it and"]
    await w._on_utterance_end("speech_final")       # HOLD (audio A buffered)
    w._utt_pcm.extend(b"B" * 3200)                  # resumed audio keeps teeing
    w._buffer.append("the rest here.")
    w._note_resume()
    await w._on_utterance_end("speech_final")       # commit folded
    assert seen["text"] == "describe it and the rest here."
    assert seen["pcm"] == b"A" * 3200 + b"B" * 3200  # audio == full folded text


# ---------------------------------------------------------------------------
# Bypass rails (never hold)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["finalize", "fake"])
async def test_bypass_triggers_commit_no_hold(trigger) -> None:
    got: list[str] = []
    w = _worker(await _collect(got))
    w._buffer = ["mid thought and"]                 # would HOLD on a normal trigger
    await w._on_utterance_end(trigger)
    assert got == ["mid thought and"]               # committed inline
    assert w._hold_task is None                      # NO hold


async def test_closing_discards_no_hold() -> None:
    got: list[str] = []
    w = _worker(await _collect(got))
    w._closing = True
    w._buffer = ["something and"]
    await w._on_utterance_end("speech_final")
    assert got == []                                 # teardown fires no turn
    assert w._hold_task is None


async def test_sub_min_chars_no_hold() -> None:
    got: list[str] = []
    w = _worker(await _collect(got), min_chars=3)
    w._buffer = ["hi"]                               # 2 < 3
    with structlog.testing.capture_logs() as cap:
        await w._on_utterance_end("speech_final")
    assert got == []
    assert w._hold_task is None
    assert [c for c in cap if c.get("event") == "web.voice.stt.utterance_empty"]


# ---------------------------------------------------------------------------
# Default-OFF byte-identical + state reset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", [None, EndpointHoldSettings(enabled=False)])
async def test_default_off_commits_same_tick_no_hold(endpoint) -> None:
    got: list[str] = []
    w = _worker(await _collect(got), endpoint=endpoint)
    w._buffer = ["a mid thought and"]               # a HOLD signal — but disabled
    with structlog.testing.capture_logs() as cap:
        await w._on_utterance_end("speech_final")
    assert got == ["a mid thought and"]             # fires immediately (as today)
    assert w._hold_task is None
    # byte-identical log: the disabled path omits the held/hold_ms fields.
    ue = [c for c in cap if c.get("event") == "web.voice.stt.utterance_end"]
    assert len(ue) == 1 and "held" not in ue[0] and "hold_ms" not in ue[0]


async def test_two_utterances_no_state_bleed() -> None:
    """team-lead guardrail: instance-attr hold state must reset per utterance so
    utterance N+1 never inherits N's buffer / hold history."""
    got: list[str] = []
    w = _worker(await _collect(got),
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=20))
    # Utterance 1: held then committed.
    w._buffer = ["first thought and"]
    await w._on_utterance_end("speech_final")
    await asyncio.sleep(0.05)
    assert got == ["first thought and"]
    assert w._first_hold_at is None and w._ever_held is False   # reset
    assert w._buffer == [] and w._hold_ms_applied == 0
    # Utterance 2: crisp — must NOT carry utterance 1's text or hold state.
    w._buffer = ["second thought done."]
    await w._on_utterance_end("speech_final")
    assert got == ["first thought and", "second thought done."]


# ---------------------------------------------------------------------------
# PRIVACY — features-only sink, never raw text
# ---------------------------------------------------------------------------


async def test_telemetry_records_features_only_never_raw_text(tmp_path: Path) -> None:
    from alfred.web.voice_endpoint_telemetry import (
        VoiceEndpointTelemetry, _ENDPOINT_TASKS)
    tel = VoiceEndpointTelemetry(
        corpus_dir=str(tmp_path), web_user="andrew",
        voice_session_id="v9", instance_name="Salem")
    w = _worker(await _collect([]), telemetry=tel.emit,
                endpoint=EndpointHoldSettings(enabled=True))
    # A distinctive raw tail that must NEVER appear in the sink. Terminal punct
    # → COMMIT → telemetry emitted on this utterance.
    w._buffer = ["reschedule the fergus meeting and finish it."]
    await w._on_utterance_end("speech_final")
    for _ in range(50):
        pending = [t for t in list(_ENDPOINT_TASKS) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)

    raw = (tmp_path / "events.jsonl").read_text()
    assert raw.strip(), "a committed endpoint event should have been written"
    # NO raw tail text anywhere in the sink.
    for word in ("reschedule", "fergus", "meeting", "finish"):
        assert word not in raw, f"raw tail word {word!r} leaked into the sink"
    rec = json.loads(raw.splitlines()[-1])
    assert rec["event_family"] == "endpoint" and rec["web_user"] == "andrew"
    assert rec["voice_session_id"] == "v9" and rec["decision"] == "commit"
    assert set(rec) >= {"trailing_is_conjunction", "ends_with_terminal_punct",
                        "hold_ms_applied", "resumed_within_hold", "signal_category"}
    assert rec["signal_category"] is None       # punct-veto COMMIT → no attribution
    assert "text" not in rec and "transcript" not in rec and "tail" not in rec


async def test_telemetry_never_logs_the_matched_word(tmp_path: Path) -> None:
    """Contract §5 (tighter than category-only): the specific matched trigger
    word is NEVER persisted — only the category. A hold on 'because' records
    signal_category='conjunction', never the word 'because'."""
    from alfred.web.voice_endpoint_telemetry import (
        VoiceEndpointTelemetry, _ENDPOINT_TASKS)
    tel = VoiceEndpointTelemetry(
        corpus_dir=str(tmp_path), web_user="u", voice_session_id="v")
    w = _worker(await _collect([]), telemetry=tel.emit,
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=20))
    w._buffer = ["cancel it because"]                # HOLD, category=conjunction
    await w._on_utterance_end("speech_final")
    await asyncio.sleep(0.05)                          # hold expires → commit → emit
    for _ in range(50):
        pending = [t for t in list(_ENDPOINT_TASKS) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)
    raw = (tmp_path / "events.jsonl").read_text()
    assert "because" not in raw and "cancel" not in raw   # matched word + tail absent
    rec = json.loads(raw.splitlines()[-1])
    assert rec["signal_category"] == "conjunction" and rec["decision"] == "hold"
    assert rec["hold_ms_applied"] > 0


async def test_resumed_hold_latches_trigger_attribution(tmp_path: Path) -> None:
    """Soak-critical: a hold TRIGGERED by a signal (conjunction) that RESUMES and
    commits on a NON-signal tail must still record WHAT TRIGGERED it. Without the
    latch the final classify_tail overwrites _last_* → the held record reads
    signal_category=None / all-false ('held but nothing fired'), and the soak
    can't break resumed holds down per-signal (contract §6)."""
    from alfred.web.voice_endpoint_telemetry import (
        VoiceEndpointTelemetry, _ENDPOINT_TASKS)
    tel = VoiceEndpointTelemetry(
        corpus_dir=str(tmp_path), web_user="u", voice_session_id="v")
    w = _worker(await _collect([]), telemetry=tel.emit,
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=200))
    # HOLD triggered by a trailing conjunction → latches signal=conjunction.
    w._buffer = ["cancel the meeting and"]
    await w._on_utterance_end("speech_final")
    assert w._hold_task is not None
    # Resume + fold, then commit on a NON-signal tail.
    w._buffer.append("we are all set")
    w._note_resume()
    await w._on_utterance_end("speech_final")     # last='set' → COMMIT → emit
    for _ in range(50):
        pending = [t for t in list(_ENDPOINT_TASKS) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)

    rec = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    # The record reports WHAT TRIGGERED the hold — not the all-false final tail.
    assert rec["decision"] == "hold" and rec["resumed_within_hold"] is True
    assert rec["signal_category"] == "conjunction"
    assert rec["trailing_is_conjunction"] is True
    # And still no raw text / matched word.
    assert "cancel" not in json.dumps(rec) and "meeting" not in json.dumps(rec)


async def test_multi_hold_attributes_to_first_trigger(tmp_path: Path) -> None:
    """endpoint-hold #2 — multi-hold attribution is DELIBERATELY first-trigger.
    A rare utterance that holds on a DANGLING word, resumes, holds AGAIN on a
    CONJUNCTION, resumes, then commits records ONE per-utterance endpoint event
    attributed to the FIRST trigger (dangling) — the documented
    one-record-per-utterance choice (code-reviewer: defensible, not a defect).
    This pins the contract so a refactor to last/all-trigger can't land silently:
    a last-trigger change would record 'conjunction' and fail here."""
    from alfred.web.voice_endpoint_telemetry import (
        VoiceEndpointTelemetry, _ENDPOINT_TASKS)
    tel = VoiceEndpointTelemetry(
        corpus_dir=str(tmp_path), web_user="u", voice_session_id="v")
    w = _worker(await _collect([]), telemetry=tel.emit,
                endpoint=EndpointHoldSettings(enabled=True, base_extend_ms=200))
    # Hold #1: trailing dangling article → latches signal=dangling.
    w._buffer = ["put it on the"]
    await w._on_utterance_end("speech_final")
    assert w._hold_task is not None
    assert w._hold_trigger_category == "dangling"
    # Resume, then Hold #2 on a trailing conjunction — must NOT overwrite the latch.
    w._buffer.append("desk and")
    w._note_resume()
    await w._on_utterance_end("speech_final")
    assert w._hold_task is not None
    assert w._hold_trigger_category == "dangling"        # STILL the first trigger
    # Resume + commit on a non-signal tail → emit.
    w._buffer.append("we are set")
    w._note_resume()
    await w._on_utterance_end("speech_final")             # last='set' → COMMIT → emit
    for _ in range(50):
        pending = [t for t in list(_ENDPOINT_TASKS) if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending, return_exceptions=True)

    rec = json.loads((tmp_path / "events.jsonl").read_text().splitlines()[-1])
    # ONE record, attributed to the FIRST trigger (dangling), not the last (conj).
    assert rec["decision"] == "hold" and rec["resumed_within_hold"] is True
    assert rec["signal_category"] == "dangling"
    assert rec["trailing_is_dangling"] is True
    assert rec["trailing_is_conjunction"] is False       # NOT the last trigger
    # And still no raw text leaked.
    assert "desk" not in json.dumps(rec) and "flight" not in json.dumps(rec)


def test_telemetry_sink_drops_nonallowlisted_fields(tmp_path: Path) -> None:
    """Even if a caller passes a raw-text field, the sink allowlist drops it."""
    import asyncio as _a
    from alfred.web.voice_endpoint_telemetry import VoiceEndpointTelemetry

    async def _run():
        from alfred.web.voice_endpoint_telemetry import _ENDPOINT_TASKS
        tel = VoiceEndpointTelemetry(
            corpus_dir=str(tmp_path), web_user="u", voice_session_id="v")
        tel.emit({"decision": "commit", "trailing_is_filler": True,
                  "text": "SECRET RAW TAIL", "tail": "SECRET"})
        for _ in range(50):
            pending = [t for t in list(_ENDPOINT_TASKS) if not t.done()]
            if not pending:
                break
            await _a.gather(*pending, return_exceptions=True)

    _a.run(_run())
    raw = (tmp_path / "events.jsonl").read_text()
    assert "SECRET" not in raw                       # dropped by the allowlist
    rec = json.loads(raw.splitlines()[-1])
    assert rec["decision"] == "commit" and rec["trailing_is_filler"] is True
    assert "text" not in rec and "tail" not in rec


# ---------------------------------------------------------------------------
# Clinic-capture Piece 2b — STT hallucination denylist at the web commit seam
# (a caption artifact must NEVER drive a live turn — clinical-safety control).
# ---------------------------------------------------------------------------


async def test_caption_hallucination_never_drives_a_turn() -> None:
    """A fully-hallucinated utterance ("Thank you for watching!") is filtered to
    empty in _commit_inline → NO on_utterance fires. Mutation: remove the
    filter → the caption drives a live turn → ``got`` is non-empty → fails."""
    got: list[str] = []
    w = _worker(await _collect(got))                  # default denylist active
    w._buffer = ["Thank you for watching!"]
    with structlog.testing.capture_logs() as cap:
        await w._on_utterance_end("speech_final")
    assert got == []                                  # NO live turn fired
    assert [c for c in cap
            if c.get("event") == "web.voice.stt.utterance_all_noise"]


async def test_real_utterance_survives_the_denylist() -> None:
    """A genuine clinical utterance is untouched — the filter is exact-line, so
    real content always fires."""
    got: list[str] = []
    w = _worker(await _collect(got))
    w._buffer = ["send the prescription refill."]
    await w._on_utterance_end("speech_final")
    assert got == ["send the prescription refill."]


async def test_per_instance_denylist_term_dropped_at_commit() -> None:
    """A per-instance extra term (unioned onto the default) is dropped too."""
    got: list[str] = []
    w = _worker(await _collect(got))
    w._stt_denylist = ["Hedgesha"]                    # per-instance extra
    w._buffer = ["Hedgesha"]
    await w._on_utterance_end("speech_final")
    assert got == []
