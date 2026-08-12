import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { DEFAULT_FEED_VIEW, feedViewFromQuery, feedViewQuery, isFeedView } from '../lib/algernon/feedView';

// The feed's two views, driven THROUGH THE PAGE rather than through the
// components. That is the point of this file: `renderDetail` is an optional prop
// that gates every verb on the timeline, and a component-level test would pass
// happily against a page that never threads it — the feature would be accepted
// and dead in the field. So the wiring pins below click a real trace on a real
// FeedPage and assert a real FeedRow with a real Ack comes back.

const { mockList, mockAct, mockReplace, routerQuery } = vi.hoisted(() => ({
  mockList: vi.fn(),
  mockAct: vi.fn(),
  mockReplace: vi.fn(),
  routerQuery: { current: {} as Record<string, unknown> },
}));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: mockReplace, push: vi.fn(), query: routerQuery.current }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));

import FeedPage from '../pages/feed';
import type { FeedItem } from '../lib/algernon/feed';

const pad = (n: number) => String(Math.abs(Math.trunc(n))).padStart(2, '0');
function localIso(y: number, mo: number, d: number, h: number, mi = 0): string {
  const dt = new Date(y, mo - 1, d, h, mi, 0, 0);
  const offMin = -dt.getTimezoneOffset();
  const sign = offMin >= 0 ? '+' : '-';
  return `${y}-${pad(mo)}-${pad(d)}T${pad(h)}:${pad(mi)}:00${sign}${pad(offMin / 60)}:${pad(offMin % 60)}`;
}
/** An hour offset from NOW, so fixtures land inside the band whenever this runs. */
function fromNow(hours: number): string {
  const d = new Date(Date.now() + hours * 3_600_000);
  return localIso(d.getFullYear(), d.getMonth() + 1, d.getDate(), d.getHours(), d.getMinutes());
}

function item(over: Partial<FeedItem> & { id: string }): FeedItem {
  return {
    kind: 'radar',
    instance: 'salem',
    title: `title ${over.id}`,
    mode: 'fyi',
    attention: 'fyi',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: fromNow(-1),
    acted_at: null,
    expires_at: null,
    starts_at: null,
    ends_at: null,
    source_ref: {},
    ...over,
  };
}

const timedFyi = item({ id: 'radar1', starts_at: fromNow(2) });
const untimed = item({ id: 'proposal1' });
const deckable = item({ id: 'mail1', kind: 'email_tier', mode: 'decide', attention: 'needs_you', starts_at: fromNow(4) });

beforeEach(() => {
  mockList.mockReset().mockResolvedValue({ items: [timedFyi, untimed, deckable], count: 3 });
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'acted' });
  mockReplace.mockReset();
  routerQuery.current = {};
});
afterEach(() => vi.restoreAllMocks());

const loaded = () => waitFor(() => expect(screen.queryByTestId('feed-view-switch')).not.toBeNull());

describe('feedView — the URL contract', () => {
  it('defaults to the list when the query says nothing or says nonsense', () => {
    expect(DEFAULT_FEED_VIEW).toBe('list');
    expect(feedViewFromQuery(undefined)).toBe('list');
    expect(feedViewFromQuery('bogus')).toBe('list');
    // A repeated param arrives as an ARRAY from Next's router.
    expect(feedViewFromQuery(['timeline', 'list'])).toBe('timeline');
    // …and the admissible neighbour, so the fallbacks above aren't just "always list".
    expect(feedViewFromQuery('timeline')).toBe('timeline');
  });

  it('only the non-default view puts a parameter in the URL', () => {
    expect(feedViewQuery('list')).toEqual({});
    expect(feedViewQuery('timeline')).toEqual({ view: 'timeline' });
    expect(isFeedView('timeline')).toBe(true);
    expect(isFeedView('deck')).toBe(false);
  });
});

describe('FeedPage — switching views', () => {
  it('opens on the list, with the timeline absent', async () => {
    render(<FeedPage />);
    await loaded();
    expect(screen.queryByTestId('feed-timeline')).toBeNull();
    expect(screen.getByTestId('feed-view-list').getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByTestId('feed-view-timeline').getAttribute('aria-pressed')).toBe('false');
  });

  it('opens ON the timeline when the URL asks for it (a linkable view)', async () => {
    routerQuery.current = { view: 'timeline' };
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-timeline')).not.toBeNull());
    expect(screen.getByTestId('feed-view-timeline').getAttribute('aria-pressed')).toBe('true');
    // The list's sections are gone — one view at a time, not both stacked.
    expect(screen.queryByTestId('feed-fyi')).toBeNull();
  });

  it('an unrecognised ?view= leaves the operator on their feed, not on nothing', async () => {
    routerQuery.current = { view: 'waterline' };
    render(<FeedPage />);
    await loaded();
    expect(screen.queryByTestId('feed-timeline')).toBeNull();
    expect(screen.getByTestId('feed-view-list').getAttribute('aria-pressed')).toBe('true');
  });

  it('clicking Timeline swaps the view AND records it in the URL', async () => {
    render(<FeedPage />);
    await loaded();
    fireEvent.click(screen.getByTestId('feed-view-timeline'));

    expect(screen.queryByTestId('feed-timeline')).not.toBeNull();
    expect(screen.queryByTestId('feed-fyi')).toBeNull();
    expect(mockReplace).toHaveBeenCalledWith(
      { pathname: '/feed', query: { view: 'timeline' } },
      undefined,
      { shallow: true },
    );
  });

  it('clicking List swaps back AND clears the parameter', async () => {
    routerQuery.current = { view: 'timeline' };
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-timeline')).not.toBeNull());
    fireEvent.click(screen.getByTestId('feed-view-list'));

    expect(screen.queryByTestId('feed-timeline')).toBeNull();
    expect(mockReplace).toHaveBeenCalledWith({ pathname: '/feed', query: {} }, undefined, { shallow: true });
  });
});

describe('FeedPage — the timeline is wired to the page, not just wire-able', () => {
  it('THREADS renderDetail: a trace expands the real FeedRow with its real verbs', async () => {
    // The pin that a component-level test cannot make. `renderDetail` is
    // optional on TimelineView; if the page stopped passing it, the band would
    // still render, the trace would still be clickable, and nothing would come
    // back — a silently dead feature. Clicking through from the page is what
    // catches that.
    routerQuery.current = { view: 'timeline' };
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-timeline')).not.toBeNull());

    const trace = screen.getAllByTestId('timeline-trace').find((t) => t.textContent?.includes('title radar1'))!;
    expect(trace).toBeTruthy();
    expect(screen.queryByTestId('timeline-detail')).toBeNull();

    fireEvent.click(trace);
    const detail = screen.getByTestId('timeline-detail');
    // A REAL FeedRow, not a placeholder…
    expect(detail.querySelector('[data-testid="feed-row"]')).not.toBeNull();
    // …carrying the verb this kind carries in the list view.
    expect(detail.querySelector('[data-testid="feed-row-ack"]')).not.toBeNull();
  });

  it('D1: a deck-able decision on the band gets NO judgment verbs, while the FYI row keeps its Ack', async () => {
    // THE D1 GUARD. `renderRow`'s fall-through deliberately hands a deck-able
    // decision a BARE FeedRow — glance and expand, nothing to act on — because
    // judgment belongs to the deck. Nothing pinned that until now: handing the
    // fall-through `completion`/`accept`/`snooze` (the timeline growing the full
    // verb set, exactly what D1 forbids) left the whole suite green.
    //
    // The load-bearing assertion is `feed-row-unavailable`. It is the branch
    // FeedRow renders when `completion` IS supplied and the item is not
    // done/suggested/completable — so it is ABSENT today and PRESENT the moment
    // the hooks leak in. Asserting only accept/complete/snooze would be nearly
    // vacuous here: those three are additionally gated on per-kind predicates
    // that an email_tier item fails anyway, so they would stay absent under the
    // very mutation this test exists to catch.
    routerQuery.current = { view: 'timeline' };
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-timeline')).not.toBeNull());

    // --- branch A: the deck-able decision, at awareness depth only ---
    const deckTrace = screen.getAllByTestId('timeline-trace').find((t) => t.textContent?.includes('title mail1'))!;
    expect(deckTrace).toBeTruthy();
    fireEvent.click(deckTrace);

    const deckDetail = screen.getByTestId('timeline-detail');
    // Positive control: a real row IS rendered. Without this, every absence
    // below would also hold for a detail panel that rendered nothing at all.
    expect(deckDetail.querySelector('[data-testid="feed-row"]')).not.toBeNull();
    for (const verb of [
      'feed-row-unavailable',
      'feed-row-accept',
      'feed-row-complete',
      'feed-row-undo',
      'feed-row-snooze',
    ]) {
      expect(deckDetail.querySelector(`[data-testid="${verb}"]`)).toBeNull();
    }

    // --- branch B: the FYI row, same render, verb PRESENT ---
    // Both branches in one test on purpose: this is what separates "the feed
    // withholds judgment verbs from deck-able items" from "the timeline renders
    // no verbs at all", which would be a different bug wearing the same green.
    const fyiTrace = screen.getAllByTestId('timeline-trace').find((t) => t.textContent?.includes('title radar1'))!;
    fireEvent.click(fyiTrace);
    expect(screen.getByTestId('timeline-detail').querySelector('[data-testid="feed-row-ack"]')).not.toBeNull();
  });

  it('the untimed register renders real rows too, not bare titles', async () => {
    routerQuery.current = { view: 'timeline' };
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-timeline')).not.toBeNull());

    const untimedRegister = screen.getByTestId('timeline-untimed');
    expect(untimedRegister.textContent).toContain('title proposal1');
    expect(untimedRegister.querySelector('[data-testid="feed-row"]')).not.toBeNull();
  });

  it('the deck link is reachable from BOTH views — judgment always has a route', async () => {
    render(<FeedPage />);
    await loaded();
    expect(screen.getByTestId('feed-deck-link').textContent).toContain('1 decision');

    fireEvent.click(screen.getByTestId('feed-view-timeline'));
    expect(screen.getByTestId('feed-deck-link').textContent).toContain('1 decision');
  });

  it('the page declares the sensor-log surface (the skin scope)', async () => {
    render(<FeedPage />);
    await loaded();
    expect(screen.getByTestId('feed-console').getAttribute('data-surface')).toBe('sensor-log');
  });
});
