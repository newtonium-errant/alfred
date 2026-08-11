"""The RAM ring buffer — fence 2 made structural (task #81, stage 1).

**Non-cued audio dies with the ring.** This module is where that stops being
a promise and becomes a property of the code: there is no import of ``os``,
``pathlib`` or ``open`` in this file, no method that takes a path, and no
method that returns one. A :class:`RingBuffer` cannot write itself anywhere.
Eviction IS the wrap, and an evicted chunk is dropped on the floor — there is
no retrieval path, no undo, and no second copy. (Pinned structurally in
``tests/test_jeeves_ring.py``.)

**Why the ring is the feature, not the buffer.** A buffer that only captures
FORWARD from a cue is press-to-talk with extra steps. Holding thirty minutes
of audio is what lets the cue be RETROSPECTIVE — the operator says something
worth keeping, realises ten seconds later that it was, and says "Jeeves, note
that". The value is entirely in the lookback, which is why the primary cue
form is past-tense and why "how far back did we actually reach" is the one
number the design instruments from day one.

**Time is a byte count, not a clock.** Stream position is derived from bytes
written, so window arithmetic is exact integer maths, a system clock
adjustment cannot move a window, and two components asked "where are we"
always agree.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import structlog

from .audio import AudioFormat

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RingRead:
    """The result of asking the ring for a span of audio.

    ``requested_seconds`` vs ``seconds`` is the whole reason this is a
    dataclass rather than a bare ``bytes``: when a cue asks for two minutes
    and the ring only held ninety seconds, the CALLER has to know it was
    short-changed — that is the ``lookback_used_seconds`` datum ruling 5
    exists to collect, and silently returning less audio would make the
    telemetry answer a question nobody asked.
    """

    audio: bytes
    #: Stream position (seconds) of the first returned byte.
    start_s: float
    #: Stream position (seconds) just past the last returned byte.
    end_s: float
    #: Span the caller asked for, before clamping to what the ring held.
    requested_seconds: float
    #: True when the ring could not reach as far back as the request wanted.
    truncated_by_ring: bool

    @property
    def seconds(self) -> float:
        return max(0.0, self.end_s - self.start_s)

    @property
    def is_empty(self) -> bool:
        return not self.audio


class RingBuffer:
    """A fixed-capacity, RAM-only, circular PCM buffer.

    Implemented as a bounded FIFO of chunks rather than one pre-allocated
    block. The trade is deliberate: a single ``bytearray`` with a moving
    write head would need a ``del buf[:n]`` memmove of the WHOLE buffer on
    every append, which at a 30-minute (~57 MB) capacity and 12 appends a
    second is hundreds of MB/s of pointless copying on exactly the small
    device this is meant to run on. Evicting whole chunks costs nothing, and
    the one partial slice per eviction is a single frame-sized copy.
    """

    def __init__(self, audio_format: AudioFormat, seconds: float) -> None:
        if seconds <= 0:
            raise ValueError(f"ring seconds must be positive; got {seconds}")
        self._format = audio_format
        self._capacity_bytes = audio_format.bytes_for(seconds)
        if self._capacity_bytes <= 0:
            raise ValueError(
                f"ring of {seconds}s holds no whole frames at "
                f"{audio_format.sample_rate}Hz/{audio_format.frame_bytes}B"
            )
        self._chunks: deque[bytes] = deque()
        self._held_bytes = 0
        # Absolute stream position (BYTES) of the first byte still held, and
        # of the next byte to be written. Both only ever increase; their
        # difference is _held_bytes.
        self._oldest_byte = 0
        self._write_byte = 0
        self._evicted_bytes = 0

    # -- properties ------------------------------------------------------

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    @property
    def capacity_seconds(self) -> float:
        return self._format.duration_of(self._capacity_bytes)

    @property
    def held_seconds(self) -> float:
        """How much audio is retrievable right now."""
        return self._format.duration_of(self._held_bytes)

    @property
    def position_s(self) -> float:
        """Stream position of the write head — total audio ever appended."""
        return self._format.duration_of(self._write_byte)

    @property
    def oldest_position_s(self) -> float:
        """Stream position of the oldest byte still held. Everything before
        this has wrapped and is gone."""
        return self._format.duration_of(self._oldest_byte)

    @property
    def evicted_seconds(self) -> float:
        """Total audio the wrap has discarded. Reported for observability —
        it is the one number that says the fence is doing its job."""
        return self._format.duration_of(self._evicted_bytes)

    # -- mutation --------------------------------------------------------

    def append(self, data: bytes) -> None:
        """Append PCM, evicting from the front to stay within capacity."""
        if not data:
            return
        self._chunks.append(bytes(data))
        self._held_bytes += len(data)
        self._write_byte += len(data)
        self._evict_to_capacity()

    def _evict_to_capacity(self) -> None:
        while self._held_bytes > self._capacity_bytes and self._chunks:
            head = self._chunks[0]
            overflow = self._held_bytes - self._capacity_bytes
            if len(head) <= overflow:
                self._chunks.popleft()
                self._held_bytes -= len(head)
                self._oldest_byte += len(head)
                self._evicted_bytes += len(head)
            else:
                # Trim the head to the frame boundary at or beyond the
                # overflow, so the ring never holds a partial frame.
                cut = overflow + ((-overflow) % self._format.frame_bytes)
                cut = min(cut, len(head))
                self._chunks[0] = head[cut:]
                self._held_bytes -= cut
                self._oldest_byte += cut
                self._evicted_bytes += cut

    def clear(self) -> None:
        """Drop everything held, keeping the stream timeline intact.

        Used on shutdown and on a mode change. The stream position does NOT
        reset: positions stay globally monotonic, so a window request that
        arrives just after a clear is refused as unavailable rather than
        silently served audio from a different era.
        """
        dropped = self._held_bytes
        self._chunks.clear()
        self._held_bytes = 0
        self._oldest_byte = self._write_byte
        self._evicted_bytes += dropped
        if dropped:
            log.info(
                "jeeves.ring.cleared",
                dropped_seconds=round(self._format.duration_of(dropped), 3),
            )
        else:
            # Intentionally-left-blank: a clear on an already-empty ring is a
            # real event (shutdown with no traffic) and must not look like a
            # clear that never ran.
            log.info("jeeves.ring.cleared", dropped_seconds=0.0,
                     detail="ring was already empty")

    # -- retrieval -------------------------------------------------------

    def read_last(self, seconds: float) -> RingRead:
        """The most recent ``seconds`` of audio, clamped to what is held."""
        return self.read_range(self.position_s - seconds, self.position_s,
                               requested_seconds=seconds)

    def read_range(
        self,
        start_s: float,
        end_s: float,
        *,
        requested_seconds: float | None = None,
    ) -> RingRead:
        """Audio between two stream positions, clamped to what is held.

        Both bounds are clamped rather than raising: a cue that reaches
        further back than the ring holds is a NORMAL, expected event (it is
        the signal that the ring is too small), not an error condition.
        """
        want = requested_seconds if requested_seconds is not None else max(
            0.0, end_s - start_s)

        lo_byte = max(self._oldest_byte, self._to_byte(start_s))
        hi_byte = min(self._write_byte, self._to_byte(end_s))
        if hi_byte <= lo_byte:
            return RingRead(
                audio=b"",
                start_s=self._format.duration_of(max(lo_byte, hi_byte)),
                end_s=self._format.duration_of(max(lo_byte, hi_byte)),
                requested_seconds=want,
                truncated_by_ring=want > 0,
            )

        out = bytearray()
        cursor = self._oldest_byte
        for chunk in self._chunks:
            chunk_end = cursor + len(chunk)
            if chunk_end > lo_byte and cursor < hi_byte:
                begin = max(0, lo_byte - cursor)
                finish = min(len(chunk), hi_byte - cursor)
                out += chunk[begin:finish]
            cursor = chunk_end
            if cursor >= hi_byte:
                break

        # Truncated when the ring could not reach as far back as asked. The
        # comparison is on the LOW end only: a request that runs past the
        # write head is asking for audio that does not exist YET (the
        # lookahead's job), which is a different fact from the ring having
        # already dropped it.
        #
        # UNCLAMPED on purpose. At stream start a 45-second lookback asks for
        # position -44s, and clamping that to zero before comparing would
        # report "not truncated" for the very captures most likely to be
        # short — the first ones after a restart, which is exactly when the
        # operator would be testing the thing.
        truncated = self._to_byte_unclamped(start_s) < self._oldest_byte
        return RingRead(
            audio=bytes(out),
            start_s=self._format.duration_of(lo_byte),
            end_s=self._format.duration_of(hi_byte),
            requested_seconds=want,
            truncated_by_ring=truncated,
        )

    def _to_byte(self, position_s: float) -> int:
        """Stream position (seconds) → absolute byte offset, frame-aligned,
        floored at the stream start."""
        return max(0, self._to_byte_unclamped(position_s))

    def _to_byte_unclamped(self, position_s: float) -> int:
        """As :meth:`_to_byte` but able to go NEGATIVE — a position before
        the stream began. Only the truncation test wants this; every other
        caller wants the floored value."""
        raw = int(position_s * self._format.bytes_per_second)
        return raw - (raw % self._format.frame_bytes)
