import { describe, expect, it } from 'vitest';

// The day board's pure model: the SLOT grouping (not a renamed tier), carryover
// detection off the feed's episode `created_at`, the overdue day-key compare, the
// attention ranking, and the two caps that demote-without-dropping.

import {
  BOARD_UNSLOTTED,
  CANDIDATE_CAP,
  CARRYOVER_CAP,
  SLOT_COVERAGE_WARN,
  boardCoverage,
  boardCoverageIsLow,
  boardIsCarryover,
  boardIsOverdue,
  boardSlotOf,
  boardSlotsWithADone,
  boardStacks,
  carryoverRank,
  carryoverReason,
} from '../lib/algernon/board';
import { ringItemStage } from '../lib/algernon/rings';
import type { FeedItem } from '../lib/algernon/feed';

// 12:00 Halifax on 2026-08-12 — a fixed clock so every date-scoped assertion is
// deterministic regardless of when the suite runs.
const NOW = new Date('2026-08-12T15:00:00Z');
const TODAY_CREATED = '2026-08-12T13:00:00Z'; // 10:00 Halifax, same day
const YESTERDAY_CREATED = '2026-08-11T13:00:00Z'; // 10:00 Halifax, prior day

function slot(overrides: Partial<FeedItem> = {}, evidence: Record<string, unknown> = {}): FeedItem {
  return {
    id: 'slot:x',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'A thing',
    mode: 'fyi',
    attention: 'fyi',
    evidence: { tier: 1, name: 'A thing', ...evidence },
    actions: [],
    state: 'open',
    created_at: TODAY_CREATED,
    acted_at: null,
    expires_at: null,
    ...overrides,
  } as FeedItem;
}

const stageOf = (it: FeedItem) => ringItemStage(it);
const stackFor = (items: FeedItem[], key: string) =>
  boardStacks(items, stageOf, NOW).find((s) => s.key === key);

describe('boardSlotOf — the grouping choke-point reads the SLOT axis', () => {
  // THE LOAD-BEARING PIN. `tier/slots.py` states it outright: "Slot is a second
  // axis, orthogonal to tier […] G9's rename-only reading (Duty=T1, Rhythm=T2,
  // Fuel=T3) is SUPERSEDED." Its worked example is a cadence routine item that is
  // due today: T1 by urgency, Rhythm by kind. Under a relabelled tier this item
  // reads Duty and the balance the feature measures becomes unmeasurable.
  it('a TIER-1 item stamped rhythm groups under RHYTHM, not duty', () => {
    const cadenceDueToday = slot({ id: 'guitar' }, { tier: 1, slot: 'rhythm', slot_rule: 'target_cadence_days' });
    expect(boardSlotOf(cadenceDueToday)).toBe('rhythm');
    expect(stackFor([cadenceDueToday], 'rhythm')?.today.map((i) => i.id)).toEqual(['guitar']);
    expect(stackFor([cadenceDueToday], 'duty')?.today).toEqual([]);
  });

  // The positive control for the pin above: the SAME tier, a different stamp,
  // lands in the other stack. Without this, "rhythm is not duty" would pass
  // identically against a board that put everything in rhythm.
  it('a TIER-1 item stamped duty groups under DUTY (positive control)', () => {
    const datedTask = slot({ id: 'rent' }, { tier: 1, slot: 'duty', slot_rule: 'dated_task' });
    expect(boardSlotOf(datedTask)).toBe('duty');
    expect(stackFor([datedTask], 'duty')?.today.map((i) => i.id)).toEqual(['rent']);
    expect(stackFor([datedTask], 'rhythm')?.today).toEqual([]);
  });

  // And the third: a TIER-3 item stamped duty. Tier 3 would be "Fuel" under the
  // rename, so this is the same refutation from the opposite end of the tier range.
  it('a TIER-3 item stamped duty groups under DUTY, not fuel', () => {
    const t3Duty = slot({ id: 'admin' }, { tier: 3, slot: 'duty', slot_rule: 'explicit' });
    expect(stackFor([t3Duty], 'duty')?.today.map((i) => i.id)).toEqual(['admin']);
    expect(stackFor([t3Duty], 'fuel')?.today).toEqual([]);
  });

  it('mirrors normalize_slot: case- and whitespace-insensitive', () => {
    expect(boardSlotOf(slot({}, { slot: '  Fuel ' }))).toBe('fuel');
  });

  it('an unrecognised or missing slot degrades to unslotted, never a fourth category', () => {
    expect(boardSlotOf(slot({}, { slot: 'leisure' }))).toBe(BOARD_UNSLOTTED);
    expect(boardSlotOf(slot({}, { slot: 42 }))).toBe(BOARD_UNSLOTTED);
    expect(boardSlotOf(slot({}, {}))).toBe(BOARD_UNSLOTTED);
  });
});

describe('boardStacks — shape and the honest residue', () => {
  it('always returns the three canonical stacks, even on an empty day', () => {
    expect(boardStacks([], stageOf, NOW).map((s) => s.key)).toEqual(['duty', 'rhythm', 'fuel']);
  });

  it('appends the unslotted residue only when it has members, and last', () => {
    const slotted = slot({ id: 'a' }, { slot: 'duty' });
    expect(boardStacks([slotted], stageOf, NOW).map((s) => s.key)).toEqual(['duty', 'rhythm', 'fuel']);
    const residual = slot({ id: 'b' }, {});
    expect(boardStacks([slotted, residual], stageOf, NOW).map((s) => s.key)).toEqual([
      'duty',
      'rhythm',
      'fuel',
      BOARD_UNSLOTTED,
    ]);
  });

  it('drops non-slot kinds', () => {
    const notASlot = slot({ id: 'e1', kind: 'email_tier' }, { slot: 'duty' });
    expect(stackFor([notASlot], 'duty')?.today).toEqual([]);
  });
});

describe('boardSlotsWithADone — the balanced-day scoreline', () => {
  const done = (id: string, key: string) =>
    slot({ id, state: 'acted', acted_at: '2026-08-12T14:00:00Z' }, { slot: key, done: true });

  it('counts a slot once it has anything done', () => {
    expect(boardSlotsWithADone(boardStacks([done('a', 'duty'), done('b', 'fuel')], stageOf, NOW))).toBe(2);
  });

  // The exclusion `tier/slots.py` calls for: an unslotted item must not be able to
  // move the goal, or a migration artifact reads as personal progress.
  it('EXCLUDES the unslotted residue from the scoreline', () => {
    const residueDone = slot({ id: 'r', state: 'acted', acted_at: '2026-08-12T14:00:00Z' }, { done: true });
    expect(boardSlotsWithADone(boardStacks([residueDone], stageOf, NOW))).toBe(0);
    // Positive control: the same item, stamped, DOES count.
    expect(boardSlotsWithADone(boardStacks([done('r', 'duty')], stageOf, NOW))).toBe(1);
  });
});

describe('stayInPlace — completion is a STAGE, not a disappearance', () => {
  const doneToday = slot(
    { id: 'r', created_at: TODAY_CREATED, state: 'acted', acted_at: '2026-08-12T14:00:00Z' },
    { slot: 'duty', done: true },
  );

  it('a just-completed row stays in its section instead of vanishing into the drill', () => {
    const [duty] = boardStacks([doneToday], stageOf, NOW, { stayInPlace: (it) => it.id === 'r' });
    expect(duty.today.map((i) => i.id)).toEqual(['r']);
    expect(duty.done).toEqual([]);
  });

  // Positive control — the SAME item without the flag DOES go to the drill, so
  // the pin above cannot pass against a board that simply never uses the drill.
  it('a server-done row the operator did not just tap goes to the done drill', () => {
    const [duty] = boardStacks([doneToday], stageOf, NOW);
    expect(duty.done.map((i) => i.id)).toEqual(['r']);
    expect(duty.today).toEqual([]);
  });

  it('the scoreline is identical either way — where a row RENDERS never moves the count', () => {
    const inPlace = boardStacks([doneToday], stageOf, NOW, { stayInPlace: () => true })[0];
    const settled = boardStacks([doneToday], stageOf, NOW)[0];
    expect([inPlace.doneCount, inPlace.committedCount]).toEqual([1, 1]);
    expect([settled.doneCount, settled.committedCount]).toEqual([1, 1]);
  });
});

describe('carryover — the episode age signal, off created_at', () => {
  it('an item first seen before today is carryover; one first seen today is not', () => {
    expect(boardIsCarryover(slot({ created_at: YESTERDAY_CREATED }), NOW)).toBe(true);
    expect(boardIsCarryover(slot({ created_at: TODAY_CREATED }), NOW)).toBe(false);
  });

  it('partitions a slot into today vs carried, never listing an item twice', () => {
    const fresh = slot({ id: 'fresh', created_at: TODAY_CREATED }, { slot: 'duty' });
    const carried = slot({ id: 'carried', created_at: YESTERDAY_CREATED }, { slot: 'duty' });
    const duty = stackFor([fresh, carried], 'duty');
    expect(duty?.today.map((i) => i.id)).toEqual(['fresh']);
    expect(duty?.carryover.map((i) => i.id)).toEqual(['carried']);
  });
});

describe('boardIsOverdue — a DAY-KEY compare, not a timestamp compare', () => {
  // The measured trap: `new Date('2026-08-12')` is UTC midnight, whose Halifax
  // day key is 2026-08-11 — so a timestamp compare calls a task due TODAY
  // overdue. Both directions are asserted so the pin cannot pass by always
  // answering false.
  it('a bare due date of TODAY is NOT overdue', () => {
    expect(boardIsOverdue(slot({}, { due_iso: '2026-08-12' }), NOW)).toBe(false);
  });

  it('a bare due date of YESTERDAY IS overdue (positive control)', () => {
    expect(boardIsOverdue(slot({}, { due_iso: '2026-08-11' }), NOW)).toBe(true);
  });

  it('a future due date is not overdue; absent/empty/malformed never guesses', () => {
    expect(boardIsOverdue(slot({}, { due_iso: '2026-08-13' }), NOW)).toBe(false);
    expect(boardIsOverdue(slot({}, { due_iso: '' }), NOW)).toBe(false);
    expect(boardIsOverdue(slot({}, { due_iso: 'soon' }), NOW)).toBe(false);
    expect(boardIsOverdue(slot({}, {}), NOW)).toBe(false);
  });
});

describe('carryoverRank — attention ordering over stamped evidence', () => {
  it('ranks snooze-breakthrough above overdue above the rest', () => {
    const broke = slot({}, { snooze_breakthrough: 'crossed_due' });
    const overdue = slot({}, { due_iso: '2026-08-11' });
    const plain = slot({}, {});
    expect(carryoverRank(broke, NOW)).toBeLessThan(carryoverRank(overdue, NOW));
    expect(carryoverRank(overdue, NOW)).toBeLessThan(carryoverRank(plain, NOW));
  });

  it('surfaces the most attention-worthy carryover first, oldest breaking ties', () => {
    const mk = (id: string, created: string, ev: Record<string, unknown>) =>
      slot({ id, created_at: created }, { slot: 'duty', ...ev });
    const items = [
      mk('plain-new', '2026-08-11T13:00:00Z', {}),
      mk('plain-old', '2026-08-05T13:00:00Z', {}),
      mk('overdue', '2026-08-10T13:00:00Z', { due_iso: '2026-08-09' }),
      mk('broke', '2026-08-10T13:00:00Z', { snooze_breakthrough: 'moved_earlier' }),
    ];
    expect(stackFor(items, 'duty')?.carryover.map((i) => i.id)).toEqual(['broke', 'overdue', 'plain-old']);
  });
});

describe('boardCoverage — the ratified 0.80 floor', () => {
  const slotted = (n: number) =>
    Array.from({ length: n }, (_, i) => slot({ id: `s${i}` }, { slot: 'duty' }));
  const unslotted = (n: number) =>
    Array.from({ length: n }, (_, i) => slot({ id: `u${i}` }, {}));
  const cov = (s: number, u: number) => boardCoverage(boardStacks([...slotted(s), ...unslotted(u)], stageOf, NOW));

  it('counts slotted vs residue off the rendered stacks', () => {
    const c = cov(3, 1);
    expect([c.slotted, c.unslotted, c.total]).toEqual([3, 1, 4]);
    expect(c.fraction).toBe(0.75);
  });

  it('an empty board has NO fraction — no denominator, no claim', () => {
    const c = boardCoverage(boardStacks([], stageOf, NOW));
    expect(c.fraction).toBeNull();
    expect(c.total).toBe(0);
    // …and therefore cannot warn. This is the third state, and it is the one a
    // naive `slotted / total || 0` would get wrong by warning at 0%.
    expect(boardCoverageIsLow(c)).toBe(false);
  });

  // THE BOUNDARY. `< SLOT_COVERAGE_WARN` means exactly 80% is silent, and that
  // edge is a float question: 4/5 is exactly 0.8 in IEEE double, so no epsilon is
  // needed — but if the comparison ever becomes `<=`, this reddens.
  it('exactly 80% does NOT warn; a hair under DOES', () => {
    const at = cov(4, 1); // 4/5 = 0.8 exactly
    expect(at.fraction).toBe(SLOT_COVERAGE_WARN);
    expect(boardCoverageIsLow(at)).toBe(false);

    const under = cov(3, 1); // 0.75
    expect(boardCoverageIsLow(under)).toBe(true);
  });

  it('holds at the boundary for other ratios that reduce to 4/5', () => {
    for (const [s, u] of [[8, 2], [16, 4], [40, 10]] as const) {
      expect(boardCoverageIsLow(cov(s, u))).toBe(false);
    }
  });

  it('full coverage is silent — the dormant case that ships today', () => {
    const c = cov(3, 0);
    expect(c.fraction).toBe(1);
    expect(boardCoverageIsLow(c)).toBe(false);
  });

  it('the threshold is the ratified 0.80', () => {
    // Pinned as a VALUE, not just consumed: the operator set this dial on
    // 2026-08-12 and a silent drift of it changes what the board admits to.
    expect(SLOT_COVERAGE_WARN).toBe(0.8);
  });
});

describe('carryoverReason — the row says WHY it is still here', () => {
  it('names the breakthrough cause when the item came back early', () => {
    expect(carryoverReason(slot({}, { snooze_breakthrough: 'crossed_due' }), NOW)).toBe(
      'Back early — its due date passed',
    );
    expect(carryoverReason(slot({}, { snooze_breakthrough: 'moved_earlier' }), NOW)).toBe(
      'Back early — it moved sooner',
    );
    // An unrecognised breakthrough value still says the true, weaker thing rather
    // than inventing a cause it does not know.
    expect(carryoverReason(slot({}, { snooze_breakthrough: 'something_new' }), NOW)).toBe('Back early');
  });

  it('falls back to overdue, then to the plain carried note', () => {
    expect(carryoverReason(slot({}, { due_iso: '2026-08-11' }), NOW)).toBe('Overdue');
    expect(carryoverReason(slot({}, { due_iso: '2026-08-12' }), NOW)).toBe('Carried over');
    expect(carryoverReason(slot({}, {}), NOW)).toBe('Carried over');
  });
});

describe('the caps DEMOTE, they never drop', () => {
  const carried = (n: number) =>
    Array.from({ length: n }, (_, i) =>
      slot({ id: `c${i}`, created_at: `2026-08-0${i + 1}T13:00:00Z` }, { slot: 'duty' }),
    );
  const candidates = (n: number) =>
    Array.from({ length: n }, (_, i) =>
      slot({ id: `s${i}`, created_at: TODAY_CREATED }, { slot: 'duty', candidate: true }),
    );

  it('caps carryover at 3 and puts the remainder in overflow', () => {
    const duty = stackFor(carried(5), 'duty');
    expect(duty?.carryover).toHaveLength(CARRYOVER_CAP);
    expect(duty?.overflow).toHaveLength(2);
  });

  it('caps candidates at 3 and puts the remainder in overflow', () => {
    const duty = stackFor(candidates(5), 'duty');
    expect(duty?.candidates).toHaveLength(CANDIDATE_CAP);
    expect(duty?.overflow).toHaveLength(2);
  });

  it('conserves every item across the partition — nothing is silently lost', () => {
    const items = [...carried(5), ...candidates(4)];
    const duty = stackFor(items, 'duty');
    const rendered =
      (duty?.today.length ?? 0) +
      (duty?.carryover.length ?? 0) +
      (duty?.candidates.length ?? 0) +
      (duty?.done.length ?? 0) +
      (duty?.overflow.length ?? 0);
    expect(rendered).toBe(items.length);
  });

  it('never caps today’s own commitments — a commitment is not a suggestion', () => {
    const todays = Array.from({ length: 6 }, (_, i) =>
      slot({ id: `t${i}`, created_at: TODAY_CREATED }, { slot: 'duty' }),
    );
    expect(stackFor(todays, 'duty')?.today).toHaveLength(6);
    expect(stackFor(todays, 'duty')?.overflow).toHaveLength(0);
  });

  it('counts committed (planned + done) and excludes candidates from the denominator', () => {
    const items = [
      slot({ id: 'p', created_at: TODAY_CREATED }, { slot: 'duty' }),
      slot({ id: 'd', state: 'acted', acted_at: '2026-08-12T14:00:00Z' }, { slot: 'duty', done: true }),
      slot({ id: 'c', created_at: TODAY_CREATED }, { slot: 'duty', candidate: true }),
    ];
    const duty = stackFor(items, 'duty');
    expect(duty?.committedCount).toBe(2);
    expect(duty?.doneCount).toBe(1);
  });
});
