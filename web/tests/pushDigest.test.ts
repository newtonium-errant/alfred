import { describe, expect, it, vi } from 'vitest';
import {
  DIGEST_KIND,
  DIGEST_MIN_ITEMS,
  DIGEST_TAG,
  DIGEST_URL,
  EMAIL_URGENT_KIND,
  digestBreakdown,
  digestPayloadFor,
  partitionForPush,
  pushKindNoun,
  pushTagFor,
} from '../lib/algernon/pushDigest';
import { composeBatch, runPollOnce, type PollDeps } from '../lib/algernon/pushNotifier';
import { isPushEligible } from '../lib/algernon/pushPolicy';
import type { StoredSubscription } from '../lib/algernon/pushStore';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

// THE DIGEST. One push per poll batch instead of one per item: a single brief
// fire sent the operator 6+ separate phone buzzes, which is one piece of
// information delivered six times on the one surface that interrupts him.
//
// The ratified exception is `email_urgent` — a genuinely urgent email rings
// alone, because being singled out IS its value.

function item(id: string, kind = 'slot_suggestion'): FeedItem {
  return withServedActions({
    id,
    kind,
    instance: 'salem',
    title: `Item ${id}`,
    mode: 'decide',
    attention: 'needs_you',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-08-16T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  });
}
const sub = (endpoint: string): StoredSubscription => ({
  user: 'Andrew', endpoint, p256dh: 'p', auth: 'a', at: '',
});

function makeDeps(overrides: Partial<PollDeps> = {}): PollDeps {
  return {
    readSubscriptions: vi.fn().mockResolvedValue([sub('https://push.example/a')]),
    removeSubscription: vi.fn().mockResolvedValue(true),
    readSeenIds: vi.fn().mockResolvedValue(new Set<string>()),
    writeSeenIds: vi.fn().mockResolvedValue(undefined),
    fetchNeedsYouItems: vi.fn().mockResolvedValue([]),
    sendPush: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}
const payloadsFrom = (deps: PollDeps) =>
  (deps.sendPush as ReturnType<typeof vi.fn>).mock.calls.map((c) => JSON.parse(c[1] as string));

describe('digest text', () => {
  it('counts by kind, most-numerous first, ties broken by kind name', () => {
    const items = [item('a'), item('b'), item('c'), item('d'), item('e', 'health')];
    expect(digestBreakdown(items)).toBe('4 tasks, 1 health');
  });

  it('is DETERMINISTIC for a set — the same state never renders two ways', () => {
    const forward = [item('a'), item('b', 'health'), item('c', 'email_tier')];
    const reversed = [...forward].reverse();
    expect(digestBreakdown(forward)).toBe(digestBreakdown(reversed));
  });

  it('uses nouns, not UI chrome — and never pluralises an uncountable one', () => {
    expect(pushKindNoun('slot_suggestion')).toBe('tasks');
    expect(pushKindNoun('health')).toBe('health'); // NOT "healths"
  });

  it('COUNTS an unknown kind rather than dropping it', () => {
    // A digest that silently omitted a kind would under-report, which is the
    // failure this whole design exists to avoid — worse than an ugly noun.
    expect(pushKindNoun('some_new_kind')).toBe('some new kind');
    expect(digestBreakdown([item('a'), item('b', 'some_new_kind')])).toContain('1 some new kind');
  });

  it('the payload carries the count in the TITLE, and rolls on one tag', () => {
    const p = digestPayloadFor([item('a'), item('b'), item('c', 'health')]);
    expect(p.title).toBe('2 tasks, 1 health need you');
    expect(p.kind).toBe(DIGEST_KIND);
    expect(p.url).toBe(DIGEST_URL);
    expect(p.tag).toBe(DIGEST_TAG);
    // The title, not the body, carries the count: a service worker older than
    // this change composes its own body and would drop whatever we put there.
    expect(p.title).toContain('need you');
  });
});

describe('partitionForPush', () => {
  it('splits urgent email out and is TOTAL — no item can be lost', () => {
    const items = [item('a'), item('u1', EMAIL_URGENT_KIND), item('b')];
    const { individual, digestable } = partitionForPush(items);
    expect(individual.map((i) => i.id)).toEqual(['u1']);
    expect(digestable.map((i) => i.id)).toEqual(['a', 'b']);
    expect(individual.length + digestable.length).toBe(items.length);
  });

  it('DRIFT PIN: the exempt kind is the one the policy layer means by it', () => {
    // `pushPolicy` owns the eligibility gate and this lane must not touch it, so
    // the kind string is spelled twice. This drives the REAL policy function
    // with our constant — the two cannot diverge without reddening here.
    expect(isPushEligible(item('x', EMAIL_URGENT_KIND), 'email_urgent_all')).toBe(true);
    expect(isPushEligible(item('x', 'slot_suggestion'), 'email_urgent_all')).toBe(false);
  });
});

describe('composeBatch — the collapse rule', () => {
  it('COLLAPSES a multi-item batch into ONE notification', () => {
    const fresh = [item('a'), item('b'), item('c'), item('d')];
    const out = composeBatch(fresh, fresh);
    expect(out).toHaveLength(1);
    expect(out[0].kind).toBe(DIGEST_KIND);
    expect(out[0].title).toBe('4 tasks need you');
  });

  it('an urgent email rings ALONE, alongside the digest, with its own tag', () => {
    const urgent = item('u1', EMAIL_URGENT_KIND);
    const fresh = [item('a'), item('b'), urgent];
    const out = composeBatch(fresh, fresh);
    expect(out).toHaveLength(2);
    const [first, second] = out;
    expect(first.kind).toBe(EMAIL_URGENT_KIND);
    expect(first.title).toBe('Item u1'); // its own title survives — not a count
    expect(second.kind).toBe(DIGEST_KIND);
    // The tags MUST differ or the digest replaces the urgent one in the tray —
    // the ratified exception defeated by a collapse rule one layer down.
    expect(first.tag).toBe(pushTagFor(urgent));
    expect(first.tag).not.toBe(second.tag);
  });

  it('two urgent emails do not overwrite EACH OTHER', () => {
    const fresh = [item('u1', EMAIL_URGENT_KIND), item('u2', EMAIL_URGENT_KIND)];
    const out = composeBatch(fresh, fresh);
    expect(out).toHaveLength(2);
    expect(out[0].tag).not.toBe(out[1].tag);
  });

  it('a LONE item rings as itself — a digest of one loses its title for nothing', () => {
    const out = composeBatch([item('a')], [item('a')]);
    expect(out).toHaveLength(1);
    expect(out[0].title).toBe('Item a');
    expect(out[0].kind).not.toBe(DIGEST_KIND);
  });

  it('the digest counts ALL OUTSTANDING work, not just the new arrivals', () => {
    // The rolling tag makes each digest REPLACE the last, so it must be a
    // complete statement. Counting only the new one would replace "3 need you"
    // with "1 needs you" while four were waiting — collapsing into an
    // under-report, which is worse than the noise it set out to fix.
    const fresh = [item('d')];
    const outstanding = [item('a'), item('b'), item('c'), item('d')];
    const out = composeBatch(fresh, outstanding);
    expect(out).toHaveLength(1);
    expect(out[0].title).toBe('4 tasks need you');
  });

  it('rings NOTHING when nothing is fresh, however much is outstanding', () => {
    expect(composeBatch([], [item('a'), item('b')])).toEqual([]);
  });

  it('DIGEST_MIN_ITEMS is the documented floor', () => {
    expect(DIGEST_MIN_ITEMS).toBe(2);
  });
});

describe('runPollOnce — the digest, through the real poll', () => {
  it('sends ONE push for a six-item batch (the reported 6+ buzzes)', async () => {
    const items = ['a', 'b', 'c', 'd', 'e', 'f'].map((i) => item(i));
    const deps = makeDeps({ fetchNeedsYouItems: vi.fn().mockResolvedValue(items) });
    const res = await runPollOnce(deps);
    expect(res.notifications).toBe(1);
    expect(deps.sendPush).toHaveBeenCalledTimes(1); // one subscription × one notification
    expect(payloadsFrom(deps)[0].title).toBe('6 tasks need you');
  });

  it('still marks EVERY folded item seen, so the digest cannot re-ring', async () => {
    const items = [item('a'), item('b'), item('c')];
    const deps = makeDeps({ fetchNeedsYouItems: vi.fn().mockResolvedValue(items) });
    await runPollOnce(deps);
    const written = (deps.writeSeenIds as ReturnType<typeof vi.fn>).mock.calls[0][0] as string[];
    expect([...written].sort()).toEqual(['a', 'b', 'c']);
  });

  it('urgent + routine in one batch → two notifications, urgent intact', async () => {
    const items = [item('a'), item('b'), item('u1', EMAIL_URGENT_KIND)];
    const deps = makeDeps({ fetchNeedsYouItems: vi.fn().mockResolvedValue(items) });
    const res = await runPollOnce(deps);
    expect(res.notifications).toBe(2);
    const titles = payloadsFrom(deps).map((p) => p.title);
    expect(titles).toContain('Item u1');
    expect(titles).toContain('2 tasks need you');
  });

  it('POSITIVE CONTROL: notifications still reach every subscription', async () => {
    // Without this, the collapse pins above would pass identically against a
    // poller that had stopped sending altogether.
    const deps = makeDeps({
      readSubscriptions: vi.fn().mockResolvedValue([
        sub('https://push.example/a'), sub('https://push.example/b'),
      ]),
      fetchNeedsYouItems: vi.fn().mockResolvedValue([item('a'), item('b')]),
    });
    const res = await runPollOnce(deps);
    expect(res.notifications).toBe(1);
    expect(res.sent).toBe(2); // ONE notification, TWO subscribers
    expect(deps.sendPush).toHaveBeenCalledTimes(2);
  });
});
