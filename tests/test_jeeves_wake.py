"""Wake-word detection seam (task #81, stage 1).

The real detector needs a dependency this venv does not have and a model
file this repo does not ship, so the two pieces most likely to be wrong in
the field — frame sizing and refractory suppression — were factored OUT of
that class specifically so they could be tested here. What CAN be tested of
the real adapter is its refusal behaviour, which is the part that decides
whether a mis-configured device fails loudly or listens to nothing forever.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.jeeves import wake
from alfred.jeeves.audio import AudioChunk, AudioFormat
from alfred.jeeves.config import (
    WAKE_PROVIDER_FAKE,
    WAKE_PROVIDER_OFF,
    WAKE_PROVIDER_OPENWAKEWORD,
    JeevesWakeConfig,
)

FMT = AudioFormat(sample_rate=1000, sample_width=2, channels=1)   # 2000 B/s


def chunk(nbytes: int, position_s: float) -> AudioChunk:
    return AudioChunk(data=b"\x01" * nbytes, position_s=position_s)


# ---------------------------------------------------------------------------
# Frame accumulation — the "works in the demo, never fires in the field" bug
# ---------------------------------------------------------------------------


def test_ragged_chunks_are_re_framed_to_the_model_size():
    """A mic adapter produces whatever its host hands it; the model wants
    exactly one size. Feeding arbitrary chunk sizes straight through is the
    usual cause of a detector that scores nothing on real audio."""
    acc = wake._FrameAccumulator(FMT, frame_samples=100)   # 200 bytes/frame
    assert acc.push(chunk(150, 0.0)) == []                 # short of a frame
    frames = acc.push(chunk(150, 0.15))                    # 300 total → 1 frame
    assert len(frames) == 1
    assert len(frames[0][0]) == 200


def test_frame_positions_are_the_frame_END_in_stream_time():
    """A detection over a frame has happened by the END of it, and the window
    extractor reaches backwards from that anchor."""
    acc = wake._FrameAccumulator(FMT, frame_samples=100)   # 0.1 s per frame
    frames = acc.push(chunk(600, 0.0))                     # 3 frames
    assert [round(p, 4) for _, p in frames] == [0.1, 0.2, 0.3]


def test_frame_positions_continue_from_where_the_stream_started():
    acc = wake._FrameAccumulator(FMT, frame_samples=100)
    frames = acc.push(chunk(200, 10.0))
    assert frames[0][1] == pytest.approx(10.1)


def test_the_remainder_carries_across_calls():
    acc = wake._FrameAccumulator(FMT, frame_samples=100)
    acc.push(chunk(250, 0.0))            # 1 frame, 50 bytes remain
    frames = acc.push(chunk(150, 0.125))  # 200 total → 1 frame
    assert len(frames) == 1


def test_reset_drops_the_remainder():
    acc = wake._FrameAccumulator(FMT, frame_samples=100)
    acc.push(chunk(150, 0.0))
    acc.reset()
    assert acc.push(chunk(150, 5.0)) == []


# ---------------------------------------------------------------------------
# Refractory — one utterance is one cue
# ---------------------------------------------------------------------------


def test_the_refractory_suppresses_a_detection_burst():
    """One spoken wake word spans several model frames, so a raw detector
    emits a burst. Without suppression every utterance becomes half a dozen
    captures and half a dozen paid STT calls."""
    ref = wake._Refractory(seconds=2.0)
    assert ref.accepts(10.0) is True
    assert ref.accepts(10.08) is False
    assert ref.accepts(11.9) is False
    assert ref.accepts(12.0) is True


def test_a_zero_refractory_accepts_everything():
    ref = wake._Refractory(seconds=0.0)
    assert ref.accepts(1.0) is True
    assert ref.accepts(1.0) is True


def test_reset_forgets_the_last_detection():
    ref = wake._Refractory(seconds=5.0)
    ref.accepts(10.0)
    ref.reset()
    assert ref.accepts(10.1) is True


# ---------------------------------------------------------------------------
# The inert default
# ---------------------------------------------------------------------------


def test_the_null_detector_never_fires():
    det = wake.NullWakeDetector()
    assert det.feed(chunk(2000, 0.0)) == []


def test_the_null_detector_says_it_is_inert():
    """Intentionally-left-blank at its quietest: 'Jeeves heard nothing' must
    be distinguishable from 'Jeeves was never listening'."""
    with structlog.testing.capture_logs() as captured:
        wake.NullWakeDetector()
    events = [c for c in captured if c.get("event") == "jeeves.wake.inert"]
    assert len(events) == 1
    assert "never fire" in events[0]["detail"]


# ---------------------------------------------------------------------------
# The scripted fake
# ---------------------------------------------------------------------------


def test_the_scripted_detector_fires_inside_the_chunk_that_contains_it():
    det = wake.ScriptedWakeDetector([1.5], audio_format=FMT)
    assert det.feed(chunk(2000, 0.0)) == []          # 0.0 – 1.0 s
    events = det.feed(chunk(2000, 1.0))              # 1.0 – 2.0 s
    assert len(events) == 1
    assert events[0].position_s == pytest.approx(1.5)


def test_the_scripted_detector_never_fires_late():
    """A fake that fires after its position would make a test pass for the
    wrong reason — the window it reaches back from would be wrong."""
    det = wake.ScriptedWakeDetector([0.5], audio_format=FMT)
    det.feed(chunk(2000, 1.0))                       # position already behind
    assert det.feed(chunk(2000, 2.0)) == []


def test_the_scripted_detector_fires_each_position_once():
    det = wake.ScriptedWakeDetector([0.2, 0.4], audio_format=FMT)
    first = det.feed(chunk(2000, 0.0))
    assert len(first) == 2
    assert det.feed(chunk(2000, 1.0)) == []


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def test_build_detector_dispatches_off_to_the_null_detector():
    det = wake.build_detector(
        JeevesWakeConfig(provider=WAKE_PROVIDER_OFF), FMT,
    )
    assert isinstance(det, wake.NullWakeDetector)


def test_build_detector_dispatches_fake_to_the_scripted_detector():
    det = wake.build_detector(
        JeevesWakeConfig(provider=WAKE_PROVIDER_FAKE), FMT,
        scripted_positions=[1.0],
    )
    assert isinstance(det, wake.ScriptedWakeDetector)
    assert det.feed(chunk(2000, 0.5)).__len__() == 1


def test_scripted_positions_are_ignored_by_other_providers():
    """A fixture that flips the provider should not also have to strip the
    argument."""
    det = wake.build_detector(
        JeevesWakeConfig(provider=WAKE_PROVIDER_OFF), FMT,
        scripted_positions=[1.0],
    )
    assert isinstance(det, wake.NullWakeDetector)


# ---------------------------------------------------------------------------
# The real adapter's refusals — the part that IS runnable here
# ---------------------------------------------------------------------------


def test_openwakeword_without_a_model_path_fails_LOUD():
    """A real detector with no model is not a detector. Degrading here would
    produce a device that believes it is listening and is not — and cue
    false-negatives leave no trace anywhere, so nothing would contradict it."""
    with pytest.raises(wake.JeevesWakeError) as exc:
        wake.build_detector(
            JeevesWakeConfig(
                provider=WAKE_PROVIDER_OPENWAKEWORD, model_path=""),
            FMT,
        )
    assert "model_path" in str(exc.value)
    assert "'off'" in str(exc.value)


def test_openwakeword_without_the_extra_installed_fails_LOUD(monkeypatch):
    """The refusal an un-provisioned capture device gets. Named the extra so
    the message is actionable from the garage."""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("openwakeword"):
            raise ImportError("No module named 'openwakeword'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(wake.JeevesWakeError) as exc:
        wake.build_detector(
            JeevesWakeConfig(
                provider=WAKE_PROVIDER_OPENWAKEWORD,
                model_path="/nonexistent/jeeves.onnx"),
            FMT,
        )
    assert "alfred-vault[jeeves]" in str(exc.value)
    assert "Refusing to run deaf" in str(exc.value)


def test_the_model_frame_size_is_openwakewords_native_stride():
    """1280 samples at 16 kHz (80 ms) is what the model is trained on."""
    assert wake.OWW_FRAME_SAMPLES == 1280
    assert AudioFormat().duration_of(
        wake.OWW_FRAME_SAMPLES * AudioFormat().frame_bytes,
    ) == pytest.approx(0.08)
