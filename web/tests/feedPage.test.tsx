import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// First-contact fix: needs-you decide items with NO wired verb (slot_suggestion)
// get an honest row + "acts arrive with the board" note (never a phantom deck
// count, never a dead control). Only deck-able decisions feed the deck-link count.

const { mockList } = vi.hoisted(() => ({ mockList: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: vi.fn() } }));
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {} }) }));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));

import FeedPage from '../pages/feed';
import type { FeedItem } from '../lib/algernon/feed';

function item(kind: string, id: string, attention: string, mode: string): FeedItem {
  return {
    id,
    kind,
    instance: 'salem',
    title: `${kind} ${id}`,
    mode,
    attention,
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-07-31T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  };
}

beforeEach(() => mockList.mockReset());
afterEach(() => vi.restoreAllMocks());

describe('FeedPage — honest affordance for unactionable decide items', () => {
  it('routes deck-able decisions to the deck link + renders slot rows with a hint (no Ack)', async () => {
    mockList.mockResolvedValue({
      items: [
        item('email_tier', 'e1', 'needs_you', 'decide'), // deck-able → counted in the link
        item('slot_suggestion', 's1', 'needs_you', 'decide'), // no verb → pending row + hint
        item('radar', 'f1', 'fyi', 'fyi'), // FYI → Ack row
      ],
      count: 3,
    });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-needs-you')).not.toBeNull());

    // Deck link counts ONLY the deck-able decision (email), not the slot.
    expect(screen.getByTestId('feed-deck-link').textContent).toContain('1 decision');

    // The slot is a pending row with the honest note + NO Ack.
    const pending = screen.getByTestId('feed-pending');
    expect(pending.textContent).toContain('acts arrive with the board');
    expect(screen.getByTestId('feed-row-hint').textContent).toContain('acts arrive with the board');

    // Exactly one Ack button exists — the FYI row's, never the slot's.
    expect(screen.getAllByTestId('feed-row-ack')).toHaveLength(1);
    expect(screen.getByTestId('feed-fyi')).toBeTruthy();
  });

  it('all needs-you unactionable → pending rows only, no deck link', async () => {
    mockList.mockResolvedValue({
      items: [item('slot_suggestion', 's1', 'needs_you', 'decide')],
      count: 1,
    });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-pending')).not.toBeNull());
    expect(screen.queryByTestId('feed-deck-link')).toBeNull();
    expect(screen.queryByTestId('feed-row-ack')).toBeNull();
  });
});
