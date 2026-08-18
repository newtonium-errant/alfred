import { describe, expect, it, vi } from 'vitest';
import {
  CONCLUDED_PUSH_ID,
  ROW_CANCELLED,
  ROW_CONCLUDED,
  buildSchedule,
  concludedRow,
  dueSlots,
  runTrialOnce,
  spentIds,
  type TrialDeps,
  type TrialRow,
} from '../lib/algernon/pushTrial';

// THE TRIAL'S EARLY END. A seven-day schedule with no stop verb left only bad
// options once the question was answered: keep firing for days it no longer
// needs, or switch the sender off and leave every remaining slot looking like an
// instrument outage.
//
// The rule the whole module is built on (its own docstring): a quiet instrument
// is indistinguishable from a stopped one. So a concluded trial does not go
// silent — it records, per slot, that the slot was CANCELLED and why.

const START = Date.parse('2026-08-16T00:00:00-03:00');
const cfg = { startMs: START, days: 2 };

function makeDeps(rows: TrialRow[], nowMs: number, overrides: Partial<TrialDeps> = {}): {
  deps: TrialDeps;
  appended: TrialRow[];
} {
  const appended: TrialRow[] = [];
  const live = [...rows];
  const deps: TrialDeps = {
    readRows: vi.fn().mockImplementation(async () => [...live]),
    appendRow: vi.fn().mockImplementation(async (r: TrialRow) => {
      appended.push(r);
      live.push(r);
    }),
    sendTrialPush: vi.fn().mockResolvedValue(1),
    now: () => nowMs,
    config: () => cfg,
    ...overrides,
  };
  return { deps, appended };
}

/** The materialised schedule, as the first pass would write it. */
const scheduledRows = (): TrialRow[] =>
  buildSchedule(cfg).map((s) => ({
    type: 'scheduled' as const,
    push_id: s.push_id,
    due_ts: new Date(s.dueMs).toISOString(),
  }));

const concluded = (reason = 'delivery confirmed'): TrialRow => ({
  type: ROW_CONCLUDED as TrialRow['type'],
  push_id: CONCLUDED_PUSH_ID,
  concluded_ts: '2026-08-16T18:00:00.000Z',
  reason,
});

// A moment after every slot of day 1 is due and before day 2 — so there is real
// remaining schedule to cancel, and a real due slot that would otherwise send.
const AFTER_DAY1 = START + 22 * 3_600_000;

describe('conclusion stops the sends', () => {
  it('a due slot does NOT send once the trial is concluded', async () => {
    const { deps } = makeDeps([...scheduledRows(), concluded()], AFTER_DAY1);
    const res = await runTrialOnce(deps);
    expect(deps.sendTrialPush).not.toHaveBeenCalled();
    expect(res.reason).toBe('concluded');
  });

  it('POSITIVE CONTROL: without the conclusion, the same due slot DOES send', async () => {
    // The pin above is vacuous without this — it would pass identically against
    // a sender that had stopped working entirely.
    const { deps } = makeDeps(scheduledRows(), AFTER_DAY1);
    const res = await runTrialOnce(deps);
    expect(deps.sendTrialPush).toHaveBeenCalled();
    expect(res.sent).toBeGreaterThan(0);
    expect(res.cancelled).toBe(0);
  });

  it('records every remaining slot as CANCELLED, with the reason — never skipped', async () => {
    const { deps, appended } = makeDeps([...scheduledRows(), concluded('delivery confirmed')], AFTER_DAY1);
    const res = await runTrialOnce(deps);
    const cancels = appended.filter((r) => r.type === ROW_CANCELLED);
    // Every slot in the schedule is accounted for: none silently skipped.
    expect(cancels).toHaveLength(buildSchedule(cfg).length);
    expect(res.cancelled).toBe(cancels.length);
    for (const row of cancels) {
      expect(row.reason).toBe('delivery confirmed');
      expect(row.cancelled_ts).toBeTruthy();
    }
  });

  it('retires FUTURE slots too, not just the ones already due', async () => {
    // A check that only guarded due slots would leave later ones reading
    // `pending` — a status surface promising sends that will never come.
    const { appended, deps } = makeDeps([...scheduledRows(), concluded()], AFTER_DAY1);
    await runTrialOnce(deps);
    const ids = appended.filter((r) => r.type === ROW_CANCELLED).map((r) => r.push_id);
    expect(ids).toContain('trial-d2-w3'); // day 2 is entirely in the future here
  });

  it('does NOT cancel a slot that already sent — an observation is not retracted', async () => {
    const already: TrialRow = { type: 'sent', push_id: 'trial-d1-w1', sent_ts: '2026-08-16T11:07:03.000Z' };
    const { appended, deps } = makeDeps([...scheduledRows(), already, concluded()], AFTER_DAY1);
    await runTrialOnce(deps);
    const ids = appended.filter((r) => r.type === ROW_CANCELLED).map((r) => r.push_id);
    expect(ids).not.toContain('trial-d1-w1');
  });

  it('is IDEMPOTENT — a second pass writes nothing and still says so', async () => {
    // The poller keeps beating every 60s after a conclusion. Re-cancelling would
    // grow an append-only ledger without bound to record one decision.
    const rows = [...scheduledRows(), concluded()];
    const first = makeDeps(rows, AFTER_DAY1);
    await runTrialOnce(first.deps);
    const afterFirst = [...rows, ...first.appended];
    const second = makeDeps(afterFirst, AFTER_DAY1 + 60_000);
    const res = await runTrialOnce(second.deps);
    expect(second.appended).toHaveLength(0);
    expect(res.reason).toBe('concluded');
    expect(res.cancelled).toBe(0);
  });
});

describe('the ledger helpers', () => {
  it('spentIds counts a cancelled slot as spent (so it is not re-cancelled)', () => {
    const rows: TrialRow[] = [
      { type: 'sent', push_id: 'a' },
      { type: 'send_failed', push_id: 'b' },
      { type: ROW_CANCELLED as TrialRow['type'], push_id: 'c' },
      { type: 'scheduled', push_id: 'd' },
    ];
    expect([...spentIds(rows)].sort()).toEqual(['a', 'b', 'c']);
  });

  it('dueSlots skips a cancelled slot but still returns an untouched one', () => {
    const schedule = buildSchedule(cfg);
    const rows: TrialRow[] = [{ type: ROW_CANCELLED as TrialRow['type'], push_id: schedule[0].push_id }];
    const due = dueSlots(schedule, rows, AFTER_DAY1).map((s) => s.push_id);
    expect(due).not.toContain(schedule[0].push_id);
    expect(due).toContain(schedule[1].push_id); // control: the rest are still due
  });

  it('concludedRow is FIRST-wins — a conclusion is an event, not an opinion', () => {
    const rows = [
      { ...concluded('first'), concluded_ts: '2026-08-16T18:00:00.000Z' },
      { ...concluded('second'), concluded_ts: '2026-08-17T09:00:00.000Z' },
    ];
    expect(concludedRow(rows)?.reason).toBe('first');
    expect(concludedRow([])).toBeNull();
  });

  it('the conclusion row carries a PRESENT-but-empty push_id', () => {
    // Both readers require push_id to be a string and treat a row without one as
    // malformed — the Python side counts it into `skipped_rows`, its corruption
    // signal. A stop marker must not read as a damaged ledger.
    expect(CONCLUDED_PUSH_ID).toBe('');
    expect(concluded()).toHaveProperty('push_id');
  });
});
