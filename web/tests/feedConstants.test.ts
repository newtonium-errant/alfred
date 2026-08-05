import { describe, expect, it } from 'vitest';
import { affirmLabelFor, deckVerbsFor, isDeckDealt } from '../lib/algernon/feedConstants';
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
