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
    # EXACT expectation (the loose <= 48000*2 upper bound HID the plane-padding
    # bug that inflated the stream): 10×480 samples @48k → downmix+downsample to
    # 16k mono = exactly 1600 samples = 3200 bytes once the end-of-track flush
    # drains the resampler. ±1 output-frame tolerance covers av-version
    # resampler-latency rounding; the padding bug added ~9 KB here (294 %).
    assert len(prov.fed) % 2 == 0            # whole s16 samples
    assert abs(len(prov.fed) - 3200) <= 64, (
        f"expected ~3200 B (100 ms @16k mono s16), got {len(prov.fed)} "
        "— plane padding leaking into the PCM stream?")


async def test_frame_to_pcm_strips_plane_padding() -> None:
    # THE padding pin: _frame_to_pcm must return exactly o.samples*2 bytes per
    # resampled frame (content-exact vs to_ndarray), NOT bytes(o.planes[0])
    # which includes FFmpeg's SIMD plane padding. Fails on the pre-fix code.
    prov = _CollectProvider()

    async def _noop(_):
        return None

    worker = VoiceSttWorker(
        provider=prov, voice_session_id="v", on_utterance=_noop,
        sample_rate=16000, chunk_ms=100,   # real av path (resample_fn=None)
    )
    # Deterministic input so an independent reference resampler agrees.
    data = ((np.arange(480 * 2) % 4000) - 2000).astype(np.int16).reshape(1, -1)

    def _mk():
        f = av.AudioFrame.from_ndarray(data, format="s16", layout="stereo")
        f.sample_rate = 48000
        return f

    r_worker = av.AudioResampler(format="s16", layout="mono", rate=16000)
    r_ref = av.AudioResampler(format="s16", layout="mono", rate=16000)
    got = b"".join(worker._frame_to_pcm(_mk(), r_worker))
    outs = r_ref.resample(_mk())
    if not isinstance(outs, list):
        outs = [outs] if outs is not None else []
    outs = [o for o in outs if o is not None and o.samples > 0]
    expected = b"".join(o.to_ndarray().tobytes() for o in outs)
    assert len(got) == sum(o.samples for o in outs) * 2   # exact sample count
    assert got == expected                                # padding-free content


async def test_resample_content_integrity_sine() -> None:
    # CONTENT pin (fake providers are content-blind): a pure sine through the
    # REAL resample path preserves RMS energy. The padding bug interleaved
    # garbage/zeros every frame, corrupting the energy — this catches that at
    # the media layer even though the byte COUNT could be argued benign.
    from alfred.web.utils import pcm_rms

    prov = _CollectProvider()

    async def _noop(_):
        return None

    worker = VoiceSttWorker(
        provider=prov, voice_session_id="v", on_utterance=_noop,
        sample_rate=16000, chunk_ms=100,
    )
    amp = 8000.0
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    out = bytearray()
    phase = 0
    # Small (480-sample) frames — the same shape as the real 48k track frames,
    # where the plane padding is a LARGE fraction, so a padded stream's RMS
    # (padding is zeros) drops well outside tolerance. Bigger frames would let
    # the padding hide under the average.
    for _ in range(40):                       # 40×480 @48k = 400 ms of sine
        idx = np.arange(phase, phase + 480)
        wave = (amp * np.sin(2 * np.pi * 440 * idx / 48000)).astype(np.int16)
        phase += 480
        frame = av.AudioFrame.from_ndarray(wave.reshape(1, -1),
                                           format="s16", layout="mono")
        frame.sample_rate = 48000
        for pcm in worker._frame_to_pcm(frame, resampler):
            out.extend(pcm)
    for pcm in worker._frame_to_pcm(None, resampler):   # flush
        out.extend(pcm)
    rms = pcm_rms(bytes(out))
    expected = amp / (2 ** 0.5)               # sine RMS = amp/√2 ≈ 5657
    assert abs(rms - expected) / expected < 0.15, (
        f"resampled sine RMS {rms:.0f} vs expected {expected:.0f} — content "
        "corrupted (plane padding injects zero-gaps every frame)")


async def test_silence_source_yields_silent_frames() -> None:
    src = _silence_source()
    frame = await asyncio.wait_for(src.recv(), timeout=2.0)
    assert frame.samples > 0
    arr = frame.to_ndarray()
    assert int(np.abs(arr).max()) == 0  # silence
