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
import { withServedActions } from './helpers/servedActions';
import { DECK_SNOOZE_KEY, writeDeckSnoozed } from '../lib/algernon/deckSnooze';

function item(kind: string, id: string, attention: string, mode: string, evidence: Record<string, unknown> = {}): FeedItem {
  return withServedActions({
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
  });
}
const routineSlot = (id: string, over: Partial<FeedItem> = {}): FeedItem => ({
  ...item('slot_suggestion', id, 'needs_you', 'decide', { tier: 1, routine_record: 'routine/Bills.md', item_text: 'Pay' }),
  ...over,
});

beforeEach(() => {
  mockList.mockReset();
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'acted' });
  // The deck's hide-list is sessionStorage — real state that survives a test.
  // Clearing it here keeps one test's snooze out of the next test's banner.
  window.sessionStorage.clear();
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

    // #87 — WAIT FOR THE CONDITION, don't budget microtasks for it.
    //
    // This previously used a fixed `flush()` (act + exactly two `await
    // Promise.resolve()` ticks) and was 1-of-3 flaky in the full vitest run
    // while 16/16 green alone. Two ticks is a FIXED BUDGET for a settle whose
    // real cost varies: the completion path is `feedApi.act(...).then(setState)`
    // with a `.catch` attached after it, so the state lands some microtask hops
    // later and React then schedules the re-render. Under a loaded run those
    // hops can outlast the budget. `waitFor` retries until the DOM actually
    // says what we claim, which is the same assertion without the guess — NOT
    // a widened deadline, because nothing here waits on a clock.
    await waitFor(() => {
      // The lag this pins: pre-fix the filter read the RAW stage, so the
      // completed row stayed under "Needs you" (and "All clear" stayed
      // suppressed) until the next poll.
      expect(screen.queryByTestId('feed-pending')).toBeNull();
      expect(screen.queryByTestId('feed-needs-you')).toBeNull();
      expect(screen.queryByTestId('feed-empty')).not.toBeNull();
    });

    // Same render pass — nothing re-fetched the feed to produce that state.
    // OUTSIDE the waitFor deliberately: this is the invariant that keeps the
    // test meaningful. If the clear only ever came from a refetch, waitFor
    // would happily retry until that refetch landed — and this assertion is
    // what refuses it, because a refetch makes the count 2.
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

// --- #14: the labelled Snooze verb on a worklist row -------------------------
// The ruling: Skip and Snooze each carry their own visible word, and a snoozed
// row STAGES rather than vanishing — un-snooze lives on it. These drive the real
// page, so they fail if the hook is wired to the wrong rows or not at all.

describe('FeedPage — the row Snooze verb (#14)', () => {
  it('a slot row offers a LABELLED Snooze opening the four durations', async () => {
    mockList.mockResolvedValue({ items: [routineSlot('s1')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-snooze')).not.toBeNull());

    const button = screen.getByTestId('feed-row-snooze');
    expect(button.textContent).toBe('Snooze'); // the word, not an icon
    act(() => fireEvent.click(button));

    const menu = screen.getByTestId('feed-row-snooze-menu');
    expect(menu.textContent).toContain('1 day');
    expect(menu.textContent).toContain('3 days');
    expect(menu.textContent).toContain('7 days');
    expect(menu.textContent).toContain('Until I say');
    expect(mockAct).not.toHaveBeenCalled(); // opening a menu decides nothing
  });

  it('picking a duration POSTs that rung and STAGES the row behind Show snoozed', async () => {
    mockList.mockResolvedValue({ items: [routineSlot('s1')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-snooze')).not.toBeNull());
    act(() => fireEvent.click(screen.getByTestId('feed-row-snooze')));
    act(() => fireEvent.click(screen.getByTestId('feed-row-snooze-snooze_3d')));

    await waitFor(() => expect(mockAct).toHaveBeenCalledWith('s1', 'snooze_3d'));
    // Staged, not vanished — and collapsed by default (parity with done).
    await waitFor(() => expect(screen.queryByTestId('feed-show-snoozed')).not.toBeNull());
    expect(screen.queryByTestId('feed-snoozed-rows')).toBeNull();
    act(() => fireEvent.click(screen.getByTestId('feed-show-snoozed')));
    expect(screen.getAllByTestId('feed-snoozed-row')).toHaveLength(1);
  });

  it('Unsnooze on the staged row POSTs unsnooze and returns it to the worklist', async () => {
    // The escape hatch. The indefinite rung has no clock to bring a row back, so
    // if this door doesn't work the operator has no way out but a config file.
    mockList.mockResolvedValue({ items: [routineSlot('s1')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-snooze')).not.toBeNull());
    act(() => fireEvent.click(screen.getByTestId('feed-row-snooze')));
    act(() => fireEvent.click(screen.getByTestId('feed-row-snooze-snooze_until_i_say')));
    await waitFor(() => expect(mockAct).toHaveBeenCalledWith('s1', 'snooze_until_i_say'));

    act(() => fireEvent.click(screen.getByTestId('feed-show-snoozed')));
    act(() => fireEvent.click(screen.getByTestId('feed-row-unsnooze')));
    await waitFor(() => expect(mockAct).toHaveBeenCalledWith('s1', 'unsnooze'));
    await waitFor(() => expect(screen.queryByTestId('feed-row-snooze')).not.toBeNull());
    expect(screen.queryByTestId('feed-snoozed')).toBeNull();
  });

  it('a REFUSED snooze keeps the row and shows the server’s own words', async () => {
    // Success is `acted`, never a 200. The router refuses a snooze on a done row
    // and on an instance with no tier.snooze.path — flipping the row on status
    // alone would tell the operator "snoozed" about a row that wasn't.
    //
    // Mutation: gate the flip on `res.ok` alone → the row stages anyway and this
    // fails on both the surviving-button and the error-text asserts.
    mockList.mockResolvedValue({ items: [routineSlot('s1')], count: 1 });
    mockAct.mockResolvedValue({
      ok: false,
      status: 'error',
      detail: "board snooze isn't configured on this instance",
    });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-snooze')).not.toBeNull());
    act(() => fireEvent.click(screen.getByTestId('feed-row-snooze')));
    act(() => fireEvent.click(screen.getByTestId('feed-row-snooze-snooze_1d')));

    await waitFor(() => expect(screen.queryByTestId('feed-row-snooze-error')).not.toBeNull());
    expect(screen.getByTestId('feed-row-snooze-error').textContent).toContain("isn't configured");
    expect(screen.queryByTestId('feed-snoozed')).toBeNull(); // never staged
    expect(screen.queryByTestId('feed-row-snooze')).not.toBeNull(); // retry from here
  });

  it('a FYI row gets no Snooze control — there is no store behind that kind', async () => {
    mockList.mockResolvedValue({ items: [item('radar', 'f1', 'fyi', 'fyi')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-ack')).not.toBeNull());
    expect(screen.queryByTestId('feed-row-snooze')).toBeNull();
  });
});

describe('FeedPage — an ok-but-not-acted snooze does not stage the row (#14)', () => {
  it('already_acted (ok: true) leaves the row where it is', async () => {
    // The shape that makes `res.ok` alone the wrong gate: the router answers
    // ok=true / already_acted for an idempotent noop, and staging on that would
    // hide a row on the strength of a change that didn't happen.
    //
    // Mutation: gate the flip on `res.ok` instead of `res.ok && status ===
    // 'acted'` → the row stages and this fails.
    mockList.mockResolvedValue({ items: [routineSlot('s1')], count: 1 });
    mockAct.mockResolvedValue({ ok: true, status: 'already_acted', detail: 'wasn’t snoozed (no change)' });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-row-snooze')).not.toBeNull());
    act(() => fireEvent.click(screen.getByTestId('feed-row-snooze')));
    act(() => fireEvent.click(screen.getByTestId('feed-row-snooze-snooze_1d')));

    await waitFor(() => expect(screen.queryByTestId('feed-row-snooze-error')).not.toBeNull());
    expect(screen.queryByTestId('feed-snoozed')).toBeNull();
    expect(screen.queryByTestId('feed-row-snooze')).not.toBeNull();
  });
});

// ── The banner counts what the DECK WILL ACTUALLY DEAL ───────────────────────
//
// The operator's 2026-08-12 screenshots: the feed promised "2 decisions waiting
// → Open the deck", and the deck answered "DECK CLEAR — 2 snoozed". The banner
// was counting cards the deck had already set aside, because a deck snooze lives
// in `sessionStorage` and this page had no way to see it. Both directions are
// pinned — a count that never fires and a count that always fires would each
// pass half of this on their own.
describe('FeedPage — the deck banner promises no trip to a wall', () => {
  const deckable = (id: string) => item('email_tier', id, 'needs_you', 'decide');

  it('a population the deck WILL deal is counted (positive control)', async () => {
    mockList.mockResolvedValue({ items: [deckable('e1'), deckable('e2')], count: 2 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-deck-link')).not.toBeNull());
    expect(screen.getByTestId('feed-deck-link').textContent).toContain('2 decisions waiting');
    expect(screen.queryByTestId('feed-deck-set-aside')).toBeNull();
  });

  it('cards the DECK snoozed are not "waiting" — and the zero case says why', async () => {
    // Seeded exactly as the deck writes it, through the shared owner both pages
    // now read. Before that owner existed this page could not have known.
    writeDeckSnoozed(new Set(['e1', 'e2']));
    mockList.mockResolvedValue({ items: [deckable('e1'), deckable('e2')], count: 2 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-deck-set-aside')).not.toBeNull());

    // No promise of a trip to a clear deck…
    expect(screen.queryByTestId('feed-deck-link')).toBeNull();
    // …and not silence either: the page says where they went.
    expect(screen.getByTestId('feed-deck-set-aside').textContent).toContain('2 set aside');
  });

  it('counts only the dealable remainder when the deck holds SOME of them', async () => {
    writeDeckSnoozed(new Set(['e1']));
    mockList.mockResolvedValue({ items: [deckable('e1'), deckable('e2')], count: 2 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-deck-link')).not.toBeNull());
    expect(screen.getByTestId('feed-deck-link').textContent).toContain('1 decision waiting');
    // The set-aside line is for the ZERO case only — one waiting card is not a
    // moment to also narrate what isn't.
    expect(screen.queryByTestId('feed-deck-set-aside')).toBeNull();
  });

  it('an unreadable hide-list hides NOTHING rather than everything', async () => {
    // The safe direction: a corrupt store must not empty the banner, because an
    // empty deck link with no explanation is the failure this whole fix is about.
    window.sessionStorage.setItem(DECK_SNOOZE_KEY, '{not json');
    mockList.mockResolvedValue({ items: [deckable('e1')], count: 1 });
    render(<FeedPage />);
    await waitFor(() => expect(screen.queryByTestId('feed-deck-link')).not.toBeNull());
    expect(screen.getByTestId('feed-deck-link').textContent).toContain('1 decision waiting');
  });
});
