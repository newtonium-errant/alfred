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
// GET, which the client hits on every PWA load). Chosen over instrumentation.ts
// because that hook is still behind `experimental.instrumentationHook` in 14.2
// (an experimental global toggle) and runs in every runtime (needs a
// NEXT_RUNTIME guard). The singleton needs no config change, no edge hazard, and
// for an inert-by-default doorbell it's the lower-risk boot path. Trade-off: after
// a server restart the poller resumes on the next push-route hit (the client's
// on-load public-key probe covers this) rather than at boot — acceptable for the
// operator trial. It reads the feed through the EXISTING BFF-internal path
// (callTransportFeed) — NO new transport surface.

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
  // #27 slice 3 — push-eligibility gate. Ring ONLY for items the operator's
  // policy permits (default: email_urgent + high_source==="override"). Widening
  // is a PUSH_POLICY env flip, not a code change (see pushPolicy.ts). Applied
  // HERE (not inside fetchNeedsYouItems) so the gate is exercised regardless of
  // the injected fetch dep.
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

let pollerTimer: ReturnType<typeof setInterval> | null = null;

/**
 * Start the background poller IF push is enabled and it isn't already running.
 * Idempotent + inert-safe: with PUSH_ENABLED unset/false the interval is never
 * constructed. Kicked by the push routes. The iteration is crash-isolated — a
 * failed poll logs and the interval keeps firing; it can NEVER take the server
 * down.
 */
export function ensurePushPoller(): void {
  if (pollerTimer != null) return;
  if (!isPushEnabled()) return;
  pollerTimer = setInterval(() => {
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
  if (typeof pollerTimer.unref === 'function') pollerTimer.unref();
  console.log(`[push:poll] poller started interval_ms=${POLL_INTERVAL_MS}`);
}

/** Test seam: whether the singleton interval is currently constructed. */
export function __isPushPollerRunningForTest(): boolean {
  return pollerTimer != null;
}

/** Test seam: stop + reset the singleton between cases. */
export function __stopPushPollerForTest(): void {
  if (pollerTimer != null) {
    clearInterval(pollerTimer);
    pollerTimer = null;
  }
}
