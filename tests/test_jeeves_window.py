"""Window extraction (task #81, stage 1).

Two behaviours carry the design: the lookahead must require CONTIGUOUS
silence (a breath between words is not the end of a sentence), and the
extraction must report honestly when the ring could not reach as far back as
the cue asked for — that report is ruling 5's datum.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.jeeves import window
from alfred.jeeves.audio import AudioChunk, AudioFormat, silence, tone
from alfred.jeeves.config import JeevesWindowConfig
from alfred.jeeves.ring import RingBuffer

FMT = AudioFormat(sample_rate=1000, sample_width=2, channels=1)   # 2000 B/s
CFG = JeevesWindowConfig(
    lookback_seconds=45.0,
    extended_lookback_seconds=120.0,
    silence_seconds=3.0,
    max_lookahead_seconds=10.0,
    max_lookback_seconds=300.0,
    silence_rms_threshold=0.01,
)


def chunk(data: bytes, position_s: float = 0.0) -> AudioChunk:
    return AudioChunk(data=data, position_s=position_s)


# ---------------------------------------------------------------------------
# Lookback resolution
# ---------------------------------------------------------------------------


def test_no_modifier_uses_the_configured_default():
    resolved = window.resolve_lookback(None, CFG)
    assert resolved.seconds == pytest.approx(45.0)
    assert resolved.source == window.LOOKBACK_DEFAULT
    assert resolved.clamped is False


def test_a_spoken_modifier_is_honoured():
    resolved = window.resolve_lookback(120.0, CFG)
    assert resolved.seconds == pytest.approx(120.0)
    assert resolved.source == window.LOOKBACK_MODIFIER
    assert resolved.clamped is False


def test_an_absurd_modifier_is_clamped_and_says_so():
    """A mis-heard "the last thirty minutes" would otherwise upload most of
    the ring to the cloud in one call — the cost argument and the privacy
    fence both undone by one bad transcription."""
    with structlog.testing.capture_logs() as captured:
        resolved = window.resolve_lookback(1800.0, CFG)
    assert resolved.seconds == pytest.approx(300.0)
    assert resolved.clamped is True
    events = [c for c in captured
              if c.get("event") == "jeeves.window.lookback_clamped"]
    assert len(events) == 1
    assert events[0]["requested_seconds"] == pytest.approx(1800.0)


# ---------------------------------------------------------------------------
# The lookahead — contiguity is the rule
# ---------------------------------------------------------------------------


def test_a_single_quiet_frame_does_not_end_the_window():
    """A gap between two words is normal speech. Ending there would truncate
    every capture at its first breath."""
    collector = window.LookaheadCollector(FMT, CFG)
    assert collector.feed(chunk(tone(1.0, FMT))) is False
    assert collector.feed(chunk(silence(1.0, FMT))) is False   # 1s < 3s
    assert collector.feed(chunk(tone(1.0, FMT))) is False
    assert collector.done is False


def test_contiguous_silence_ends_the_window():
    collector = window.LookaheadCollector(FMT, CFG)
    collector.feed(chunk(tone(1.0, FMT)))
    for _ in range(3):
        collector.feed(chunk(silence(1.0, FMT)))
    assert collector.done is True
    assert collector.end_reason == window.LOOKAHEAD_SILENCE


def test_an_audible_frame_resets_the_silence_run():
    """Two seconds of quiet, a word, then two more seconds of quiet is NOT
    three contiguous seconds — the run restarts."""
    collector = window.LookaheadCollector(FMT, CFG)
    collector.feed(chunk(silence(2.0, FMT)))
    collector.feed(chunk(tone(0.5, FMT)))
    collector.feed(chunk(silence(2.0, FMT)))
    assert collector.done is False


def test_the_cap_ends_a_window_that_never_goes_quiet():
    collector = window.LookaheadCollector(FMT, CFG)
    for _ in range(20):
        if collector.feed(chunk(tone(1.0, FMT))):
            break
    assert collector.done is True
    assert collector.end_reason == window.LOOKAHEAD_CAP
    # The cap is a real bound, not an approximate one.
    assert collector.seconds == pytest.approx(CFG.max_lookahead_seconds)


def test_closing_ends_a_window_at_stream_end():
    """A device shutting down mid-capture is a distinct fact from a
    completed utterance, and the reason records which it was."""
    collector = window.LookaheadCollector(FMT, CFG)
    collector.feed(chunk(tone(0.5, FMT)))
    collector.close()
    assert collector.done is True
    assert collector.end_reason == window.LOOKAHEAD_STREAM_END


def test_closing_an_already_finished_window_does_not_relabel_it():
    collector = window.LookaheadCollector(FMT, CFG)
    for _ in range(3):
        collector.feed(chunk(silence(1.0, FMT)))
    assert collector.end_reason == window.LOOKAHEAD_SILENCE
    collector.close()
    assert collector.end_reason == window.LOOKAHEAD_SILENCE


def test_completion_is_logged_with_its_reason():
    collector = window.LookaheadCollector(FMT, CFG)
    with structlog.testing.capture_logs() as captured:
        for _ in range(3):
            collector.feed(chunk(silence(1.0, FMT)))
    events = [c for c in captured
              if c.get("event") == "jeeves.window.lookahead_complete"]
    assert len(events) == 1
    assert events[0]["reason"] == window.LOOKAHEAD_SILENCE


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _filled_ring(seconds: float = 60.0, audio_seconds: float = 30.0):
    ring = RingBuffer(FMT, seconds=seconds)
    ring.append(tone(audio_seconds, FMT))
    return ring


def test_extraction_reaches_backwards_from_the_cue():
    """The retrospective reach — the whole reason the ring exists."""
    ring = _filled_ring()
    extracted = window.extract(
        ring, cue_position_s=30.0,
        lookback=window.ResolvedLookback(10.0, window.LOOKBACK_DEFAULT),
    )
    assert extracted.lookback_used_seconds == pytest.approx(10.0)
    assert extracted.truncated_by_ring is False
    assert len(extracted.audio) == FMT.bytes_for(10.0)


def test_the_lookahead_is_glued_on_after_the_lookback():
    ring = _filled_ring()
    tail = tone(2.0, FMT)
    extracted = window.extract(
        ring, cue_position_s=30.0,
        lookback=window.ResolvedLookback(5.0, window.LOOKBACK_DEFAULT),
        lookahead_audio=tail,
        lookahead_end_reason=window.LOOKAHEAD_SILENCE,
    )
    assert extracted.lookahead_used_seconds == pytest.approx(2.0)
    assert extracted.seconds == pytest.approx(7.0)
    assert extracted.audio.endswith(tail)


def test_a_reach_past_the_ring_reports_the_truncation():
    """RULING 5. A capture that wanted more than the ring held is the signal
    that the ring is too small — and it is invisible if only the REQUESTED
    figure is kept."""
    ring = RingBuffer(FMT, seconds=5.0)
    ring.append(tone(30.0, FMT))                # wraps; 5 s held
    extracted = window.extract(
        ring, cue_position_s=30.0,
        lookback=window.ResolvedLookback(45.0, window.LOOKBACK_DEFAULT),
    )
    assert extracted.requested_lookback_seconds == pytest.approx(45.0)
    assert extracted.lookback_used_seconds == pytest.approx(5.0)
    assert extracted.truncated_by_ring is True


def test_the_clamped_flag_travels_into_the_window():
    ring = _filled_ring()
    extracted = window.extract(
        ring, cue_position_s=30.0,
        lookback=window.ResolvedLookback(
            300.0, window.LOOKBACK_MODIFIER, clamped=True),
    )
    assert extracted.lookback_clamped is True


def test_an_empty_extraction_is_reported_not_silent():
    """Intentionally-left-blank: a cue that extracted nothing has a real
    cause (an empty ring at startup, a clear between cue and extraction) and
    must not present as 'no cue fired'."""
    ring = RingBuffer(FMT, seconds=5.0)
    with structlog.testing.capture_logs() as captured:
        extracted = window.extract(
            ring, cue_position_s=0.0,
            lookback=window.ResolvedLookback(45.0, window.LOOKBACK_DEFAULT),
        )
    assert extracted.is_empty
    events = [c for c in captured if c.get("event") == "jeeves.window.empty"]
    assert len(events) == 1
    assert events[0]["ring_held_seconds"] == 0.0


def test_a_successful_extraction_logs_the_lookback_actually_used():
    """The telemetry-bearing log line. If this stops carrying
    lookback_used_seconds, the operator's grep answers nothing."""
    ring = _filled_ring()
    with structlog.testing.capture_logs() as captured:
        window.extract(
            ring, cue_position_s=30.0,
            lookback=window.ResolvedLookback(10.0, window.LOOKBACK_DEFAULT),
        )
    events = [c for c in captured if c.get("event") == "jeeves.window.extracted"]
    assert len(events) == 1
    assert events[0]["lookback_used_seconds"] == pytest.approx(10.0)
    assert events[0]["requested_lookback_seconds"] == pytest.approx(10.0)
    assert events[0]["truncated_by_ring"] is False
