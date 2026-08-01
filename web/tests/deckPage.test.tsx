import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// The deck deals ONLY isDeckDealt items — classic decisions + C2 SUGGESTED slots.
// A PLANNED slot (committed, non-candidate) is a worklist item, not a deck card, so
// it must never enter the stack; and the empty-state distinguishes "nothing to
// decide" from "open items are on the worklist, not the deck".

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
function slotItem(id: string, evidence: Record<string, unknown>): FeedItem {
  return { ...item('slot_suggestion', id), evidence };
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

  it('deals a SUGGESTED slot candidate as a card (C2) — enabled Accept + tier badge on the face', async () => {
    mockList.mockResolvedValue({
      items: [slotItem('s1', { tier: 1, origin: 'routine_item', routine_record: 'r/S.md', item_text: 'Meditate', name: 'Meditate', candidate: true })],
      count: 1,
    });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-card')).not.toBeNull());
    expect(screen.getByTestId('deck-card').getAttribute('data-kind')).toBe('slot_suggestion');
    expect(screen.getByTestId('deck-count').textContent).toBe('1 card');
    expect((screen.getByTestId('deck-btn-affirm') as HTMLButtonElement).disabled).toBe(false); // accept wired
    expect(screen.queryByTestId('deck-slot-tier')?.textContent).toContain('T1');
    expect(screen.queryByTestId('deck-unactionable')).toBeNull();
  });

  it('all-PLANNED (committed, non-dealt) → the worklist empty state, no deck', async () => {
    // Planned slots (evidence {} → no candidate) aren't deck cards; they're worklist
    // items on the Feed. Distinct from "done" and from "nothing to decide".
    mockList.mockResolvedValue({
      items: [slotItem('s1', { tier: 1 }), slotItem('s2', { tier: 2 })],
      count: 2,
    });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-unactionable')).not.toBeNull());
    expect(screen.getByTestId('deck-unactionable').textContent).toContain('2 items');
    expect(screen.getByTestId('deck-unactionable').textContent).toContain('worklist');
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
