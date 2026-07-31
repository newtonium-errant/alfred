import { describe, expect, it } from 'vitest';
import {
  isTodayInstanceTz,
  ringItemCompletable,
  ringItemDone,
  ringItemUndoable,
  ringItemVisibleToday,
  ringTierOf,
  tierRingBuckets,
} from '../lib/algernon/rings';
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
  it('is true for a task-backed lane (C1b: the board task-completion writer is wired)', () => {
    expect(ringItemCompletable(slot({ evidence: { tier: 1, origin: 'task', path: 'task/A.md' } }))).toBe(true);
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
  it('a task lane is completable regardless of tier (origin wins, C1b)', () => {
    expect(ringItemCompletable(slot({ evidence: { tier: 3, origin: 'task' } }))).toBe(true);
    expect(ringItemCompletable(slot({ evidence: { tier: 2, origin: 'task' } }))).toBe(true);
  });
});

describe('ringItemUndoable — task is completable but NOT board-undoable (v1)', () => {
  it('is false for a task lane (undo_done → 422 "undo via chat"; the board hides the control)', () => {
    expect(ringItemUndoable(slot({ evidence: { tier: 1, origin: 'task', path: 'task/A.md' } }))).toBe(false);
    // even at tier 3 — origin task always excludes board undo.
    expect(ringItemUndoable(slot({ evidence: { tier: 3, origin: 'task' } }))).toBe(false);
  });
  it('is true for a routine-item lane (routine_undone wired)', () => {
    expect(ringItemUndoable(slot({ evidence: { tier: 1, origin: 'routine_item', routine_record: 'routine/Bills.md', item_text: 'Pay' } }))).toBe(true);
  });
  it('is true for a free-text T3 lane (tier_undone wired)', () => {
    expect(ringItemUndoable(slot({ evidence: { tier: 3 } }))).toBe(true);
    expect(ringItemUndoable(slot({ evidence: { tier: '3' } }))).toBe(true);
  });
  it('is false for an unknown origin (not completable → not undoable — the conjunct)', () => {
    expect(ringItemUndoable(slot({ evidence: { tier: 1 } }))).toBe(false);
    expect(ringItemUndoable(slot({ evidence: {} }))).toBe(false);
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

  it("includes today's DONE (acted) items and drops prior-day acted ones", () => {
    const now = new Date('2026-07-31T15:00:00Z'); // Halifax 12:00 2026-07-31 (ADT, UTC-3)
    const buckets = tierRingBuckets(
      [
        slot({ id: 'open', evidence: { tier: 1 } }),
        slot({ id: 'done-today', state: 'acted', acted_at: '2026-07-31T16:00:00Z', evidence: { tier: 1 } }),
        slot({ id: 'done-yst', state: 'acted', acted_at: '2026-07-30T18:00:00Z', evidence: { tier: 1 } }),
      ],
      now,
    );
    expect(buckets[0].items.map((i) => i.id).sort()).toEqual(['done-today', 'open']);
  });
});

describe('isTodayInstanceTz (America/Halifax day-scope)', () => {
  const now = new Date('2026-07-31T15:00:00Z'); // Halifax 12:00 2026-07-31 (ADT, UTC-3)
  it('true for a UTC instant on the same Halifax day', () => {
    expect(isTodayInstanceTz('2026-07-31T16:30:00Z', now)).toBe(true);
  });
  it('false for a prior Halifax day', () => {
    expect(isTodayInstanceTz('2026-07-30T20:00:00Z', now)).toBe(false);
  });
  it('respects the tz boundary — an early-UTC instant maps to the previous Halifax day', () => {
    // 02:00Z − 3h = 23:00 on 2026-07-30 in Halifax → NOT today.
    expect(isTodayInstanceTz('2026-07-31T02:00:00Z', now)).toBe(false);
  });
  it('false for null / unparseable', () => {
    expect(isTodayInstanceTz(null, now)).toBe(false);
    expect(isTodayInstanceTz('not-a-date', now)).toBe(false);
  });
});

describe('ringItemVisibleToday — completion is a STAGE, not a disappearance', () => {
  const now = new Date('2026-07-31T15:00:00Z');
  it('open items always show (planned, or open-with-evidence.done)', () => {
    expect(ringItemVisibleToday(slot({ state: 'open' }), now)).toBe(true);
  });
  it('an acted-today item STAYS on the ring (green, not gone)', () => {
    expect(ringItemVisibleToday(slot({ state: 'acted', acted_at: '2026-07-31T16:00:00Z' }), now)).toBe(true);
  });
  it('an acted-yesterday item is gone — only TODAY counts', () => {
    expect(ringItemVisibleToday(slot({ state: 'acted', acted_at: '2026-07-30T18:00:00Z' }), now)).toBe(false);
  });
  it('acted with no acted_at, or acked / expired, are gone (defensive)', () => {
    expect(ringItemVisibleToday(slot({ state: 'acted', acted_at: null }), now)).toBe(false);
    expect(ringItemVisibleToday(slot({ state: 'acked' }), now)).toBe(false);
    expect(ringItemVisibleToday(slot({ state: 'expired' }), now)).toBe(false);
  });
});
