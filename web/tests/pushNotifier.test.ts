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

// Pins the poll diff (new-item detection, no-re-push, seen persistence, gone-sub
// pruning), the seen-set bounding, and the inert singleton gate.

function item(id: string, overrides: Partial<FeedItem> = {}): FeedItem {
  return {
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
  };
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

const VAPID = ['ALGERNON_VAPID_PUBLIC', 'ALGERNON_VAPID_PRIVATE', 'ALGERNON_VAPID_SUBJECT', 'PUSH_ENABLED'];
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
