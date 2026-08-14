import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { isPushEligible, readPushPolicy, type PushPolicy } from '../lib/algernon/pushPolicy';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

// #27 slice 3 — the push-eligibility policy. Default is the STRICTEST gate
// (operator ruling C): only email_urgent + high_source==="override". Widening is
// a PUSH_POLICY env flip, never a code change.

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
    id: 'email_urgent:note/A.md',
    kind: 'email_urgent',
    instance: 'salem',
    title: 'Urgent email: a@b.com — Subject',
    mode: 'decide',
    attention: 'needs_you',
    evidence: { high_source: 'override' },
    actions: [],
    state: 'open',
    created_at: '2026-08-01T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...overrides,
  });
}

beforeEach(() => {
  delete process.env.PUSH_POLICY;
});
afterEach(() => {
  delete process.env.PUSH_POLICY;
});

describe('readPushPolicy', () => {
  it('defaults to needs_you when unset — the operator ruling', () => {
    // REVERSED from #27 slice 3's strictest-default. That default was set before
    // anything had rung, when the risk worth managing was a doorbell that cried
    // wolf. The ruling: reminders, urgent email and anything dealt
    // needs-you/decide RING; routine cards wait silently. Gating inside the
    // needs-you set was suppressing the very items the operator asked to be
    // interrupted for.
    expect(readPushPolicy()).toBe('needs_you');
  });

  it('parses the two NARROWING values', () => {
    process.env.PUSH_POLICY = 'email_urgent_all';
    expect(readPushPolicy()).toBe('email_urgent_all');
    process.env.PUSH_POLICY = 'email_urgent_override';
    expect(readPushPolicy()).toBe('email_urgent_override');
  });

  it('falls back to the DEFAULT on an unrecognized value — a typo widens, never silences', () => {
    // The direction is deliberate and it flipped with the default. A mistyped
    // flag now fails toward "you were told about something" rather than toward a
    // doorbell that quietly stopped — silence is the failure mode nobody notices,
    // which is the whole reason this codebase distinguishes idle from broken.
    process.env.PUSH_POLICY = 'everything';
    expect(readPushPolicy()).toBe('needs_you');
  });

  it('trims surrounding whitespace', () => {
    process.env.PUSH_POLICY = '  email_urgent_all  ';
    expect(readPushPolicy()).toBe('email_urgent_all');
  });
});

describe('isPushEligible — default (email_urgent_override)', () => {
  const policy: PushPolicy = 'email_urgent_override';

  it('rings for an override-high email_urgent', () => {
    expect(isPushEligible(item({ evidence: { high_source: 'override' } }), policy)).toBe(true);
  });

  it('does NOT ring for an LLM-verdict-high email_urgent', () => {
    expect(isPushEligible(item({ evidence: { high_source: 'llm' } }), policy)).toBe(false);
  });

  it('does NOT ring for a non-email_urgent needs-you item (e.g. email_tier)', () => {
    expect(
      isPushEligible(item({ kind: 'email_tier', evidence: { high_source: 'override' } }), policy),
    ).toBe(false);
  });

  it('does NOT ring for an email_urgent with missing/blank high_source', () => {
    expect(isPushEligible(item({ evidence: {} }), policy)).toBe(false);
  });
});

describe('isPushEligible — email_urgent_all (widened to all highs)', () => {
  const policy: PushPolicy = 'email_urgent_all';

  it('rings for BOTH llm and override email_urgent', () => {
    expect(isPushEligible(item({ evidence: { high_source: 'llm' } }), policy)).toBe(true);
    expect(isPushEligible(item({ evidence: { high_source: 'override' } }), policy)).toBe(true);
  });

  it('still does NOT ring for a non-email_urgent kind', () => {
    expect(isPushEligible(item({ kind: 'proposal' }), policy)).toBe(false);
  });
});

describe('isPushEligible — needs_you (pre-#27 behavior)', () => {
  const policy: PushPolicy = 'needs_you';

  it('rings for any needs-you item regardless of kind', () => {
    expect(isPushEligible(item({ kind: 'email_tier', evidence: {} }), policy)).toBe(true);
    expect(isPushEligible(item({ kind: 'proposal', evidence: {} }), policy)).toBe(true);
    expect(isPushEligible(item({ kind: 'email_urgent', evidence: { high_source: 'llm' } }), policy)).toBe(true);
  });
});
