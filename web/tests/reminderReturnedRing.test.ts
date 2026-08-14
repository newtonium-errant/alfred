import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { runPollOnce, type PollDeps } from '../lib/algernon/pushNotifier';
import { isNeedsYouItem } from '../lib/algernon/feedNeedsYou';
import { isPushEligible, readPushPolicy } from '../lib/algernon/pushPolicy';
import type { FeedItem } from '../lib/algernon/feed';
import type { StoredSubscription } from '../lib/algernon/pushStore';
import { withServedActions } from './helpers/servedActions';

// T2-1 — THE PIN THAT THE DOORBELL RINGS FOR A RETURNED REMINDER.
//
// WHY THIS TEST IS IN TYPESCRIPT AND READS PYTHON. The ring is a cross-language
// property with its switch on the far side: `reminder_returned` reaches the
// phone because `KIND_DEFAULTS` in `src/alfred/feed/model.py` gives it
// `needs_you`, and for no other reason — `fetchNeedsYouItems` filters by
// ATTENTION with no kind allowlist, and the default push policy admits every
// needs-you item. So the Python registration is the whole wiring, and a Python
// test asserting "the tuple says needs_you" would only be restating the line it
// is guarding. The property worth pinning is that an item carrying whatever
// Python actually says REACHES `sendPush`.
//
// Hence the item below takes its mode/attention FROM THE PYTHON SOURCE rather
// than from a literal typed here. Downgrade the `KIND_DEFAULTS` entry (or delete
// it — `.get` silently falls back to fyi) and this test goes red, which is the
// one thing a hand-written `attention: 'needs_you'` fixture could never do: it
// would keep passing while the operator's phone stayed silent.

const MODEL_PY = join(process.cwd(), '..', 'src', 'alfred', 'feed', 'model.py');

/** Top-level `NAME = "value"` constants, so a tuple of symbols can be resolved. */
function pythonStringConstants(source: string): Record<string, string> {
  const out: Record<string, string> = {};
  const re = /^([A-Z][A-Z0-9_]*)\s*=\s*["']([^"']+)["']\s*$/gm;
  for (const m of source.matchAll(re)) out[m[1]] = m[2];
  return out;
}

/**
 * The `(mode, attention)` Python actually assigns to `reminder_returned`.
 *
 * Handles the entry being keyed by the `KIND_REMINDER_RETURNED` constant rather
 * than a bare literal, and resolves the tuple's two symbols through the module's
 * own constants — so this reads the source's meaning, not its spelling.
 */
function kindDefaultsForReturnedReminder(): { kind: string; mode: string; attention: string } {
  const source = readFileSync(MODEL_PY, 'utf8');
  const constants = pythonStringConstants(source);
  const kind = constants.KIND_REMINDER_RETURNED;
  if (!kind) throw new Error('KIND_REMINDER_RETURNED not found in model.py');

  // The KIND_DEFAULTS entry, keyed by the constant OR by the literal string.
  const entry = new RegExp(
    `^\\s*(?:KIND_REMINDER_RETURNED|["']${kind}["'])\\s*:\\s*\\(\\s*([A-Za-z0-9_"']+)\\s*,\\s*([A-Za-z0-9_"']+)\\s*\\)`,
    'm',
  ).exec(source);
  if (!entry) throw new Error(`no KIND_DEFAULTS entry for ${kind} in model.py`);

  const resolve = (token: string): string => {
    const literal = /^["'](.+)["']$/.exec(token);
    if (literal) return literal[1];
    const value = constants[token];
    if (!value) throw new Error(`cannot resolve ${token} in model.py`);
    return value;
  };
  return { kind, mode: resolve(entry[1]), attention: resolve(entry[2]) };
}

const PY = kindDefaultsForReturnedReminder();

function returnedReminderItem(id = 'reminder_returned:task/Fix Carfax.md|2026-08-14T09:00:00+00:00'): FeedItem {
  return withServedActions({
    id,
    kind: PY.kind,
    instance: 'Salem',
    title: 'Chase Carfax: Fix Carfax Mileage Discrepancy',
    // FROM PYTHON — see the header. Not a literal.
    mode: PY.mode,
    attention: PY.attention,
    evidence: { record_path: 'task/Fix Carfax.md', return_kind: 'waiting_chase' },
    actions: [],
    state: 'open',
    created_at: '2026-08-14T09:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: { producer: 'transport_scheduler' },
  });
}

/** A glance card, for the negative control: same pipeline, must NOT ring. */
function fyiItem(): FeedItem {
  return withServedActions({
    id: 'weather:2026-08-14',
    kind: 'weather',
    instance: 'Salem',
    title: 'Fog 11:00–15:00',
    mode: 'fyi',
    attention: 'fyi',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-08-14T06:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  });
}

function sub(endpoint: string): StoredSubscription {
  return { user: 'Andrew', endpoint, p256dh: 'p', auth: 'a', at: '' };
}

/**
 * Deps whose fetch applies the REAL server-side filter (`isNeedsYouItem`), the
 * same one `fetchNeedsYouItems` applies to the transport's response. Injecting a
 * pre-filtered list would skip the gate this test exists to exercise.
 */
function makeDeps(feed: FeedItem[], overrides: Partial<PollDeps> = {}): PollDeps {
  return {
    readSubscriptions: vi.fn().mockResolvedValue([sub('https://push.example/a')]),
    removeSubscription: vi.fn().mockResolvedValue(true),
    readSeenIds: vi.fn().mockResolvedValue(new Set<string>()),
    writeSeenIds: vi.fn().mockResolvedValue(undefined),
    fetchNeedsYouItems: vi.fn().mockImplementation(async () => feed.filter(isNeedsYouItem)),
    sendPush: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

// PUSH_POLICY is deliberately left UNSET in every case below: the default is the
// operator's ruling ('needs_you'), and the default is what production runs.
const ENV = ['PUSH_POLICY', 'PUSH_ENABLED'];
beforeEach(() => {
  for (const k of ENV) delete process.env[k];
  vi.spyOn(console, 'log').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});
afterEach(() => {
  for (const k of ENV) delete process.env[k];
  vi.restoreAllMocks();
});

describe('reminder_returned reaches the doorbell', () => {
  it('reads a real KIND_DEFAULTS entry from the Python source', () => {
    // Positive control for the READER itself: every assertion below is vacuous
    // if the regex silently matched nothing, or the path silently moved.
    expect(PY.kind).toBe('reminder_returned');
    expect(PY.mode).toBe('decide');
    expect(PY.attention).toBe('needs_you');
  });

  it('passes the needs-you fetch filter and the default push policy', () => {
    const item = returnedReminderItem();
    expect(isNeedsYouItem(item)).toBe(true);
    expect(readPushPolicy()).toBe('needs_you');
    expect(isPushEligible(item, readPushPolicy())).toBe(true);
  });

  it('rings: one push per subscription for a newly-seen returned reminder', async () => {
    const deps = makeDeps([returnedReminderItem()]);
    const result = await runPollOnce(deps);
    expect(result.reason).toBe('sent');
    expect(result.fresh).toBe(1);
    expect(deps.sendPush).toHaveBeenCalledTimes(1);
    // The payload the phone actually shows carries the card's own title.
    const [, payload] = (deps.sendPush as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(payload as string).title).toContain('Chase Carfax');
  });

  it('does not ring twice for the same return (seen-set)', async () => {
    const item = returnedReminderItem();
    const deps = makeDeps([item], {
      readSeenIds: vi.fn().mockResolvedValue(new Set([item.id])),
    });
    const result = await runPollOnce(deps);
    expect(result.reason).toBe('no_new_items');
    expect(deps.sendPush).not.toHaveBeenCalled();
  });

  it('negative control: an FYI card in the same poll is filtered out', async () => {
    // The pipeline CAN say no — without this, "the returned reminder rang" is
    // consistent with a filter that passes everything.
    const deps = makeDeps([fyiItem(), returnedReminderItem()]);
    const result = await runPollOnce(deps);
    expect(result.fresh).toBe(1);
    expect(deps.sendPush).toHaveBeenCalledTimes(1);
    const [, payload] = (deps.sendPush as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(JSON.parse(payload as string).kind).toBe(PY.kind);
  });

  it('is dealt into the deck: the ceiling serves it a gesture-bearing verb', () => {
    // The other half of "the operator is told". A decide card served no
    // gesture-bearing verb is `arrivedVerbless` — a FAULT the deck reports
    // rather than a card it deals — so the ring would land him on a card he
    // could not clear. The fixture is generated from `actions_for()` and pinned
    // to it by tests/feed/test_feed_advertised_actions.py.
    const item = returnedReminderItem();
    const verbs = (item.actions ?? []) as Array<Record<string, unknown>>;
    expect(verbs.length).toBeGreaterThan(0);
    expect(verbs.some((v) => v.gesture === 'affirm' && v.verb === 'ack')).toBe(true);
  });
});
