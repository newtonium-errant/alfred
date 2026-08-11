"""The capture service, end to end (task #81, stage 1).

Every test here drives the WHOLE chain — ring, detector, window, STT,
classification, dispatch — against synthetic audio, a scripted detector and
a stub backend. No microphone, no model files, no network. That is the point
of the seams: the thing that runs in the garage is the thing under test,
minus only the mic adapter and the acoustic model.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.jeeves import cues, marklog, service, telemetry
from alfred.jeeves.audio import AudioFormat, MemoryAudioSource, silence, tone
from alfred.jeeves.config import (
    JEEVES_MODE_LIVE,
    JeevesCueConfig,
    JeevesRingConfig,
    JeevesSttConfig,
    JeevesWindowConfig,
    JeevesConfig,
)
from alfred.jeeves.wake import ScriptedWakeDetector
from alfred.telegram.stt_backends import SttResult

FMT = AudioFormat(sample_rate=1000, sample_width=2, channels=1)   # 2000 B/s


class StubStt:
    """Returns a scripted transcript per call, and records the calls."""

    def __init__(self, *texts: str):
        self.texts = list(texts) or ["Jeeves, note that"]
        self.calls: list[int] = []

    async def transcribe(self, audio: bytes, mime: str, vocab: list[str]):
        self.calls.append(len(audio))
        text = self.texts[min(len(self.calls) - 1, len(self.texts) - 1)]
        return SttResult(
            text=text, backend_id="stub", tier="comparable",
            has_speech_signal=False,   # the Q6 artefact, present on every call
            confidence_raw=-0.28,
        )


def build_config(tmp_path, **overrides) -> JeevesConfig:
    base = dict(
        mode=JEEVES_MODE_LIVE,
        ring=JeevesRingConfig(
            seconds=60, sample_rate=1000, sample_width=2, channels=1),
        window=JeevesWindowConfig(
            lookback_seconds=5.0, extended_lookback_seconds=20.0,
            silence_seconds=2.0, max_lookahead_seconds=5.0,
            max_lookback_seconds=30.0, silence_rms_threshold=0.01),
        stt=JeevesSttConfig(api_key="DUMMY_GROQ_TEST_KEY"),
        cues=JeevesCueConfig(route_target="peerbox"),
        telemetry_path=str(tmp_path / "telemetry.jsonl"),
        mark_log_path=str(tmp_path / "marks.jsonl"),
    )
    base.update(overrides)
    return JeevesConfig(**base)


#: Sentinel so a test can pass provenance=None and MEAN None — the gate's
#: strict type check is exactly what None must be able to exercise.
_DEFAULT_PROVENANCE = object()


def build_service(tmp_path, *, texts=("Jeeves, note that",), cue_at=10.0,
                  route_sink=None, config=None, provenance=_DEFAULT_PROVENANCE):
    cfg = config or build_config(tmp_path)
    return service.JeevesService(
        cfg,
        audio_format=FMT,
        detector=ScriptedWakeDetector([cue_at], audio_format=FMT),
        stt_backend=StubStt(*texts),
        route_sink=route_sink,
        provenance=(
            {"synthetic": True} if provenance is _DEFAULT_PROVENANCE
            else provenance
        ),
    )


def speech_then_quiet(speech_s: float = 12.0, quiet_s: float = 4.0):
    """A stream with audible content, then enough silence to end a lookahead."""
    blobs = [tone(1.0, FMT) for _ in range(int(speech_s))]
    blobs += [silence(1.0, FMT) for _ in range(int(quiet_s))]
    return MemoryAudioSource(blobs, audio_format=FMT)


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


async def test_a_mark_cue_lands_in_the_local_log(tmp_path):
    svc = build_service(tmp_path, texts=("Jeeves, note that — the 6203",))
    outcomes = await svc.run(speech_then_quiet())

    assert len(outcomes) == 1
    assert outcomes[0].verb == cues.CUE_MARK_DOWN
    assert outcomes[0].disposition == service.DISPOSITION_MARKED

    entries = marklog.read_entries(str(tmp_path / "marks.jsonl"))
    assert len(entries) == 1
    assert entries[0]["kind"] == marklog.KIND_MARK
    assert entries[0]["text"] == "Jeeves, note that — the 6203"


async def test_a_mark_never_leaves_the_device(tmp_path):
    """The whole cost model of MARK-DOWN rests on this."""
    sent: list = []

    async def sink(capture):
        sent.append(capture)
        return True

    svc = build_service(tmp_path, texts=("Jeeves, note that",), route_sink=sink)
    await svc.run(speech_then_quiet())
    assert sent == []


async def test_a_route_cue_reaches_the_sink(tmp_path):
    sent: list[service.RoutedCapture] = []

    async def sink(capture):
        sent.append(capture)
        return True

    svc = build_service(
        tmp_path, texts=("Jeeves, tell peerbox the compressor is leaking",),
        route_sink=sink,
    )
    outcomes = await svc.run(speech_then_quiet())

    assert outcomes[0].disposition == service.DISPOSITION_ROUTED
    assert len(sent) == 1
    assert sent[0].transcript == "Jeeves, tell peerbox the compressor is leaking"
    assert sent[0].target == "peerbox"
    assert sent[0].verb == cues.CUE_ROUTE


async def test_what_leaves_the_device_carries_no_audio(tmp_path):
    """FENCE 2 surviving all the way to the wire: there is no field on a
    RoutedCapture that could hold the window's bytes."""
    sent: list[service.RoutedCapture] = []

    async def sink(capture):
        sent.append(capture)
        return True

    svc = build_service(
        tmp_path, texts=("Jeeves, send that to peerbox",), route_sink=sink)
    await svc.run(speech_then_quiet())

    from dataclasses import fields
    names = {f.name for f in fields(service.RoutedCapture)}
    assert "audio" not in names and "audio_b64" not in names and "pcm" not in names
    for value in sent[0].capture_facts.values():
        assert not isinstance(value, (bytes, bytearray))


async def test_a_miss_report_is_logged_as_its_own_kind(tmp_path):
    """The scarce signal — cue false-negatives are invisible by
    construction, so this is the only labelled negative example available."""
    svc = build_service(tmp_path, texts=("Jeeves, you missed that",))
    outcomes = await svc.run(speech_then_quiet())

    assert outcomes[0].verb == cues.CUE_MISS_REPORT
    assert outcomes[0].disposition == service.DISPOSITION_MISS_LOGGED
    entries = marklog.read_entries(str(tmp_path / "marks.jsonl"))
    assert entries[0]["kind"] == marklog.KIND_MISS


async def test_a_miss_report_says_its_audio_is_not_retained(tmp_path):
    """An operator reporting misses deserves to know the audio behind them
    is not being kept — otherwise he is producing a dataset that does not
    exist."""
    svc = build_service(tmp_path, texts=("Jeeves, you missed that",))
    with structlog.testing.capture_logs() as captured:
        await svc.run(speech_then_quiet())
    events = [c for c in captured
              if c.get("event") == "jeeves.service.miss_audio_not_retained"]
    assert len(events) == 1


async def test_a_cue_with_no_verb_writes_nothing_but_records_the_attempt(tmp_path):
    svc = build_service(tmp_path, texts=("Jeeves, what time is it",))
    outcomes = await svc.run(speech_then_quiet())

    assert outcomes[0].verb == cues.CUE_NONE
    assert outcomes[0].disposition == service.DISPOSITION_DROPPED
    assert outcomes[0].reason == cues.NONE_REASON_NO_VERB
    assert marklog.read_entries(str(tmp_path / "marks.jsonl")) == []

    rows = telemetry.read_rows(str(tmp_path / "telemetry.jsonl"))
    assert [r["event"] for r in rows] == [telemetry.EVENT_CAPTURE_DROPPED]


# ---------------------------------------------------------------------------
# The gate — fail-closed, and FIRST
# ---------------------------------------------------------------------------


async def test_synthetic_mode_REFUSES_an_untagged_capture(tmp_path):
    """The fence. An instance that has not been deliberately flipped to live
    processes nothing, however loudly the room talks."""
    cfg = build_config(tmp_path)
    cfg.mode = "synthetic"
    svc = build_service(tmp_path, config=cfg, provenance={})

    outcomes = await svc.run(speech_then_quiet())
    assert outcomes[0].disposition == service.DISPOSITION_REFUSED
    assert outcomes[0].reason == "missing_synthetic_provenance"


async def test_a_refusal_spends_no_stt_and_writes_no_transcript(tmp_path):
    """The refusal is BEFORE extraction and before any network call — the
    difference between a fence and a filter. Asserting only 'it was refused'
    would pass against a build that refused after paying for the STT."""
    cfg = build_config(tmp_path)
    cfg.mode = "synthetic"
    backend = StubStt("Jeeves, note that")
    svc = service.JeevesService(
        cfg, audio_format=FMT,
        detector=ScriptedWakeDetector([10.0], audio_format=FMT),
        stt_backend=backend, provenance={},
    )
    await svc.run(speech_then_quiet())

    assert backend.calls == []                                    # nothing sent
    assert not (tmp_path / "marks.jsonl").exists()                # nothing written
    rows = telemetry.read_rows(str(tmp_path / "telemetry.jsonl"))
    assert [r["reason"] for r in rows] == ["missing_synthetic_provenance"]


async def test_a_refusal_logs_exactly_one_decision_with_its_reason(tmp_path):
    """REFUSAL PIN. A denial for an unrelated cause renders identically to
    the gate firing, so the logged decision — and its reason — is what
    distinguishes them."""
    cfg = build_config(tmp_path)
    cfg.mode = "synthetic"
    svc = build_service(tmp_path, config=cfg, provenance={})
    with structlog.testing.capture_logs() as captured:
        await svc.run(speech_then_quiet())

    decisions = [c for c in captured
                 if c.get("event") == "jeeves.capture_decision"]
    assert len(decisions) == 1
    assert decisions[0]["accepted"] is False
    assert decisions[0]["reason"] == "missing_synthetic_provenance"
    assert decisions[0]["mode"] == "synthetic"


async def test_synthetic_mode_ACCEPTS_a_tagged_capture(tmp_path):
    """The other direction — the gate must not be so tight that the
    synthetic path it exists to permit cannot run."""
    cfg = build_config(tmp_path)
    cfg.mode = "synthetic"
    svc = build_service(
        tmp_path, config=cfg, provenance={"synthetic": True},
        texts=("Jeeves, note that",),
    )
    outcomes = await svc.run(speech_then_quiet())
    assert outcomes[0].disposition == service.DISPOSITION_MARKED


@pytest.mark.parametrize("provenance", [
    {"synthetic": "true"}, {"synthetic": 1}, {"synthetic": None},
    {"synth": True}, None, "synthetic",
])
async def test_only_a_literal_boolean_true_counts_as_synthetic(tmp_path, provenance):
    """STRICT. Accepting truthy values would mean a YAML quirk or a JSON
    round-trip could arm the live path."""
    cfg = build_config(tmp_path)
    cfg.mode = "synthetic"
    svc = build_service(tmp_path, config=cfg, provenance=provenance)
    outcomes = await svc.run(speech_then_quiet())
    assert outcomes[0].disposition == service.DISPOSITION_REFUSED


# ---------------------------------------------------------------------------
# A ROUTE with nowhere to go stays LOCAL
# ---------------------------------------------------------------------------


async def test_a_route_with_no_sink_falls_back_to_the_local_log(tmp_path):
    """Dropping would lose the operator's words for a configuration mistake
    he cannot see from the garage; improvising a destination is worse than
    both."""
    svc = build_service(
        tmp_path, texts=("Jeeves, tell peerbox about the compressor",),
        route_sink=None,
    )
    with structlog.testing.capture_logs() as captured:
        outcomes = await svc.run(speech_then_quiet())

    assert outcomes[0].verb == cues.CUE_ROUTE
    assert outcomes[0].disposition == service.DISPOSITION_MARKED
    assert outcomes[0].reason == "route_sink_unconfigured"

    entries = marklog.read_entries(str(tmp_path / "marks.jsonl"))
    assert entries[0]["provenance"]["route_fallback"] is True
    events = [c for c in captured
              if c.get("event") == "jeeves.service.route_sink_unconfigured"]
    assert len(events) == 1


async def test_a_failed_send_falls_back_to_the_local_log(tmp_path):
    async def failing_sink(capture):
        return False

    svc = build_service(
        tmp_path, texts=("Jeeves, tell peerbox about it",),
        route_sink=failing_sink,
    )
    outcomes = await svc.run(speech_then_quiet())
    assert outcomes[0].disposition == service.DISPOSITION_MARKED
    assert outcomes[0].reason == "route_send_failed"
    entries = marklog.read_entries(str(tmp_path / "marks.jsonl"))
    assert entries[0]["provenance"]["route_failed"] is True


async def test_a_raising_sink_does_not_kill_the_loop(tmp_path):
    async def exploding_sink(capture):
        raise RuntimeError("connection reset")

    svc = build_service(
        tmp_path, texts=("Jeeves, tell peerbox about it",),
        route_sink=exploding_sink,
    )
    with structlog.testing.capture_logs() as captured:
        outcomes = await svc.run(speech_then_quiet())
    assert outcomes[0].disposition == service.DISPOSITION_MARKED
    events = [c for c in captured
              if c.get("event") == "jeeves.service.route_failed"]
    assert len(events) == 1
    assert events[0]["error_type"] == "RuntimeError"


async def test_an_empty_transcript_is_never_routed(tmp_path):
    """A vault record with no content is one the operator has to read and
    delete at morning review."""
    sent: list = []

    async def sink(capture):
        sent.append(capture)
        return True

    # The transcript classifies as none (empty), so nothing routes.
    svc = build_service(tmp_path, texts=("",), route_sink=sink)
    outcomes = await svc.run(speech_then_quiet())
    assert sent == []
    assert outcomes[0].disposition == service.DISPOSITION_DROPPED


# ---------------------------------------------------------------------------
# The lookahead and the two-pass modifier
# ---------------------------------------------------------------------------


async def test_the_lookahead_extends_the_window_past_the_cue(tmp_path):
    """"Jeeves, note that — the bearing is the wrong size" is one utterance,
    and cutting it at the cue would keep the wrong half."""
    svc = build_service(tmp_path, cue_at=6.0)
    await svc.run(speech_then_quiet(speech_s=12.0, quiet_s=4.0))
    rows = telemetry.read_rows(str(tmp_path / "telemetry.jsonl"))
    assert rows[0]["lookahead_used_seconds"] > 0


async def test_a_spoken_modifier_triggers_a_second_wider_pass(tmp_path):
    """THE TWO-PASS DESIGN. The modifier lives in the transcript, which
    needs the window, which needs the lookback. Rather than always sending
    the extended window (≈3x the audio on EVERY cue, which discards the cost
    argument), the default window is classified first and only a real
    modifier pays for a second pass."""
    backend = StubStt(
        "Jeeves, note the last couple of minutes",   # pass 1: classify
        "the whole longer conversation",             # pass 2: content
    )
    cfg = build_config(tmp_path)
    svc = service.JeevesService(
        cfg, audio_format=FMT,
        detector=ScriptedWakeDetector([14.0], audio_format=FMT),
        stt_backend=backend, provenance={"synthetic": True},
    )
    outcomes = await svc.run(speech_then_quiet(speech_s=16.0, quiet_s=4.0))

    assert len(backend.calls) == 2
    assert backend.calls[1] > backend.calls[0], (
        "the second pass must send MORE audio than the first"
    )
    assert outcomes[0].stt_calls == 2
    entries = marklog.read_entries(str(tmp_path / "marks.jsonl"))
    assert entries[0]["text"] == "the whole longer conversation"


async def test_no_modifier_means_exactly_one_stt_call(tmp_path):
    """The cost fence on the two-pass design: an ordinary cue must never pay
    twice."""
    backend = StubStt("Jeeves, note that")
    cfg = build_config(tmp_path)
    svc = service.JeevesService(
        cfg, audio_format=FMT,
        detector=ScriptedWakeDetector([10.0], audio_format=FMT),
        stt_backend=backend, provenance={"synthetic": True},
    )
    outcomes = await svc.run(speech_then_quiet())
    assert len(backend.calls) == 1
    assert outcomes[0].stt_calls == 1


async def test_a_second_cue_during_a_lookahead_is_folded_in(tmp_path):
    """Two cues three seconds apart are one thought. Treating them as two
    captures would double the STT spend to produce two overlapping records."""
    backend = StubStt("Jeeves, note that")
    cfg = build_config(tmp_path)
    svc = service.JeevesService(
        cfg, audio_format=FMT,
        detector=ScriptedWakeDetector([6.0, 7.0], audio_format=FMT),
        stt_backend=backend, provenance={"synthetic": True},
    )
    outcomes = await svc.run(speech_then_quiet(speech_s=12.0, quiet_s=4.0))
    assert len(outcomes) == 1


async def test_a_capture_still_collecting_at_stream_end_is_completed(tmp_path):
    """A device shutting down mid-capture must not silently lose it."""
    svc = build_service(tmp_path, cue_at=10.0)
    # No trailing silence — the lookahead never terminates on its own.
    source = MemoryAudioSource(
        [tone(1.0, FMT) for _ in range(12)], audio_format=FMT)
    outcomes = await svc.run(source)
    assert len(outcomes) == 1
    rows = telemetry.read_rows(str(tmp_path / "telemetry.jsonl"))
    assert rows[0]["lookahead_end_reason"] == "stream_end"


# ---------------------------------------------------------------------------
# Telemetry from the real chain
# ---------------------------------------------------------------------------


async def test_the_telemetry_row_carries_ruling_5s_datum(tmp_path):
    svc = build_service(tmp_path, cue_at=10.0)
    await svc.run(speech_then_quiet())
    rows = telemetry.read_rows(str(tmp_path / "telemetry.jsonl"))
    assert len(rows) == 1
    assert rows[0]["lookback_used_seconds"] == pytest.approx(5.0)
    assert rows[0]["requested_lookback_seconds"] == pytest.approx(5.0)
    assert rows[0]["event"] == telemetry.EVENT_CAPTURE_MARKED


async def test_the_telemetry_row_from_a_real_capture_carries_no_content(tmp_path):
    """The end-to-end content pin. The service builds rows from a real
    transcript; nothing spoken may survive into the retained file."""
    secret = "Jeeves note that the alarm code is nine four two seven"
    svc = build_service(tmp_path, texts=(secret,))
    await svc.run(speech_then_quiet())

    raw = (tmp_path / "telemetry.jsonl").read_text(encoding="utf-8")
    assert "alarm code" not in raw
    assert "nine four two seven" not in raw
    # ...but the LENGTH is there, which is the useful, harmless part.
    rows = telemetry.read_rows(str(tmp_path / "telemetry.jsonl"))
    assert rows[0]["transcript_chars"] == len(secret)


async def test_a_truncated_reach_is_recorded(tmp_path):
    """A capture that wanted more than the ring held is the signal that the
    ring is too small."""
    cfg = build_config(tmp_path)
    cfg.ring = JeevesRingConfig(
        seconds=3, sample_rate=1000, sample_width=2, channels=1)
    svc = build_service(tmp_path, config=cfg, cue_at=10.0)
    await svc.run(speech_then_quiet())
    rows = telemetry.read_rows(str(tmp_path / "telemetry.jsonl"))
    assert rows[0]["truncated_by_ring"] is True
    assert rows[0]["lookback_used_seconds"] < rows[0]["requested_lookback_seconds"]


async def test_the_speech_flag_is_recorded_and_never_acted_on(tmp_path):
    """The stub returns the Q6 artefact (False) on every call, and every
    capture above still succeeded. Here the recorded value is pinned."""
    svc = build_service(tmp_path)
    await svc.run(speech_then_quiet())
    rows = telemetry.read_rows(str(tmp_path / "telemetry.jsonl"))
    assert rows[0]["has_speech_signal"] is False
    assert rows[0]["event"] == telemetry.EVENT_CAPTURE_MARKED


# ---------------------------------------------------------------------------
# Intentionally left blank
# ---------------------------------------------------------------------------


async def test_a_quiet_run_says_so_rather_than_going_silent(tmp_path):
    """The NORMAL case for a garage nobody is in. It must be visibly
    distinct from a run that never happened."""
    cfg = build_config(tmp_path)
    svc = service.JeevesService(
        cfg, audio_format=FMT,
        detector=ScriptedWakeDetector([], audio_format=FMT),
        stt_backend=StubStt(), provenance={"synthetic": True},
    )
    with structlog.testing.capture_logs() as captured:
        outcomes = await svc.run(speech_then_quiet())

    assert outcomes == []
    events = [c for c in captured if c.get("event") == "jeeves.service.idle"]
    assert len(events) == 1
    assert events[0]["cues_seen"] == 0
    assert "nothing to do" in events[0]["detail"]

    rows = telemetry.read_rows(str(tmp_path / "telemetry.jsonl"))
    assert [r["event"] for r in rows] == [telemetry.EVENT_SERVICE_IDLE]


async def test_a_cue_firing_is_logged_with_its_position_and_confidence(tmp_path):
    svc = build_service(tmp_path, cue_at=10.0)
    with structlog.testing.capture_logs() as captured:
        await svc.run(speech_then_quiet())
    events = [c for c in captured
              if c.get("event") == "jeeves.service.cue_fired"]
    assert len(events) == 1
    assert events[0]["position_s"] == pytest.approx(10.0)
    assert events[0]["confidence"] == pytest.approx(0.9)


async def test_the_default_provenance_is_refused_in_synthetic_mode(tmp_path):
    """A service constructed without anyone declaring what it is listening
    to must not process anything."""
    cfg = build_config(tmp_path)
    cfg.mode = "synthetic"
    svc = service.JeevesService(
        cfg, audio_format=FMT,
        detector=ScriptedWakeDetector([10.0], audio_format=FMT),
        stt_backend=StubStt(),
    )
    outcomes = await svc.run(speech_then_quiet())
    assert outcomes[0].disposition == service.DISPOSITION_REFUSED


# ---------------------------------------------------------------------------
# Ring behaviour under the service
# ---------------------------------------------------------------------------


async def test_the_ring_never_exceeds_its_configured_size(tmp_path):
    """FENCE 2 under load: audio older than the ring is gone, whatever the
    stream does."""
    cfg = build_config(tmp_path)
    cfg.ring = JeevesRingConfig(
        seconds=4, sample_rate=1000, sample_width=2, channels=1)
    svc = build_service(tmp_path, config=cfg)
    await svc.run(speech_then_quiet(speech_s=30.0, quiet_s=4.0))
    assert svc.ring.held_seconds <= 4.0
    assert svc.ring.evicted_seconds > 0
