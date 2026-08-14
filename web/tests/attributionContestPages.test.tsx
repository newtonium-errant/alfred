import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

// #63a — the contest door THROUGH THE REAL PAGES.
//
// Separate from attributionContest.test.tsx on purpose. That file pins the hook
// and the row in isolation, and every one of those pins stays green if the pages
// never pass `onContest` — an optional prop that gates a feature is exactly the
// shape that gets tested by direct invocation and then forgotten at the call
// site, leaving the feature accepted and dead in the field. These render the
// actual pages against a mocked feed and assert the button is really there and
// really POSTs.
//
// Both surfaces render FYI rows (the feed page's awareness section and the home
// composer's feed-first list), so both are pinned; wiring one and not the other
// is the likely half-miss.

const { mockList, mockAct, modeState } = vi.hoisted(() => ({
  mockList: vi.fn(),
  mockAct: vi.fn(),
  modeState: { current: 'feed' as 'brief' | 'checkin' | 'feed' },
}));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {}, events: { on: vi.fn(), off: vi.fn() } }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));
vi.mock('../lib/algernon/composer', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../lib/algernon/composer')>()),
  composeMode: () => modeState.current,
  halifaxHour: () => 12,
  composeModeForDate: () => modeState.current,
}));

// C4 router (folded here with the console-completion lane, which owns this
// merge surface): `HomePage` runs `useContactRouter` on mount, so without this
// mock every test in this file makes a real `/api/day/state` fetch. It is
// caught by the router's own fail-safe and harmless, but an unmocked network
// call in a unit test is a latent flake and a lie about what the test exercises.
// `configured: false` is the router's stay-put state — the page renders exactly
// as it did before C4 existed.
vi.mock('../lib/algernon/dayClient', () => ({
  dayApi: {
    state: vi.fn().mockResolvedValue({ configured: false, armed_rules: [], rule_order: [] }),
    contact: vi.fn().mockResolvedValue({ contact_id: '', recorded: false }),
    override: vi.fn().mockResolvedValue({ recorded: false, patterns_surfaced: 0 }),
  },
}));
vi.mock('../lib/algernon/composerLog', () => ({ useComposerLog: () => {} }));

import FeedPage from '../pages/feed';
import HomePage from '../pages/index';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

const ATTRIBUTION_ID = 'attribution:note/A.md|inf-1';

function attributionItem(over: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
    id: ATTRIBUTION_ID,
    kind: 'attribution',
    instance: 'salem',
    title: 'Attribution: note/A.md',
    mode: 'fyi',
    attention: 'fyi',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-08-10T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  });
}

beforeEach(() => {
  modeState.current = 'feed';
  mockList.mockReset();
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'contested' });
});
afterEach(() => vi.restoreAllMocks());

describe('FeedPage — the contest door is actually wired', () => {
  it('renders it on the FYI attribution row and POSTs "contest" on tap', async () => {
    mockList.mockResolvedValue({ items: [attributionItem()], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-contest')).not.toBeNull());

    fireEvent.click(screen.getByTestId('feed-row-contest'));

    expect(mockAct).toHaveBeenCalledWith(ATTRIBUTION_ID, 'contest', undefined, undefined);
  });

  it('leaves other FYI kinds alone — an Ack, and no contest door', async () => {
    mockList.mockResolvedValue({
      items: [attributionItem({ id: 'radar:r1', kind: 'radar', title: 'Radar r1' })],
      count: 1,
    });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-ack')).not.toBeNull());
    expect(screen.queryByTestId('feed-row-contest')).toBeNull();
  });

  it('a contested row moves out of awareness and into the deck-able count —\n     the operator sees it come back, not vanish', async () => {
    mockList.mockResolvedValue({ items: [attributionItem()], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-contest')).not.toBeNull());

    fireEvent.click(screen.getByTestId('feed-row-contest'));

    await waitFor(() => expect(screen.queryByTestId('feed-row-contest')).toBeNull());
    // It left the FYI section rather than being acked away: the deck link (which
    // counts deck-able needs-you items) now advertises it.
    expect(screen.queryByTestId('feed-deck-link')).not.toBeNull();
  });
});

describe('HomePage composer — the contest door is actually wired', () => {
  it('renders it on the FYI attribution row and POSTs "contest" on tap', async () => {
    mockList.mockResolvedValue({ items: [attributionItem()], count: 1 });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-contest')).not.toBeNull());

    fireEvent.click(screen.getByTestId('feed-row-contest'));

    expect(mockAct).toHaveBeenCalledWith(ATTRIBUTION_ID, 'contest', undefined, undefined);
  });
});
