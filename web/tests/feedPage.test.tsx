import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

// Surface parity: the feed's slot rows carry the SAME per-lane completion control
// as the rings panel — completable lanes (task / routine / free-text T3) get a live
// ✓, an unknown-origin slot gets the honest "Completion isn't available for this
// item" note (the stale "acts arrive with the board" line is gone). Deck-able
// decisions still feed the deck-link count.

const { mockList, mockAct } = vi.hoisted(() => ({ mockList: vi.fn(), mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {} }) }));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));

import FeedPage from '../pages/feed';
import type { FeedItem } from '../lib/algernon/feed';

function item(kind: string, id: string, attention: string, mode: string, evidence: Record<string, unknown> = {}): FeedItem {
  return {
    id,
    kind,
    instance: 'salem',
    title: `${kind} ${id}`,
    mode,
    attention,
    evidence,
    actions: [],
    state: 'open',
    created_at: '2026-07-31T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  };
}
const routineSlot = (id: string, over: Partial<FeedItem> = {}): FeedItem => ({
  ...item('slot_suggestion', id, 'needs_you', 'decide', { tier: 1, routine_record: 'routine/Bills.md', item_text: 'Pay' }),
  ...over,
});

beforeEach(() => {
  mockList.mockReset();
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'acted' });
});
afterEach(() => vi.restoreAllMocks());

describe('FeedPage — slot rows get the live per-lane completion control', () => {
  it('deck-able → deck link; a NON-completable slot → honest note (no board line, no Ack)', async () => {
    mockList.mockResolvedValue({
      items: [
        item('email_tier', 'e1', 'needs_you', 'decide'), // deck-able → the deck link
        item('slot_suggestion', 's1', 'needs_you', 'decide'), // no lane → honest note
        item('radar', 'f1', 'fyi', 'fyi'), // FYI → Ack
      ],
      count: 3,
    });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-needs-you')).not.toBeNull());

    expect(screen.getByTestId('feed-deck-link').textContent).toContain('1 decision');

    const pending = screen.getByTestId('feed-pending');
    expect(pending.textContent).toContain("Completion isn't available for this item"); // honest note
    expect(pending.textContent).not.toContain('arrive with the board'); // stale line GONE
    expect(screen.queryByTestId('feed-row-unavailable')).not.toBeNull();
    expect(screen.queryByTestId('feed-row-hint')).toBeNull(); // hint prop removed

    // Exactly one Ack — the FYI row's, never the slot's.
    expect(screen.getAllByTestId('feed-row-ack')).toHaveLength(1);
  });

  it('a COMPLETABLE slot (routine lane) renders a LIVE ✓', async () => {
    mockList.mockResolvedValue({ items: [routineSlot('r1')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-pending')).not.toBeNull());
    expect(screen.queryByTestId('feed-row-complete')).not.toBeNull(); // real ✓, not a note
    expect(screen.queryByTestId('feed-row-unavailable')).toBeNull();
  });

  it('a DONE slot (state acted) drops out of needs-you (isDone counting)', async () => {
    mockList.mockResolvedValue({ items: [routineSlot('r1', { state: 'acted' })], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-empty')).not.toBeNull());
    // Done slot → not in needs-you at all → the all-clear empty state.
    expect(screen.queryByTestId('feed-needs-you')).toBeNull();
    expect(screen.queryByTestId('feed-pending')).toBeNull();
  });
});

describe('FeedPage — needs-you reads the SAME truth as the row ✓ (no one-fetch lag)', () => {
  const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

  it('completing the last slot clears needs-you in the SAME render — no refetch needed', async () => {
    mockList.mockResolvedValue({ items: [routineSlot('r1')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-pending')).not.toBeNull());
    expect(screen.queryByTestId('feed-empty')).toBeNull();

    await act(async () => {
      fireEvent.click(screen.getByTestId('feed-row-complete'));
    });
    await flush();

    // The lag this pins: pre-fix the filter read the RAW stage, so the completed row
    // stayed under "Needs you" (and "All clear" stayed suppressed) until the next poll.
    expect(screen.queryByTestId('feed-pending')).toBeNull();
    expect(screen.queryByTestId('feed-needs-you')).toBeNull();
    expect(screen.queryByTestId('feed-empty')).not.toBeNull();
    // Same render pass — nothing re-fetched the feed to produce that state.
    expect(mockList).toHaveBeenCalledTimes(1);
  });

  it('a FAILED completion keeps the row in needs-you (the drop-out tracks success, not the tap)', async () => {
    mockAct.mockReset().mockRejectedValue(new Error('boom'));
    mockList.mockResolvedValue({ items: [routineSlot('r1')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-pending')).not.toBeNull());

    await act(async () => {
      fireEvent.click(screen.getByTestId('feed-row-complete'));
    });
    await flush();

    expect(screen.queryByTestId('feed-pending')).not.toBeNull(); // reverted → still needs you
    expect(screen.queryByTestId('feed-empty')).toBeNull();
    expect(screen.getByTestId('feed-row-completion-error').textContent).toBe('That action failed.');
  });
});

// --- #26 (D4): done STAGES on the feed worklist, it never vanishes ------------
// The feed dropped a completed slot out of the list entirely — right for the
// COUNTS (it no longer needs you, and must not inflate the deck) and wrong for
// the LIST, since a mis-tap was then unrecoverable here while home's rings kept
// Undo behind a drill. One rule everywhere.

describe('FeedPage — a completed slot stages behind Show done', () => {
  const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

  async function completeTheOnlySlot() {
    mockList.mockResolvedValue({ items: [routineSlot('r1')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-pending')).not.toBeNull());
    await act(async () => {
      fireEvent.click(screen.getByTestId('feed-row-complete'));
    });
    await flush();
  }

  it('the completed row leaves the worklist but STAYS in the DOM, behind the drill', async () => {
    await completeTheOnlySlot();

    // Out of the remaining-work list...
    expect(screen.queryByTestId('feed-pending')).toBeNull();
    // ...but reachable, not gone. Pre-fix there was no affordance at all here.
    const drill = screen.getByTestId('feed-show-done');
    expect(drill.textContent).toContain('Show done (1)');
    // Same render — nothing re-fetched to produce the staged state.
    expect(mockList).toHaveBeenCalledTimes(1);
  });

  it('the drill toggles the staged list open and closed', async () => {
    await completeTheOnlySlot();

    // Collapsed by default — the worklist still reads as remaining work.
    expect(screen.queryByTestId('feed-done')).toBeNull();
    expect(screen.getByTestId('feed-show-done').getAttribute('aria-expanded')).toBe('false');

    await act(async () => {
      fireEvent.click(screen.getByTestId('feed-show-done'));
    });
    expect(screen.queryByTestId('feed-done')).not.toBeNull();
    expect(screen.getByTestId('feed-show-done').textContent).toContain('Hide done');
    expect(screen.getByTestId('feed-show-done').getAttribute('aria-expanded')).toBe('true');

    await act(async () => {
      fireEvent.click(screen.getByTestId('feed-show-done'));
    });
    expect(screen.queryByTestId('feed-done')).toBeNull();
    expect(screen.getByTestId('feed-show-done').textContent).toContain('Show done (1)');
  });

  it('the staged row still carries its row controls, so Undo is one tap in', async () => {
    await completeTheOnlySlot();
    await act(async () => {
      fireEvent.click(screen.getByTestId('feed-show-done'));
    });

    // The point of staging rather than removing: the row is still a live
    // FeedRow, and a DONE row's control is UNDO — which is the whole reason a
    // mis-tap has to stay reachable. (A done row renders `feed-row-done` +
    // `feed-row-undo`, not the ✓ it was completed with.)
    const staged = screen.getByTestId('feed-done');
    expect(staged.querySelector('[data-testid="feed-row-undo"]')).not.toBeNull();
    expect(staged.querySelector('[data-testid="feed-row-done"]')).not.toBeNull();
  });

  it('no drill when nothing is done — the affordance is not permanent furniture', async () => {
    mockList.mockResolvedValue({ items: [routineSlot('r1')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-pending')).not.toBeNull());

    expect(screen.queryByTestId('feed-show-done')).toBeNull();
    expect(screen.queryByTestId('feed-done')).toBeNull();
  });

  it('a FAILED completion does NOT stage the row — it stays remaining work', async () => {
    mockAct.mockReset().mockRejectedValue(new Error('boom'));
    mockList.mockResolvedValue({ items: [routineSlot('r1')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-pending')).not.toBeNull());
    await act(async () => {
      fireEvent.click(screen.getByTestId('feed-row-complete'));
    });
    await flush();

    // Staging tracks SUCCESS, not the tap — same rule the drop-out already used.
    expect(screen.queryByTestId('feed-show-done')).toBeNull();
    expect(screen.queryByTestId('feed-pending')).not.toBeNull();
  });
});
