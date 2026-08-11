"""Audio format + the capture adapter seam (task #81, stage 1).

THE MIC IS NOT HERE, and that is the point. :class:`AudioSource` is a thin
adapter interface; the real microphone implementation belongs to whatever
host ends up running the device (tablet foreground service, Pi ALSA capture),
which is stage-3 territory. Stage 1 ships the service driving the two
in-repo sources below, which is also exactly what the tests drive — so the
whole capture chain is exercised end to end without a microphone, without
model files, and without touching a network.

Everything speaks RAW PCM BYTES in one declared :class:`AudioFormat`. There
is no decoding, resampling, or format negotiation anywhere in this package:
a source declares what it produces, the ring is built to match, and a
mismatch is refused loudly at the seam rather than silently corrupting both
the wake model's input and the STT upload.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Iterable, Iterator, Protocol, runtime_checkable


@dataclass(frozen=True)
class AudioFormat:
    """Raw-PCM format declaration.

    Frozen and compared by value, so "does this source match this ring" is a
    plain ``==`` rather than a field-by-field check some future caller
    forgets half of.
    """

    sample_rate: int = 16000
    sample_width: int = 2
    channels: int = 1

    def __post_init__(self) -> None:
        if self.sample_rate <= 0 or self.sample_width <= 0 or self.channels <= 0:
            raise ValueError(
                "AudioFormat requires positive sample_rate / sample_width / "
                f"channels; got {self.sample_rate}/{self.sample_width}/"
                f"{self.channels}"
            )

    @property
    def bytes_per_second(self) -> int:
        return self.sample_rate * self.sample_width * self.channels

    @property
    def frame_bytes(self) -> int:
        """Bytes in one sample frame (all channels)."""
        return self.sample_width * self.channels

    def bytes_for(self, seconds: float) -> int:
        """Byte count for ``seconds`` of audio, rounded DOWN to a whole frame.

        Rounding down rather than up matters at the ring's capacity edge: a
        partial frame at the boundary would put the buffer permanently one
        fragment over its declared size, and every duration this module
        reports back would be off by that fragment.
        """
        if seconds <= 0:
            return 0
        raw = int(seconds * self.bytes_per_second)
        return raw - (raw % self.frame_bytes)

    def duration_of(self, nbytes: int) -> float:
        """Seconds represented by ``nbytes`` of audio in this format."""
        if nbytes <= 0:
            return 0.0
        return nbytes / self.bytes_per_second


@dataclass(frozen=True)
class AudioChunk:
    """One contiguous run of PCM produced by a source.

    ``position_s`` is the chunk's START offset in the SOURCE STREAM — a
    monotonic count of audio produced, not a wall clock. Everything in this
    package measures time that way: a stream position derived from byte
    counts is exact, survives a clock adjustment, and makes window
    arithmetic ordinary integer maths instead of timestamp reconciliation.
    """

    data: bytes
    position_s: float


@runtime_checkable
class AudioSource(Protocol):
    """A source of raw PCM.

    Deliberately synchronous and pull-shaped: the caller decides when to take
    the next chunk. A mic adapter wraps its own thread/callback and hands
    chunks over; nothing in this package needs an event loop to capture.
    """

    @property
    def audio_format(self) -> AudioFormat:  # pragma: no cover - protocol
        ...

    def chunks(self) -> Iterator[AudioChunk]:  # pragma: no cover - protocol
        ...


class MemoryAudioSource:
    """An in-memory source over a list of PCM blobs — the test/fake adapter.

    Positions are derived from the running byte count, so a caller can hand
    it uneven chunk sizes and still get an exact stream timeline.
    """

    def __init__(
        self,
        blobs: Iterable[bytes],
        audio_format: AudioFormat | None = None,
    ) -> None:
        self._blobs = [bytes(b) for b in blobs]
        self._format = audio_format or AudioFormat()

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    def chunks(self) -> Iterator[AudioChunk]:
        written = 0
        for blob in self._blobs:
            yield AudioChunk(data=blob, position_s=self._format.duration_of(written))
            written += len(blob)


class RawFileAudioSource:
    """A source over a headerless raw-PCM file, read in fixed-size chunks.

    Headerless on purpose: this is a development/replay adapter, not a
    general audio importer, and adding container parsing here would be the
    first step toward this module growing the decoding responsibility the
    module docstring rules out. A trial recording is converted to raw PCM
    once, outside this package, and replayed through here.
    """

    def __init__(
        self,
        path: str,
        audio_format: AudioFormat | None = None,
        chunk_seconds: float = 0.08,
    ) -> None:
        self._path = path
        self._format = audio_format or AudioFormat()
        self._chunk_bytes = max(
            self._format.frame_bytes, self._format.bytes_for(chunk_seconds),
        )

    @property
    def audio_format(self) -> AudioFormat:
        return self._format

    def chunks(self) -> Iterator[AudioChunk]:
        written = 0
        with open(self._path, "rb") as fh:
            while True:
                blob = fh.read(self._chunk_bytes)
                if not blob:
                    return
                yield AudioChunk(
                    data=blob, position_s=self._format.duration_of(written),
                )
                written += len(blob)


def frame_rms(data: bytes, audio_format: AudioFormat) -> float:
    """Normalized RMS (0..1) of a 16-bit PCM run — the silence measure.

    Only 16-bit is implemented because that is the one format this package
    declares; anything else returns 0.0, which reads as silence. That is the
    SAFE direction for the one consumer: a lookahead that treats unknown
    audio as silence ENDS the window early (a short capture) rather than
    running to the 60-second cap on every cue.
    """
    if audio_format.sample_width != 2 or not data:
        return 0.0
    usable = len(data) - (len(data) % 2)
    if usable <= 0:
        return 0.0
    samples = struct.unpack_from(f"<{usable // 2}h", data, 0)
    total = 0.0
    for s in samples:
        total += float(s) * float(s)
    return math.sqrt(total / len(samples)) / 32768.0


def silence(seconds: float, audio_format: AudioFormat | None = None) -> bytes:
    """``seconds`` of digital silence — the fixture primitive for tests and
    for padding a replay stream."""
    fmt = audio_format or AudioFormat()
    return b"\x00" * fmt.bytes_for(seconds)


def tone(
    seconds: float,
    audio_format: AudioFormat | None = None,
    amplitude: float = 0.3,
    frequency: float = 220.0,
) -> bytes:
    """``seconds`` of a sine tone — SYNTHETIC audio, the only kind this repo
    ever contains. Loud enough at the default amplitude to read as speech to
    :func:`frame_rms`, which is all any test needs from it."""
    fmt = audio_format or AudioFormat()
    nbytes = fmt.bytes_for(seconds)
    nframes = nbytes // fmt.frame_bytes
    peak = int(max(0.0, min(1.0, amplitude)) * 32767)
    out = bytearray()
    for i in range(nframes):
        value = int(peak * math.sin(2.0 * math.pi * frequency * i / fmt.sample_rate))
        frame = struct.pack("<h", value)
        out += frame * fmt.channels
    return bytes(out)
