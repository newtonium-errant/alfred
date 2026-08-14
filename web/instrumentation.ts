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
 * 2. `register()` RUNS IN EVERY RUNTIME, including edge. `pushNotifier` pulls in
 *    `web-push` and the node fs-backed stores, which cannot be bundled for edge
 *    — so the import must be DYNAMIC and must sit behind the runtime guard. A
 *    static import at the top of this file would be evaluated when the edge
 *    bundle is built, which is the failure the guard actually prevents; it is
 *    not defensive politeness.
 *
 * Everything here is best-effort by construction: `armPushPollerAtBoot` catches
 * its own failures, because a doorbell must never be able to stop the server
 * from starting.
 */
export async function register(): Promise<void> {
  if (process.env.NEXT_RUNTIME !== 'nodejs') return;
  const { armPushPollerAtBoot } = await import('./lib/algernon/pushNotifier');
  await armPushPollerAtBoot();
}
