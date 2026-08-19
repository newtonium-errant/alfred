import { describe, expect, it } from 'vitest';
import {
  SNOOZE_ACTIONS,
  SNOOZE_INDEFINITE_ACTION,
  SNOOZE_LABELS,
  SNOOZE_X_TOLERANCE,
  SNOOZE_Y_THRESHOLD,
  STAMP_FADE_START,
  affirmLabelFor,
  verbsFromActions,
  inSnoozeHoldBand,
  isDeckDealt,
  snoozeActionFor,
  snoozeIsBacked,
} from '../lib/algernon/feedConstants';
import type { FeedItem } from '../lib/algernon/feed';
import { servedActionsForItem } from './helpers/servedActions';

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
    // Fixtures carry the verbs the SERVER would have served for this kind +
    // stage — an item with `actions: []` is a DEGRADED payload, not a neutral one.
    actions: servedActionsForItem(kind, evidence),
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

describe('slot verbs, READ FROM THE WIRE — Accept / Skip (#102 1b-ii)', () => {
  it('affirm accepts; reject is null but rejectDefers routes LEFT to a client skip', () => {
    const v = verbsFromActions(feedItem('slot_suggestion', { tier: 1, candidate: true }));
    expect(v?.affirm).toBe('accept');
    expect(v?.reject).toBeNull();
    expect(v?.rejectDefers).toBe(true);
    expect(v?.rejectLabel).toBe('Skip');
  });

  it('a COMMITTED slot is offered NO swipe verb — the server withheld its accept', () => {
    // The per-item narrowing, seen from the client end: `actions_for_item` drops
    // `accept` for a non-candidate, and EVERY other ceiling verb this kind has
    // (done / undo_done / the snoozes / the sort verbs) carries no gesture, so
    // nothing is left to swipe. This is why `isDeckDealt` can stop dealing it
    // without the client knowing anything about slot lifecycle rules beyond its
    // stage. Stated as "every other" rather than as a count on purpose — the
    // count expired the first time the ceiling grew (2026-08-19, the sort
    // verbs), and the invariant is what the assertion below actually rests on.
    expect(verbsFromActions(feedItem('slot_suggestion', { tier: 1 }))).toBeNull();
  });

  it('affirmLabelFor a slot candidate carries the tier — "Take it — T2"', () => {
    expect(affirmLabelFor(feedItem('slot_suggestion', { tier: 2, candidate: true }))).toBe('Take it — T2');
  });

  it('a DEGRADED payload (no actions at all) yields no verbs and no card', () => {
    // Half-deployed box: a client that reads verbs from the wire against a
    // backend that does not yet stamp them. The honest answer is no swipe verbs
    // — never a guessed one, which is what a client-side fallback table would
    // have supplied. The item still exists and still lists in the feed; only the
    // gesture surface declines to invent controls for it.
    const undealt = feedItem('email_tier', {}, { actions: [] });
    expect(verbsFromActions(undealt)).toBeNull();
    expect(isDeckDealt(undealt)).toBe(false);
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
