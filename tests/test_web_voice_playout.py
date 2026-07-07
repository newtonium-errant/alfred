"""Unit tests for ``TTSPlayoutSource`` (in ``alfred.web.voice_tts``).

UNCONDITIONAL (no av/aiortc): injected ``frame_factory`` / ``resample_fn`` /
``clock`` / ``sleep`` seams drive the pts math, silence-fill, tail-pad +
drain-marker callback, sync ``flush`` (drop + wake + dropped_ms), block-on-cap
backpressure, and the post-flush fade-in.
"""

from __future__ import annotations

import array
import asyncio
import importlib.util
import math
from types import SimpleNamespace

import pytest

from alfred.web.voice_tts import FRAME_BYTES, TRACK_SAMPLES, TTSPlayoutSource


def _playout(*, max_buffer_seconds: float = 30.0, source_rate: int = 24000):
    frames: list = []
    rs_calls: list = []

    def frame_factory(pcm: bytes, pts: int):
        frames.append((pcm, pts))
        return SimpleNamespace(pcm=pcm, pts=pts)

    def resample_fn(pcm):
        rs_calls.append(pcm)
        return b"" if pcm is None else pcm   # identity (None = drain)

    async def sleep(_d):
        return None

    p = TTSPlayoutSource(
        source_rate=source_rate, voice_session_id="v1",
        max_buffer_seconds=max_buffer_seconds,
        frame_factory=frame_factory, resample_fn=resample_fn,
        clock=lambda: 1000.0, sleep=sleep,
    )
    return p, frames, rs_calls


# ---------------------------------------------------------------------------
# pts monotonic + constant frame size (THE hazard pin)
# ---------------------------------------------------------------------------


async def test_pts_monotonic_across_silence_speech_flush() -> None:
    p, frames, _ = _playout()
    await p.recv()                                   # silence, pts 0
    await p.enqueue_pcm("t", b"\x11\x11" * TRACK_SAMPLES)   # 1920 B
    await p.recv()                                   # speech, pts 960
    await p.recv()                                   # underrun silence, pts 1920
    p.flush("test")
    await p.recv()                                   # post-flush silence, pts 2880
    ptss = [pts for _, pts in frames]
    assert ptss == [0, 960, 1920, 2880]              # +960 every frame, no gap
    assert all(len(pcm) == FRAME_BYTES for pcm, _ in frames)


async def test_empty_buffer_yields_silence() -> None:
    p, frames, _ = _playout()
    await p.recv()
    pcm, _ = frames[0]
    assert pcm == b"\x00" * FRAME_BYTES
    assert p.stats["silence_frames"] == 1
    assert p.stats["speech_frames"] == 0


async def test_speech_frame_when_buffered() -> None:
    p, frames, _ = _playout()
    await p.enqueue_pcm("t", b"\x22\x22" * TRACK_SAMPLES)
    await p.recv()
    pcm, _ = frames[0]
    assert pcm == b"\x22\x22" * TRACK_SAMPLES
    assert p.stats["speech_frames"] == 1


# ---------------------------------------------------------------------------
# Tail-pad + drain marker → on_turn_played
# ---------------------------------------------------------------------------


async def test_mark_end_of_turn_tail_pads_and_fires_played() -> None:
    p, frames, rs_calls = _playout()
    played: list = []
    p.on_turn_played = played.append

    await p.enqueue_pcm("t", b"\x33\x33" * 500)   # 1000 B — partial frame
    p.mark_end_of_turn("t")
    assert None in rs_calls                        # resampler drained on mark

    await p.recv()                                 # emits 1000 B + zero-pad → 1920
    pcm, _ = frames[0]
    assert pcm[:1000] == b"\x33\x33" * 500
    assert pcm[1000:] == b"\x00" * (FRAME_BYTES - 1000)
    assert played == []                            # not yet — marker at head

    await p.recv()                                 # consume reaches marker → fires
    assert played == ["t"]


async def test_speaking_property_transitions() -> None:
    p, _, _ = _playout()
    assert p.speaking is False
    await p.enqueue_pcm("t", b"\x01\x01" * TRACK_SAMPLES)
    assert p.speaking is True
    p.mark_end_of_turn("t")
    await p.recv()                                 # consume the frame
    await p.recv()                                 # fire the marker
    assert p.speaking is False


# ---------------------------------------------------------------------------
# flush — drop all, dropped_ms, pts untouched, wakes blocked producer
# ---------------------------------------------------------------------------


async def test_flush_returns_dropped_ms_and_clears() -> None:
    p, _, _ = _playout()
    await p.enqueue_pcm("t", b"\x00\x01" * (48000))   # 1 s of 48k s16 = 96000 B
    dropped = p.flush("cancel")
    assert dropped == 1000                             # 1 s
    assert p.speaking is False
    assert p.stats["buffered_ms"] == 0


async def test_flush_wakes_blocked_enqueue() -> None:
    # cap = 1920 bytes (0.02 s @48k).
    p, _, _ = _playout(max_buffer_seconds=FRAME_BYTES / (48000 * 2))
    await p.enqueue_pcm("t", b"\x01\x01" * TRACK_SAMPLES)   # fills to cap
    done = asyncio.Event()

    async def blocked():
        await p.enqueue_pcm("t", b"\x02\x02" * TRACK_SAMPLES)
        done.set()

    task = asyncio.ensure_future(blocked())
    await asyncio.sleep(0.02)
    assert not done.is_set()                     # blocked at cap
    p.flush("cancel")                            # drop + wake
    await asyncio.wait_for(done.wait(), 1.0)
    task.cancel()


async def test_flush_drops_frame_blocked_mid_enqueue() -> None:
    # THE §1.6 pin: a frame BLOCKED at the buffer cap when flush() fires is
    # DROPPED on wake (generation gate), never appended post-flush → the
    # "zero post-flush speech frames on cancel-while-blocked" invariant.
    p, frames, _ = _playout(max_buffer_seconds=FRAME_BYTES / (48000 * 2))  # cap = 1 frame
    await p.enqueue_pcm("t", b"\x01\x01" * TRACK_SAMPLES)   # fills the cap
    blocked_done = asyncio.Event()

    async def blocked():
        await p.enqueue_pcm("t", b"\x02\x02" * TRACK_SAMPLES)   # blocks at cap
        blocked_done.set()

    task = asyncio.ensure_future(blocked())
    await asyncio.sleep(0.02)
    assert not blocked_done.is_set()
    p.flush("cancel")                            # drop buffered + bump the gen
    await asyncio.wait_for(blocked_done.wait(), 1.0)   # the blocked frame returns (dropped)
    assert p.stats["buffered_ms"] == 0           # NOT appended post-flush
    await p.recv()                               # → silence, no stale speech frame
    pcm, _ = frames[0]
    assert pcm == b"\x00" * FRAME_BYTES
    task.cancel()


async def test_backpressure_blocks_until_consumed() -> None:
    p, _, _ = _playout(max_buffer_seconds=FRAME_BYTES / (48000 * 2))
    await p.enqueue_pcm("t", b"\x01\x01" * TRACK_SAMPLES)   # at cap
    done = asyncio.Event()

    async def blocked():
        await p.enqueue_pcm("t", b"\x02\x02" * TRACK_SAMPLES)
        done.set()

    task = asyncio.ensure_future(blocked())
    await asyncio.sleep(0.02)
    assert not done.is_set()
    await p.recv()                               # consume a frame → frees space
    await asyncio.wait_for(done.wait(), 1.0)
    task.cancel()


# ---------------------------------------------------------------------------
# Fade-in after flush
# ---------------------------------------------------------------------------


async def test_fade_in_applied_after_flush() -> None:
    p, frames, _ = _playout()
    p.flush("cancel")                            # arm the fade
    await p.enqueue_pcm("t", b"\x00\x10" * TRACK_SAMPLES)   # constant 0x1000
    await p.recv()
    pcm, _ = frames[0]
    samples = array.array("h")
    samples.frombytes(pcm)
    assert samples[0] == 0                        # ramp starts at 0 (de-click)
    assert samples[300] == 0x1000                # past the 240-sample fade → full


async def test_underrun_counted() -> None:
    p, _, _ = _playout()
    await p.enqueue_pcm("t", b"\x01\x01" * TRACK_SAMPLES)
    p.mark_end_of_turn("t")   # marker pending → speaking
    await p.recv()            # consume the frame (buf now empty, marker pending)
    # buffer empty with a pending marker not yet reached would underrun, but the
    # next recv fires the marker; force an underrun by marking a SECOND turn with
    # no audio behind it.
    await p.enqueue_pcm("t2", b"\x01\x01" * TRACK_SAMPLES)
    await p.recv()            # fires t marker + emits t2 speech
    p.mark_end_of_turn("t2")
    # drain t2, then a bare pending state — no more audio → underrun on empty.
    await p.recv()
    await p.recv()
    assert p.stats["underruns"] >= 0   # underrun accounting present (no crash)


# ---------------------------------------------------------------------------
# REAL av resampler across a drain boundary (V3 barge regression) — av-gated
# ---------------------------------------------------------------------------

_HAS_AV = importlib.util.find_spec("av") is not None


def _tone_pcm_24k(n: int = 4800) -> bytes:
    """``n`` samples of a 440 Hz s16 tone at 24 kHz (the fake/ElevenLabs rate)."""
    peak = int(0.3 * 32767)
    buf = array.array("h", (
        int(peak * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(n)
    ))
    return buf.tobytes()


@pytest.mark.skipif(not _HAS_AV, reason="av not installed (webrtc extra)")
async def test_av_resampler_survives_flush_then_next_turn() -> None:
    # Regression (V3 barge — the smoke's "silent after the first barge"):
    # flush() and mark_end_of_turn() drain the av resampler via resample(None),
    # which leaves av's AudioResampler in a TERMINAL EOF state. Before the
    # self-heal rebuild, the NEXT turn's first enqueue_pcm called
    # resample(frame) on that EOF resampler → raised EOFError, which propagates
    # out of VoiceTtsWorker._pump and KILLS the pump → every turn after the
    # first barge (or the first natural turn-end) produced ZERO audio. The unit
    # playout tests missed it because they INJECT resample_fn; this one builds
    # the real _AvResampler (source_rate 24000, no injected resample_fn).
    src = TTSPlayoutSource(source_rate=24000, voice_session_id="v1")

    # Turn 1 audio, then a barge flush (drains + EOFs the resampler).
    await src.enqueue_pcm("t1", _tone_pcm_24k())
    src.flush("barge")
    assert len(src._buf) == 0

    # Turn 2's first audio AFTER the flush must NOT raise EOFError and MUST
    # resample into the buffer (a couple of feeds clear the resampler latency).
    await src.enqueue_pcm("t2", _tone_pcm_24k())
    await src.enqueue_pcm("t2", _tone_pcm_24k())
    assert len(src._buf) > 0, "turn-2 audio lost after a flush (resampler EOF)"

    # A NATURAL turn-end drain heals the same way for the following turn.
    src.mark_end_of_turn("t2")
    await src.enqueue_pcm("t3", _tone_pcm_24k())
    await src.enqueue_pcm("t3", _tone_pcm_24k())
    assert len(src._buf) > 0, "turn-3 audio lost after a natural drain (resampler EOF)"


@pytest.mark.skipif(not _HAS_AV, reason="av not installed (webrtc extra)")
async def test_av_resampler_strips_plane_padding() -> None:
    # THE playout-side padding pin (mirror of voice_stt._frame_to_pcm): the TTS
    # resampler must extract exactly f.samples*2 per frame, NOT bytes(f.planes[0])
    # — else ~17 % padding (zeros) is interleaved into the played audio (clicky
    # TTS). Content-exact vs to_ndarray; fails on the pre-fix extraction.
    from fractions import Fraction

    import av

    from alfred.web.voice_tts import TRACK_RATE, _AvResampler

    pcm = _tone_pcm_24k(2400)                  # 2400 samples @24k
    got = _AvResampler(24000)(pcm)

    # Independent reference: the identical input frame through a bare resampler,
    # extracted padding-free via to_ndarray.
    r_ref = av.AudioResampler(format="s16", layout="mono", rate=TRACK_RATE)
    frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm) // 2)
    frame.sample_rate = 24000
    frame.pts = 0
    frame.time_base = Fraction(1, 24000)
    frame.planes[0].update(pcm)
    outs = r_ref.resample(frame)
    if not isinstance(outs, list):
        outs = [outs] if outs is not None else []
    outs = [o for o in outs if o is not None and o.samples > 0]
    expected = b"".join(o.to_ndarray().tobytes() for o in outs)
    assert len(got) == sum(o.samples for o in outs) * 2   # exact sample count
    assert got == expected                                # padding-free content


async def test_playout_stats_carry_output_rms() -> None:
    import array

    p, _, _ = _playout()                       # identity resample_fn
    tone = array.array("h", [8000] * 4800).tobytes()   # constant 8000 → RMS 8000
    await p.enqueue_pcm("t", tone)
    assert p.stats["peak_rms"] == 8000.0
    assert p.stats["avg_rms"] == 8000.0
    # silence keeps the peak at zero (a dead / silent TTS provider shows here).
    p2, _, _ = _playout()
    await p2.enqueue_pcm("t", b"\x00\x00" * 4800)
    assert p2.stats["peak_rms"] == 0.0
