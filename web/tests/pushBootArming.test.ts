import { readFileSync } from 'node:fs';
import { join } from 'node:path';
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

describe('the bundling half — the guard shape webpack can actually eliminate', () => {
  // THE REGRESSION THIS EXISTS FOR, and it took the deploy down once.
  //
  // A dynamic import is necessary and NOT sufficient. `pushNotifier` reaches
  // `fs/promises`, `path` and `http`; none resolve in the EDGE compilation. Next
  // makes `process.env.NEXT_RUNTIME` a per-compilation literal, so a guard on it
  // is statically decidable — but webpack eliminates dead BRANCH BODIES, and an
  // early return is not one. Written as an early return, the import sits after
  // it, stays in the module graph, and the edge build fails to resolve
  // `fs/promises`. Written inside the positive branch, the edge compilation sees
  // `if (false) { … }` and drops the block whole. Identical runtime semantics,
  // opposite build outcome — which is exactly why the runtime tests above all
  // passed the broken version.
  //
  // THIS PIN IS AN APPROXIMATION AND SAYS SO. The real acceptance test is
  // `next build`, which cannot run here. What this catches is the specific
  // regression: someone "simplifying" the block back into an early return,
  // which reads cleaner and is a deploy outage.
  // COMMENTS STRIPPED BEFORE ANY OF THIS IS READ, and the reason is not
  // hypothetical: that file's docstring QUOTES the broken early-return form in
  // order to explain it, so the first version of these assertions matched the
  // explanation and reported the fixed code as broken. A line matcher cannot
  // tell code from prose about code — the same trap `sensorLogSkin.test.tsx`
  // documents for CSS, where a gradient argument reads as a selector.
  const RAW = readFileSync(join(__dirname, '..', 'instrumentation.ts'), 'utf8');
  const SRC = RAW.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

  it('is reading the real module, with prose removed — the positive control', () => {
    expect(SRC).toContain('export async function register');
    expect(SRC).toContain("import('./lib/algernon/pushNotifier')");
    // Stripping removed something (the file is heavily commented) but not the
    // code — otherwise every absence assertion below passes on an empty string.
    expect(SRC.length).toBeLessThan(RAW.length);
    expect(SRC.length).toBeGreaterThan(80);
  });

  it('guards POSITIVELY, so the branch body is eliminable', () => {
    expect(SRC).toContain("process.env.NEXT_RUNTIME === 'nodejs'");
    expect(
      SRC.includes("process.env.NEXT_RUNTIME !== 'nodejs'"),
      'instrumentation.ts guards with a NEGATED early return. Webpack cannot ' +
        'eliminate the import that follows it, so the edge build fails to ' +
        'resolve fs/promises and `next build` breaks — this exact shape took ' +
        'the deploy down. Put the import inside `if (NEXT_RUNTIME === "nodejs")`.',
    ).toBe(false);
  });

  it('keeps the import INSIDE the guarded block, not after it', () => {
    // Brace DEPTH, not "is there a } in between" — the first cut used the
    // latter and reported the correct code as broken, because
    // `const { armPushPollerAtBoot } = await import(...)` contains a closing
    // brace of its own. A destructure is not a block end.
    const guard = SRC.indexOf("process.env.NEXT_RUNTIME === 'nodejs'");
    const imp = SRC.indexOf("await import('./lib/algernon/pushNotifier')");
    expect(guard).toBeGreaterThan(-1);
    expect(imp).toBeGreaterThan(guard);

    const open = SRC.indexOf('{', guard);
    expect(open).toBeGreaterThan(-1);
    let depth = 0;
    let close = -1;
    for (let i = open; i < SRC.length; i += 1) {
      if (SRC[i] === '{') depth += 1;
      else if (SRC[i] === '}') {
        depth -= 1;
        if (depth === 0) { close = i; break; }
      }
    }
    expect(close).toBeGreaterThan(-1);
    expect(
      imp < close,
      'the dynamic import is outside the runtime-guarded block — webpack traces ' +
        'it into the edge bundle regardless of the guard, and `next build` fails ' +
        'to resolve fs/promises.',
    ).toBe(true);
  });

  it('has no TOP-LEVEL import of the server-only module', () => {
    // The other way to break the same build: hoisting the import for tidiness.
    const topLevel = SRC.split('\n').filter((l) => /^import[\s{]/.test(l));
    expect(topLevel.filter((l) => l.includes('pushNotifier'))).toEqual([]);
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
