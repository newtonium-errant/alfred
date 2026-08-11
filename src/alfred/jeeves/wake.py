"""Local wake-word detection — fence 1 (task #81, stage 1).

This is the component that makes always-on listening compatible with a
privacy fence: it runs on the raw mic stream, on the device, and sends
NOTHING anywhere. Only after it fires does any audio leave the ring, and
only that window. Cost and privacy agree here — cued-only transcription is
roughly a hundred times cheaper than streaming the room, which is usually a
sign the shape is right.

A SEAM WITH A FAKE, on purpose. The real detector needs an ONNX model file
and a dependency that is not installed in this venv, so the suite would
either skip the whole capture chain or carry model binaries. Instead the
detector is an interface with three implementations — ``off`` (inert),
``fake`` (deterministic, scripted), ``openwakeword`` (the real one) — and
every test drives the service through the fake. The framing and refractory
logic that the real adapter depends on lives in :class:`_FrameAccumulator`
and :class:`_Refractory`, OUTSIDE the un-runnable branch, so the parts most
likely to be wrong are the parts the suite actually exercises.

**The wake word carries the burden** (design §3). "Jeeves" is three phonemes
with no common English collision, so the VERB after it can be classified
from the transcript (:mod:`.cues`) instead of needing its own acoustic
model. The Q6 trial measured the real confusion set — cheese / leaves /
Jesus / jeeps / eaves — and confirmed the signal survives the room over a
bass line; that set is carried in config and used only for telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

import structlog

from .audio import AudioChunk, AudioFormat
from .config import (
    WAKE_PROVIDER_FAKE,
    WAKE_PROVIDER_OFF,
    WAKE_PROVIDER_OPENWAKEWORD,
    JeevesWakeConfig,
)

log = structlog.get_logger(__name__)

# openWakeWord's native input frame: 1280 samples at 16 kHz (80 ms). The
# model is trained on that stride; feeding it arbitrary chunk sizes is the
# usual cause of "it works in the demo and never fires in the field".
OWW_FRAME_SAMPLES = 1280


class JeevesWakeError(Exception):
    """A wake detector could not be constructed — FAIL LOUD.

    Never degraded into a silent no-op detector: a device that believes it
    is listening and is not produces exactly the invisible failure mode the
    design calls out (cue false-negatives leave no trace anywhere).
    """


@dataclass(frozen=True)
class WakeEvent:
    """One wake-word detection.

    ``position_s`` is the stream position at which the detection FIRED — the
    end of the utterance that triggered it, which is the anchor the window
    extractor reaches backwards from.
    """

    position_s: float
    confidence: float
    model: str


@runtime_checkable
class WakeWordDetector(Protocol):
    """Consumes audio, emits detections. Nothing else."""

    def feed(self, chunk: AudioChunk) -> list[WakeEvent]:  # pragma: no cover
        ...

    def reset(self) -> None:  # pragma: no cover - protocol
        ...


class _FrameAccumulator:
    """Re-chunks an arbitrary PCM stream into fixed-size model frames.

    A mic adapter produces whatever its host hands it — 10 ms, 100 ms, or
    ragged. The model wants exactly one size. This carries the remainder
    across calls and reports each frame's stream position, so a detection is
    located in the stream rather than at "wherever the caller happened to be".
    """

    def __init__(self, audio_format: AudioFormat, frame_samples: int) -> None:
        self._format = audio_format
        self._frame_bytes = frame_samples * audio_format.frame_bytes
        if self._frame_bytes <= 0:
            raise ValueError("frame_samples must be positive")
        self._buffer = bytearray()
        # Stream position (bytes) of the first byte currently in _buffer.
        self._buffer_start_byte = 0
        self._seeded = False

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    def push(self, chunk: AudioChunk) -> list[tuple[bytes, float]]:
        """Add a chunk; return ``(frame_bytes, end_position_s)`` per complete
        frame. The position reported is the frame's END, because that is when
        a detection over that frame has actually happened."""
        if not self._seeded:
            self._buffer_start_byte = int(
                chunk.position_s * self._format.bytes_per_second)
            self._seeded = True
        self._buffer += chunk.data

        out: list[tuple[bytes, float]] = []
        while len(self._buffer) >= self._frame_bytes:
            frame = bytes(self._buffer[: self._frame_bytes])
            del self._buffer[: self._frame_bytes]
            self._buffer_start_byte += self._frame_bytes
            out.append((frame, self._format.duration_of(self._buffer_start_byte)))
        return out

    def reset(self) -> None:
        self._buffer.clear()
        self._seeded = False


class _Refractory:
    """Suppresses re-fires within ``seconds`` of the last accepted detection.

    One spoken wake word spans several model frames, so a raw detector emits
    a BURST. Without this every utterance becomes half a dozen cues, half a
    dozen STT calls and half a dozen near-identical captures — the operator
    would read it as the thing being broken, and it would be, expensively.
    """

    def __init__(self, seconds: float) -> None:
        self._seconds = max(0.0, seconds)
        self._last: float | None = None

    def accepts(self, position_s: float) -> bool:
        if self._last is not None and position_s - self._last < self._seconds:
            return False
        self._last = position_s
        return True

    def reset(self) -> None:
        self._last = None


class NullWakeDetector:
    """``provider: off`` — never fires.

    The fail-closed default, and the correct state for every instance that
    has no capture device. It logs ONCE at construction so "Jeeves heard
    nothing" is distinguishable from "Jeeves was never listening" — the
    intentionally-left-blank rule applied to the quietest possible path.
    """

    def __init__(self) -> None:
        log.info(
            "jeeves.wake.inert",
            provider=WAKE_PROVIDER_OFF,
            detail="wake provider is 'off' — the detector will never fire and "
                   "no audio will ever leave the ring. This is the "
                   "fail-closed default for an instance with no capture "
                   "device; set jeeves.wake.provider to arm it.",
        )

    def feed(self, chunk: AudioChunk) -> list[WakeEvent]:  # noqa: ARG002
        return []

    def reset(self) -> None:
        return None


class ScriptedWakeDetector:
    """``provider: fake`` — fires at pre-declared stream positions.

    The deterministic CI detector. It needs no model, no dependency and no
    audio content: a test says "a cue happens at t=50s" and the whole chain
    downstream of the detector runs for real.
    """

    def __init__(
        self,
        positions_s: Iterable[float],
        audio_format: AudioFormat | None = None,
        confidence: float = 0.9,
        model: str = "fake",
    ) -> None:
        self._pending = sorted(float(p) for p in positions_s)
        self._format = audio_format or AudioFormat()
        self._confidence = confidence
        self._model = model

    def feed(self, chunk: AudioChunk) -> list[WakeEvent]:
        chunk_end = chunk.position_s + self._format.duration_of(len(chunk.data))
        fired: list[WakeEvent] = []
        remaining: list[float] = []
        for position in self._pending:
            if chunk.position_s <= position < chunk_end:
                fired.append(WakeEvent(
                    position_s=position,
                    confidence=self._confidence,
                    model=self._model,
                ))
            elif position >= chunk_end:
                remaining.append(position)
            # A position already behind the stream is dropped: the fake must
            # not fire late and make a test pass for the wrong reason.
        self._pending = remaining
        return fired

    def reset(self) -> None:
        return None


class OpenWakeWordDetector:
    """``provider: openwakeword`` — the real on-device acoustic model.

    Requires the ``[jeeves]`` extra AND a model file. Both absences raise
    :class:`JeevesWakeError` at construction rather than degrading, because
    the degraded state (a detector that cannot fire) is invisible in
    operation: cue false-negatives leave no trace by construction, so a
    silently-inert detector would present as "Jeeves is ignoring me" with
    nothing in any log to contradict it.

    NOT EXERCISED BY THE SUITE. The dependency is not installed and no model
    binaries live in this repo, so the prediction call below is written
    against openWakeWord's documented API and verified only by review. The
    two pieces most likely to be wrong in the field — frame sizing and
    refractory suppression — were deliberately factored OUT of this class so
    the suite can test them (see :class:`_FrameAccumulator`,
    :class:`_Refractory`).
    """

    def __init__(
        self,
        audio_format: AudioFormat,
        model_path: str,
        threshold: float,
        refractory_seconds: float,
    ) -> None:
        if not model_path:
            raise JeevesWakeError(
                "jeeves.wake.provider is 'openwakeword' but "
                "jeeves.wake.model_path is empty — a real detector with no "
                "model is not a detector. Point model_path at the .onnx / "
                ".tflite wake-word model, or set provider to 'off'."
            )
        try:
            from openwakeword.model import Model  # type: ignore[import-not-found]
        except ImportError as exc:
            raise JeevesWakeError(
                "openWakeWord is not installed but jeeves.wake.provider is "
                "'openwakeword'. Install the optional extra "
                "(pip install 'alfred-vault[jeeves]') on the capture device, "
                "or set provider to 'off'. Refusing to run deaf."
            ) from exc

        try:
            self._model = Model(wakeword_models=[model_path])
        except Exception as exc:  # noqa: BLE001 — any load failure is fatal
            raise JeevesWakeError(
                f"openWakeWord failed to load the model at {model_path!r}: "
                f"{exc}"
            ) from exc

        self._threshold = threshold
        self._frames = _FrameAccumulator(audio_format, OWW_FRAME_SAMPLES)
        self._refractory = _Refractory(refractory_seconds)
        self._format = audio_format
        log.info(
            "jeeves.wake.armed",
            provider=WAKE_PROVIDER_OPENWAKEWORD,
            model_path=model_path,
            threshold=threshold,
            frame_samples=OWW_FRAME_SAMPLES,
        )

    def feed(self, chunk: AudioChunk) -> list[WakeEvent]:
        import numpy as np  # local: only the real detector needs numpy

        events: list[WakeEvent] = []
        for frame, position_s in self._frames.push(chunk):
            samples = np.frombuffer(frame, dtype=np.int16)
            scores = self._model.predict(samples)
            if not isinstance(scores, dict) or not scores:
                continue
            name, score = max(scores.items(), key=lambda kv: kv[1])
            if float(score) < self._threshold:
                continue
            if not self._refractory.accepts(position_s):
                continue
            events.append(WakeEvent(
                position_s=position_s,
                confidence=float(score),
                model=str(name),
            ))
        return events

    def reset(self) -> None:
        self._frames.reset()
        self._refractory.reset()


def build_detector(
    config: JeevesWakeConfig,
    audio_format: AudioFormat,
    *,
    scripted_positions: Iterable[float] | None = None,
) -> WakeWordDetector:
    """Construct the detector the config asks for.

    ``scripted_positions`` is honoured only by the ``fake`` provider; passing
    it with any other provider is ignored rather than an error, so a test
    fixture that flips the provider does not have to also strip the argument.
    """
    provider = config.provider
    if provider == WAKE_PROVIDER_OPENWAKEWORD:
        return OpenWakeWordDetector(
            audio_format=audio_format,
            model_path=config.model_path,
            threshold=config.threshold,
            refractory_seconds=config.refractory_seconds,
        )
    if provider == WAKE_PROVIDER_FAKE:
        return ScriptedWakeDetector(
            positions_s=scripted_positions or [], audio_format=audio_format,
        )
    return NullWakeDetector()
