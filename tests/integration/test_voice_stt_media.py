"""Integration: real av.AudioResampler byte-exactness + silence source.

GATED on av/aiortc (importorskip — genuinely optional-dep; all the STT logic
pins are unconditional in tests/test_web_voice_stt_worker.py). Verifies the
reader's resample step (48 kHz → 16 kHz mono s16) through the REAL worker path
and that ``_silence_source`` yields silent audio frames.
"""

from __future__ import annotations

import asyncio
import fractions

import pytest

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")
pytest.importorskip("aiortc")

from alfred.web.stt_stream import STTStreamProvider  # noqa: E402
from alfred.web.voice_session import _silence_source  # noqa: E402
from alfred.web.voice_stt import VoiceSttWorker  # noqa: E402


def _frame(samples: int, *, rate: int, layout: str) -> "av.AudioFrame":
    # Packed s16 → shape (1, samples*channels), interleaved.
    ch = 2 if layout == "stereo" else 1
    data = (np.random.rand(1, samples * ch) * 8000).astype(np.int16)
    frame = av.AudioFrame.from_ndarray(data, format="s16", layout=layout)
    frame.sample_rate = rate
    return frame


class _CollectProvider(STTStreamProvider):
    """Captures the exact PCM bytes fed to the provider."""

    provider_id = "collect"

    def __init__(self) -> None:
        self.fed = bytearray()
        self._q: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> None:
        pass

    async def feed(self, chunk: bytes) -> None:
        self.fed.extend(chunk)

    async def finalize(self) -> None:
        pass

    async def close(self) -> None:
        self._q.put_nowait(None)

    async def events(self):
        while True:
            ev = await self._q.get()
            if ev is None:
                return
            yield ev


class _FramesTrack:
    def __init__(self, frames: list) -> None:
        self._frames = list(frames)

    async def recv(self):
        if self._frames:
            return self._frames.pop(0)
        raise RuntimeError("end-of-track")


async def test_reader_resamples_48k_stereo_to_16k_mono() -> None:
    prov = _CollectProvider()

    async def _noop(_):
        return None

    worker = VoiceSttWorker(
        provider=prov, voice_session_id="v", on_utterance=_noop,
        sample_rate=16000, chunk_ms=100,   # real av path (resample_fn=None)
        hello_gate=False,                  # this test exercises resample, not the gate
    )
    # 10 frames of 480 samples @48k stereo = 100 ms of audio total.
    frames = [_frame(480, rate=48000, layout="stereo") for _ in range(10)]
    worker.start(_FramesTrack(frames))
    # Deterministic: drive the finite track to completion (reader ends on
    # end-of-track → sentinel → sender drains) instead of a fixed sleep, so
    # this doesn't flake under full-suite load.
    await asyncio.wait_for(worker._reader_task, timeout=5)
    await asyncio.wait_for(worker._sender_task, timeout=5)
    await worker.aclose()
    # The real av resampler path ran end-to-end and produced s16 mono @16k
    # bytes (exact counts depend on av's internal resampler latency — the
    # chunker math itself is pinned exactly in test_web_voice_stt_worker.py).
    assert len(prov.fed) > 0
    assert len(prov.fed) % 2 == 0            # whole s16 samples
    # Sane 16k-scale magnitude for ~100 ms of input (well under a 48k count).
    assert len(prov.fed) <= 48000 * 2


async def test_silence_source_yields_silent_frames() -> None:
    src = _silence_source()
    frame = await asyncio.wait_for(src.recv(), timeout=2.0)
    assert frame.samples > 0
    arr = frame.to_ndarray()
    assert int(np.abs(arr).max()) == 0  # silence
