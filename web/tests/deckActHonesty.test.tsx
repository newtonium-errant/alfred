import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, renderHook, screen } from '@testing-library/react';

// THE DECK NO LONGER SWALLOWS A REFUSED VERDICT (the 2026-08-15 P1).
//
// The incident: five email_tier cards swiped in one burst, every act refused
// 409 `stale_item` (`aged_out_of_last_batch` — a quota-dead classifier's stale
// batch), the deck advanced past all five, and the operator's five verdicts were
// recorded nowhere. He got at most ONE toast, which named no card, arrived while
// a different card was on screen, and never said his verdict had not stuck.
//
// These pins are written against the four compounding softeners that produced
// it, one describe each:
//   1. the POST is DEFERRED, so a failure lands while a later card is up;
//   2. the advance never consulted the POST — nothing could put the card back;
//   3. the LAST card's failure left by the unmount door, which discarded it;
//   4. a toast REPLACES, so five failures rendered at most one.
//
// The boundary pins matter as much as the failure pins: a card must come back on
// an ANSWER ("no") and must NOT come back on the ABSENCE of one (a timeout — the
// act may have committed, and a second verdict on a decision that stuck is the
// harm running the other way).

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { useDeck } from '../components/feed/useDeck';
import { Deck } from '../components/feed/Deck';
import { UNDO_MS } from '../lib/algernon/feedConstants';
import { ApiError } from '../lib/algernon/http';
import { DECK_UNRECORDED_KEY, readUnrecorded } from '../lib/algernon/deckUnrecorded';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
    id: 'email_tier:note/A.md',
    kind: 'email_tier',
    instance: 'salem',
    title: 'Email tier: a@b.com — Subject',
    mode: 'decide',
    attention: 'needs_you',
    evidence: { sender: 'a@b.com' },
    actions: [],
    state: 'open',
    created_at: '2026-07-30T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...overrides,
  });
}

/**
 * The 409 the operator actually hit, in the shape the wire actually produces.
 *
 * `code` is 'request_failed', NOT 'stale_item', and that is not a detail. The
 * transport answers a stale act with HTTP 409 and an ActResult BODY
 * (`{ok, status, detail, ...}` — routes_feed.py), which has no `error` key; so
 * `http.ts` parseOrThrow falls back to 'request_failed' and the machine `status`
 * is dropped on the throw path. A fixture that put 'stale_item' in `code` would
 * be pinning a shape the server never sends — and would hide that `e.status`
 * is the only term that fires. (The `code` spelling is pinned too, below.)
 */
function staleItem409(): ApiError {
  return new ApiError(409, 'request_failed', 'aged out of the last batch');
}

beforeEach(() => {
  mockAct.mockReset();
  mockAct.mockResolvedValue({ ok: true, status: 'acted' });
  window.localStorage.clear();
  vi.useFakeTimers();
});
afterEach(() => {
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
  window.localStorage.clear();
});

/** Let the deferred POST fire and its outcome settle. */
async function flushAct() {
  await act(async () => {
    vi.advanceTimersByTime(UNDO_MS);
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe('a refused verdict RETURNS the card (softeners 1 + 2)', () => {
  it('409 → the card is back in the deck, marked, and NOT toasted at', async () => {
    mockAct.mockRejectedValueOnce(staleItem409());
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));

    act(() => result.current.affirm());
    expect(result.current.current).toBeNull(); // advanced past the only card
    await flushAct();

    // The card came back...
    expect(result.current.current?.id).toBe('a');
    expect(result.current.cleared).toBe(false);
    expect(result.current.unrecordedIds.has('a')).toBe(true);
    // ...carrying the server's OWN words, and the operator's own verdict.
    expect(result.current.unrecorded).toHaveLength(1);
    expect(result.current.unrecorded[0].reason).toBe('aged out of the last batch');
    expect(result.current.unrecorded[0].verdict).toBe('affirm');
    expect(result.current.unrecorded[0].title).toBe('Email tier: a@b.com — Subject');
    // The toast is what failed him here. The notice speaks instead.
    expect(result.current.toast).toBeNull();
  });

  it('POSITIVE CONTROL — a verdict that LANDS advances and leaves no debt', async () => {
    // Without this the pin above passes identically against a deck that returns
    // every card it ever deals.
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));
    act(() => result.current.affirm());
    await flushAct();

    expect(mockAct).toHaveBeenCalledWith('a', 'confirm');
    expect(result.current.current).toBeNull();
    expect(result.current.cleared).toBe(true);
    expect(result.current.unrecorded).toHaveLength(0);
    expect(readUnrecorded()).toHaveLength(0);
  });

  it('the DEFERRAL survives — the POST still waits out the undo window', async () => {
    // The fix must not have bought honesty by deleting the undo window.
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' }), item({ id: 'b' })] }));
    act(() => result.current.affirm());
    expect(mockAct).not.toHaveBeenCalled();
    act(() => result.current.undo());
    act(() => vi.advanceTimersByTime(UNDO_MS));
    expect(mockAct).not.toHaveBeenCalled(); // cancelled, never an un-act
    expect(result.current.current?.id).toBe('a');
    expect(result.current.unrecorded).toHaveLength(0);
  });

  it('the returned card re-enters at the TAIL, never under the moving thumb', async () => {
    // The failure for card `a` lands while `b` is on screen. Materialising `a` at
    // the cursor would put a different card under a gesture already in motion —
    // the same class of harm as the swallow itself.
    mockAct.mockRejectedValueOnce(staleItem409());
    const items = [item({ id: 'a' }), item({ id: 'b' }), item({ id: 'c' })];
    const { result } = renderHook(() => useDeck({ items }));

    act(() => result.current.affirm()); // a → deferred
    act(() => result.current.affirm()); // b → flushes a's POST (which fails)
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(result.current.current?.id).toBe('c'); // the thumb's card is untouched
    expect(result.current.ahead.map((i) => i.id)).toEqual(['a']); // a waits behind it
    // NOTE: with ONE returned card, head and tail are the same position — this
    // pin proves the card does not land ON the cursor, not that returns are
    // ordered. The ORDERING property is pinned by the burst tests below, which
    // assert ['a','c','e'] in the sequence they were refused.
  });

  it('a refused SNOOZE un-persists the hide-list, or the return dies at reload', async () => {
    // deckSnooze is applied at the next LOAD. A returned card still on the
    // hide-list would be filtered out of the very batch handing it back.
    mockAct.mockRejectedValueOnce(staleItem409());
    const onSnoozePersist = vi.fn();
    const onUnsnoozePersist = vi.fn();
    const slot = item({ id: 's', kind: 'slot_suggestion', title: 'Slot: deep work' });
    const { result } = renderHook(() =>
      useDeck({ items: [slot], onSnoozePersist, onUnsnoozePersist }),
    );

    act(() => result.current.snooze());
    expect(onSnoozePersist).toHaveBeenCalledWith('s');
    await flushAct();

    expect(onUnsnoozePersist).toHaveBeenCalledWith('s');
    expect(result.current.snoozedCount).toBe(0); // not "snoozed" — it never was
    expect(result.current.current?.id).toBe('s');
  });
});

describe('what does NOT return a card — the boundary (#62)', () => {
  async function failWith(e: unknown) {
    mockAct.mockRejectedValueOnce(e);
    const onAuthExpired = vi.fn();
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })], onAuthExpired }));
    act(() => result.current.affirm());
    await flushAct();
    return { result, onAuthExpired };
  }

  it('a TIMEOUT does not return the card — the act may have landed', async () => {
    const { result } = await failWith(new ApiError(0, 'timeout', 'the request timed out'));
    expect(result.current.current).toBeNull(); // stays gone
    expect(result.current.unrecorded).toHaveLength(0);
    expect(readUnrecorded()).toHaveLength(0);
    expect(result.current.toast?.message).toContain('reconcile');
    expect(mockAct).toHaveBeenCalledTimes(1); // and never resent
  });

  it('a 401 does not return the card — it expires the session instead', async () => {
    const { result, onAuthExpired } = await failWith(new ApiError(401, 'invalid_session'));
    expect(onAuthExpired).toHaveBeenCalledTimes(1);
    expect(result.current.unrecorded).toHaveLength(0);
  });

  it('a 502 DOES return it, and banners — unreachable is still an answer', async () => {
    const { result } = await failWith(new ApiError(502, 'feed_upstream_unavailable'));
    expect(result.current.banner).toContain('server-side');
    expect(result.current.current?.id).toBe('a');
    expect(result.current.unrecorded).toHaveLength(1);
  });

  it('an ALREADY-DECIDED item is named, not returned (it would return forever)', async () => {
    mockAct.mockResolvedValueOnce({
      ok: true,
      status: 'already_acted',
      detail: 'already acted',
      id: 'a',
      action_id: 'confirm',
    });
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));
    act(() => result.current.affirm());
    await flushAct();

    expect(result.current.current).toBeNull(); // not returned
    expect(result.current.unrecorded).toHaveLength(0); // not a debt
    // But NOT silent, and NOT unattributable: the card is named.
    expect(result.current.toast?.message).toContain('Email tier: a@b.com — Subject');
    expect(result.current.toast?.message).toContain('confirmation');
  });

  it('a refusal that arrives on a 2xx returns it too (ok=false is the contract)', async () => {
    // Defensive today — the transport maps every ok=false status to a non-2xx —
    // and load-bearing if the server lane ever answers a refusal with a 200.
    mockAct.mockResolvedValueOnce({
      ok: false,
      status: 'stale_item',
      detail: 'aged out of the last batch',
      id: 'a',
      action_id: 'confirm',
    });
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));
    act(() => result.current.affirm());
    await flushAct();

    expect(result.current.current?.id).toBe('a');
    expect(result.current.unrecorded[0].reason).toBe('aged out of the last batch');
  });

  it('a 409 spelled in `code` rather than `status` is refused identically', async () => {
    // The BFF's own error shape (`{error: 'stale_item'}`) reaches the client as
    // code, not status. Both spellings mean the server answered no.
    mockAct.mockRejectedValueOnce(new ApiError(400, 'stale_item', 'that batch moved on'));
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));
    act(() => result.current.affirm());
    await flushAct();
    expect(result.current.current?.id).toBe('a');
    expect(result.current.unrecorded).toHaveLength(1);
  });
});

describe('a BURST accumulates — five refusals read as five (softener 4)', () => {
  it('three refused among five: exactly those three come back and are listed', async () => {
    // The mixed burst is the pin AND its own positive control: a deck that
    // returned everything, or nothing, fails it in opposite directions.
    const refused = new Set(['a', 'c', 'e']);
    mockAct.mockImplementation((id: string) =>
      refused.has(id)
        ? Promise.reject(staleItem409())
        : Promise.resolve({ ok: true, status: 'acted', id, action_id: 'confirm', detail: '' }),
    );
    const ids = ['a', 'b', 'c', 'd', 'e'];
    const { result } = renderHook(() => useDeck({ items: ids.map((id) => item({ id })) }));

    for (const _id of ids) {
      act(() => result.current.affirm()); // each commit flushes the previous POST
    }
    await flushAct(); // the last one leaves on its own timer

    expect(result.current.unrecorded.map((u) => u.id)).toEqual(['a', 'c', 'e']);
    expect(mockAct).toHaveBeenCalledTimes(5);
    // The three are dealable again, in the order they were refused; b and d are gone.
    expect([result.current.current, ...result.current.ahead].map((i) => i?.id)).toEqual([
      'a',
      'c',
      'e',
    ]);
    expect(result.current.remaining).toBe(3);
  });

  it('re-giving a returned verdict SETTLES that debt and leaves the others', async () => {
    const refused = new Set(['a', 'b']);
    mockAct.mockImplementation((id: string) =>
      refused.has(id)
        ? Promise.reject(staleItem409())
        : Promise.resolve({ ok: true, status: 'acted', id, action_id: 'confirm', detail: '' }),
    );
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' }), item({ id: 'b' })] }));
    act(() => result.current.affirm());
    act(() => result.current.affirm());
    await flushAct();
    expect(result.current.unrecorded.map((u) => u.id)).toEqual(['a', 'b']);

    refused.delete('a'); // the server recovers
    act(() => result.current.affirm()); // re-decide the returned `a`
    await flushAct();

    expect(result.current.unrecorded.map((u) => u.id)).toEqual(['b']);
    expect(readUnrecorded().map((u) => u.id)).toEqual(['b']); // the store agrees
    expect(result.current.unrecordedIds.has('a')).toBe(false);
  });

  it('acknowledging clears the list AND the marks, and the store with them', async () => {
    mockAct.mockRejectedValueOnce(staleItem409());
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));
    act(() => result.current.affirm());
    await flushAct();
    expect(result.current.unrecorded).toHaveLength(1);

    act(() => result.current.acknowledgeUnrecorded());
    expect(result.current.unrecorded).toHaveLength(0);
    expect(result.current.unrecordedIds.size).toBe(0);
    expect(readUnrecorded()).toHaveLength(0);
    expect(result.current.current?.id).toBe('a'); // the card stays in the deck
  });
});

describe('the LAST card leaves by the unmount door (softener 3)', () => {
  it('a failure with nowhere to land is written to the store, not discarded', async () => {
    mockAct.mockRejectedValueOnce(staleItem409());
    const { result, unmount } = renderHook(() => useDeck({ items: [item({ id: 'z' })] }));

    act(() => result.current.affirm()); // deferred; the deck is now clear
    unmount(); // …and the operator leaves /deck before the window expires

    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(mockAct).toHaveBeenCalledWith('z', 'confirm');
    const stored = readUnrecorded();
    expect(stored.map((u) => u.id)).toEqual(['z']);
    expect(stored[0].reason).toBe('aged out of the last batch');
    expect(stored[0].title).toBe('Email tier: a@b.com — Subject');
    // Written through the real key, so the next mount (and only that) finds it.
    expect(window.localStorage.getItem(DECK_UNRECORDED_KEY)).toContain('"id":"z"');
  });

  it('the NEXT mount re-presents it — the debt survives the deck', async () => {
    mockAct.mockRejectedValueOnce(staleItem409());
    const first = renderHook(() => useDeck({ items: [item({ id: 'z' })] }));
    act(() => first.result.current.affirm());
    first.unmount();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const second = renderHook(() => useDeck({ items: [item({ id: 'z' })] }));
    expect(second.result.current.unrecorded.map((u) => u.id)).toEqual(['z']);
    expect(second.result.current.unrecordedIds.has('z')).toBe(true);
    // Not a duplicate deal: the card is in the fresh batch already, at its own place.
    expect(second.result.current.remaining).toBe(1);
  });

  it('a CLEAN unmount leaves no debt behind (control for the pin above)', async () => {
    const { result, unmount } = renderHook(() => useDeck({ items: [item({ id: 'z' })] }));
    act(() => result.current.affirm());
    unmount();
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(mockAct).toHaveBeenCalledTimes(1); // it DID fire — the flush still works
    expect(readUnrecorded()).toHaveLength(0);
  });
});

describe('the DOM the operator actually sees', () => {
  it('names the card, says the verdict did not stick, and marks the card', async () => {
    mockAct.mockRejectedValueOnce(staleItem409());
    render(<Deck items={[item({ id: 'a', title: 'Email tier: payroll@rrts.ca — Invoice' })]} />);

    act(() => fireEvent.click(screen.getByTestId('deck-btn-affirm')));
    expect(screen.getByTestId('deck-cleared')).toBeTruthy(); // it advanced
    await flushAct();

    const notice = screen.getByTestId('deck-unrecorded');
    expect(notice.getAttribute('role')).toBe('alert');
    expect(notice.textContent).toContain('A verdict was not recorded.');
    expect(notice.textContent).toContain('Email tier: payroll@rrts.ca — Invoice');
    expect(notice.textContent).toContain('did not stick');
    expect(notice.textContent).toContain('aged out of the last batch');
    expect(notice.textContent).toContain("It's back in the deck.");
    // The card itself is back, and says so where the decision gets made.
    expect(screen.queryByTestId('deck-cleared')).toBeNull();
    expect(screen.getByTestId('deck-unrecorded-mark').textContent).toContain('Not recorded');
    // The typographic apostrophe is what the card renders (`&rsquo;`); asserting
    // the ASCII one would pass only against copy nobody ships.
    expect(screen.getByTestId('deck-unrecorded-note').textContent).toContain('wasn’t recorded');
    // No toast competing with it.
    expect(screen.queryByTestId('deck-toast')).toBeNull();
  });

  it('a burst renders one row per refused card, not one line total', async () => {
    mockAct.mockRejectedValue(staleItem409());
    const items = ['a', 'b', 'c'].map((id) => item({ id, title: `Card ${id}` }));
    render(<Deck items={items} />);

    for (let i = 0; i < 3; i += 1) {
      act(() => fireEvent.click(screen.getByTestId('deck-btn-affirm')));
    }
    await flushAct();

    const rows = screen.getAllByTestId('deck-unrecorded-row');
    expect(rows).toHaveLength(3);
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('Card a'),
      expect.stringContaining('Card b'),
      expect.stringContaining('Card c'),
    ]);
    expect(screen.getByTestId('deck-unrecorded').textContent).toContain('3 verdicts were not recorded.');
  });

  it('a card no longer in the batch is named and NOT promised back', async () => {
    // The ledger's whole reason for carrying a title: this is the operator's
    // only way to learn what was lost, and telling him to swipe a card that
    // isn't there would be a wrong steer dressed as a helpful one.
    window.localStorage.setItem(
      DECK_UNRECORDED_KEY,
      JSON.stringify([
        { id: 'gone', title: 'Email tier: old@b.com — Gone', verdict: 'reject', actionId: 'spam', reason: 'aged out of the last batch', at: 1 },
      ]),
    );
    render(<Deck items={[item({ id: 'still-here' })]} />);
    await act(async () => {
      await Promise.resolve();
    });

    const notice = screen.getByTestId('deck-unrecorded');
    expect(notice.textContent).toContain('Email tier: old@b.com — Gone');
    expect(notice.textContent).toContain('your rejection did not stick');
    expect(notice.textContent).toContain('It is not in this batch');
    expect(notice.textContent).not.toContain("It's back in the deck.");
  });

  it('Acknowledge removes the notice and the card mark in one tap', async () => {
    mockAct.mockRejectedValueOnce(staleItem409());
    render(<Deck items={[item({ id: 'a' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-affirm')));
    await flushAct();
    expect(screen.getByTestId('deck-unrecorded-mark')).toBeTruthy();

    act(() => fireEvent.click(screen.getByTestId('deck-unrecorded-ack')));

    expect(screen.queryByTestId('deck-unrecorded')).toBeNull();
    expect(screen.queryByTestId('deck-unrecorded-mark')).toBeNull();
    expect(screen.queryByTestId('deck-unrecorded-note')).toBeNull();
    expect(screen.getByTestId('deck-card')).toBeTruthy(); // the card is still dealt
  });

  it('nothing is drawn when nothing was refused (no notice on a healthy deck)', async () => {
    render(<Deck items={[item({ id: 'a' })]} />);
    act(() => fireEvent.click(screen.getByTestId('deck-btn-affirm')));
    await flushAct();
    expect(screen.queryByTestId('deck-unrecorded')).toBeNull();
    expect(screen.queryByTestId('deck-unrecorded-mark')).toBeNull();
  });
});

describe('the ledger degrades toward saying less', () => {
  it('unparseable storage reads as no debt, never as an invented one', () => {
    window.localStorage.setItem(DECK_UNRECORDED_KEY, '{not json');
    expect(readUnrecorded()).toEqual([]);
    const { result } = renderHook(() => useDeck({ items: [item({ id: 'a' })] }));
    expect(result.current.unrecorded).toHaveLength(0);
  });

  it('entries missing the fields the notice needs are dropped, not rendered blank', () => {
    window.localStorage.setItem(
      DECK_UNRECORDED_KEY,
      JSON.stringify([{ id: 'ok', title: 'T', actionId: 'confirm', verdict: 'affirm', reason: 'r', at: 1 }, { id: 42 }, null, 'x']),
    );
    expect(readUnrecorded().map((u) => u.id)).toEqual(['ok']);
  });
});
