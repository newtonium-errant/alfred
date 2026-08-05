import { describe, expect, it } from 'vitest';
import {
  SNOOZE_ACTIONS,
  SNOOZE_INDEFINITE_ACTION,
  SNOOZE_LABELS,
  SNOOZE_X_TOLERANCE,
  SNOOZE_Y_THRESHOLD,
  STAMP_FADE_START,
  affirmLabelFor,
  deckVerbsFor,
  inSnoozeHoldBand,
  isDeckDealt,
  snoozeActionFor,
  snoozeIsBacked,
} from '../lib/algernon/feedConstants';
import type { FeedItem } from '../lib/algernon/feed';

// The C2 deck routing predicate + slot verbs. isDeckDealt is the ONE predicate the
// deck's dealing AND the deck-link count both use (team-lead ruling) — pinned here
// so a divergent bespoke count can't creep in.

function feedItem(kind: string, evidence: Record<string, unknown> = {}, over: Partial<FeedItem> = {}): FeedItem {
  return {
    id: `${kind}:x`,
    kind,
    instance: 'salem',
    title: 't',
    mode: 'decide',
    attention: 'needs_you',
    evidence,
    actions: [],
    state: 'open',
    created_at: '2026-07-22T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  };
}

describe('isDeckDealt — the ONE deck dealing/count predicate', () => {
  it('deals classic decision kinds (a wired verb)', () => {
    expect(isDeckDealt(feedItem('email_tier'))).toBe(true);
    expect(isDeckDealt(feedItem('proposal'))).toBe(true);
  });

  it('deals a SUGGESTED slot ONLY — never planned / done / accepted', () => {
    expect(isDeckDealt(feedItem('slot_suggestion', { tier: 1, candidate: true }))).toBe(true); // suggested
    expect(isDeckDealt(feedItem('slot_suggestion', { tier: 1 }))).toBe(false); // planned (no candidate)
    // accepted (acted + acted_action=accept) → PLANNED, not suggested → not dealt.
    expect(isDeckDealt(feedItem('slot_suggestion', { tier: 1, candidate: true }, { state: 'acted', acted_action: 'accept' }))).toBe(false);
    // done (acted + acted_action=done) → not dealt.
    expect(isDeckDealt(feedItem('slot_suggestion', { tier: 1, candidate: true }, { state: 'acted', acted_action: 'done' }))).toBe(false);
  });

  it('does NOT deal an unmapped kind', () => {
    expect(isDeckDealt(feedItem('radar'))).toBe(false);
  });
});

describe('DECK_VERBS.slot_suggestion — Accept / Skip=snooze', () => {
  it('affirm accepts; reject is null but rejectDefers routes LEFT to a client snooze', () => {
    const v = deckVerbsFor('slot_suggestion');
    expect(v?.affirm).toBe('accept');
    expect(v?.reject).toBeNull();
    expect(v?.rejectDefers).toBe(true);
    expect(v?.rejectLabel).toBe('Skip');
  });

  it('affirmLabelFor a slot candidate carries the tier — "Take it — T2"', () => {
    expect(affirmLabelFor(feedItem('slot_suggestion', { tier: 2, candidate: true }))).toBe('Take it — T2');
  });
});

describe('snooze hold-band geometry (#14)', () => {
  it('the band is exactly the span the Snooze stamp fades in over', () => {
    // Not a new magic number: holding where the stamp is ALREADY showing is what
    // makes the affordance self-explanatory. If either endpoint drifts away from
    // the stamp geometry, the menu starts opening somewhere the operator sees
    // nothing — this is the pin that says so.
    expect(STAMP_FADE_START).toBe(40);
    expect(SNOOZE_Y_THRESHOLD).toBe(80);
    expect(inSnoozeHoldBand(0, -39)).toBe(false); // below the stamp: nothing shown yet
    expect(inSnoozeHoldBand(0, -40)).toBe(true);  // stamp just visible
    expect(inSnoozeHoldBand(0, -80)).toBe(true);  // still the band at the boundary
    expect(inSnoozeHoldBand(0, -81)).toBe(false); // past it, the release would commit
  });

  it('a diagonal is not a hold — the band uses the same |dx| tolerance as the verdict', () => {
    // Otherwise a menu could open on a drag whose RELEASE would have been a
    // reject, and the two affordances would disagree about the same gesture.
    expect(inSnoozeHoldBand(SNOOZE_X_TOLERANCE - 1, -60)).toBe(true);
    expect(inSnoozeHoldBand(SNOOZE_X_TOLERANCE, -60)).toBe(false);
    expect(inSnoozeHoldBand(-SNOOZE_X_TOLERANCE, -60)).toBe(false);
  });

  it('downward and idle drags are never a hold', () => {
    expect(inSnoozeHoldBand(0, 60)).toBe(false);
    expect(inSnoozeHoldBand(0, 0)).toBe(false);
  });
});

describe('snooze ladder + per-kind capability (#14)', () => {
  it('offers four rungs, indefinite last, each with a label', () => {
    expect([...SNOOZE_ACTIONS]).toEqual(['snooze_1d', 'snooze_3d', 'snooze_7d', 'snooze_until_i_say']);
    expect(SNOOZE_INDEFINITE_ACTION).toBe('snooze_until_i_say');
    for (const action of SNOOZE_ACTIONS) {
      expect(SNOOZE_LABELS[action]).toBeTruthy();
    }
  });

  it('only a slot_suggestion reaches the backend; every other kind defers locally', () => {
    // FEED_ACTIONS admits snooze_* under slot_suggestion alone. A null action id
    // is the caller's signal to set the card aside without a POST.
    const slot = { kind: 'slot_suggestion' } as FeedItem;
    const email = { kind: 'email_tier' } as FeedItem;
    expect(snoozeIsBacked(slot)).toBe(true);
    expect(snoozeIsBacked(email)).toBe(false);
    expect(snoozeActionFor(slot, 'snooze_3d')).toBe('snooze_3d');
    expect(snoozeActionFor(email, 'snooze_3d')).toBeNull();
  });
});
