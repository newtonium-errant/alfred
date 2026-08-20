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
import { withServedActions } from './helpers/servedActions';

function item(kind: string, id: string): FeedItem {
  return withServedActions({
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
  });
}
function slotItem(id: string, evidence: Record<string, unknown>): FeedItem {
  // `actions: []` resets the base item's verbs so they are re-derived from THIS
  // evidence — a slot's accept is stage-dependent, and the base was built with
  // no evidence at all.
  return withServedActions({ ...item('slot_suggestion', id), evidence, actions: [] });
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

  it('deals a QUIET suggestion card — fyi/fyi reaches the deck without ringing', async () => {
    // THE PAGE-LAYER WALL THIS LANE MEASURED AND CLOSED: the fetch was
    // mode=decide, so an fyi/fyi rotation card was produced, served,
    // verb-carrying — and rendered nowhere a gesture exists. `isDeckCandidate`
    // is the fix; this drives it through the page. The fyi WEATHER row in the
    // same payload is the positive control for the other direction: quiet
    // glance rows still never deal (the operator's demotion rulings hold).
    function sortCard(id: string): FeedItem {
      return withServedActions({
        ...item('sort_suggestion', id),
        mode: 'fyi',
        attention: 'fyi',
        evidence: { proposed_slot: 'duty', proposal_shape: 'task|due:n|t2' },
        actions: [],
      });
    }
    const fyiWeather = { ...item('weather', 'w1'), mode: 'fyi', attention: 'fyi', actions: [] };
    mockList.mockResolvedValue({ items: [sortCard('r1'), fyiWeather], count: 2 });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-card')).not.toBeNull());
    expect(screen.getByTestId('deck-count').textContent).toBe('1 card');
    expect(screen.getByTestId('deck-card').getAttribute('data-kind')).toBe('sort_suggestion');
    expect(document.querySelector('[data-kind="weather"]')).toBeNull();
  });

  it('a degraded sort card (no proposal) is not a candidate — no deal, no fault claim', async () => {
    // Its affirm gesture never arrives (the proposal is the gesture), so it is
    // neither dealable nor a candidate: the page reads genuinely empty rather
    // than inventing a worklist or a fault for a card the feed still shows.
    const degraded = withServedActions({
      ...item('sort_suggestion', 'r2'),
      mode: 'fyi',
      attention: 'fyi',
      evidence: {},
      actions: [],
    });
    mockList.mockResolvedValue({ items: [degraded], count: 1 });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-empty')).not.toBeNull());
    expect(screen.queryByTestId('deck-card')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// The DEGRADED payload — an empty deck that must not read as a tidy one
// ---------------------------------------------------------------------------
// Since the deck derives its verbs from the wire, an item can arrive with NO
// actions at all: a half-deployed box, the serve side's actions_unavailable /
// actions_stamp_failed degradation, or a decide kind the ceiling has no entry
// for. Such an item fails `isDeckDealt` exactly as a committed slot does — and
// before this split it was counted into the same sentence, so the operator was
// told they had a worklist. They did not: the Feed cannot action a verbless
// item either, so that line sent them somewhere to hit the same wall.
//
// ILB in its sharpest form: the absence was legible, just legible as the WRONG
// thing. A wrong explanation is worse than a bare count, because it forecloses
// the question.

/** An item stripped of its served verbs — the wire shape of a degraded payload. */
function verbless(kind: string, id: string): FeedItem {
  return { ...item(kind, id), actions: [] };
}

describe('DeckPage — a verbless payload is a FAULT, not an empty queue', () => {
  beforeEach(() => {
    mockList.mockReset();
    try {
      window.sessionStorage.clear();
    } catch {
      /* jsdom without storage */
    }
  });

  it('says the controls did not arrive — and does NOT claim a worklist', async () => {
    mockList.mockResolvedValue({ items: [verbless('email_tier', 'e1')], count: 1 });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-verbless')).not.toBeNull());
    const text = screen.getByTestId('deck-verbless').textContent ?? '';
    expect(text).toContain('without controls');
    // The three things the copy must NOT do: claim a worklist, claim the
    // queue is empty, or imply anything was decided or lost.
    expect(screen.queryByTestId('deck-unactionable')).toBeNull();
    expect(screen.queryByTestId('deck-empty')).toBeNull();
    expect(text).not.toContain('worklist');
    expect(text).toContain('nothing has been decided or lost');
  });

  it('POSITIVE CONTROL — a real worklist item still gets the worklist line', async () => {
    // The pin above is only meaningful if the OTHER branch still fires for the
    // population it was written for. A committed slot carries a full verb list
    // (done / undo_done / the snoozes) — it is simply not a SWIPE card, and
    // sending the operator to the Feed for it is correct advice.
    mockList.mockResolvedValue({ items: [slotItem('s1', { tier: 1 })], count: 1 });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-unactionable')).not.toBeNull());
    expect(screen.getByTestId('deck-unactionable').textContent).toContain('worklist');
    expect(screen.queryByTestId('deck-verbless')).toBeNull();
  });

  it('a MIXED population reports the fault, not the worklist', async () => {
    // Both branches are eligible. The fault wins: one of the two sentences says
    // something is wrong, and that is the half worth the operator's attention —
    // a worklist line here would bury it under a routine-sounding count.
    mockList.mockResolvedValue({
      items: [verbless('email_tier', 'e1'), slotItem('s1', { tier: 1 })],
      count: 2,
    });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-verbless')).not.toBeNull());
    expect(screen.getByTestId('deck-verbless').textContent).toContain('1 decision');
    expect(screen.queryByTestId('deck-unactionable')).toBeNull();
  });

  it('counts only the verbless ones, and still deals the cards that DID arrive', async () => {
    // Partial degradation: the dealt cards are unaffected, so no empty state
    // renders at all. Pinned so the split cannot start swallowing live cards —
    // and noted as the known gap: the verbless items are invisible while a deck
    // is live. That needs a note beside a running deck, not an empty state.
    mockList.mockResolvedValue({
      items: [item('email_tier', 'e1'), verbless('proposal', 'p1')],
      count: 2,
    });
    render(<DeckPage />);
    await waitFor(() => expect(screen.queryByTestId('deck-card')).not.toBeNull());
    expect(screen.getByTestId('deck-count').textContent).toBe('1 card');
    expect(screen.queryByTestId('deck-verbless')).toBeNull();
    expect(screen.queryByTestId('deck-empty')).toBeNull();
  });
});
