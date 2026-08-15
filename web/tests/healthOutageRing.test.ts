import { describe, expect, it } from 'vitest';
import { isNeedsYouItem } from '../lib/algernon/feedNeedsYou';
import { isPushEligible, readPushPolicy } from '../lib/algernon/pushPolicy';
import { pushDeepLink } from '../lib/algernon/pushPayload';
import type { FeedItem } from '../lib/algernon/feed';

// 2026-08-15 — THE PIN THAT A SUSTAINED AGENT-BACKEND OUTAGE REACHES THE PHONE.
//
// The incident: curator's Claude backend hit its weekly quota, email intake
// stopped for days, and the only signal was an FYI-attention `health:curator`
// card. The operator found it by noticing an empty deck.
//
// WHY THIS PIN IS SPLIT ACROSS TWO LANGUAGES, AND WHAT EACH HALF OWNS. The
// escalated card is a CONTRACT with two ends:
//
//   * Python promises to PRODUCE it — `brief.feed_producer._health_attention`
//     stamps `attention: needs_you` on a FAIL health card while deliberately
//     leaving `mode: fyi` alone. That half is pinned in
//     `tests/feed/test_health_card_escalation.py`, which drives the real
//     producer and the real `act` router.
//   * TypeScript promises to ACT on it — an item shaped like that rings.
//
// This file owns the SECOND half only, so its fixture is a deliberate literal
// rather than a read of the Python source (the sibling `reminderReturnedRing`
// test reads `KIND_DEFAULTS` because its wiring genuinely lives in that table;
// a health card's attention is a per-ITEM override, so there is no table entry
// to read — `KIND_DEFAULTS['health']` says `fyi` and always will).
//
// The property under test is the one that would fail SILENTLY: `isNeedsYouItem`
// is `attention === 'needs_you' || mode === 'decide'`, and if that first clause
// were ever dropped, the Python side would keep stamping `needs_you`, every
// Python test would stay green, and the doorbell would go quiet for outages.

/** A FAIL health card exactly as the brief's producer emits one. */
function outageHealthCard(): FeedItem {
  return {
    id: 'health:curator',
    kind: 'health',
    instance: 'Salem',
    title:
      'Health: curator FAIL — claude -p quota-limited since 2026-08-15T00:31:00+00:00 — ' +
      '4 consecutive agent failures, no success in between: email intake is stopped and ' +
      'new mail is being quarantined.',
    // The escalation. `mode` stays 'fyi' ON PURPOSE — promoting it would break
    // the universal FYI ack, which is a health card's only dismiss path.
    mode: 'fyi',
    attention: 'needs_you',
    evidence: { tool: 'curator', status: 'fail', detail: 'quota-limited' },
    actions: [],
    state: 'open',
    created_at: '2026-08-15T05:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: { producer: 'brief' },
  } as FeedItem;
}

/** A WARN health card — the glance case, and the control that must NOT ring. */
function warnHealthCard(): FeedItem {
  return { ...outageHealthCard(), id: 'health:surveyor', attention: 'fyi',
    title: 'Health: surveyor WARN — ollama 404',
    evidence: { tool: 'surveyor', status: 'warn', detail: 'ollama 404' } } as FeedItem;
}

describe('a sustained-outage health card reaches the doorbell', () => {
  it('rings on ATTENTION ALONE, with mode still fyi', () => {
    const card = outageHealthCard();
    // The premise, asserted rather than assumed: if a future refactor promotes
    // mode as well, this pin should be re-read rather than silently satisfied
    // by the second clause of isNeedsYouItem.
    expect(card.mode).toBe('fyi');
    expect(isNeedsYouItem(card)).toBe(true);
    expect(readPushPolicy()).toBe('needs_you');
    expect(isPushEligible(card, readPushPolicy())).toBe(true);
  });

  it('does NOT ring for a warn card — the escalation is severity-gated', () => {
    // The positive control for the negative: same kind, same producer, same
    // pipeline. Without this, the test above passes identically against a
    // build that promoted every health card to needs-you.
    //
    // The exclusion is asserted at the stage that actually performs it. Note
    // `isPushEligible` is NOT that stage: under the default `needs_you` policy
    // it returns true for anything handed to it, because the poller has
    // already narrowed to needs-you items via `fetchNeedsYouItems`. So the
    // composed poller predicate is what this pins — asserting eligibility
    // alone would have been a check that cannot fire.
    const warn = warnHealthCard();
    expect(isNeedsYouItem(warn)).toBe(false);

    const policy = readPushPolicy();
    const wouldRing = (it: FeedItem) => isNeedsYouItem(it) && isPushEligible(it, policy);
    expect(wouldRing(warn)).toBe(false);
    expect(wouldRing(outageHealthCard())).toBe(true);
  });

  it('routes the operator to the deck-side surface, not the FYI feed', () => {
    expect(pushDeepLink(outageHealthCard())).toBe('/deck');
    expect(pushDeepLink(warnHealthCard())).toBe('/feed');
  });
});
