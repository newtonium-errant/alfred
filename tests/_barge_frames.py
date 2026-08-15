"""Classify captured outbound audio frames by WHEN THEY WERE GENERATED.

Why this exists (the §1.6 barge pins were scoring the wrong clock)
------------------------------------------------------------------
The barge integration tests capture frames off a client ``RTCPeerConnection``
and ask "did any of T1's tone survive past the flush?". They used to answer it
with the frame's RECEIVE time. Under load that is the wrong clock: aiortc's
receive/decode path stalls and then delivers the whole backlog in a burst, so a
frame GENERATED before the flush can arrive well over a second after the barge
and be scored as post-flush leakage.

Measured, 20 runs under load (load avg 25-46 on a 12-core box), two batches of
``BARGE_DIAG_N=10`` against master 20714396:

  * 8 of 20 runs failed — 7x "stale T1 audio after the flush", 1x "T1 never
    spoke" (the same stall emptying ``pre`` instead of filling ``post``).
  * Delivery stall in every failure: 2.61-2.73 s. In passes: 0.29-2.70 s — one
    pass took the stall and survived on alignment luck, which is the flake.
  * Max asyncio event-loop lag across all 20 runs: 0.640 s. Never near the
    2.7 s stall, so the stall is in aiortc's receive path, not our loop.

And the product was ACQUITTED on the same data: counting contiguous runs of
loud frames in GENERATION order gives exactly 1 run in all 20 of 20 runs — the
tone plays and stops, and audio never resumes past the flush. The latest
generation time of any loud frame relative to the barge was +0.089 s worst
case, against the tests' 1.000 s guard band (an 11x margin).

``pts`` is the right clock because it is the SERVER's own monotonic sample
counter, carried on the wire as the RTP timestamp. That is not an assumption:
a probe that made a sender jump its pts by 48000 samples measured a received
delta of 48960, and ``aiortc/codecs/opus.py`` assigns
``packet.pts = encoded_frame.timestamp`` on decode.

The half-fix trap — both halves are ONE change
----------------------------------------------
Reclassifying the assertions is NOT sufficient on its own. The tests' wait loop
also counts RECEIVED frames, and a backlog burst satisfies that loop with ZERO
frames generated past the guard band. Measured on the same 20 runs: with the
wait loop left alone, 4 of 10 loaded runs end with an empty post-window and the
failure merely changes text to "no audio frames captured after the barge". So a
caller must use :func:`generated_past` for the wait loop AND
:func:`split_by_generation` for the assertions. ``tests/
test_barge_frame_classification.py`` pins all of this against two REAL captures.

Frames are ``(recv_time, pts, peak)`` triples throughout.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

# The outbound track's sample rate (``alfred.web.voice_tts.TRACK_RATE``). Kept
# local so this helper stays importable without the webrtc extra installed.
TRACK_RATE = 48000

Frame = tuple[float, int, int]


def generation_epoch(frames: Iterable[Frame]) -> float:
    """Wall-clock time at which ``pts == 0`` was generated.

    Delivery only ever ADDS delay, so the least-delayed frame in the capture
    fixes the epoch. Any residual bias is the best-case transit time and shifts
    generation times LATER — i.e. it biases toward reporting leakage, against
    the conclusion these pins draw, which is the safe direction.
    """
    return min(t - pts / TRACK_RATE for (t, pts, _) in frames)


def generated_at(epoch: float, pts: int) -> float:
    """Wall-clock generation time of the frame carrying ``pts``."""
    return epoch + pts / TRACK_RATE


def generated_past(frames: Sequence[Frame], cut: float) -> int:
    """How many frames were GENERATED after ``cut``.

    This is the wait-loop predicate. Counting received frames here is what a
    delivery stall satisfies vacuously.
    """
    if not frames:
        return 0
    epoch = generation_epoch(frames)
    return sum(1 for (_, pts, _) in frames if generated_at(epoch, pts) > cut)


def split_by_generation(
    frames: Sequence[Frame], barge_time: float, guard: float = 1.0,
) -> tuple[list[int], list[int]]:
    """Peaks generated before the barge, and those generated ``guard`` seconds
    after it — the window in which flushed T1 tone would still be ringing."""
    if not frames:
        return [], []
    epoch = generation_epoch(frames)
    pre = [p for (_, pts, p) in frames if generated_at(epoch, pts) < barge_time]
    post = [p for (_, pts, p) in frames
            if generated_at(epoch, pts) > barge_time + guard]
    return pre, post


def loud_generated_past(
    frames: Sequence[Frame], cut: float, loud_floor: int = 500,
) -> list[int]:
    """Peaks of the LOUD frames generated after ``cut``.

    The "did T2 actually speak after the flush" counter. Counting by receive
    time instead admits late-delivered T1 tone into T2's window, which pushes
    that pin toward a FALSE PASS — it would credit the flushed turn's audio to
    the new one. Measured on the captured stall: 5 such frames.
    """
    if not frames:
        return []
    epoch = generation_epoch(frames)
    return [p for (_, pts, p) in frames
            if generated_at(epoch, pts) > cut and p > loud_floor]


def loud_runs_in_generation_order(
    frames: Sequence[Frame], loud_floor: int = 500,
) -> int:
    """Contiguous runs of loud frames, walked in GENERATION order.

    The discriminator that acquitted the product: one run means the tone played
    and stopped, so any post-barge loud RECEIVE time is a delivery artifact.
    Two or more means audio genuinely resumed after the flush.
    """
    runs, in_run = 0, False
    for (_, _, peak) in sorted(frames, key=lambda f: f[1]):
        if peak > loud_floor:
            if not in_run:
                runs += 1
                in_run = True
        else:
            in_run = False
    return runs
