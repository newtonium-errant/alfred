import { describe, expect, it } from 'vitest';
import {
  isTodayInstanceTz,
  ringItemCommitted,
  ringItemCompletable,
  ringItemDone,
  ringItemStage,
  ringItemSuggested,
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
  it('is FALSE for an acted-by-ACCEPT item (committed, not completed — the verb decides, not candidate)', () => {
    // ← the state=acted overload: acted_action==='accept' is a just-accepted slot
    // (→ planned), never a completion. Stage-derived, so it can't disagree.
    expect(ringItemDone(slot({ state: 'acted', acted_action: 'accept', evidence: { tier: 1, candidate: true } }))).toBe(false);
  });
  it('is TRUE for a done-AFTER-accept item (acted_action==="done", candidate still stale-true)', () => {
    // The exact flow the verb stamp exists for — a candidate-keyed rule would LIE here.
    expect(ringItemDone(slot({ state: 'acted', acted_action: 'done', evidence: { tier: 1, candidate: true } }))).toBe(true);
  });
});

describe('ringItemStage — C2 lifecycle (suggested → planned → done), VERB-stamped', () => {
  it('OPEN + candidate → suggested', () => {
    expect(ringItemStage(slot({ state: 'open', evidence: { tier: 1, candidate: true } }))).toBe('suggested');
  });
  it('OPEN + no candidate → planned (absence of the field never sprouts Accept — pre-C2 items)', () => {
    expect(ringItemStage(slot({ state: 'open', evidence: { tier: 1 } }))).toBe('planned');
    expect(ringItemStage(slot({ state: 'open', evidence: { tier: 1, candidate: false } }))).toBe('planned');
  });
  it('evidence.done → done (precedence: done beats a lingering candidate)', () => {
    expect(ringItemStage(slot({ state: 'open', evidence: { tier: 1, done: true, candidate: true } }))).toBe('done');
  });
  it('ACTED + acted_action="accept" → planned (accepted/committed — ✓ enabled, no undo)', () => {
    // candidate stays stale-true — the VERB, not candidate, decides.
    expect(ringItemStage(slot({ state: 'acted', acted_action: 'accept', evidence: { tier: 1, candidate: true } }))).toBe('planned');
  });
  it('ACTED + acted_action="done" → done (a completion, incl. done-AFTER-accept with candidate stale-true)', () => {
    expect(ringItemStage(slot({ state: 'acted', acted_action: 'done', evidence: { tier: 1, candidate: true } }))).toBe('done');
  });
  it('ACTED + absent acted_action → done (legacy degrade — C1b unchanged)', () => {
    expect(ringItemStage(slot({ state: 'acted', evidence: { tier: 1 } }))).toBe('done');
  });
  it('ringItemSuggested / ringItemCommitted track the stage (a candidate is NOT committed → excluded from the count)', () => {
    const cand = slot({ state: 'open', evidence: { tier: 1, candidate: true } });
    const planned = slot({ state: 'open', evidence: { tier: 1 } });
    const accepted = slot({ state: 'acted', acted_action: 'accept', evidence: { tier: 1, candidate: true } });
    const done = slot({ state: 'acted', acted_action: 'done', evidence: { tier: 1 } });
    expect(ringItemSuggested(cand)).toBe(true);
    expect(ringItemSuggested(planned)).toBe(false);
    expect(ringItemCommitted(cand)).toBe(false);
    expect(ringItemCommitted(accepted)).toBe(true); // accepted = planned = committed
    expect(ringItemCommitted(done)).toBe(true);
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

  it('Case-B dedup: an OPEN committed re-emit suppresses the spent acted-by-accept phantom, keyed on (tier, name) — origin DROPS OUT', () => {
    const now = new Date('2026-07-31T15:00:00Z');
    // Two T3 phantom pairs, verbatim from c2's before/after at 94a6dcfd:
    const buckets = tierRingBuckets(
      [
        // (a) self-care ROUTINE — origin MATCHES (routine_item both). Collapses under
        //     either key; the baseline.
        slot({ id: 'slot_suggestion:routine:Self Care::Meditate', state: 'acted', acted_at: '2026-07-31T16:00:00Z', acted_action: 'accept', evidence: { tier: 3, origin: 'routine_item', name: 'Meditate', candidate: true } }),
        slot({ id: 'slot_suggestion:text:Meditate', state: 'open', evidence: { tier: 3, origin: 'routine_item', name: 'Meditate', candidate: false } }),
        // (b) self-care TASK — origin FLIPS on commit (task → routine_item, the free-text
        //     T3Entry re-read). ← the GAP-CATCHER: (tier,origin,name) would MISS this and
        //     leave the phantom; only (tier,name) collapses it. This pin reddens if origin
        //     is ever re-added to the key.
        slot({ id: 'slot_suggestion:task:task/Book a massage.md', state: 'acted', acted_at: '2026-07-31T16:00:00Z', acted_action: 'accept', evidence: { tier: 3, origin: 'task', name: 'Book a massage', candidate: true } }),
        slot({ id: 'slot_suggestion:text:Book a massage', state: 'open', evidence: { tier: 3, origin: 'routine_item', name: 'Book a massage', candidate: false } }),
      ],
      now,
    );
    // Both phantoms deduped → only the two committed (open) re-emits survive in T3.
    expect(buckets[2].items.map((i) => i.id).sort()).toEqual([
      'slot_suggestion:text:Book a massage',
      'slot_suggestion:text:Meditate',
    ]);
  });

  it('Case-B: an acted-by-accept with NO open sibling is KEPT (the transient accepted item shows)', () => {
    const now = new Date('2026-07-31T15:00:00Z');
    const buckets = tierRingBuckets(
      [
        slot({
          id: 'a',
          state: 'acted',
          acted_at: '2026-07-31T16:00:00Z',
          acted_action: 'accept',
          evidence: { tier: 1, origin: 'task', name: 'X', candidate: true },
        }),
      ],
      now,
    );
    expect(buckets[0].items.map((i) => i.id)).toEqual(['a']);
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
  it('an acted-by-accept item still SHOWS here — the T3 phantom double is Case-B dedup, not visibility', () => {
    // A transient accepted item (no open sibling yet) must show as PLANNED during the
    // accept→re-emit window; suppression of the SPENT phantom lives in tierRingBuckets.
    expect(
      ringItemVisibleToday(slot({ state: 'acted', acted_at: '2026-07-31T16:00:00Z', acted_action: 'accept', evidence: { tier: 1, candidate: true } }), now),
    ).toBe(true);
    // A board-completed acted-today item also shows (C1b).
    expect(
      ringItemVisibleToday(slot({ state: 'acted', acted_at: '2026-07-31T16:00:00Z', acted_action: 'done', evidence: { tier: 1 } }), now),
    ).toBe(true);
  });
});
