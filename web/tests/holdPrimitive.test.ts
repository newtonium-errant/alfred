import { describe, expect, it } from 'vitest';

// The affirm-with-hold-modifier primitive's PURE halves — band geometry and
// wire-derived choices. The gesture machine built on top is pinned in
// deckHoldSelector.test.tsx (real pointers, fake clock); this file is the
// DOM-free contract, mirroring how feedConstants.test.ts pins the #14 band.

import type { FeedItem } from '../lib/algernon/feed';
import {
  GESTURE_HOLD_MS,
  HOLD_CROSS_TOLERANCE,
  SNOOZE_HOLD_MS,
  SNOOZE_X_TOLERANCE,
  STAMP_FADE_START,
  SWIPE_X_THRESHOLD,
  hasSuggestedChoice,
  holdChoicesFor,
  holdChoicesForVerb,
  inGestureHoldBand,
  inSnoozeHoldBand,
  isDeckCandidate,
  verdictForDrag,
  DEFER_QUICK_ACTION,
} from '../lib/algernon/feedConstants';
import { servedActionsForItem, withServedActions } from './helpers/servedActions';

function sortItem(evidence: Record<string, unknown> = { proposed_slot: 'duty' }): FeedItem {
  return withServedActions({
    id: 'sort_suggestion:task:task/X.md',
    kind: 'sort_suggestion',
    instance: 'salem',
    title: 'Sort: X',
    mode: 'fyi',
    attention: 'fyi',
    evidence,
    actions: [],
    state: 'open',
    created_at: '2026-08-19T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  });
}

function decideItem(kind = 'email_tier'): FeedItem {
  return withServedActions({
    id: `${kind}:x`,
    kind,
    instance: 'salem',
    title: kind,
    mode: 'decide',
    attention: 'needs_you',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-08-19T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  });
}

// --- the clock is one number ---------------------------------------------------

describe('the family clock', () => {
  it('GESTURE_HOLD_MS aliases SNOOZE_HOLD_MS — one number, two names', () => {
    expect(GESTURE_HOLD_MS).toBe(SNOOZE_HOLD_MS);
  });
});

// --- band geometry, direction-general -------------------------------------------

describe('inGestureHoldBand', () => {
  it('affirm: in-band between the stamp fade and the commit threshold', () => {
    const mid = Math.round((STAMP_FADE_START + SWIPE_X_THRESHOLD) / 2);
    expect(inGestureHoldBand('affirm', mid, 0)).toBe(true);
    expect(inGestureHoldBand('affirm', STAMP_FADE_START - 5, 0)).toBe(false); // stamp not showing
    expect(inGestureHoldBand('affirm', SWIPE_X_THRESHOLD + 5, 0)).toBe(false); // full-swipe territory
    expect(inGestureHoldBand('affirm', -mid, 0)).toBe(false); // wrong direction
  });

  it('affirm: a diagonal past the cross tolerance is a swipe, not a hold', () => {
    const mid = Math.round((STAMP_FADE_START + SWIPE_X_THRESHOLD) / 2);
    expect(inGestureHoldBand('affirm', mid, HOLD_CROSS_TOLERANCE + 5)).toBe(false);
    expect(inGestureHoldBand('affirm', mid, -(HOLD_CROSS_TOLERANCE + 5))).toBe(false);
    expect(inGestureHoldBand('affirm', mid, HOLD_CROSS_TOLERANCE - 5)).toBe(true);
  });

  it('reject mirrors affirm exactly — direction is a parameter, not a copy', () => {
    // Defined-not-wired this lane (ratified-in-principle): the geometry is one
    // rule, so the mirror is pinned even though nothing arms it yet.
    for (let dx = -120; dx <= 120; dx += 5) {
      for (let dy = -120; dy <= 40; dy += 5) {
        expect(inGestureHoldBand('reject', dx, dy)).toBe(inGestureHoldBand('affirm', -dx, dy));
      }
    }
  });

  it('snooze delegates to the shipped #14 band — prior art, one spelling', () => {
    for (let dx = -120; dx <= 120; dx += 5) {
      for (let dy = -120; dy <= 40; dy += 5) {
        expect(inGestureHoldBand('snooze', dx, dy)).toBe(inSnoozeHoldBand(dx, dy));
      }
    }
  });

  it('THE BANDS ARE PAIRWISE DISJOINT — no point can arm two holds', () => {
    // The property Deck.tsx's one-timer machine rests on (its band tag would
    // otherwise be a race). Swept, not reasoned: every point on a grid over
    // the whole gesture space is in at most one band.
    const dirs = ['affirm', 'reject', 'snooze'] as const;
    let inSomeBand = 0;
    for (let dx = -150; dx <= 150; dx += 3) {
      for (let dy = -150; dy <= 60; dy += 3) {
        const claims = dirs.filter((d) => inGestureHoldBand(d, dx, dy));
        expect(claims.length, `(${dx},${dy}) claimed by ${claims.join('+')}`).toBeLessThanOrEqual(1);
        if (claims.length === 1) inSomeBand += 1;
      }
    }
    // Positive control: the sweep really visited all three bands — a disjoint
    // check over bands that never fire is vacuous.
    expect(inSomeBand).toBeGreaterThan(100);
  });

  it('every in-band point releases to a NULL verdict — a hold can never race a commit', () => {
    for (let dx = -150; dx <= 150; dx += 3) {
      for (let dy = -150; dy <= 60; dy += 3) {
        for (const d of ['affirm', 'reject', 'snooze'] as const) {
          if (inGestureHoldBand(d, dx, dy)) {
            expect(verdictForDrag(dx, dy), `(${dx},${dy}) in ${d} band`).toBeNull();
          }
        }
      }
    }
    // The cross tolerance the nullity rests on really is inside the ↑ verdict's.
    expect(HOLD_CROSS_TOLERANCE).toBeLessThan(SNOOZE_X_TOLERANCE);
  });
});

// --- the choices, from the wire --------------------------------------------------

describe('holdChoicesFor', () => {
  it('derives the co-equal family from the served group, suggested marked', () => {
    const choices = holdChoicesFor(sortItem({ proposed_slot: 'rhythm' }), 'affirm');
    expect(choices).not.toBeNull();
    expect(choices!.map((c) => c.verb)).toEqual(['sort_duty', 'sort_rhythm', 'sort_fuel']);
    expect(choices!.map((c) => c.label)).toEqual(['Duty', 'Rhythm', 'Fuel']);
    expect(choices!.filter((c) => c.suggested).map((c) => c.verb)).toEqual(['sort_rhythm']);
  });

  it('null without a gestured verb — a proposal-less card offers no selector', () => {
    expect(holdChoicesFor(sortItem({}), 'affirm')).toBeNull();
  });

  it('null when the gestured verb carries no group (an ordinary affirm)', () => {
    expect(holdChoicesFor(decideItem('email_tier'), 'affirm')).toBeNull();
  });

  it("null for the sort card's REJECT — its defer is gestured but ungrouped", () => {
    expect(holdChoicesFor(sortItem(), 'reject')).toBeNull();
  });

  it('null on a group of one — a selector with one option is a confirm stage in disguise', () => {
    const item = sortItem();
    item.actions = [
      { verb: 'sort_duty', label: 'Duty', weight: 'light', gesture: 'affirm', group: 'slot' },
    ];
    expect(holdChoicesFor(item, 'affirm')).toBeNull();
  });
});

// --- the deck's candidate question ----------------------------------------------

describe('isDeckCandidate', () => {
  it('a decide item is a candidate, as ever', () => {
    expect(isDeckCandidate(decideItem())).toBe(true);
  });

  it('a QUIET suggestion card is a candidate — fyi/fyi, deals without ringing', () => {
    const card = sortItem();
    expect(card.mode).toBe('fyi'); // the premise: nothing here touches needs-you
    expect(hasSuggestedChoice(card)).toBe(true);
    expect(isDeckCandidate(card)).toBe(true);
  });

  it('a degraded suggestion card (no proposal) is NOT a candidate', () => {
    expect(isDeckCandidate(sortItem({}))).toBe(false);
  });

  it('an fyi row with gestured-but-ungrouped verbs is NOT a candidate — the attribution guard', () => {
    // The counterexample that forbids `isDeckDealt ∧ fyi` as the rule: an fyi
    // attribution row serves gestured confirm/reject, and dealing every glance
    // row would undo the operator's own demotion ruling.
    const fyiAttribution = decideItem('attribution');
    fyiAttribution.mode = 'fyi';
    fyiAttribution.attention = 'fyi';
    expect(isDeckCandidate(fyiAttribution)).toBe(false);
  });
});

// --- the honest not-now copy's constant -------------------------------------------

describe('DEFER_QUICK_ACTION', () => {
  it("is the quick defer's wire id, and the sort card's reject really serves it", () => {
    const served = servedActionsForItem('sort_suggestion', { proposed_slot: 'duty' });
    const rejectVerb = served.find((a) => a.gesture === 'reject');
    expect(rejectVerb?.verb).toBe(DEFER_QUICK_ACTION);
    expect(rejectVerb?.label).toBe('Not now');
  });
});

// --- the verb-anchored evolution (backdated completion, 2026-08-20) ----------

function slotItem(evidence: Record<string, unknown> = {}): FeedItem {
  return withServedActions({
    id: 'slot_suggestion:routine:Waste::Garbage Day',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'T1: Garbage Day',
    mode: 'fyi',
    attention: 'needs_you',
    evidence: { tier: 1, routine_record: 'Waste', item_text: 'Garbage Day', ...evidence },
    actions: [],
    state: 'open',
    created_at: '2026-08-20T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  });
}

describe('holdChoicesForVerb — the ✓ anchor (a button has no swipe direction)', () => {
  it('derives the when-family from the REAL wire fixture, anchor suggested', () => {
    const choices = holdChoicesForVerb(slotItem({ backdate_limit_days: 3 }), 'done');
    expect(choices).not.toBeNull();
    expect(choices!.map((c) => c.verb)).toEqual(['done', 'done_1d', 'done_2d', 'done_3d']);
    expect(choices!.map((c) => c.label)).toEqual(['Today', 'Yesterday', '2 days ago', '3 days ago']);
    // The anchor — the plain control's own commit — is the suggested member.
    expect(choices!.filter((c) => c.suggested).map((c) => c.verb)).toEqual(['done']);
  });

  it('a partially-served family keeps only the wire-admitted rungs', () => {
    const choices = holdChoicesForVerb(slotItem({ backdate_limit_days: 1 }), 'done');
    expect(choices!.map((c) => c.verb)).toEqual(['done', 'done_1d']);
  });

  it('a family of one is no family (limit 0 strips every rung)', () => {
    expect(holdChoicesForVerb(slotItem({ backdate_limit_days: 0 }), 'done')).toBeNull();
    expect(holdChoicesForVerb(slotItem({}), 'done')).toBeNull();
  });

  it('an anchor the wire never served, or one without a group, is null', () => {
    expect(holdChoicesForVerb(slotItem({ backdate_limit_days: 3 }), 'no_such_verb')).toBeNull();
    // `unsnooze` is served but carries no group.
    expect(holdChoicesForVerb(slotItem({ backdate_limit_days: 3 }), 'unsnooze')).toBeNull();
  });

  it('holdChoicesFor is now the gesture-resolving wrapper — same family, one derivation', () => {
    const item = sortItem({ proposed_slot: 'duty' });
    expect(holdChoicesFor(item, 'affirm')).toEqual(holdChoicesForVerb(item, 'sort_duty'));
  });

  it('the deck slot card is untouched: its affirm (accept) anchors NO family', () => {
    // The guard the gesture-free serving buys: a candidate's swipe stays
    // Take-it, and holding it opens nothing — the when-family hangs off the
    // BOARD's ✓, not the deck's affirm.
    const candidate = slotItem({ candidate: true, backdate_limit_days: 3 });
    expect(holdChoicesFor(candidate, 'affirm')).toBeNull();
  });
});
