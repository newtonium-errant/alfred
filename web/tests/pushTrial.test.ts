import { mkdtemp, readFile, writeFile } from 'fs/promises';
import { tmpdir } from 'os';
import { join } from 'path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  TRIAL_DAYS,
  TRIAL_SENDS_PER_DAY,
  TRIAL_WINDOW_BASES_MIN,
  appendTrialRow,
  buildSchedule,
  dueSlots,
  isTrialEnabled,
  readTrialConfig,
  readTrialRows,
  runTrialOnce,
  slotPushId,
  trialPayloadFor,
  type TrialDeps,
  type TrialRow,
} from '../lib/algernon/pushTrial';

// Pins the trial instrument: schedule determinism, the four-state ledger, the
// no-retry rule, and the ILB line on an idle pass.
//
// THE PROPERTY THIS FILE EXISTS FOR: a slot that was never SENT must never be
// recorded (or later readable) as a slot that failed to ARRIVE. The trial was
// commissioned because "I didn't get a notification" is ambiguous; an instrument
// that reproduces the ambiguity measures nothing.

const ENV = ['PUSH_TRIAL_ENABLED', 'PUSH_TRIAL_START', 'PUSH_TRIAL_DAYS', 'ALFRED_WEB_PUSH_TRIAL'];
const START = '2026-08-12T00:00:00.000Z';
const START_MS = Date.parse(START);

beforeEach(() => {
  for (const k of ENV) delete process.env[k];
  vi.spyOn(console, 'log').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});
afterEach(() => {
  for (const k of ENV) delete process.env[k];
  vi.restoreAllMocks();
});

function makeDeps(over: Partial<TrialDeps> = {}): TrialDeps {
  const rows: TrialRow[] = [];
  return {
    readRows: vi.fn(async () => [...rows]),
    appendRow: vi.fn(async (r: TrialRow) => {
      rows.push(r);
    }),
    sendTrialPush: vi.fn(async () => 1),
    now: () => START_MS,
    config: () => ({ startMs: START_MS, days: 2 }),
    ...over,
  };
}

// --- config gate -------------------------------------------------------------

describe('trial config gate', () => {
  it('is inert until explicitly enabled', () => {
    expect(isTrialEnabled()).toBe(false);
    expect(readTrialConfig()).toBeNull();
  });

  it('needs a start date even when enabled — no implicit "today"', () => {
    // An implicit start would make the schedule un-reproducible: a restart would
    // silently re-anchor the trial and every prior slot would read as orphaned.
    process.env.PUSH_TRIAL_ENABLED = 'true';
    expect(readTrialConfig()).toBeNull();
  });

  it('reads an explicit window', () => {
    process.env.PUSH_TRIAL_ENABLED = 'true';
    process.env.PUSH_TRIAL_START = START;
    process.env.PUSH_TRIAL_DAYS = '3';
    expect(readTrialConfig()).toEqual({ startMs: START_MS, days: 3 });
  });

  it('falls back to the default length on a junk day count', () => {
    process.env.PUSH_TRIAL_ENABLED = 'true';
    process.env.PUSH_TRIAL_START = START;
    process.env.PUSH_TRIAL_DAYS = 'seven';
    expect(readTrialConfig()?.days).toBe(TRIAL_DAYS);
  });

  it('an unparseable start is null, not epoch 0', () => {
    process.env.PUSH_TRIAL_ENABLED = 'true';
    process.env.PUSH_TRIAL_START = 'next tuesday';
    expect(readTrialConfig()).toBeNull();
  });
});

// --- schedule ----------------------------------------------------------------

describe('schedule', () => {
  it('is three-a-day across the full window', () => {
    const slots = buildSchedule({ startMs: START_MS, days: 7 });
    expect(slots).toHaveLength(7 * TRIAL_SENDS_PER_DAY);
    expect(TRIAL_WINDOW_BASES_MIN).toHaveLength(TRIAL_SENDS_PER_DAY);
  });

  it('is deterministic — same config, same slots', () => {
    const a = buildSchedule({ startMs: START_MS, days: 7 });
    const b = buildSchedule({ startMs: START_MS, days: 7 });
    expect(a).toEqual(b);
  });

  it('spreads across morning / midday / evening', () => {
    const slots = buildSchedule({ startMs: START_MS, days: 1 });
    const hours = slots.map((s) => new Date(s.dueMs).getUTCHours());
    // Distinct windows, ascending — the diurnal question needs spread sends.
    expect(hours[0]).toBeLessThan(hours[1]);
    expect(hours[1]).toBeLessThan(hours[2]);
  });

  it('does not put every send on the hour', () => {
    const slots = buildSchedule({ startMs: START_MS, days: 3 });
    expect(slots.some((s) => new Date(s.dueMs).getUTCMinutes() !== 0)).toBe(true);
  });

  it('ids are unique and stable', () => {
    const slots = buildSchedule({ startMs: START_MS, days: 7 });
    expect(new Set(slots.map((s) => s.push_id)).size).toBe(slots.length);
    expect(slots[0].push_id).toBe(slotPushId(0, 0));
  });
});

// --- payload -----------------------------------------------------------------

describe('payload', () => {
  it('carries the push_id in the deep link so a tap is attributable', () => {
    const p = trialPayloadFor('trial-d1-w2');
    expect(p.url).toBe('/feed?push=trial-d1-w2');
    expect(p.kind).toBe('push_trial');
  });

  it('keeps the three-key lock-screen shape', () => {
    // The privacy rule is not relaxed for a test push.
    expect(Object.keys(trialPayloadFor('x')).sort()).toEqual(['kind', 'title', 'url']);
  });
});

// --- due selection: the no-retry rule ---------------------------------------

describe('dueSlots', () => {
  const schedule = buildSchedule({ startMs: START_MS, days: 1 });

  it('returns nothing before the first slot is due', () => {
    expect(dueSlots(schedule, [], START_MS)).toEqual([]);
  });

  it('returns a slot once its time has passed', () => {
    const due = dueSlots(schedule, [], schedule[0].dueMs);
    expect(due.map((s) => s.push_id)).toContain(schedule[0].push_id);
  });

  it('never re-sends a slot already sent', () => {
    const rows: TrialRow[] = [{ type: 'sent', push_id: schedule[0].push_id, sent_ts: START }];
    const due = dueSlots(schedule, rows, schedule[0].dueMs);
    expect(due.map((s) => s.push_id)).not.toContain(schedule[0].push_id);
  });

  it('does NOT retry a failed send', () => {
    // A retry would fire at an unplanned time; a delivery attributed to a slot
    // it did not come from is worse than a recorded failure.
    const rows: TrialRow[] = [
      { type: 'send_failed', push_id: schedule[0].push_id, sent_ts: START, error: 'boom' },
    ];
    const due = dueSlots(schedule, rows, schedule[0].dueMs);
    expect(due.map((s) => s.push_id)).not.toContain(schedule[0].push_id);
  });

  it('a scheduled row alone does not count as attempted', () => {
    // The whole never-sent state depends on this: a materialised slot with no
    // send row is still owed.
    const rows: TrialRow[] = [
      { type: 'scheduled', push_id: schedule[0].push_id, due_ts: START },
    ];
    expect(dueSlots(schedule, rows, schedule[0].dueMs).map((s) => s.push_id))
      .toContain(schedule[0].push_id);
  });
});

// --- the pass ----------------------------------------------------------------

describe('runTrialOnce', () => {
  it('is a no-op when the trial is off', async () => {
    const deps = makeDeps({ config: () => null });
    const r = await runTrialOnce(deps);
    expect(r.reason).toBe('disabled');
    expect(deps.appendRow).not.toHaveBeenCalled();
    expect(deps.sendTrialPush).not.toHaveBeenCalled();
  });

  it('materialises the whole schedule once', async () => {
    const deps = makeDeps();
    await runTrialOnce(deps);
    const rows = await deps.readRows();
    expect(rows.filter((r) => r.type === 'scheduled')).toHaveLength(2 * TRIAL_SENDS_PER_DAY);
    // Second pass must not duplicate.
    await runTrialOnce(deps);
    const after = await deps.readRows();
    expect(after.filter((r) => r.type === 'scheduled')).toHaveLength(2 * TRIAL_SENDS_PER_DAY);
  });

  it('every scheduled row carries its due time', async () => {
    // Without due_ts the reader cannot tell a never-sent slot from a future one.
    const deps = makeDeps();
    await runTrialOnce(deps);
    const rows = (await deps.readRows()).filter((r) => r.type === 'scheduled');
    expect(rows.every((r) => typeof r.due_ts === 'string' && r.due_ts.length > 0)).toBe(true);
  });

  it('sends a due slot and records it', async () => {
    const schedule = buildSchedule({ startMs: START_MS, days: 2 });
    const deps = makeDeps({ now: () => schedule[0].dueMs });
    const r = await runTrialOnce(deps);
    expect(r.sent).toBe(1);
    const rows = await deps.readRows();
    const sent = rows.filter((x) => x.type === 'sent');
    expect(sent).toHaveLength(1);
    expect(sent[0].push_id).toBe(schedule[0].push_id);
    expect(sent[0].sent_ts).toBeTruthy();
  });

  it('records a send failure WITHOUT a sent row', async () => {
    // The two must never be conflated downstream.
    const schedule = buildSchedule({ startMs: START_MS, days: 2 });
    const deps = makeDeps({
      now: () => schedule[0].dueMs,
      sendTrialPush: vi.fn(async () => {
        throw new Error('no_subscriptions');
      }),
    });
    const r = await runTrialOnce(deps);
    expect(r.failed).toBe(1);
    const rows = await deps.readRows();
    expect(rows.filter((x) => x.type === 'sent')).toHaveLength(0);
    const failed = rows.filter((x) => x.type === 'send_failed');
    expect(failed).toHaveLength(1);
    expect(failed[0].error).toBe('no_subscriptions');
  });

  it('says so on an idle pass (ILB)', async () => {
    const logs: string[] = [];
    (console.log as unknown as { mockImplementation: (f: (s: string) => void) => void })
      .mockImplementation((s: string) => logs.push(s));
    const deps = makeDeps();
    await runTrialOnce(deps);
    // Silence from a seven-day instrument is indistinguishable from a stopped one.
    expect(logs.some((l) => l.includes('[push:trial] ran due=0'))).toBe(true);
  });
});

// --- ledger I/O --------------------------------------------------------------

describe('ledger', () => {
  it('round-trips through the real file writer', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'trial-'));
    const path = join(dir, 'push_trial.jsonl');
    await appendTrialRow({ type: 'scheduled', push_id: 'trial-d1-w1', due_ts: START }, path);
    await appendTrialRow({ type: 'receipt', push_id: 'trial-d1-w1', received_ts: START }, path);
    const rows = await readTrialRows(path);
    expect(rows.map((r) => r.type)).toEqual(['scheduled', 'receipt']);
  });

  it('is empty (not an error) before the trial starts', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'trial-'));
    expect(await readTrialRows(join(dir, 'absent.jsonl'))).toEqual([]);
  });

  it('skips a malformed line rather than losing the whole ledger', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'trial-'));
    const path = join(dir, 'push_trial.jsonl');
    await writeFile(path, `{"type":"sent","push_id":"a"}\nnot json\n{"type":"receipt","push_id":"a"}\n`);
    expect(await readTrialRows(path)).toHaveLength(2);
  });

  it('regenerates the cross-language fixture BYTE-FOR-BYTE', async () => {
    // CROSS-LANGUAGE AGREEMENT PIN. `alfred push-trial status` is Python and
    // reads this exact committed ledger; this side proves the real writer still
    // emits that shape. If either language drifts on field names or key order,
    // its own half goes red — which is the only way a contract spanning two
    // runtimes stays honest without duplicating the logic.
    const dir = await mkdtemp(join(tmpdir(), 'trial-fixture-'));
    const path = join(dir, 'ledger.jsonl');
    const rows: TrialRow[] = [
      { type: 'scheduled', push_id: 'trial-d1-w1', due_ts: '2026-08-12T08:07:00.000Z' },
      { type: 'scheduled', push_id: 'trial-d1-w2', due_ts: '2026-08-12T13:24:00.000Z' },
      { type: 'scheduled', push_id: 'trial-d1-w3', due_ts: '2026-08-12T19:41:00.000Z' },
      { type: 'scheduled', push_id: 'trial-d2-w1', due_ts: '2026-08-13T08:38:00.000Z' },
      { type: 'scheduled', push_id: 'trial-d2-w2', due_ts: '2026-08-13T13:55:00.000Z' },
      { type: 'scheduled', push_id: 'trial-d2-w3', due_ts: '2026-08-14T19:00:00.000Z' },
      { type: 'sent', push_id: 'trial-d1-w1', sent_ts: '2026-08-12T08:07:30.000Z' },
      { type: 'receipt', push_id: 'trial-d1-w1', received_ts: '2026-08-12T08:09:10.000Z' },
      { type: 'sent', push_id: 'trial-d1-w2', sent_ts: '2026-08-12T13:24:20.000Z' },
      {
        type: 'send_failed', push_id: 'trial-d1-w3',
        sent_ts: '2026-08-12T19:41:05.000Z', error: 'no_subscriptions',
      },
      { type: 'sent', push_id: 'trial-d9-w9', sent_ts: '2026-08-12T20:00:00.000Z' },
    ];
    for (const row of rows) await appendTrialRow(row, path);

    const written = await readFile(path, 'utf8');
    const committed = await readFile(
      join(__dirname, '..', '..', 'tests', 'fixtures', 'push_trial_ledger.jsonl'),
      'utf8',
    );
    expect(written).toBe(committed);
  });

  it('tolerates the `ruling` row the PYTHON side writes', async () => {
    // Cross-language, the other direction: `alfred push-trial rule` appends a
    // row type this runtime never writes. A reader that dropped it would be
    // fine today (nothing here consumes rulings) and wrong the moment anything
    // does — and the drop would be silent, which is the failure mode.
    const dir = await mkdtemp(join(tmpdir(), 'trial-'));
    const path = join(dir, 'push_trial.jsonl');
    await writeFile(
      path,
      '{"type":"scheduled","push_id":"trial-d1-w1","due_ts":"2026-08-12T08:07:00.000Z"}\n' +
        '{"type":"sent","push_id":"trial-d1-w1","sent_ts":"2026-08-12T08:07:30.000Z"}\n' +
        '{"type":"ruling","push_id":"trial-d1-w1","verdict":"missed","ruled_ts":"2026-08-13T12:00:00.000Z"}\n',
    );

    const rows = await readTrialRows(path);

    expect(rows).toHaveLength(3);
    expect(rows[2].type).toBe('ruling');
    expect(rows[2].verdict).toBe('missed');
  });

  it('a ruled slot stays ATTEMPTED — a ruling never re-arms a send', async () => {
    // dueSlots filters on sent/send_failed, so this holds today; pinned because
    // the alternative is the instrument re-firing a slot the operator has
    // already answered, at an unplanned time, corrupting his own ruling.
    const schedule = buildSchedule({ startMs: START_MS, days: 1 });
    const rows: TrialRow[] = [
      { type: 'sent', push_id: schedule[0].push_id, sent_ts: START },
      {
        type: 'ruling', push_id: schedule[0].push_id,
        verdict: 'missed', ruled_ts: START,
      },
    ];

    const due = dueSlots(schedule, rows, schedule[0].dueMs);

    expect(due.map((s) => s.push_id)).not.toContain(schedule[0].push_id);
  });

  it('a newline in a value cannot forge a second row', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'trial-'));
    const path = join(dir, 'push_trial.jsonl');
    await appendTrialRow(
      { type: 'send_failed', push_id: 'a', error: 'x\n{"type":"receipt","push_id":"a"}' },
      path,
    );
    const rows = await readTrialRows(path);
    expect(rows).toHaveLength(1);
    expect(rows[0].type).toBe('send_failed');
    expect((await readFile(path, 'utf8')).trim().split('\n')).toHaveLength(1);
  });
});
