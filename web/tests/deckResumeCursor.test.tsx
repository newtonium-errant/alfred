import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

// THE CURSOR ACROSS A RESUME REFETCH.
//
// Wiring `useResumeRefetch` into /deck (the #62 family's third member) made the
// deck's items array able to change mid-session for the first time. The deck is
// the only member that walks an INDEX into that array — feed and home render
// lists — so the wiring alone regressed: decide three of five, background the
// app, resume, and the server correctly serves only the two still open while the
// index sits at 3. The deck then reported "Deck clear." over two undecided cards.
//
// That was MEASURED before the fix existed, and these are its pins. They come in
// a pair on purpose: the first fails if the cursor is left stranded, the second
// fails if someone "fixes" the first by re-dealing on EVERY refetch — which
// would throw the operator's progress away every time they glanced at another
// app. One without the other is half a specification.

const { mockList, mockAct } = vi.hoisted(() => ({ mockList: vi.fn(), mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {}, events: { on: vi.fn(), off: vi.fn() } }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));
vi.mock('../lib/algernon/usePush', () => ({
  usePush: () => ({ status: 'unsupported', busy: false, enable: vi.fn(), disable: vi.fn() }),
}));

import DeckPage from '../pages/deck';
import { withServedActions } from './helpers/servedActions';
import { readUnrecorded } from '../lib/algernon/deckUnrecorded';
import { ApiError } from '../lib/algernon/http';
import type { FeedItem } from '../lib/algernon/feed';

function item(id: string): FeedItem {
  return withServedActions({
    id,
    kind: 'email_tier',
    instance: 'salem',
    title: `Card ${id}`,
    mode: 'decide',
    attention: 'needs_you',
    evidence: { sender: 'a@b.com' },
    actions: [],
    state: 'open',
    created_at: '2026-07-30T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  });
}

beforeEach(() => {
  mockList.mockReset();
  mockAct.mockReset();
  mockAct.mockResolvedValue({ ok: true, status: 'acted' });
  window.localStorage.clear();
  window.sessionStorage.clear();
  Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
});
afterEach(() => {
  cleanup();
  window.localStorage.clear();
  window.sessionStorage.clear();
});

async function decide(times: number) {
  for (let i = 0; i < times; i += 1) {
    await act(async () => {
      fireEvent.click(screen.getByTestId('deck-btn-affirm'));
      await Promise.resolve();
    });
  }
}

async function resume() {
  await act(async () => {
    document.dispatchEvent(new Event('visibilitychange'));
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('a resume must not strand the cursor', () => {
  it('cards still open after a resume are still DEALT, not declared clear', async () => {
    const all = ['a', 'b', 'c', 'd', 'e'].map(item);
    mockList.mockResolvedValue({ items: all, count: 5 });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryAllByTestId('deck-card').length).toBeGreaterThan(0));

    await decide(3);
    expect(screen.getByTestId('deck-count').textContent).toBe('2 cards');

    // The server now serves only what is still open — the three landed.
    mockList.mockResolvedValue({ items: [item('d'), item('e')], count: 2 });
    await resume();

    // The regression this pin exists for printed "Clear" and zero cards here.
    expect(screen.queryByTestId('deck-cleared')).toBeNull();
    expect(screen.getByTestId('deck-count').textContent).toBe('2 cards');
    const titles = screen.queryAllByTestId('deck-card').map((n) => n.textContent ?? '');
    expect(titles.some((t) => t.includes('Card d'))).toBe(true);
  });

  it('a resume that changes NOTHING leaves the operator where they were', async () => {
    // The other half. Re-dealing on every refetch would pass the pin above and
    // silently restart the deck each time the operator glanced at another app.
    const all = ['a', 'b', 'c', 'd', 'e'].map(item);
    mockList.mockResolvedValue({ items: all, count: 5 });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryAllByTestId('deck-card').length).toBeGreaterThan(0));

    await decide(3);
    expect(screen.getByTestId('deck-count').textContent).toBe('2 cards');

    // Same five cards come back (the acts have not landed yet, or nothing changed).
    mockList.mockResolvedValue({ items: all, count: 5 });
    await resume();

    expect(screen.getByTestId('deck-count').textContent).toBe('2 cards'); // NOT '5 cards'
    const titles = screen.queryAllByTestId('deck-card').map((n) => n.textContent ?? '');
    expect(titles.some((t) => t.includes('Card d'))).toBe(true);
    expect(titles.some((t) => t.includes('Card a'))).toBe(false); // not re-dealt
  });

  it('an unrecorded verdict survives the re-deal — the ledger is not session state', async () => {
    // Why the ledger went to storage rather than into the hook's state. A re-deal
    // discards everything in memory; the debt must not be one of those things.
    mockList.mockResolvedValue({ items: [item('a'), item('b')], count: 2 });
    mockAct.mockRejectedValue(new ApiError(409, 'request_failed', 'aged out of the last batch'));
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryAllByTestId('deck-card').length).toBeGreaterThan(0));

    // TWO decisions, because the act is DEFERRED: card 'a's POST fires when the
    // next commit lands (flush-in-order) or when the 6s undo window expires, and
    // this test runs on real timers. Deciding 'b' is what sends 'a'.
    await decide(2);
    await act(async () => { await Promise.resolve(); });
    await waitFor(() => expect(screen.queryByTestId('deck-unrecorded')).not.toBeNull());
    expect(readUnrecorded().map((u) => u.id)).toContain('a');

    // A resume that re-deals (the id set changed — 'b' is gone from the server).
    mockList.mockResolvedValue({ items: [item('a')], count: 1 });
    await resume();

    // The notice and the card's mark are BOTH still there, rebuilt from storage.
    expect(screen.getByTestId('deck-unrecorded').textContent).toContain('Card a');
    expect(screen.getByTestId('deck-unrecorded-mark')).toBeTruthy();
  });
});
