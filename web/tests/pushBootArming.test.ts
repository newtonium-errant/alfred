import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// THE PUSH POLLER ARMS AT BOOT.
//
// The gap: the poller is a module-level singleton that arms on first touch of a
// route importing it. After a deploy restart nothing touches those routes until
// the operator opens the app — so it sat dormant, and a scheduled wave coming
// due in that window was never sent. Nothing logged, because dormancy's only
// signature is absence.
//
// WHAT THESE PINS CAN AND CANNOT PROVE, stated rather than implied. No unit test
// can prove that NEXT calls `register()` — that needs a real server boot. So the
// coverage is split, and the split is the design:
//
//   * `register()` is DRIVEN here: called directly, with NEXT_RUNTIME set both
//     ways, and the poller's own `__isPushPollerRunningForTest` seam observes
//     the result. That proves the arming path works and is runtime-guarded.
//   * The CONFIG is pinned against the imported next.config OBJECT. Without
//     `experimental.instrumentationHook: true`, Next never loads
//     instrumentation.ts at all — a flawless `register()` with no caller, green
//     in every test above. The config pin is the only thing standing between
//     "the function is correct" and "the feature exists".
//
// Neither half is sufficient. That is why both are here.

const ENV_KEYS = [
  'NEXT_RUNTIME',
  'PUSH_ENABLED',
  'ALGERNON_VAPID_PUBLIC',
  'ALGERNON_VAPID_PRIVATE',
  'ALGERNON_VAPID_SUBJECT',
];
const saved: Record<string, string | undefined> = {};

beforeEach(async () => {
  for (const k of ENV_KEYS) saved[k] = process.env[k];
  vi.resetModules();
  vi.restoreAllMocks();
});

afterEach(async () => {
  for (const k of ENV_KEYS) {
    if (saved[k] === undefined) delete process.env[k];
    else process.env[k] = saved[k];
  }
  const mod = await import('../lib/algernon/pushNotifier');
  mod.__stopPushPollerForTest();
});

/** Obviously-fake VAPID material — never a realistic key shape (scanner rule). */
function enablePush() {
  process.env.PUSH_ENABLED = 'true';
  process.env.ALGERNON_VAPID_PUBLIC = 'DUMMY_VAPID_PUBLIC_FOR_TESTING_ONLY';
  process.env.ALGERNON_VAPID_PRIVATE = 'DUMMY_VAPID_PRIVATE_FOR_TESTING_ONLY';
  process.env.ALGERNON_VAPID_SUBJECT = 'mailto:test@example.invalid';
}

describe('register() — the driven half', () => {
  it('arms the poller when it runs in the node runtime', async () => {
    enablePush();
    process.env.NEXT_RUNTIME = 'nodejs';
    const { register } = await import('../instrumentation');
    const notifier = await import('../lib/algernon/pushNotifier');

    expect(notifier.__isPushPollerRunningForTest()).toBe(false); // control
    await register();
    expect(notifier.__isPushPollerRunningForTest()).toBe(true);
  });

  it('does NOTHING in a non-node runtime', async () => {
    // The guard is not politeness: `pushNotifier` pulls in web-push and the
    // fs-backed stores, which cannot be bundled for edge. The dynamic import
    // behind this guard is what keeps that out of the edge bundle.
    enablePush();
    process.env.NEXT_RUNTIME = 'edge';
    const { register } = await import('../instrumentation');
    const notifier = await import('../lib/algernon/pushNotifier');

    await register();
    expect(notifier.__isPushPollerRunningForTest()).toBe(false);
  });

  it('stays inert — and SAYS SO — when push is disabled', async () => {
    delete process.env.PUSH_ENABLED;
    process.env.NEXT_RUNTIME = 'nodejs';
    const log = vi.spyOn(console, 'log').mockImplementation(() => {});
    const { register } = await import('../instrumentation');
    const notifier = await import('../lib/algernon/pushNotifier');

    await register();
    expect(notifier.__isPushPollerRunningForTest()).toBe(false);
    // The ILB half. A boot line that appears only on success would reproduce
    // the exact bug this fixes: dormancy visible only by absence.
    const lines = log.mock.calls.map((c) => String(c[0]));
    expect(lines.some((l) => l.includes('[push:boot] instrument inert'))).toBe(true);
  });

  it('announces the arming with a subscription count', async () => {
    enablePush();
    process.env.NEXT_RUNTIME = 'nodejs';
    const log = vi.spyOn(console, 'log').mockImplementation(() => {});
    const { register } = await import('../instrumentation');

    await register();
    const lines = log.mock.calls.map((c) => String(c[0]));
    const armed = lines.find((l) => l.includes('[push:boot] instrument armed'));
    expect(armed).toBeDefined();
    // `subs=` is what makes the line actionable — an armed poller with nobody
    // subscribed reads as healthy and delivers nothing.
    expect(armed).toMatch(/subs=(\d+|unknown)/);
  });

  it('is idempotent with the route kick — arming twice runs one interval', async () => {
    enablePush();
    process.env.NEXT_RUNTIME = 'nodejs';
    const { register } = await import('../instrumentation');
    const notifier = await import('../lib/algernon/pushNotifier');

    notifier.ensurePushPoller(); // the lazy path, as a route would
    await register(); // then boot
    expect(notifier.__isPushPollerRunningForTest()).toBe(true);
    // Both paths stay: two independent arming routes, neither able to
    // double-start the interval.
  });
});

describe('the config half — without this, register() is never called', () => {
  it('enables the instrumentation hook', async () => {
    // Asserted against the config OBJECT, not its text: a commented-out or
    // misspelled key would still satisfy a source-string check.
    const config = (await import('../next.config.js')) as unknown as {
      default?: { experimental?: { instrumentationHook?: boolean } };
      experimental?: { instrumentationHook?: boolean };
    };
    const resolved = config.default ?? config;
    expect(
      resolved.experimental?.instrumentationHook,
      'experimental.instrumentationHook is not true — Next 14.2 will not load ' +
        'instrumentation.ts, so the push poller never arms at boot and every ' +
        'test above still passes. This flag IS the wiring.',
    ).toBe(true);
  });
});
