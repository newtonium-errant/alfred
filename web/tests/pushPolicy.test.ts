import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { isPushEligible, readPushPolicy, type PushPolicy } from '../lib/algernon/pushPolicy';
import type { FeedItem } from '../lib/algernon/feed';

// #27 slice 3 — the push-eligibility policy. Default is the STRICTEST gate
// (operator ruling C): only email_urgent + high_source==="override". Widening is
// a PUSH_POLICY env flip, never a code change.

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
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
  };
}

beforeEach(() => {
  delete process.env.PUSH_POLICY;
});
afterEach(() => {
  delete process.env.PUSH_POLICY;
});

describe('readPushPolicy', () => {
  it('defaults to the strictest gate when unset', () => {
    expect(readPushPolicy()).toBe('email_urgent_override');
  });

  it('parses the two widening values', () => {
    process.env.PUSH_POLICY = 'email_urgent_all';
    expect(readPushPolicy()).toBe('email_urgent_all');
    process.env.PUSH_POLICY = 'needs_you';
    expect(readPushPolicy()).toBe('needs_you');
  });

  it('falls back to the strict default on an unrecognized value (never silently widens)', () => {
    process.env.PUSH_POLICY = 'everything';
    expect(readPushPolicy()).toBe('email_urgent_override');
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
