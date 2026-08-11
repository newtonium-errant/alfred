"""The RAM ring buffer (task #81, stage 1).

The ring is the feature, not the buffer: it exists so a cue can be
RETROSPECTIVE. So the tests that matter are about reaching BACKWARDS —
across a wrap, past the oldest byte still held, and reporting honestly when
the reach fell short.
"""

from __future__ import annotations

import pytest
import structlog

from alfred.jeeves.audio import AudioFormat
from alfred.jeeves.ring import RingBuffer

FMT = AudioFormat(sample_rate=1000, sample_width=2, channels=1)  # 2000 B/s


def pcm(nbytes: int, fill: int = 1) -> bytes:
    return bytes([fill]) * nbytes


def test_capacity_is_derived_from_format_and_seconds():
    ring = RingBuffer(FMT, seconds=2.0)
    assert ring.capacity_seconds == pytest.approx(2.0)
    assert ring.held_seconds == 0.0
    assert ring.position_s == 0.0


def test_rejects_a_nonpositive_or_sub_frame_ring():
    with pytest.raises(ValueError):
        RingBuffer(FMT, seconds=0)
    with pytest.raises(ValueError):
        RingBuffer(FMT, seconds=-1)
    # A ring too short to hold one whole frame holds nothing at all, and
    # would silently be a no-op buffer rather than a small one.
    with pytest.raises(ValueError):
        RingBuffer(AudioFormat(sample_rate=1, sample_width=2), seconds=0.0001)


def test_appending_under_capacity_holds_everything():
    ring = RingBuffer(FMT, seconds=2.0)
    ring.append(pcm(1000))
    assert ring.held_seconds == pytest.approx(0.5)
    assert ring.position_s == pytest.approx(0.5)
    assert ring.oldest_position_s == 0.0
    assert ring.evicted_seconds == 0.0


def test_the_wrap_evicts_the_oldest_audio_and_it_is_unreachable():
    """FENCE 2 as a behaviour: once the ring wraps, the evicted audio cannot
    be retrieved by any means the class offers."""
    ring = RingBuffer(FMT, seconds=1.0)          # 2000 bytes
    ring.append(pcm(2000, fill=0xAA))            # fills it exactly
    ring.append(pcm(2000, fill=0xBB))            # wraps it entirely

    assert ring.held_seconds == pytest.approx(1.0)
    assert ring.oldest_position_s == pytest.approx(1.0)
    assert ring.evicted_seconds == pytest.approx(1.0)

    # Everything still held is the NEW audio; the old fill is gone.
    read = ring.read_range(0.0, 2.0)
    assert set(read.audio) == {0xBB}
    # And reaching explicitly at the evicted span returns nothing.
    gone = ring.read_range(0.0, 1.0)
    assert gone.audio == b""


def test_partial_head_trim_keeps_the_ring_exactly_at_capacity():
    """The eviction path that slices the head rather than dropping it — the
    one that keeps a 30-minute ring from drifting a frame over forever."""
    ring = RingBuffer(FMT, seconds=1.0)          # 2000 bytes
    ring.append(pcm(1600))
    ring.append(pcm(800))                        # 2400 held → trim 400
    assert ring.held_seconds == pytest.approx(1.0)
    assert len(ring.read_range(0.0, 99.0).audio) == 2000


def test_read_last_returns_the_most_recent_span():
    ring = RingBuffer(FMT, seconds=10.0)
    ring.append(pcm(2000, fill=1))               # 0.0 – 1.0 s
    ring.append(pcm(2000, fill=2))               # 1.0 – 2.0 s

    read = ring.read_last(1.0)
    assert set(read.audio) == {2}
    assert read.seconds == pytest.approx(1.0)
    assert read.truncated_by_ring is False


def test_a_reach_past_the_oldest_byte_reports_truncation():
    """RULING 5's datum depends on this. A cue that wanted two minutes and
    got ninety seconds must SAY it was short-changed — silently returning
    less would make the lookback distribution answer a question nobody asked.
    """
    ring = RingBuffer(FMT, seconds=1.0)
    ring.append(pcm(2000))                       # exactly one second held

    read = ring.read_last(5.0)
    assert read.truncated_by_ring is True
    assert read.requested_seconds == pytest.approx(5.0)
    assert read.seconds == pytest.approx(1.0)


def test_a_reach_into_the_future_is_not_truncation():
    """Asking past the write head is asking for audio that does not exist
    YET (the lookahead's job) — a different fact from the ring having
    already dropped it, and it must not pollute the truncation signal."""
    ring = RingBuffer(FMT, seconds=10.0)
    ring.append(pcm(2000))                       # 1.0 s held, head at 1.0 s

    read = ring.read_range(0.0, 5.0)
    assert read.truncated_by_ring is False
    assert read.seconds == pytest.approx(1.0)


def test_reading_an_empty_ring_is_empty_not_an_error():
    ring = RingBuffer(FMT, seconds=1.0)
    read = ring.read_last(0.5)
    assert read.is_empty
    assert read.truncated_by_ring is True


def test_clear_drops_everything_but_keeps_the_timeline_monotonic():
    """After a clear, a window request from before it must be refused as
    unavailable — never served audio from a different era."""
    ring = RingBuffer(FMT, seconds=10.0)
    ring.append(pcm(2000, fill=7))
    position_before = ring.position_s

    ring.clear()

    assert ring.held_seconds == 0.0
    assert ring.position_s == pytest.approx(position_before)
    assert ring.oldest_position_s == pytest.approx(position_before)
    assert ring.read_last(1.0).audio == b""


def test_clear_on_an_empty_ring_still_says_so():
    """Intentionally-left-blank: a shutdown with no traffic is a real event
    and must not be indistinguishable from a clear that never ran."""
    ring = RingBuffer(FMT, seconds=1.0)
    with structlog.testing.capture_logs() as captured:
        ring.clear()
    events = [c for c in captured if c.get("event") == "jeeves.ring.cleared"]
    assert len(events) == 1
    assert events[0]["dropped_seconds"] == 0.0


def test_clear_logs_what_it_dropped():
    ring = RingBuffer(FMT, seconds=10.0)
    ring.append(pcm(2000))
    with structlog.testing.capture_logs() as captured:
        ring.clear()
    events = [c for c in captured if c.get("event") == "jeeves.ring.cleared"]
    assert len(events) == 1
    assert events[0]["dropped_seconds"] == pytest.approx(1.0)


def test_appending_nothing_is_a_no_op():
    ring = RingBuffer(FMT, seconds=1.0)
    ring.append(b"")
    assert ring.position_s == 0.0
    assert ring.held_seconds == 0.0


def test_a_thirty_minute_ring_at_the_shipped_defaults_reaches_back_thirty_minutes():
    """The ratified size, exercised at the real format so the arithmetic in
    the config docstring (~57.6 MB) is not just an assertion in prose."""
    fmt = AudioFormat()                          # 16 kHz mono 16-bit
    ring = RingBuffer(fmt, seconds=1800)
    assert ring.capacity_seconds == pytest.approx(1800.0)
    assert fmt.bytes_for(1800) == 1800 * 16000 * 2
