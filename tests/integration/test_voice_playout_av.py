"""Integration: TTSPlayoutSource over the REAL av resampler + frame factory.

GATED on av (importorskip). The unit tests inject the frame/resample seams;
this pins the actual av path — 24 k → 48 k resample and the AudioFrame build —
that only runs in production + the full-loop aiortc test (which has WebRTC
timing). Deterministic here (no network / no pc).
"""

from __future__ import annotations

import pytest

av = pytest.importorskip("av")
np = pytest.importorskip("numpy")

from alfred.web.voice_tts import FRAME_BYTES, TRACK_RATE, TTSPlayoutSource  # noqa: E402


async def test_real_resampler_24k_to_48k_frames() -> None:
    # Real av path: no injected seams (source_rate 24000 → the _AvResampler is
    # built; frames come from the default av frame factory).
    p = TTSPlayoutSource(source_rate=24000, voice_session_id="v", max_buffer_seconds=30.0)
    played: list = []
    p.on_turn_played = played.append

    # 0.5 s of a 24 k tone (12000 samples s16).
    t = np.arange(12000) / 24000
    wave = (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    await p.enqueue_pcm("t1", wave.tobytes())
    p.mark_end_of_turn("t1")

    frames = []
    for _ in range(60):                      # 60 * 20 ms = 1.2 s (covers 0.5 s @48k + pad)
        frame = await p.recv()
        frames.append(frame)
        if played:
            break

    # Every frame is a real s16 / mono / 48 k / 960-sample AudioFrame with
    # strictly monotonic pts (the documented hazard, pinned on the av path).
    ptss = [f.pts for f in frames]
    assert ptss == list(range(0, 960 * len(frames), 960))
    for f in frames:
        assert f.sample_rate == TRACK_RATE
        assert f.format.name == "s16"
        assert f.samples == 960
        assert bytes(f.planes[0]).__len__() == FRAME_BYTES
    # The 0.5 s tone resampled to 48 k drained → on_turn_played fired.
    assert played == ["t1"]
    # And a speech frame carried real (non-silent) audio.
    peaks = [int(np.abs(f.to_ndarray()).max()) for f in frames]
    assert max(peaks) > 500
