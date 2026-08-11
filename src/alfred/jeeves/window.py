"""Window extraction — how much audio a cue takes (task #81, stage 1).

A cue reaches BACKWARDS into the ring (the retrospective lookback that is the
whole reason the ring exists) and FORWARDS a little, so that "Jeeves, note
that — the bearing is the wrong size, order the 6203" is captured as one
utterance rather than cut off mid-sentence.

THE TWO-PASS PROBLEM, and why it is solved the expensive-looking way. The
lookback MODIFIER ("note the last couple of minutes") lives in the
transcript, and the transcript requires the window, which requires the
lookback. The options were: always extract the extended lookback (≈3× the
audio uploaded on EVERY cue, which discards the cost argument that makes
cued-only viable), or transcribe the default window, classify it, and
re-extract only when a modifier was actually spoken. This package takes the
second: modifier cues are rare, the ring still holds the audio when the
first transcript comes back, and the extra STT call is counted in telemetry
so its real frequency becomes a measured number rather than a guess.

JUDGE THE WHOLE WINDOW (Q6 trial finding, 2026-08-11). The trial's
conversation-only file came back with ``has_speech_signal=false`` — a
first-segment ``no_speech_prob`` artefact of a long ambient opening — on a
file containing 5,126 words of coherent speech. Nothing here or in
:mod:`.stt` may gate on a first-segment flag. The silence detector below
follows the same rule: it requires CONTIGUOUS silence for the whole
configured span, never terminating on the first quiet frame.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from .audio import AudioChunk, AudioFormat, frame_rms
from .config import JeevesWindowConfig
from .ring import RingBuffer

log = structlog.get_logger(__name__)

# Why the lookahead stopped. Distinct values because they mean different
# things about the capture: silence is a complete utterance, the cap is a
# capture that may have been cut off, and stream-end is a device shutting
# down mid-capture.
LOOKAHEAD_SILENCE = "silence"
LOOKAHEAD_CAP = "cap"
LOOKAHEAD_STREAM_END = "stream_end"

# Where the lookback figure came from.
LOOKBACK_DEFAULT = "default"
LOOKBACK_MODIFIER = "modifier"


@dataclass(frozen=True)
class ResolvedLookback:
    """The lookback a cue will actually use, and how it got there."""

    seconds: float
    source: str
    #: True when a spoken modifier asked for more than the configured
    #: ceiling. Recorded rather than silently honoured: an STT mis-hear
    #: ("the last thirty minutes") would otherwise upload the entire ring.
    clamped: bool = False


@dataclass(frozen=True)
class ExtractedWindow:
    """One cued window, ready for STT.

    ``lookback_used_seconds`` is ruling 5's datum — the number whose
    distribution over a few weeks answers whether a 30-minute ring is 30×
    oversized or exactly right. It is the ACTUAL reach, after the ring
    clamped it, which is why ``truncated_by_ring`` travels beside it: a
    capture that wanted more than the ring held is the signal that the ring
    is too small, and it is invisible if only the requested figure is kept.
    """

    audio: bytes
    cue_position_s: float
    lookback_used_seconds: float
    requested_lookback_seconds: float
    lookahead_used_seconds: float
    truncated_by_ring: bool
    lookback_clamped: bool
    lookahead_end_reason: str

    @property
    def seconds(self) -> float:
        return self.lookback_used_seconds + self.lookahead_used_seconds

    @property
    def is_empty(self) -> bool:
        return not self.audio


def resolve_lookback(
    requested_seconds: float | None, config: JeevesWindowConfig,
) -> ResolvedLookback:
    """Turn a (possibly absent) spoken modifier into a lookback figure.

    ``None`` means no modifier was spoken → the configured default, which the
    design labels a guess and the telemetry exists to replace.
    """
    if requested_seconds is None:
        return ResolvedLookback(
            seconds=config.lookback_seconds, source=LOOKBACK_DEFAULT,
        )
    ceiling = config.max_lookback_seconds
    if requested_seconds > ceiling:
        log.warning(
            "jeeves.window.lookback_clamped",
            requested_seconds=round(requested_seconds, 3),
            ceiling_seconds=round(ceiling, 3),
            detail="a spoken lookback modifier exceeded "
                   "jeeves.window.max_lookback_seconds and was clamped. A "
                   "mis-heard number would otherwise upload most of the ring "
                   "to the cloud in one call.",
        )
        return ResolvedLookback(
            seconds=ceiling, source=LOOKBACK_MODIFIER, clamped=True,
        )
    return ResolvedLookback(seconds=requested_seconds, source=LOOKBACK_MODIFIER)


class LookaheadCollector:
    """Accumulates audio after a cue until the utterance finishes.

    Stateful and fed chunk-by-chunk because that is how a live capture
    arrives — the service pushes the same chunks it is already pushing into
    the ring, and asks after each whether the window is done.

    Termination is CONTIGUOUS silence, not "a silent frame". A single quiet
    gap between two words is normal speech; ending there would truncate
    every capture at its first breath.
    """

    def __init__(
        self, audio_format: AudioFormat, config: JeevesWindowConfig,
    ) -> None:
        self._format = audio_format
        self._config = config
        self._buffer = bytearray()
        self._silence_run_bytes = 0
        self._done = False
        self._end_reason = ""
        self._cap_bytes = audio_format.bytes_for(config.max_lookahead_seconds)
        self._silence_bytes = audio_format.bytes_for(config.silence_seconds)

    @property
    def done(self) -> bool:
        return self._done

    @property
    def audio(self) -> bytes:
        return bytes(self._buffer)

    @property
    def seconds(self) -> float:
        return self._format.duration_of(len(self._buffer))

    @property
    def end_reason(self) -> str:
        return self._end_reason

    def feed(self, chunk: AudioChunk) -> bool:
        """Add a chunk; return True once the window is complete."""
        if self._done:
            return True
        self._buffer += chunk.data

        if frame_rms(chunk.data, self._format) < self._config.silence_rms_threshold:
            self._silence_run_bytes += len(chunk.data)
        else:
            # Any audible frame resets the run — contiguity is the rule.
            self._silence_run_bytes = 0

        if self._silence_bytes and self._silence_run_bytes >= self._silence_bytes:
            self._finish(LOOKAHEAD_SILENCE)
        elif len(self._buffer) >= self._cap_bytes:
            self._finish(LOOKAHEAD_CAP)
        return self._done

    def close(self) -> None:
        """End the window because the stream ended (shutdown mid-capture)."""
        if not self._done:
            self._finish(LOOKAHEAD_STREAM_END)

    def _finish(self, reason: str) -> None:
        self._done = True
        self._end_reason = reason
        # Trim a cap overshoot so the reported duration is the real one.
        if len(self._buffer) > self._cap_bytes:
            del self._buffer[self._cap_bytes:]
        log.info(
            "jeeves.window.lookahead_complete",
            reason=reason,
            seconds=round(self.seconds, 3),
        )


def extract(
    ring: RingBuffer,
    *,
    cue_position_s: float,
    lookback: ResolvedLookback,
    lookahead_audio: bytes = b"",
    lookahead_end_reason: str = LOOKAHEAD_STREAM_END,
) -> ExtractedWindow:
    """Cut the window out of the ring and glue the lookahead onto it.

    The lookahead is passed in rather than read from the ring because the
    collector already holds exactly those bytes; reading them back out would
    mean depending on the ring not having wrapped in the interim, which for a
    long capture on a small ring is precisely the case that fails.
    """
    read = ring.read_range(
        cue_position_s - lookback.seconds,
        cue_position_s,
        requested_seconds=lookback.seconds,
    )
    audio = read.audio + lookahead_audio
    window = ExtractedWindow(
        audio=audio,
        cue_position_s=cue_position_s,
        lookback_used_seconds=read.seconds,
        requested_lookback_seconds=lookback.seconds,
        lookahead_used_seconds=ring.audio_format.duration_of(len(lookahead_audio)),
        truncated_by_ring=read.truncated_by_ring,
        lookback_clamped=lookback.clamped,
        lookahead_end_reason=lookahead_end_reason,
    )
    if window.is_empty:
        # Intentionally-left-blank: a cue that extracted nothing (an empty
        # ring at startup, or a clear between cue and extraction) is a real
        # event with a real cause, and must not present as "no cue fired".
        log.warning(
            "jeeves.window.empty",
            cue_position_s=round(cue_position_s, 3),
            requested_lookback_seconds=round(lookback.seconds, 3),
            ring_held_seconds=round(ring.held_seconds, 3),
            detail="a cue fired but the window extracted zero audio — the "
                   "ring held nothing at that position",
        )
    else:
        log.info(
            "jeeves.window.extracted",
            cue_position_s=round(cue_position_s, 3),
            lookback_used_seconds=round(window.lookback_used_seconds, 3),
            requested_lookback_seconds=round(lookback.seconds, 3),
            lookahead_used_seconds=round(window.lookahead_used_seconds, 3),
            truncated_by_ring=window.truncated_by_ring,
            lookback_source=lookback.source,
        )
    return window
