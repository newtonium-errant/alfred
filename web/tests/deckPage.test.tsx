import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// First-contact fix: the deck deals ONLY kinds with a wired verb (DECK_VERBS).
// A decide-mode kind whose acts aren't built yet (slot_suggestion) must never
// enter the stack as a card dead to every gesture; and the empty-state must
// distinguish "nothing to decide" from "open decisions are not-yet-actionable".

const { mockList } = vi.hoisted(() => ({ mockList: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: vi.fn() } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {} }) }));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));

import DeckPage from '../pages/deck';
import type { FeedItem } from '../lib/algernon/feed';

function item(kind: string, id: string): FeedItem {
  return {
    id,
    kind,
    instance: 'salem',
    title: `${kind} ${id}`,
    mode: 'decide',
    attention: 'needs_you',
    evidence: {},
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
  try {
    window.sessionStorage.clear();
  } catch {
    /* ignore */
  }
});
afterEach(() => vi.restoreAllMocks());

describe('DeckPage — deals only actionable kinds', () => {
  it('deals the email card but NOT the slot cards (mixed fixture)', async () => {
    mockList.mockResolvedValue({
      items: [item('email_tier', 'e1'), item('slot_suggestion', 's1'), item('slot_suggestion', 's2')],
      count: 3,
    });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-count')).not.toBeNull());
    // Exactly one card dealt (the email), and it's an email card.
    expect(screen.getByTestId('deck-count').textContent).toBe('1 card');
    expect(screen.getByTestId('deck-card').getAttribute('data-kind')).toBe('email_tier');
    // No slot card was dealt to the stack.
    expect(document.querySelector('[data-kind="slot_suggestion"]')).toBeNull();
    expect(screen.queryByTestId('deck-unactionable')).toBeNull();
  });

  it('all-unactionable → the NOT-YET-actionable empty state (distinct from done), no deck', async () => {
    mockList.mockResolvedValue({
      items: [item('slot_suggestion', 's1'), item('slot_suggestion', 's2')],
      count: 2,
    });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-unactionable')).not.toBeNull());
    expect(screen.getByTestId('deck-unactionable').textContent).toContain('2 items');
    expect(screen.getByTestId('deck-unactionable').textContent).toContain('still being built');
    expect(screen.queryByTestId('deck-card')).toBeNull();
    expect(screen.queryByTestId('deck-empty')).toBeNull();
  });

  it('genuinely empty → the plain "nothing to decide" state (distinct from not-yet-actionable)', async () => {
    mockList.mockResolvedValue({ items: [], count: 0 });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-empty')).not.toBeNull());
    expect(screen.queryByTestId('deck-unactionable')).toBeNull();
    expect(screen.queryByTestId('deck-card')).toBeNull();
  });
});
