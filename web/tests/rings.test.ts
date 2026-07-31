import { describe, expect, it } from 'vitest';
import { ringItemCompletable, ringItemDone, ringTierOf, tierRingBuckets } from '../lib/algernon/rings';
import type { FeedItem } from '../lib/algernon/feed';

// Ring DATA binding — grouping open slot_suggestion feed items into the three
// tier rings. Pins the VERIFIED reality: evidence carries `tier` ∈ {1,2,3} (no
// duty/rhythm/fuel bucket) and no completion flag (every item planned in B).

function slot(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'slot_suggestion:task/A.md',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'T1: Pay rent',
    mode: 'fyi',
    attention: 'fyi',
    evidence: { tier: 1, name: 'Pay rent', surface_reason: 'due today' },
    actions: [],
    state: 'open',
    created_at: '2026-07-30T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...overrides,
  };
}

describe('ringTierOf', () => {
  it('reads a numeric tier', () => {
    expect(ringTierOf(slot({ evidence: { tier: 2 } }))).toBe(2);
  });
  it('coerces a numeric string tier', () => {
    expect(ringTierOf(slot({ evidence: { tier: '3' } }))).toBe(3);
  });
  it('returns null for a missing / out-of-range / non-numeric tier', () => {
    expect(ringTierOf(slot({ evidence: {} }))).toBeNull();
    expect(ringTierOf(slot({ evidence: { tier: 0 } }))).toBeNull();
    expect(ringTierOf(slot({ evidence: { tier: 4 } }))).toBeNull();
    expect(ringTierOf(slot({ evidence: { tier: 'x' } }))).toBeNull();
    expect(ringTierOf(slot({ evidence: null as unknown as Record<string, unknown> }))).toBeNull();
  });
});

describe('ringItemDone', () => {
  it('is false for an open item with no completion signal', () => {
    expect(ringItemDone(slot())).toBe(false);
  });
  it('is true when evidence.done is set (vault-completed, still emitted)', () => {
    expect(ringItemDone(slot({ evidence: { tier: 1, done: true } }))).toBe(true);
  });
  it('is true when state is "acted" (board-completed — never re-emits done)', () => {
    expect(ringItemDone(slot({ state: 'acted', evidence: { tier: 1 } }))).toBe(true);
  });
});

describe('ringItemCompletable', () => {
  it('is false for a task-backed lane (v1 has no board task writer)', () => {
    expect(ringItemCompletable(slot({ evidence: { tier: 1, origin: 'task', path: 'task/A.md' } }))).toBe(false);
  });
  it('is true for a routine-item lane', () => {
    expect(ringItemCompletable(slot({ evidence: { tier: 1, origin: 'routine_item', routine_record: 'routine/Bills.md', item_text: 'Pay' } }))).toBe(true);
  });
  it('is true for a free-text T3 lane (numeric or string tier)', () => {
    expect(ringItemCompletable(slot({ evidence: { tier: 3 } }))).toBe(true);
    expect(ringItemCompletable(slot({ evidence: { tier: '3' } }))).toBe(true);
  });
  it('is false for an unknown origin with no routine record and tier < 3', () => {
    expect(ringItemCompletable(slot({ evidence: { tier: 1 } }))).toBe(false);
    expect(ringItemCompletable(slot({ evidence: {} }))).toBe(false);
  });
  it('task origin wins even at tier 3 (never a guessed write)', () => {
    expect(ringItemCompletable(slot({ evidence: { tier: 3, origin: 'task' } }))).toBe(false);
  });
});

describe('tierRingBuckets', () => {
  it('always returns the three tiers in order, even when all empty', () => {
    const buckets = tierRingBuckets([]);
    expect(buckets.map((b) => b.key)).toEqual(['1', '2', '3']);
    expect(buckets.map((b) => b.label)).toEqual(['T1', 'T2', 'T3']);
    expect(buckets.every((b) => b.items.length === 0)).toBe(true);
  });

  it('groups items into their tier bucket', () => {
    const buckets = tierRingBuckets([
      slot({ id: 'a', evidence: { tier: 1 } }),
      slot({ id: 'b', evidence: { tier: 1 } }),
      slot({ id: 'c', evidence: { tier: 3 } }),
    ]);
    expect(buckets[0].items.map((i) => i.id)).toEqual(['a', 'b']);
    expect(buckets[1].items).toEqual([]);
    expect(buckets[2].items.map((i) => i.id)).toEqual(['c']);
  });

  it('drops non-slot_suggestion items and invalid tiers', () => {
    const buckets = tierRingBuckets([
      slot({ id: 'ok', evidence: { tier: 2 } }),
      slot({ id: 'wrong-kind', kind: 'health', evidence: { tier: 2 } }),
      slot({ id: 'bad-tier', evidence: { tier: 9 } }),
    ]);
    const all = buckets.flatMap((b) => b.items.map((i) => i.id));
    expect(all).toEqual(['ok']);
  });
});
