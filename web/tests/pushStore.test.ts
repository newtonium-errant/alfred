import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, readFileSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';
import {
  addSubscription,
  readSeenIds,
  readSubscriptions,
  removeSubscription,
  writeSeenIds,
  type StoredSubscription,
} from '../lib/algernon/pushStore';

// Pins the subscription store (dedupe-on-endpoint, remove) and the seen-id set,
// against a real temp dir. Also the forge-safety property (a newline in any field
// can't create a second JSONL record).

let dir: string;
let counter = 0;

function sub(overrides: Partial<StoredSubscription> = {}): StoredSubscription {
  return {
    user: 'Andrew',
    endpoint: 'https://push.example.com/aaa',
    p256dh: 'p256',
    auth: 'auth',
    at: '2026-07-30T00:00:00Z',
    ...overrides,
  };
}

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'push-store-'));
  process.env.ALFRED_WEB_PUSH_SUBS = join(dir, `subs-${counter}.jsonl`);
  process.env.ALFRED_WEB_PUSH_SEEN = join(dir, `seen-${counter}.jsonl`);
  counter += 1;
});
afterEach(() => {
  delete process.env.ALFRED_WEB_PUSH_SUBS;
  delete process.env.ALFRED_WEB_PUSH_SEEN;
  try {
    rmSync(dir, { recursive: true, force: true });
  } catch {
    /* best-effort */
  }
});

describe('subscription store', () => {
  it('reads empty when the file does not exist yet', async () => {
    expect(await readSubscriptions()).toEqual([]);
  });

  it('adds and dedupes on endpoint (latest wins)', async () => {
    await addSubscription(sub({ endpoint: 'https://push.example.com/a', auth: 'first' }));
    await addSubscription(sub({ endpoint: 'https://push.example.com/b' }));
    await addSubscription(sub({ endpoint: 'https://push.example.com/a', auth: 'second' }));
    const all = await readSubscriptions();
    expect(all).toHaveLength(2);
    const a = all.find((s) => s.endpoint === 'https://push.example.com/a');
    expect(a?.auth).toBe('second');
  });

  it('removes by endpoint and reports whether one was removed', async () => {
    await addSubscription(sub({ endpoint: 'https://push.example.com/a' }));
    expect(await removeSubscription('https://push.example.com/missing')).toBe(false);
    expect(await removeSubscription('https://push.example.com/a')).toBe(true);
    expect(await readSubscriptions()).toEqual([]);
  });

  it('a newline in a field cannot forge a second subscription record', async () => {
    await addSubscription(sub({ endpoint: 'https://push.example.com/a', user: 'Andrew\n{"endpoint":"forged"}' }));
    const raw = readFileSync(process.env.ALFRED_WEB_PUSH_SUBS as string, 'utf8');
    expect(raw.split('\n').filter(Boolean)).toHaveLength(1);
    const all = await readSubscriptions();
    expect(all).toHaveLength(1);
    expect(all[0].user).toBe('Andrew\n{"endpoint":"forged"}');
  });
});

describe('seen-id set', () => {
  it('round-trips ids and reads empty when absent', async () => {
    expect([...(await readSeenIds())]).toEqual([]);
    await writeSeenIds(['email_tier:note/A.md', 'attribution:x']);
    expect([...(await readSeenIds())].sort()).toEqual(['attribution:x', 'email_tier:note/A.md']);
  });

  it('escapes a newline in an id so it stays one record', async () => {
    await writeSeenIds(['weird\nid']);
    const raw = readFileSync(process.env.ALFRED_WEB_PUSH_SEEN as string, 'utf8');
    expect(raw.split('\n').filter(Boolean)).toHaveLength(1);
    expect((await readSeenIds()).has('weird\nid')).toBe(true);
  });
});
