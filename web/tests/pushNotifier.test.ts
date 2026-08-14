import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __isPushPollerRunningForTest,
  __stopPushPollerForTest,
  boundSeen,
  ensurePushPoller,
  runPollOnce,
  type PollDeps,
} from '../lib/algernon/pushNotifier';
import type { StoredSubscription } from '../lib/algernon/pushStore';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

// Pins the poll diff (new-item detection, no-re-push, seen persistence, gone-sub
// pruning), the seen-set bounding, and the inert singleton gate.

function item(id: string, overrides: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
    id,
    kind: 'email_tier',
    instance: 'salem',
    title: `Item ${id}`,
    mode: 'decide',
    attention: 'needs_you',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-07-30T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...overrides,
  });
}
function sub(endpoint: string): StoredSubscription {
  return { user: 'Andrew', endpoint, p256dh: 'p', auth: 'a', at: '' };
}

function makeDeps(overrides: Partial<PollDeps> = {}): PollDeps {
  return {
    readSubscriptions: vi.fn().mockResolvedValue([]),
    removeSubscription: vi.fn().mockResolvedValue(true),
    readSeenIds: vi.fn().mockResolvedValue(new Set<string>()),
    writeSeenIds: vi.fn().mockResolvedValue(undefined),
    fetchNeedsYouItems: vi.fn().mockResolvedValue([]),
    sendPush: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

// PUSH_POLICY joins the env-cleanup list (#27 slice 3): the default policy is
// now the strictest gate (email_urgent + override), so the mechanics tests below
// that push generic email_tier items opt into PUSH_POLICY=needs_you to exercise
// the poll diff/prune/seen machinery independent of the eligibility policy.
const VAPID = ['ALGERNON_VAPID_PUBLIC', 'ALGERNON_VAPID_PRIVATE', 'ALGERNON_VAPID_SUBJECT', 'PUSH_ENABLED', 'PUSH_POLICY'];
beforeEach(() => {
  for (const k of VAPID) delete process.env[k];
  vi.spyOn(console, 'log').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});
afterEach(() => {
  __stopPushPollerForTest();
  for (const k of VAPID) delete process.env[k];
  vi.restoreAllMocks();
});

describe('runPollOnce', () => {
  it('does nothing (and does not read the feed) with no subscriptions', async () => {
    const deps = makeDeps();
    const r = await runPollOnce(deps);
    expect(r.reason).toBe('no_subscriptions');
    expect(deps.fetchNeedsYouItems).not.toHaveBeenCalled();
    expect(deps.sendPush).not.toHaveBeenCalled();
  });

  it('sends nothing when every needs-you item is already seen', async () => {
    const deps = makeDeps({
      readSubscriptions: vi.fn().mockResolvedValue([sub('https://p/a')]),
      fetchNeedsYouItems: vi.fn().mockResolvedValue([item('x')]),
      readSeenIds: vi.fn().mockResolvedValue(new Set(['x'])),
    });
    const r = await runPollOnce(deps);
    expect(r.reason).toBe('no_new_items');
    expect(deps.sendPush).not.toHaveBeenCalled();
  });

  it('pushes each fresh item to every subscription and persists the seen id', async () => {
    process.env.PUSH_POLICY = 'needs_you'; // mechanics test: all needs-you eligible
    const writeSeenIds = vi.fn().mockResolvedValue(undefined);
    const sendPush = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps({
      readSubscriptions: vi.fn().mockResolvedValue([sub('https://p/a'), sub('https://p/b')]),
      fetchNeedsYouItems: vi.fn().mockResolvedValue([item('new1')]),
      readSeenIds: vi.fn().mockResolvedValue(new Set<string>()),
      writeSeenIds,
      sendPush,
    });
    const r = await runPollOnce(deps);
    expect(r.reason).toBe('sent');
    expect(r.sent).toBe(2); // one item × two subs
    expect(sendPush).toHaveBeenCalledTimes(2);
    // payload carries the title/kind/url, not the raw item.
    const payload = JSON.parse(sendPush.mock.calls[0][1] as string);
    expect(Object.keys(payload).sort()).toEqual(['kind', 'title', 'url']);
    expect(writeSeenIds).toHaveBeenCalledTimes(1);
    expect(writeSeenIds.mock.calls[0][0]).toContain('new1');
  });

  it('never re-pushes an already-seen item, only the fresh one', async () => {
    process.env.PUSH_POLICY = 'needs_you'; // mechanics test: all needs-you eligible
    const sendPush = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps({
      readSubscriptions: vi.fn().mockResolvedValue([sub('https://p/a')]),
      fetchNeedsYouItems: vi.fn().mockResolvedValue([item('old'), item('fresh')]),
      readSeenIds: vi.fn().mockResolvedValue(new Set(['old'])),
      sendPush,
    });
    const r = await runPollOnce(deps);
    expect(r.sent).toBe(1);
    const pushedTitles = sendPush.mock.calls.map((c) => JSON.parse(c[1] as string).title);
    expect(pushedTitles).toEqual(['Item fresh']);
  });

  it('prunes a subscription the push service reports Gone (410)', async () => {
    process.env.PUSH_POLICY = 'needs_you'; // mechanics test: all needs-you eligible
    const removeSubscription = vi.fn().mockResolvedValue(true);
    const deps = makeDeps({
      readSubscriptions: vi.fn().mockResolvedValue([sub('https://p/dead')]),
      fetchNeedsYouItems: vi.fn().mockResolvedValue([item('n')]),
      sendPush: vi.fn().mockRejectedValue({ statusCode: 410 }),
      removeSubscription,
    });
    const r = await runPollOnce(deps);
    expect(r.pruned).toBe(1);
    expect(r.sent).toBe(0);
    expect(removeSubscription).toHaveBeenCalledWith('https://p/dead');
  });

  // --- #27 slice 3: push-eligibility policy (default = strictest) ------------

  const urgentOverride = (id: string) =>
    item(id, { kind: 'email_urgent', evidence: { high_source: 'override' } });
  const urgentLlm = (id: string) =>
    item(id, { kind: 'email_urgent', evidence: { high_source: 'llm' } });
  const tierMedium = (id: string) => item(id, { kind: 'email_tier' }); // needs-you, non-urgent

  it('default policy rings for EVERY needs-you item — the operator ruling', async () => {
    // No PUSH_POLICY set → needs_you. Reversed from #27 slice 3's strictest
    // default: the needs-you set already IS the "this needs a person" set, so
    // gating inside it suppressed exactly what the operator asked to hear about.
    const sendPush = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps({
      readSubscriptions: vi.fn().mockResolvedValue([sub('https://p/a')]),
      fetchNeedsYouItems: vi.fn().mockResolvedValue([
        urgentOverride('u-ovr'),
        urgentLlm('u-llm'),
        tierMedium('t-med'),
      ]),
      sendPush,
    });
    const r = await runPollOnce(deps);
    expect(r.sent).toBe(3);
    const pushedTitles = sendPush.mock.calls.map((c) => JSON.parse(c[1] as string).title);
    expect(pushedTitles).toEqual(['Item u-ovr', 'Item u-llm', 'Item t-med']);
  });

  it('STILL suppresses everything under an explicitly NARROWED policy', async () => {
    // The counterpart the default flip must not delete: narrowing is now the
    // config flip, and it has to still work, or "needs_you by default" would
    // really mean "needs_you, always". Same fixture as the default case above,
    // opposite expectation, one env var apart.
    process.env.PUSH_POLICY = 'email_urgent_override';
    const sendPush = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps({
      readSubscriptions: vi.fn().mockResolvedValue([sub('https://p/a')]),
      fetchNeedsYouItems: vi.fn().mockResolvedValue([urgentLlm('u-llm'), tierMedium('t-med')]),
      sendPush,
    });
    const r = await runPollOnce(deps);
    expect(r.reason).toBe('no_new_items');
    expect(sendPush).not.toHaveBeenCalled();
  });

  it('PUSH_POLICY=email_urgent_all widens to BOTH highs (still not email_tier)', async () => {
    process.env.PUSH_POLICY = 'email_urgent_all';
    const sendPush = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps({
      readSubscriptions: vi.fn().mockResolvedValue([sub('https://p/a')]),
      fetchNeedsYouItems: vi.fn().mockResolvedValue([
        urgentOverride('u-ovr'),
        urgentLlm('u-llm'),
        tierMedium('t-med'),
      ]),
      sendPush,
    });
    const r = await runPollOnce(deps);
    expect(r.sent).toBe(2);
    const pushedTitles = sendPush.mock.calls.map((c) => JSON.parse(c[1] as string).title).sort();
    expect(pushedTitles).toEqual(['Item u-llm', 'Item u-ovr']);
  });

  it('only the ELIGIBLE open ids are pinned in the persisted seen-set', async () => {
    // Driven under an explicitly NARROWED policy now that the default is
    // needs_you. The property is unchanged and still worth pinning — an
    // ineligible item is never pushed and so must never enter the seen-set, or
    // it would be suppressed forever if the policy later widened. Under the new
    // default every needs-you item is eligible, so the default can no longer
    // EXPRESS this distinction: the pin needs a policy that filters something.
    process.env.PUSH_POLICY = 'email_urgent_override';
    const writeSeenIds = vi.fn().mockResolvedValue(undefined);
    const deps = makeDeps({
      readSubscriptions: vi.fn().mockResolvedValue([sub('https://p/a')]),
      fetchNeedsYouItems: vi.fn().mockResolvedValue([urgentOverride('u-ovr'), tierMedium('t-med')]),
      writeSeenIds,
    });
    await runPollOnce(deps);
    const persisted = writeSeenIds.mock.calls[0][0] as string[];
    expect(persisted).toContain('u-ovr');
    expect(persisted).not.toContain('t-med');
  });
});

describe('boundSeen', () => {
  it('returns everything when under the cap', () => {
    expect(boundSeen(new Set(['a', 'b']), new Set(), 5).sort()).toEqual(['a', 'b']);
  });

  it('keeps ALL still-open ids and stays within the cap', () => {
    const seen = new Set(['g1', 'g2', 'g3', 'g4', 'o1', 'o2', 'o3']);
    const open = new Set(['o1', 'o2', 'o3']);
    const bounded = boundSeen(seen, open, 5);
    expect(bounded.length).toBe(5);
    for (const id of open) expect(bounded).toContain(id);
  });

  it('keeps the most-recent open ids when open alone exceeds the cap', () => {
    const seen = new Set(['o1', 'o2', 'o3', 'o4']);
    const bounded = boundSeen(seen, new Set(['o1', 'o2', 'o3', 'o4']), 2);
    expect(bounded).toEqual(['o3', 'o4']);
  });
});

describe('ensurePushPoller (inert gate)', () => {
  it('does NOT construct the interval when PUSH_ENABLED is unset', () => {
    ensurePushPoller();
    expect(__isPushPollerRunningForTest()).toBe(false);
  });

  it('creates exactly ONE interval across concurrent kicks (setInterval call-count)', () => {
    process.env.ALGERNON_VAPID_PUBLIC = 'pub';
    process.env.ALGERNON_VAPID_PRIVATE = 'priv';
    process.env.ALGERNON_VAPID_SUBJECT = 'mailto:a@b.com';
    process.env.PUSH_ENABLED = 'true';
    // Count the actual setInterval calls — this reddens if the singleton guard
    // leaks and a second kick constructs a second interval (which the prior
    // running-state assertion could NOT detect). Verified by removing the
    // `pollerTimer != null` guard: two kicks → 2 calls → this fails.
    const spy = vi.spyOn(global, 'setInterval');
    ensurePushPoller();
    ensurePushPoller(); // second kick must NOT construct a second interval
    expect(spy).toHaveBeenCalledTimes(1);
    expect(__isPushPollerRunningForTest()).toBe(true);
    __stopPushPollerForTest();
  });
});
