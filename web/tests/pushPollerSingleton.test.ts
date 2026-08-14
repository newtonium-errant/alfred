import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// THE DUPLICATE-ARM RACE, pinned at the shape that actually produces it.
//
// `ensurePushPoller`'s guard was `if (pollerTimer != null) return` over a
// MODULE-LEVEL `let`. That is idempotent within one module instance, and the
// server does not have one. `next build` emits the poller into TWO server
// chunks, one `setInterval` and one "poller started" log each, because
// `instrumentation.ts` and the pages-router API routes are separate
// compilations. The chunk filenames are build coordinates rather than names and
// change between builds of the same tree — count the chunks carrying the start
// log; do not go looking for a numbered file. Boot-arming touches one
// copy, the first route hit touches the other, each reads its own `null`, and
// both arm. Every due item then rings TWICE, forever, with two "poller started"
// lines in the journal as the only tell.
//
// NO EXISTING TEST COULD SEE IT. vitest loads exactly one module instance —
// precisely the world the old guard was sufficient for. So this file builds the
// two-instance world deliberately: `vi.resetModules()` between imports is the
// closest in-process analogue of what webpack does across chunks, and the
// assertion is on the number of INTERVALS CONSTRUCTED, not on a flag.

const ENV = ['ALGERNON_VAPID_PUBLIC', 'ALGERNON_VAPID_PRIVATE', 'ALGERNON_VAPID_SUBJECT', 'PUSH_ENABLED'];

beforeEach(() => {
  for (const k of ENV) delete process.env[k];
  process.env.PUSH_ENABLED = 'true';
  process.env.ALGERNON_VAPID_PUBLIC = 'DUMMY_VAPID_TEST_PUBLIC';
  process.env.ALGERNON_VAPID_PRIVATE = 'DUMMY_VAPID_TEST_PRIVATE';
  process.env.ALGERNON_VAPID_SUBJECT = 'mailto:test@example.invalid';
});

afterEach(async () => {
  const m = await import('../lib/algernon/pushNotifier');
  m.__stopPushPollerForTest();
  for (const k of ENV) delete process.env[k];
  vi.restoreAllMocks();
  vi.resetModules();
});

describe('the poller singleton spans module instances', () => {
  it('constructs exactly ONE interval across two independent module copies', async () => {
    const spy = vi.spyOn(globalThis, 'setInterval');

    vi.resetModules();
    const first = await import('../lib/algernon/pushNotifier');
    first.__stopPushPollerForTest(); // start from a known-clear global
    spy.mockClear();
    first.ensurePushPoller();
    expect(first.__isPushPollerRunningForTest()).toBe(true);
    // POSITIVE CONTROL: arming really does construct an interval, so a zero
    // below would mean "nothing armed", not "the guard worked".
    expect(spy).toHaveBeenCalledTimes(1);

    vi.resetModules();
    const second = await import('../lib/algernon/pushNotifier');
    // A genuinely different module instance — the thing webpack hands the server.
    expect(second).not.toBe(first);

    // The state is visible ACROSS the boundary. Before the fix this was `false`,
    // because the second copy read its own module-level `null`.
    expect(second.__isPushPollerRunningForTest()).toBe(true);

    second.ensurePushPoller();
    // THE PROPERTY: the second arming path is a no-op. Not "a flag says running"
    // — no second timer was constructed, which is what the operator's phone
    // would have felt as every notification arriving twice.
    expect(spy).toHaveBeenCalledTimes(1);

    second.__stopPushPollerForTest();
    expect(first.__isPushPollerRunningForTest()).toBe(false);
  });

  it('stays inert across instances when push is disabled', async () => {
    process.env.PUSH_ENABLED = 'false';
    const spy = vi.spyOn(globalThis, 'setInterval');

    vi.resetModules();
    const first = await import('../lib/algernon/pushNotifier');
    first.__stopPushPollerForTest();
    spy.mockClear();
    first.ensurePushPoller();

    vi.resetModules();
    const second = await import('../lib/algernon/pushNotifier');
    second.ensurePushPoller();

    // Inert means inert in BOTH copies — the shared handle must not become a way
    // for one instance to arm a disabled deployment on another's behalf.
    expect(spy).not.toHaveBeenCalled();
    expect(second.__isPushPollerRunningForTest()).toBe(false);
  });
});
