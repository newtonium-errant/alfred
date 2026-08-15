"""Deterministic pins for the §1.6 barge audio classification.

These replay TWO REAL CAPTURES taken while the flake was reproducing under load
(see ``tests/fixtures/voice_barge/``), so the fix for a timing bug is not itself
proved by timing. Nothing here touches aiortc, a socket, or a clock — it is
arithmetic over recorded frames, and therefore runs unconditionally rather than
behind the webrtc extra's skipif (the integration tests that produced the
captures are gated; the classification rule they got wrong must not be).

The three findings each get a pin, plus a positive control:

1. the shipped RECEIVE-time classification scores pre-flush tone as leakage,
2. GENERATION-time classification clears the very same capture,
3. reclassifying ALONE leaves the window unjudgeable — which is why the wait
   loop moves in the same change,
4. and on a capture without a delivery stall the window is populated AND
   silent, so pin 2 is a check that can actually fail.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests._barge_frames import (
    generated_at,
    generated_past,
    generation_epoch,
    loud_runs_in_generation_order,
    split_by_generation,
)

FIXTURES = Path(__file__).parent / "fixtures" / "voice_barge"

# The captures store receive times RELATIVE to the barge, so the barge is 0.0.
BARGE = 0.0
GUARD = 1.0          # the guard band both integration tests use
LOUD = 500           # the amplitude floor both integration tests use


def _capture(name: str) -> list[tuple[float, int, int]]:
    payload = json.loads((FIXTURES / f"{name}.json").read_text())
    return [tuple(f) for f in payload["frames"]]


def _recv_post(frames, cut: float = BARGE + GUARD) -> list[int]:
    """What the SHIPPED tests used to compute: peaks by receive time."""
    return [p for (t, _, p) in frames if t > cut]


def test_recv_time_classification_scores_preflush_tone_as_leakage() -> None:
    """The flake, replayed. Receive time puts full-amplitude T1 tone in the
    post-flush window even though the flush had already stopped the audio."""
    frames = _capture("loaded_failure_recv_stall")

    post = _recv_post(frames)
    assert post, "capture is supposed to contain post-window frames by recv time"
    # This is the shipped assertion (`max(post) < 500`) going red: peak 6985.
    assert max(post) >= LOUD

    # And every loud frame it scored was generated BEFORE the barge — delivered
    # 2.2-2.3 s late by a 2.702 s stall in aiortc's receive path.
    epoch = generation_epoch(frames)
    late = [(t, generated_at(epoch, pts)) for (t, pts, p) in frames
            if t > BARGE + GUARD and p > LOUD]
    assert len(late) == 5
    assert all(gen < BARGE for (_, gen) in late), late
    assert all(recv > BARGE + GUARD for (recv, _) in late), late


def test_generation_classification_clears_the_same_capture() -> None:
    """The product, acquitted on the identical bytes: no tone was GENERATED
    past the flush. The pre-window assertion is the positive control — it
    proves this classifier can see loud frames at all, so the empty post-window
    below is a finding rather than a classifier that returns nothing."""
    frames = _capture("loaded_failure_recv_stall")

    pre, post = split_by_generation(frames, BARGE, GUARD)
    assert pre and max(pre) > LOUD          # positive control: T1 did speak
    assert not [p for p in post if p > LOUD]

    # The discriminator itself: the tone forms ONE contiguous run in generation
    # order, so it played and stopped. A gen-gate leak would show a second run.
    assert loud_runs_in_generation_order(frames) == 1


def test_reclassifying_alone_leaves_the_window_unjudgeable() -> None:
    """Why the wait loop is part of the same change.

    The shipped wait loop breaks once 10 frames have been RECEIVED past the
    guard band, which the backlog burst satisfies instantly. On this capture
    that leaves ZERO frames actually generated past the band — so a fix that
    only re-keyed the assertions would trade "stale T1 audio" for "no audio
    frames captured after the barge" (measured: 4 of 10 loaded runs).
    """
    frames = _capture("loaded_failure_recv_stall")

    # The old wait-loop predicate is satisfied...
    assert len(_recv_post(frames)) >= 10
    # ...while the honest one is not, on the same frames.
    assert generated_past(frames, BARGE + GUARD) == 0


def test_clean_capture_gives_a_judgeable_and_silent_window() -> None:
    """Positive control for the whole rule: same scenario, same load, no
    delivery stall. The window is populated (so the assertion is live) and
    every frame in it is silent (so the flush really did stop the tone)."""
    frames = _capture("loaded_pass_clean_delivery")

    assert generated_past(frames, BARGE + GUARD) >= 10
    pre, post = split_by_generation(frames, BARGE, GUARD)
    assert pre and max(pre) > LOUD          # T1 spoke
    assert post and max(post) < LOUD        # and nothing survived the flush
    assert loud_runs_in_generation_order(frames) == 1


def test_recv_time_counting_contaminates_the_t2_spoke_counter() -> None:
    """The sibling pin's exposure, and why it is re-keyed too.

    ``test_barge_then_t2_speaks_over_the_track`` asserts loud frames are
    PRESENT after the barge (T2's tone survived the resampler). Counting those
    by receive time admits late-delivered T1 frames, which pushes that pin
    toward a FALSE PASS — it would credit T1's flushed tone as T2 speaking.
    Generation-time counting admits none of them.
    """
    frames = _capture("loaded_failure_recv_stall")

    by_recv = [p for (t, _, p) in frames if t > BARGE + GUARD and p > LOUD]
    epoch = generation_epoch(frames)
    by_gen = [p for (_, pts, p) in frames
              if generated_at(epoch, pts) > BARGE + GUARD and p > LOUD]

    assert len(by_recv) == 5      # T1 tone, credited to the post-barge window
    assert by_gen == []           # nothing was generated there at all
