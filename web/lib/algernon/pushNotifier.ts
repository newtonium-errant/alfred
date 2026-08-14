import webpush from 'web-push';
import type { FeedItem } from './feed';
import { isNeedsYouItem } from './feedNeedsYou';
import { isPushEnabled, readVapidConfig } from './pushConfig';
import { pushPayloadFor } from './pushPayload';
import { isPushEligible, readPushPolicy } from './pushPolicy';
import {
  readSeenIds,
  readSubscriptions,
  removeSubscription,
  writeSeenIds,
  type StoredSubscription,
} from './pushStore';
import {
  appendTrialRow,
  readTrialConfig,
  readTrialRows,
  runTrialOnce,
  type TrialDeps,
} from './pushTrial';

// SERVER-ONLY. The push notifier: a background poller that diffs the HOME feed's
// needs-you items and rings the doorbell (one push per NEWLY-seen item id) to
// every stored subscription. Built INERT — the singleton only constructs its
// interval when PUSH_ENABLED (+ VAPID) is set; otherwise it never runs.
//
// Lifecycle (VERIFIED, not assumed — Next 14.2.35, plain `next start`): a
// lazy module-level SINGLETON kicked by the push routes (subscribe + public-key
// GET, which the client hits on every PWA load) — AND, since the arming gap
// below was observed in production, ALSO at server boot via instrumentation.ts.
// It reads the feed through the EXISTING BFF-internal path (callTransportFeed)
// — NO new transport surface.
//
// THE TRADE-OFF THAT WAS REVISED, AND WHY IT IS RECORDED RATHER THAN ERASED.
// The original design chose the lazy singleton over instrumentation.ts because
// that hook sits behind `experimental.instrumentationHook` in 14.2 (verified
// again here: `next/dist/server/config-shared.js` defaults it false inside the
// experimental block, in the installed 14.2.35) and runs in EVERY runtime, so it
// needs a NEXT_RUNTIME guard. Both objections were true and remain true. The
// stated trade-off was: "after a server restart the poller resumes on the next
// push-route hit (the client's on-load public-key probe covers this) rather than
// at boot — acceptable for the operator trial."
//
// The probe does NOT cover it, and the gap has a cost that was invisible until
// it was paid: after a deploy restart the poller sits dormant until the operator
// opens the app, so a scheduled wave that comes due in that window is simply
// never sent. Nothing logs, because dormancy's only signature is absence. That
// is the friction the deferral was waiting for, so the deferral is now spent —
// boot arming is added, the two objections are paid rather than dodged (the
// experimental flag is set deliberately in next.config.js; the runtime guard is
// in instrumentation.ts and is why its import is dynamic), and the lazy kick
// STAYS as the belt: two independent arming paths, both idempotent.

const POLL_INTERVAL_MS = 60_000;
const SEEN_MAX = 500;

// --- pure helpers (unit-pinned) ---------------------------------------------

/**
 * Bound the seen-id set to `max`, NEVER dropping an id that is still open (those
 * must not re-push). Fills the remaining budget with the most-recently-seen gone
 * ids; if open ids alone exceed the cap, keeps the most-recent `max` of them.
 */
export function boundSeen(seen: Set<string>, openIds: Set<string>, max = SEEN_MAX): string[] {
  const all = [...seen];
  if (all.length <= max) return all;
  const open = all.filter((id) => openIds.has(id));
  if (open.length >= max) return open.slice(open.length - max);
  const gone = all.filter((id) => !openIds.has(id));
  const keptGone = gone.slice(gone.length - (max - open.length));
  return [...keptGone, ...open];
}

function isGoneError(e: unknown): boolean {
  // web-push rejects with a statusCode; 404/410 = the browser dropped the sub.
  const code = (e as { statusCode?: number } | null)?.statusCode;
  return code === 404 || code === 410;
}

function errName(e: unknown): string {
  return (e as { statusCode?: number; message?: string } | null)?.statusCode !== undefined
    ? `http_${(e as { statusCode: number }).statusCode}`
    : (e as Error)?.message ?? 'unknown';
}

// Endpoints are capability URLs — log only the origin, never the full path/token.
function redactEndpoint(endpoint: string): string {
  try {
    return new URL(endpoint).origin;
  } catch {
    return '(invalid)';
  }
}

// --- the poll (injectable deps → fully testable without FS/network) ---------

export interface PollDeps {
  readSubscriptions: () => Promise<StoredSubscription[]>;
  removeSubscription: (endpoint: string) => Promise<boolean>;
  readSeenIds: () => Promise<Set<string>>;
  writeSeenIds: (ids: string[]) => Promise<void>;
  fetchNeedsYouItems: () => Promise<FeedItem[]>;
  sendPush: (sub: StoredSubscription, payload: string) => Promise<void>;
}

export interface PollResult {
  reason: 'no_subscriptions' | 'no_new_items' | 'sent';
  fresh: number;
  sent: number;
  pruned: number;
}

/** One poll pass: diff needs-you items against the seen-set, push the new ones. */
export async function runPollOnce(deps: PollDeps): Promise<PollResult> {
  const subs = await deps.readSubscriptions();
  if (subs.length === 0) {
    // ILB: ran, nothing to send to.
    console.log('[push:poll] ran subs=0 (no subscriptions — nothing to send)');
    return { reason: 'no_subscriptions', fresh: 0, sent: 0, pruned: 0 };
  }

  const items = await deps.fetchNeedsYouItems();
  // Push-eligibility gate. The DEFAULT is `needs_you` — every needs-you item
  // rings (operator ruling); NARROWING is the `PUSH_POLICY` env flip, not a code
  // change (see pushPolicy.ts). Applied HERE, not inside fetchNeedsYouItems, so
  // the gate is exercised regardless of the injected fetch dep.
  //
  // This comment said the opposite until the gate caught it, and the reason is
  // worth more than the correction: the default was swept where it is DEFINED
  // and not where it is CONSUMED. A semantic inversion leaves every NAME
  // untouched — "default" and "widening" were both still literally the right
  // words — so a rename-grep cannot see it. Whoever debugs a doorbell reads
  // THIS line before pushPolicy.ts, which is what made a stale comment three
  // lines above the call the expensive place for it to sit.
  const policy = readPushPolicy();
  const eligible = items.filter((it) => isPushEligible(it, policy));
  const seen = await deps.readSeenIds();
  const fresh = eligible.filter((it) => !seen.has(it.id));
  if (fresh.length === 0) {
    // ILB: ran, nothing new to ring for (needs_you vs eligible distinguishes
    // "nothing waiting" from "policy filtered everything out").
    console.log(
      `[push:poll] ran subs=${subs.length} needs_you=${items.length} policy=${policy} eligible=${eligible.length} fresh=0 (nothing new)`,
    );
    return { reason: 'no_new_items', fresh: 0, sent: 0, pruned: 0 };
  }

  let sent = 0;
  let pruned = 0;
  let liveSubs = subs;
  for (const item of fresh) {
    const payload = JSON.stringify(pushPayloadFor(item));
    for (const sub of liveSubs) {
      try {
        await deps.sendPush(sub, payload);
        sent += 1;
      } catch (e) {
        if (isGoneError(e)) {
          await deps.removeSubscription(sub.endpoint);
          liveSubs = liveSubs.filter((s) => s.endpoint !== sub.endpoint);
          pruned += 1;
        } else {
          console.warn(`[push:poll] send_failed endpoint=${redactEndpoint(sub.endpoint)} err=${errName(e)}`);
        }
      }
    }
    seen.add(item.id);
  }

  // Persist the bounded seen-set (open ids pinned so a still-open item never
  // re-rings after the cap rolls over). Pin the ELIGIBLE open ids — those are
  // the only ones that were pushed and added to the seen-set.
  const openIds = new Set(eligible.map((i) => i.id));
  await deps.writeSeenIds(boundSeen(seen, openIds, SEEN_MAX));
  console.log(
    `[push:poll] ran subs=${subs.length} needs_you=${items.length} policy=${policy} eligible=${eligible.length} fresh=${fresh.length} sent=${sent} pruned=${pruned}`,
  );
  return { reason: 'sent', fresh: fresh.length, sent, pruned };
}

// --- the real deps + the singleton ------------------------------------------

let vapidInitialised = false;
function realSendPush(sub: StoredSubscription, payload: string): Promise<void> {
  const vapid = readVapidConfig();
  if (!vapid) return Promise.reject(new Error('vapid_not_configured'));
  if (!vapidInitialised) {
    webpush.setVapidDetails(vapid.subject, vapid.publicKey, vapid.privateKey);
    vapidInitialised = true;
  }
  return webpush
    .sendNotification({ endpoint: sub.endpoint, keys: { p256dh: sub.p256dh, auth: sub.auth } }, payload)
    .then(() => undefined);
}

async function fetchNeedsYouItems(): Promise<FeedItem[]> {
  // Lazy import to keep the transport (and its env reads) out of the module graph
  // until the poller actually runs.
  const { callTransportFeed } = await import('./transport');
  const { status, body } = await callTransportFeed('GET', '/feed/items', { query: { state: 'open' } });
  if (status !== 200 || !body || typeof body !== 'object') return [];
  const items = (body as { items?: unknown }).items;
  if (!Array.isArray(items)) return [];
  return (items as FeedItem[]).filter(isNeedsYouItem);
}

const realDeps: PollDeps = {
  readSubscriptions,
  removeSubscription,
  readSeenIds,
  writeSeenIds,
  fetchNeedsYouItems,
  sendPush: realSendPush,
};

// --- the delivery trial, riding the same heartbeat ---------------------------

/** Send one trial push to every stored subscription. Returns the recipient count;
 * throws only if EVERY subscription failed, so a partial send still counts as
 * sent (the trial measures whether the doorbell reaches the phone, and one live
 * endpoint answers that). */
async function sendTrialToAll(payload: string): Promise<number> {
  const subs = await readSubscriptions();
  if (subs.length === 0) throw new Error('no_subscriptions');
  let ok = 0;
  let lastErr = 'unknown';
  for (const sub of subs) {
    try {
      await realSendPush(sub, payload);
      ok += 1;
    } catch (e) {
      lastErr = errName(e);
      if (isGoneError(e)) await removeSubscription(sub.endpoint);
    }
  }
  if (ok === 0) throw new Error(lastErr);
  return ok;
}

const realTrialDeps: TrialDeps = {
  readRows: () => readTrialRows(),
  appendRow: (row) => appendTrialRow(row),
  sendTrialPush: sendTrialToAll,
  now: () => Date.now(),
  config: readTrialConfig,
};

// THE SINGLETON HAS TO OUTLIVE THE MODULE INSTANCE, because there is more than
// one of those.
//
// This was a module-level `let`, and `ensurePushPoller`'s `if (pollerTimer !=
// null) return` is idempotent only WITHIN one module instance. It is not one
// instance. `next build` emits this module into TWO server chunks, each carrying
// its own `setInterval` and its own "poller started" log, because
// `instrumentation.ts` and the pages-router API routes are separate
// compilations. THAT is the invariant; the chunk FILENAMES are build
// coordinates, not names — reproduce the claim by COUNTING chunks that contain
// the start log, never by looking for a numbered file.
//
// And the reason is worse than "the ids are random". They are STABLE WITHIN AN
// ENVIRONMENT ACROSS COMMITS and DIFFER ACROSS ENVIRONMENTS: this machine
// produced 4477/1867 from two different trees, the reviewer's produced
// 4294/4676 from two different trees. So the number is perfectly reproducible
// for whoever writes it down, holds every time they re-check it, and is wrong
// for everybody else. A reproducible-but-local coordinate is more dangerous in
// a comment than a random one, because nothing the author can do will catch it. Boot-arming touches one copy, the first
// route hit touches the other, each sees its own `null`, and BOTH arm. Every due
// item then rings twice on every poll.
//
// No unit test can see this: vitest loads exactly one module instance, which is
// precisely the world the old guard was written for and the only world in which
// it was sufficient. The two arming paths were called "both idempotent" — true
// of each path, false of the pair.
//
// `globalThis` is per-PROCESS and is the one scope the copies share, so the
// timer handle lives there. The accessors exist so no call site can reach past
// them and re-introduce a local.
const POLLER_GLOBAL_KEY = '__algernonPushPollerTimer__';
type PollerHost = Record<string, ReturnType<typeof setInterval> | null | undefined>;

function pollerTimerGet(): ReturnType<typeof setInterval> | null {
  return (globalThis as unknown as PollerHost)[POLLER_GLOBAL_KEY] ?? null;
}

function pollerTimerSet(t: ReturnType<typeof setInterval> | null): void {
  (globalThis as unknown as PollerHost)[POLLER_GLOBAL_KEY] = t;
}

/**
 * Start the background poller IF push is enabled and it isn't already running.
 * Idempotent + inert-safe: with PUSH_ENABLED unset/false the interval is never
 * constructed. Kicked by the push routes. The iteration is crash-isolated — a
 * failed poll logs and the interval keeps firing; it can NEVER take the server
 * down.
 */
export function ensurePushPoller(): void {
  if (pollerTimerGet() != null) return;
  if (!isPushEnabled()) return;
  const timer = setInterval(() => {
    void runPollOnce(realDeps).catch((e) => {
      console.warn(`[push:poll] iteration_failed err=${errName(e)}`);
    });
    // The delivery trial rides this heartbeat rather than owning a timer: it is
    // a seven-day instrument, and a second scheduler would be a second thing
    // that can silently stop. Crash-isolated SEPARATELY from the feed poll —
    // neither can take the other down, and a trial fault must never cost the
    // operator a real doorbell.
    void runTrialOnce(realTrialDeps).catch((e) => {
      console.warn(`[push:trial] iteration_failed err=${errName(e)}`);
    });
  }, POLL_INTERVAL_MS);
  pollerTimerSet(timer);
  if (typeof timer.unref === 'function') timer.unref();
  console.log(`[push:poll] poller started interval_ms=${POLL_INTERVAL_MS}`);
}

/**
 * Arm the poller at server boot — called from `instrumentation.ts`.
 *
 * SPEAKS ON BOTH OUTCOMES, and that is the point rather than a nicety.
 * `ensurePushPoller` logs only when it STARTS something; on a disabled or
 * already-armed instance it returns in silence. A boot line that appears only on
 * success reproduces exactly the failure this function exists to fix — dormancy
 * whose only signature is absence. So every path says which path it took, and
 * "the doorbell is off" is as visible in the journal as "the doorbell is on".
 *
 * The subscription count rides the armed line because it is the number that
 * makes the line actionable: an armed poller with `subs=0` is a doorbell with
 * nobody behind it, which reads as healthy and delivers nothing.
 *
 * NEVER THROWS. This runs inside Next's boot hook; an exception here would take
 * the server down at start, which is a strictly worse failure than the one being
 * fixed. A push doorbell must not be able to stop the app from serving.
 */
export async function armPushPollerAtBoot(): Promise<void> {
  try {
    if (!isPushEnabled()) {
      console.log('[push:boot] instrument inert reason=push_disabled_or_unconfigured');
      return;
    }
    const already = pollerTimerGet() != null;
    ensurePushPoller();
    let subs = -1;
    try {
      subs = (await readSubscriptions()).length;
    } catch (e) {
      // A store read failure must not un-arm the poller — it is already running
      // by this point. Report the count as unknown rather than claiming zero.
      console.warn(`[push:boot] subs_unreadable err=${errName(e)}`);
    }
    console.log(
      `[push:boot] instrument armed subs=${subs < 0 ? 'unknown' : subs}` +
        `${already ? ' (already running — kicked by a route first)' : ''}`,
    );
  } catch (e) {
    console.warn(`[push:boot] arming_failed err=${errName(e)}`);
  }
}

/** Test seam: whether the singleton interval is currently constructed. */
export function __isPushPollerRunningForTest(): boolean {
  return pollerTimerGet() != null;
}

/** Test seam: stop + reset the singleton between cases. */
export function __stopPushPollerForTest(): void {
  const t = pollerTimerGet();
  if (t != null) {
    clearInterval(t);
    pollerTimerSet(null);
  }
}
