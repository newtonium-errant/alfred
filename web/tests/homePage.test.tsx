import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// Composer deck-pill honesty: the deck PROMISE counts only DECK-ABLE kinds (a
// wired verb) — never the full needs-you total (mirrors feed.tsx / b1). The
// check-in "N things need you" line stays the feed total (true, and distinct
// from the deck subset).

const { mockList, modeState } = vi.hoisted(() => ({
  mockList: vi.fn(),
  modeState: { current: 'checkin' as 'brief' | 'checkin' | 'feed' },
}));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: vi.fn() } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {}, events: { on: vi.fn(), off: vi.fn() } }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));
// Composition mode is controllable per test (default check-in).
vi.mock('../lib/algernon/composer', () => ({
  composeMode: () => modeState.current,
  halifaxHour: () => 12,
  composeModeForDate: () => modeState.current,
}));
// No-op the telemetry hook (avoids a real fetch on mount).
vi.mock('../lib/algernon/composerLog', () => ({ useComposerLog: () => {} }));

import HomePage from '../pages/index';
import type { FeedItem } from '../lib/algernon/feed';

function item(kind: string, id: string, attention: string, mode: string): FeedItem {
  return {
    id,
    kind,
    instance: 'salem',
    title: `${kind} ${id}`,
    mode,
    attention,
    evidence: kind === 'slot_suggestion' ? { tier: 1 } : {},
    actions: [],
    state: 'open',
    created_at: '2026-07-31T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  };
}

beforeEach(() => {
  mockList.mockReset();
  modeState.current = 'checkin';
});
afterEach(() => vi.restoreAllMocks());

describe('HomePage composer — the deck pill counts only deck-able kinds', () => {
  it('mixed needs-you: pill = deck-able (1), needs-you line = feed total (2)', async () => {
    mockList.mockResolvedValue({
      items: [
        item('email_tier', 'e1', 'needs_you', 'decide'), // deck-able (wired verb)
        item('slot_suggestion', 's1', 'needs_you', 'decide'), // NOT deck-able (a ring)
      ],
      count: 2,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-deck-pill')).not.toBeNull());

    const pill = screen.getByTestId('composer-deck-pill').textContent ?? '';
    expect(pill).toContain('1 decision');
    expect(pill).not.toContain('2 decision'); // never over-promises the deck

    // The needs-you line is the FEED total — true, and distinct from the deck.
    expect(screen.getByTestId('composer-needs-you').textContent).toContain('2 things need you');
  });

  it('all needs-you non-deck-able → NO deck pill (empty deck never promised)', async () => {
    mockList.mockResolvedValue({
      items: [item('slot_suggestion', 's1', 'needs_you', 'decide')],
      count: 1,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-needs-you')).not.toBeNull());

    expect(screen.queryByTestId('composer-deck-pill')).toBeNull();
    expect(screen.getByTestId('composer-needs-you').textContent).toContain('1 thing needs you');
  });

  it('a DONE slot item is excluded from the needs-you count (Phase C)', async () => {
    const doneSlot: FeedItem = { ...item('slot_suggestion', 's1', 'needs_you', 'decide'), state: 'acted' };
    mockList.mockResolvedValue({
      items: [item('email_tier', 'e1', 'needs_you', 'decide'), doneSlot],
      count: 2,
    });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('composer-needs-you')).not.toBeNull());
    // Two needs-you items, but the done slot no longer needs you → count is 1.
    expect(screen.getByTestId('composer-needs-you').textContent).toContain('1 thing needs you');
  });
});

describe('HomePage composer — the rings are PERSISTENT across every mode', () => {
  for (const mode of ['brief', 'checkin', 'feed'] as const) {
    it(`renders the rings header in ${mode} mode (completion surface always exists)`, async () => {
      modeState.current = mode;
      mockList.mockResolvedValue({
        items: [item('slot_suggestion', 's1', 'needs_you', 'decide')],
        count: 1,
      });
      render(<HomePage />);
      await waitFor(() => expect(screen.queryByTestId(`compose-${mode}`)).not.toBeNull());
      expect(screen.queryByTestId('rings-header')).not.toBeNull();
    });
  }

  it('does NOT double-fetch — RingsHeader shares the composer feed load (controlled)', async () => {
    modeState.current = 'feed';
    mockList.mockResolvedValue({ items: [], count: 0 });
    render(<HomePage />);
    await waitFor(() => expect(screen.queryByTestId('compose-feed')).not.toBeNull());
    // The composer's single list({state:'open'}) — RingsHeader is controlled, so no
    // extra list({kind:'slot_suggestion'}) fetch.
    expect(mockList).toHaveBeenCalledTimes(1);
    expect(mockList).toHaveBeenCalledWith({ state: 'open' });
  });
});
