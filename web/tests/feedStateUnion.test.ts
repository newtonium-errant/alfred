import { describe, it, expect } from 'vitest';
import { FEED_STATES, isFeedState, type FeedState } from '../lib/algernon/feed';

// PY-C item 5, web half. `FeedState` is a CLOSED union mirroring the backend's
// states, and `retired` (alfred.feed.model.STATE_RETIRED) is the sixth. A closed
// union missing a state the backend can hold is not a neutral omission: it makes
// TS narrow a real value into the wrong arm, and `isFeedState` — the deliberate
// opt-in seam where an untrusted wire string becomes the union — would reject a
// legitimate card as unrecognised.
//
// These are contract pins. `FEED_STATES`/`isFeedState` have no consumer in the
// app yet (same posture `deferred` shipped in), which is exactly why they need a
// pin: nothing else in the suite would notice them drifting from the backend.

describe('FeedState — the closed union and its runtime twin', () => {
  it('admits every state the backend store can hold, including retired', () => {
    expect(FEED_STATES).toEqual(['open', 'acted', 'acked', 'expired', 'deferred', 'retired']);
  });

  it('narrows a retired card rather than rejecting it as unrecognised', () => {
    expect(isFeedState('retired')).toBe(true);
  });

  it('still rejects a string that is not a state — the guard is not a rubber stamp', () => {
    // The positive control for the assertion above: without this, "retired
    // narrows" passes identically against an isFeedState that returns true for
    // anything, which would defeat the whole point of the seam.
    expect(isFeedState('withdrawn')).toBe(false);
    expect(isFeedState('')).toBe(false);
    expect(isFeedState(null)).toBe(false);
  });

  it('keeps the type and its runtime twin in step', () => {
    // `retired` must be assignable to the TYPE, not merely present in the
    // array — the array is annotated `readonly FeedState[]`, so this fails to
    // COMPILE if the union itself was not widened. tsc is the assertion here.
    const retired: FeedState = 'retired';
    expect(FEED_STATES).toContain(retired);
  });
});
