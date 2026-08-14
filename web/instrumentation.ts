/**
 * Next's boot hook — the ONLY place a pages-router app on `next start` can run
 * code at server start.
 *
 * WHY THIS FILE EXISTS. The push poller is a module-level singleton that arms on
 * first touch of a route importing it. After a deploy restart nothing touches
 * those routes until the operator opens the app, so the poller sits dormant and
 * a scheduled wave coming due in that window is never sent — with no log line,
 * because dormancy's only signature is absence. This arms it at boot instead.
 *
 * TWO CONSTRAINTS, BOTH LOAD-BEARING, BOTH MEASURED AGAINST THE INSTALLED NEXT
 * (14.2.35) RATHER THAN ASSUMED:
 *
 * 1. THIS FILE IS DEAD WITHOUT `experimental.instrumentationHook: true` in
 *    next.config.js. In 14.2 the hook is still experimental and defaults to
 *    false (`next/dist/server/config-shared.js`, inside the experimental block),
 *    so without that flag Next never loads this module and the arming silently
 *    does nothing — a perfect `register()` with no caller. The config is pinned
 *    by pushBootArming.test.ts for exactly that reason.
 *
 * 2. `register()` RUNS IN EVERY RUNTIME, including edge — and the shape of the
 *    guard is load-bearing at BUILD time, not just at run time. This is the
 *    part the first version of this file got wrong, so it is written down
 *    precisely:
 *
 *    `pushNotifier` pulls in `web-push` and the node fs-backed stores, so its
 *    transitive imports need `fs/promises`, `path` and `http` — none of which
 *    resolve in the EDGE compilation. Next replaces `process.env.NEXT_RUNTIME`
 *    with a per-compilation literal, so a guard on it is statically decidable;
 *    but webpack eliminates dead BRANCH BODIES, and an early return is not one.
 *    Written as
 *
 *        if (process.env.NEXT_RUNTIME !== 'nodejs') return;
 *        await import('./lib/algernon/pushNotifier');
 *
 *    the import sits AFTER the return, still reachable in the module graph,
 *    still traced, and the edge build fails to resolve `fs/promises`. Written
 *    with the import INSIDE the positive branch — as below — the edge
 *    compilation sees `if (false) { … }` and drops the block whole, import and
 *    all. Same runtime semantics, opposite build outcome.
 *
 *    So: a dynamic import is necessary and NOT sufficient. It must also be
 *    inside the eliminable branch. `pushBootArming.test.ts` pins that shape,
 *    but the real acceptance test is `next build` — see the note below.
 *
 * Everything here is best-effort by construction: `armPushPollerAtBoot` catches
 * its own failures, because a doorbell must never be able to stop the server
 * from starting.
 *
 * THE GATE THIS FILE NEEDS. `vitest` and `tsc` both pass a broken version of
 * this module — neither is the compiler that deploys. The first version of this
 * file shipped green through both and failed `next build`, which took the deploy
 * down. Any change here, to `next.config.js`, or to what this module's import
 * graph reaches, must be verified with `next build` run to a bare exit code.
 */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME === 'nodejs') {
    const { armPushPollerAtBoot } = await import('./lib/algernon/pushNotifier');
    await armPushPollerAtBoot();
  }
}
