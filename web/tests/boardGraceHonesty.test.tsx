import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';

// THE DAY BOARD NO LONGER SWALLOWS A REFUSED ✓.
//
// `useBoardGrace` was built as a knowing copy of the deck's deferred-act quartet
// — hold the write for the undo window, flush it on the timer, on the next tap,
// or from the unmount cleanup — and it copied everything EXCEPT the part that
// makes a deferred write honest. So the same incident the deck was fixed for on
// 2026-08-15 (five verdicts refused, recorded nowhere, never reported) was live
// on home: a refused completion at the unmount door answered into a dead
// closure, where no per-row error line exists and nothing was written down.
//
// The pins below are written against that class, one describe each:
//   1. the unmount door — a refusal with no component left to tell;
//   2. the next mount — the debt has to come back into view or it is not a debt;
//   3. the mounted path — a refusal must show in place, and five must read five;
//   4. the boundary — a debt is recorded on an ANSWER, never on the absence of
//      one (a timeout may well have committed; inventing a failure is the #62
//      overreach, and here it would send the operator to re-do work that stuck);
//   5. one key per surface — the deck's ledger and the board's are separate
//      debts, paid by whoever was shown them.

const { mockList, mockAct, modeState } = vi.hoisted(() => ({
  mockList: vi.fn(),
  mockAct: vi.fn(),
  modeState: { current: 'checkin' as 'brief' | 'checkin' | 'feed' },
}));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { list: mockList, act: mockAct } }));

import { SlotBoard } from '../components/feed/SlotBoard';
import { useRingCompletion } from '../components/feed/useRingCompletion';
import { UNDO_MS } from '../lib/algernon/feedConstants';
import { ApiError } from '../lib/algernon/http';
import { SESSION_EXPIRED_REASON } from '../lib/algernon/actConfirm';
import { BOARD_UNRECORDED_KEY, readBoardUnrecorded } from '../lib/algernon/boardUnrecorded';
import { DECK_UNRECORDED_KEY, readUnrecorded } from '../lib/algernon/deckUnrecorded';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

const NOW = new Date('2026-08-12T15:00:00Z'); // 12:00 Halifax
const TODAY_CREATED = '2026-08-12T13:00:00Z';

function slot(over: Partial<FeedItem> = {}, evidence: Record<string, unknown> = {}): FeedItem {
  return withServedActions({
    id: 'slot:x',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'A thing',
    mode: 'fyi',
    attention: 'fyi',
    evidence: {
      tier: 1,
      slot: 'duty',
      name: 'A thing',
      origin: 'routine_item',
      routine_record: 'routine/Bills.md',
      item_text: 'A thing',
      ...evidence,
    },
    actions: [],
    state: 'open',
    created_at: TODAY_CREATED,
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  });
}

/**
 * The 409 the operator actually hit, in the shape the wire actually produces.
 *
 * Copied deliberately from `deckActHonesty`'s fixture rather than simplified:
 * `code` is 'request_failed', NOT 'stale_item', because the transport answers a
 * stale act with HTTP 409 and an ActResult BODY (no `error` key), so `http.ts`
 * falls back to 'request_failed' and the machine status is dropped on the throw
 * path. A fixture that put 'stale_item' in `code` would pin a shape the server
 * never sends — and would hide that `detail` is what the notice quotes.
 */
function staleItem409(): ApiError {
  return new ApiError(409, 'request_failed', 'aged out of the last batch');
}

/**
 * The board as home actually mounts it.
 *
 * Two things here are the PAGE's job, not the board's, and both are load-bearing
 * for the durability pin below. The page owns the ONE completion instance, and
 * the page — not `SlotBoard` — runs `completion.reconcile(items)` on every feed
 * render (`pages/index.tsx`, the effect keyed on `items`). `SlotBoard` runs only
 * `grace.reconcile`. A harness that omitted the page's half would drive a poll
 * that supersedes nothing, and a "it survives the poll" pin standing on it would
 * be prose asserting a property its own fixture never exercises — the exact
 * shape this lane exists to stop shipping.
 */
function Harness({
  items,
  now = NOW,
  onAuthExpired,
}: {
  items: FeedItem[] | null;
  now?: Date;
  onAuthExpired?: () => void;
}) {
  const completion = useRingCompletion({ onAuthExpired });
  useEffect(() => {
    if (items) completion.reconcile(items);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);
  return <SlotBoard items={items} completion={completion} now={now} onAuthExpired={onAuthExpired} />;
}

/**
 * Let the act's promise chain finish.
 *
 * Eight microtask ticks rather than two because the verify path (#62) awaits a
 * list call and can retry it once; two ticks is enough for a straight refusal
 * and silently short of the verified ones, which would read as "the boundary
 * pin found no debt" when it had simply not looked yet.
 */
const settle = () =>
  act(async () => {
    for (let i = 0; i < 8; i += 1) await Promise.resolve();
  });

beforeEach(() => {
  mockList.mockReset().mockResolvedValue({ items: [], count: 0 });
  mockAct.mockReset().mockResolvedValue({ ok: true, status: 'acted' });
  modeState.current = 'checkin';
  window.localStorage.clear();
});
afterEach(() => {
  // Unmount FIRST, while the mocks are still alive: the board's unmount flush is
  // real behaviour and fires during teardown, and vitest runs this hook BEFORE
  // testing-library's auto-cleanup.
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe('the unmount door — a refusal with nowhere to land (the dead closure)', () => {
  it('a held ✓ refused after the board is gone is WRITTEN DOWN, not discarded', async () => {
    vi.useFakeTimers();
    mockAct.mockRejectedValueOnce(staleItem409());
    const { unmount } = render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);

    fireEvent.click(screen.getByTestId('board-complete'));
    expect(mockAct).not.toHaveBeenCalled(); // still held
    unmount(); // …the operator leaves home before the window expires
    await settle();

    expect(mockAct).toHaveBeenCalledWith('r', 'done');
    const stored = readBoardUnrecorded();
    expect(stored.map((u) => u.id)).toEqual(['r']);
    expect(stored[0].title).toBe('Pay Eastlink');
    expect(stored[0].reason).toBe('aged out of the last batch');
    expect(stored[0].actionId).toBe('done');
    // Through the real key, so the next mount (and only that) finds it.
    expect(window.localStorage.getItem(BOARD_UNRECORDED_KEY)).toContain('"id":"r"');
  });

  it('a CLEAN unmount leaves no debt behind (control for the pin above)', async () => {
    vi.useFakeTimers();
    const { unmount } = render(<Harness items={[slot({ id: 'r' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    unmount();
    await settle();

    expect(mockAct).toHaveBeenCalledTimes(1); // it DID fire — the flush still works
    expect(readBoardUnrecorded()).toHaveLength(0);
  });
});

describe('the next mount — the debt comes back into view', () => {
  it('names the row, says the ✓ didn’t stick, and marks the row itself', async () => {
    vi.useFakeTimers();
    mockAct.mockRejectedValueOnce(staleItem409());
    const first = render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    first.unmount();
    await settle();

    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);
    await settle();

    const notice = screen.getByTestId('board-unrecorded');
    expect(notice.getAttribute('role')).toBe('alert');
    expect(notice.textContent).toContain('A completion wasn’t recorded.');
    expect(notice.textContent).toContain('Pay Eastlink');
    expect(notice.textContent).toContain('didn’t stick');
    expect(notice.textContent).toContain('aged out of the last batch');
    expect(notice.textContent).toContain('It’s still on the board.');
    // …and the row says it too, where the next decision gets made. The
    // typographic apostrophe is what the row renders (`&rsquo;`); asserting the
    // ASCII one would pass only against copy nobody ships.
    expect(screen.getByTestId('board-item-unrecorded').textContent).toContain('wasn’t recorded');
  });

  it('a row no longer on the board is named and NOT promised back', async () => {
    // The ledger's whole reason for carrying a title: this is the operator's
    // only way to learn what was lost, and telling them to re-tap a row that
    // isn't there would be a wrong steer dressed as a helpful one.
    window.localStorage.setItem(
      BOARD_UNRECORDED_KEY,
      JSON.stringify([
        { id: 'gone', title: 'Refill the water jug', actionId: 'done', reason: 'aged out of the last batch', at: 1 },
      ]),
    );
    render(<Harness items={[slot({ id: 'still-here', title: 'Pay Eastlink' })]} />);
    await settle();

    const notice = screen.getByTestId('board-unrecorded');
    expect(notice.textContent).toContain('Refill the water jug');
    expect(notice.textContent).toContain('It’s not on the board now');
    expect(notice.textContent).not.toContain('It’s still on the board.');
    // No row carries the mark — the row is gone, and marking a different one
    // would be worse than saying nothing.
    expect(screen.queryByTestId('board-item-unrecorded')).toBeNull();
  });

  it('nothing is drawn when nothing was refused (a healthy board is silent)', async () => {
    vi.useFakeTimers();
    render(<Harness items={[slot({ id: 'r' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    expect(screen.queryByTestId('board-unrecorded')).toBeNull();
    expect(screen.queryByTestId('board-item-unrecorded')).toBeNull();
    expect(readBoardUnrecorded()).toHaveLength(0);
  });
});

describe('the mounted path — it shows in place, and five read as five', () => {
  it('a refusal in the window stops claiming done and says so on the row', async () => {
    vi.useFakeTimers();
    mockAct.mockRejectedValue(staleItem409());
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);

    fireEvent.click(screen.getByTestId('board-complete'));
    expect(screen.getByTestId('board-item').getAttribute('data-stage')).toBe('done'); // optimistic
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    // The row is no longer claiming a completion the server declined…
    expect(screen.getByTestId('board-item').getAttribute('data-stage')).not.toBe('done');
    // …it is back in its own stack, tappable again…
    expect(screen.getByTestId('board-today-duty').textContent).toContain('Pay Eastlink');
    // …and it says what happened, IN PLACE. This test asserts visibility at the
    // moment of refusal and nothing beyond it: no poll runs here, so it may not
    // be read as evidence of durability. That claim needs a render to survive,
    // and it is made — and driven — by the poll pin below.
    expect(screen.getByTestId('board-item-unrecorded')).toBeTruthy();
    expect(screen.getByTestId('board-unrecorded').textContent).toContain('Pay Eastlink');
  });

  it('the debt SURVIVES the next feed poll, where the transient error line does not', async () => {
    // THE DURABILITY PIN. The shared hook's `supersede` retires an override once
    // a render has answered the question it was standing in for — right for a
    // transient error line, fatal for a debt. That is exactly why the ledger is a
    // STORE with its own mirror rather than another override, and this drives the
    // distinction instead of asserting it in prose.
    //
    // THE CONTRAST NEEDS TWO ROWS, because on ONE row these two never coexist:
    // `SlotBoard` deliberately suppresses the error line on a ledgered row (one
    // statement of a failure, and the durable one wins). So the transient half is
    // shown on a row whose failure was NOT an answer — a plain throw, which is
    // 'unknown' and correctly ledgers nothing. That row is also the positive
    // control: it proves the poll genuinely supersedes, so "the debt survived"
    // means the ledger resisted a live supersession rather than that nothing
    // happened.
    //
    // It holds by construction today. The pin exists for the change that would
    // break it: any future move of the `unrecorded` mirror under supersede's
    // reach, or a reconcile that "tidies up" the ledger alongside the overrides.
    vi.useFakeTimers();
    mockAct
      .mockRejectedValueOnce(new Error('boom')) // row a — not an answer, not a debt
      .mockRejectedValueOnce(staleItem409()); // row b — an answer, a debt
    const items = () => [
      slot({ id: 'a', title: 'Card a' }, { slot: 'duty' }),
      slot({ id: 'b', title: 'Pay Eastlink' }, { slot: 'rhythm' }),
    ];
    const { rerender } = render(<Harness items={items()} />);

    for (const btn of screen.getAllByTestId('board-complete')) fireEvent.click(btn);
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    // At the moment of refusal: a transient line on a, a debt on b.
    expect(screen.getByTestId('board-item-error')).toBeTruthy();
    expect(readBoardUnrecorded().map((u) => u.id)).toEqual(['b']);
    expect(screen.getByTestId('board-unrecorded')).toBeTruthy();

    // The next feed render lands — a NEW array, which is what drives both
    // reconcile effects (the page's, over the completion overrides; the board's,
    // over its own optimistic flags). Both rows come back still open, because
    // they are: neither ✓ was recorded.
    await act(async () => {
      rerender(<Harness items={items()} />);
    });
    await settle();

    // The transient line is GONE — superseded, correctly, by server truth…
    expect(screen.queryByTestId('board-item-error')).toBeNull();
    // …and the debt is still here, named, with the server's own words.
    expect(screen.getByTestId('board-unrecorded').textContent).toContain('Pay Eastlink');
    expect(screen.getByTestId('board-unrecorded').textContent).toContain('aged out of the last batch');
    expect(screen.getByTestId('board-item-unrecorded')).toBeTruthy();
    expect(readBoardUnrecorded().map((u) => u.id)).toEqual(['b']);
  });

  it('a burst renders one notice row per refused ✓, not one line total', async () => {
    vi.useFakeTimers();
    mockAct.mockRejectedValue(staleItem409());
    render(
      <Harness
        items={[
          slot({ id: 'a', title: 'Card a' }, { slot: 'duty' }),
          slot({ id: 'b', title: 'Card b' }, { slot: 'rhythm' }),
          slot({ id: 'c', title: 'Card c' }, { slot: 'fuel' }),
        ]}
      />,
    );

    // Each tap flushes the one before it (flush-in-order); the timer flushes the last.
    for (const btn of screen.getAllByTestId('board-complete')) fireEvent.click(btn);
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    const rows = screen.getAllByTestId('board-unrecorded-row');
    expect(rows.map((r) => r.textContent)).toEqual([
      expect.stringContaining('Card a'),
      expect.stringContaining('Card b'),
      expect.stringContaining('Card c'),
    ]);
    expect(screen.getByTestId('board-unrecorded').textContent).toContain('3 completions weren’t recorded.');
    expect(screen.getAllByTestId('board-item-unrecorded')).toHaveLength(3);
  });

  it('Acknowledge clears the notice and the row marks in one tap', async () => {
    vi.useFakeTimers();
    mockAct.mockRejectedValue(staleItem409());
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();
    expect(screen.getByTestId('board-item-unrecorded')).toBeTruthy();

    fireEvent.click(screen.getByTestId('board-unrecorded-ack'));

    expect(screen.queryByTestId('board-unrecorded')).toBeNull();
    expect(screen.queryByTestId('board-item-unrecorded')).toBeNull();
    expect(readBoardUnrecorded()).toHaveLength(0);
    // The row stays on the board either way — acknowledging is reading, not deciding.
    expect(screen.getByTestId('board-today-duty').textContent).toContain('Pay Eastlink');
  });

  it('a refusal that arrives on a 2xx does not paint the row green', async () => {
    // The defensive half of the ok/status contract. Without it the status map
    // reads an unknown status as the optimistic value and the row goes DONE over
    // a write the server declined — the silent-advance class, arriving by the
    // one door that looks like success.
    vi.useFakeTimers();
    mockAct.mockResolvedValue({ ok: false, status: 'error', detail: 'the writer refused it' });
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);

    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    expect(screen.getByTestId('board-item').getAttribute('data-stage')).not.toBe('done');
    expect(readBoardUnrecorded().map((u) => u.reason)).toEqual(['the writer refused it']);
    expect(screen.getByTestId('board-unrecorded').textContent).toContain('the writer refused it');
  });
});

describe('the boundary — a debt is an ANSWER, never the absence of one', () => {
  it('an unresolvable timeout is NOT ledgered, while its refused neighbour IS', async () => {
    // The positive control is the point of the pairing: "no entry" is worth
    // nothing unless the same run proves an entry CAN be written. One board, two
    // rows, two failures — only one of them is an answer.
    vi.useFakeTimers();
    mockList.mockRejectedValue(new Error('offline')); // the verify cannot resolve either
    mockAct
      .mockRejectedValueOnce(new ApiError(504, 'timeout')) // row a — no answer
      .mockRejectedValueOnce(staleItem409()); // row b — an answer
    render(
      <Harness
        items={[slot({ id: 'a', title: 'Card a' }, { slot: 'duty' }), slot({ id: 'b', title: 'Card b' }, { slot: 'rhythm' })]}
      />,
    );

    for (const btn of screen.getAllByTestId('board-complete')) fireEvent.click(btn);
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    expect(readBoardUnrecorded().map((u) => u.id)).toEqual(['b']);
    expect(screen.getByTestId('board-unrecorded').textContent).toContain('Card b');
    expect(screen.getByTestId('board-unrecorded').textContent).not.toContain('Card a');
  });

  it('a timeout the verify shows LANDED is not a debt (it worked)', async () => {
    vi.useFakeTimers();
    mockAct.mockRejectedValue(new ApiError(504, 'timeout'));
    // The verify finds the item done server-side: the act committed, the reply
    // was lost. Ledgering here would send the operator to re-do work that stuck.
    mockList.mockResolvedValue({
      items: [slot({ id: 'r', title: 'Pay Eastlink', state: 'acted', acted_at: '2026-08-12T14:00:00Z' }, { done: true })],
      count: 1,
    });
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);

    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    expect(readBoardUnrecorded()).toHaveLength(0);
    expect(screen.queryByTestId('board-unrecorded')).toBeNull();
  });

  it('a timeout the verify shows NOT LANDED is a debt, in the words of the observation', async () => {
    vi.useFakeTimers();
    mockAct.mockRejectedValue(new ApiError(504, 'timeout'));
    // Still open server-side — that IS an answer, arrived the long way round.
    mockList.mockResolvedValue({ items: [slot({ id: 'r', title: 'Pay Eastlink' })], count: 1 });
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);

    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    const stored = readBoardUnrecorded();
    expect(stored.map((u) => u.id)).toEqual(['r']);
    // No server words to quote, so the row states what was OBSERVED rather than
    // inventing a server voice for it.
    expect(stored[0].reason).toBe('the server still shows it open');
  });

  it('a 401 on the held write is a debt, and says the session expired', async () => {
    // THE DIVERGENCE FROM THE DECK, PINNED. The deck does not hand a CARD back
    // on auth — there is no deck left to hand it to — and this lane read that as
    // a rule about card affordance rather than about whether the debt exists. A
    // 401 IS an answer: the request was rejected before it reached the store, so
    // the ✓ definitely did not land.
    //
    // It is also the case where the operator is guaranteed to be interrupted
    // (they are being logged out mid-tap) and least likely to remember what they
    // had just marked — so it is the debt with the strongest claim on being
    // written down, not the weakest.
    vi.useFakeTimers();
    const onAuthExpired = vi.fn();
    mockAct.mockRejectedValue(new ApiError(401, 'invalid_session'));
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} onAuthExpired={onAuthExpired} />);

    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    // The existing 401 routing is untouched — the session still expires.
    expect(onAuthExpired).toHaveBeenCalledTimes(1);
    // EXACTLY ONE debt, and it says the true thing rather than 'invalid_session'.
    const stored = readBoardUnrecorded();
    expect(stored).toHaveLength(1);
    expect(stored[0].id).toBe('r');
    expect(stored[0].reason).toBe(SESSION_EXPIRED_REASON);
    expect(screen.getByTestId('board-unrecorded').textContent).toContain('your session expired before it was sent');
  });

  it('re-marking the row after re-login settles the 401 debt', async () => {
    // The other half: a debt the operator can never discharge is a nag, not a
    // ledger. The re-login is a fresh mount — which is also the only path by
    // which this particular debt can ever be seen, since the 401 unmounts the
    // page it was incurred on.
    vi.useFakeTimers();
    mockAct.mockRejectedValueOnce(new ApiError(401, 'invalid_session'));
    const first = render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} onAuthExpired={vi.fn()} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();
    expect(readBoardUnrecorded()).toHaveLength(1);
    first.unmount();

    // …re-login: a fresh board, and the debt is waiting on it.
    mockAct.mockResolvedValue({ ok: true, status: 'acted' });
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);
    await settle();
    expect(screen.getByTestId('board-unrecorded').textContent).toContain('Pay Eastlink');

    // Mark it again; this time it lands.
    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    expect(readBoardUnrecorded()).toHaveLength(0);
    expect(screen.queryByTestId('board-unrecorded')).toBeNull();
    expect(screen.queryByTestId('board-item-unrecorded')).toBeNull();
  });

  it('re-marking a row settles the debt it was carrying', async () => {
    window.localStorage.setItem(
      BOARD_UNRECORDED_KEY,
      JSON.stringify([{ id: 'r', title: 'Pay Eastlink', actionId: 'done', reason: 'aged out of the last batch', at: 1 }]),
    );
    vi.useFakeTimers();
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);
    await settle();
    expect(screen.getByTestId('board-unrecorded')).toBeTruthy();

    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    expect(readBoardUnrecorded()).toHaveLength(0);
    expect(screen.queryByTestId('board-unrecorded')).toBeNull();
  });
});

describe('one key per surface — the deck’s debts are not the board’s', () => {
  it('a board refusal writes the board key and leaves the deck’s ledger alone', async () => {
    const deckEntry = [
      { id: 'deck-1', title: 'Email tier: a@b.com — Subject', verdict: 'affirm', actionId: 'confirm', reason: 'aged out of the last batch', at: 1 },
    ];
    window.localStorage.setItem(DECK_UNRECORDED_KEY, JSON.stringify(deckEntry));

    vi.useFakeTimers();
    mockAct.mockRejectedValue(staleItem409());
    render(<Harness items={[slot({ id: 'r', title: 'Pay Eastlink' })]} />);
    fireEvent.click(screen.getByTestId('board-complete'));
    await act(async () => { vi.advanceTimersByTime(UNDO_MS); });
    await settle();

    expect(readBoardUnrecorded().map((u) => u.id)).toEqual(['r']);
    expect(readUnrecorded().map((u) => u.id)).toEqual(['deck-1']);

    // …and the board's ONE control settles the board's debt only. A shared key
    // would let a tap here clear a debt the operator has never been shown.
    fireEvent.click(screen.getByTestId('board-unrecorded-ack'));
    expect(readBoardUnrecorded()).toHaveLength(0);
    expect(readUnrecorded().map((u) => u.id)).toEqual(['deck-1']);
    expect(window.localStorage.getItem(DECK_UNRECORDED_KEY)).toBe(JSON.stringify(deckEntry));
  });

  it('the keys are the literal strings production browsers already hold', () => {
    // The lift moved the MECHANISM into `unrecordedLedger`; it must not have
    // moved the ADDRESS. There are live deck entries in the operator's browser
    // written by the pre-lift code, and a renamed key would orphan exactly the
    // debts the ledger exists to keep — silently, since an orphaned key reads as
    // "no debt", the direction this whole module degrades toward.
    //
    // NOTHING ELSE PINS THIS. Every other test on either surface reaches its key
    // through the constant, so a rename carries them all with it and the suite
    // stays green. The literal has to be written down once, here.
    expect(DECK_UNRECORDED_KEY).toBe('algernon_deck_unrecorded');
    expect(BOARD_UNRECORDED_KEY).toBe('algernon_board_unrecorded');
  });

  it('a deck entry written BEFORE the lift still reads back whole', () => {
    // The shared reader validates the base fields and knows nothing of
    // `verdict`. If the lift had made it project onto the fields it knows —
    // the natural way to write a generic filter — every stored deck entry would
    // come back verdict-less and the deck's notice would say "your verdict did
    // not stick" about a verdict it could no longer name.
    window.localStorage.setItem(
      DECK_UNRECORDED_KEY,
      JSON.stringify([
        { id: 'z', title: 'Email tier: a@b.com — Subject', verdict: 'reject', actionId: 'spam', reason: 'aged out of the last batch', at: 1 },
      ]),
    );
    const [entry] = readUnrecorded();
    expect(entry.verdict).toBe('reject');
    expect(entry.actionId).toBe('spam');
    expect(entry.reason).toBe('aged out of the last batch');
  });

  it('unparseable board storage reads as no debt, never as an invented one', async () => {
    window.localStorage.setItem(BOARD_UNRECORDED_KEY, '{not json');
    expect(readBoardUnrecorded()).toEqual([]);
    render(<Harness items={[slot({ id: 'r' })]} />);
    await settle();
    expect(screen.queryByTestId('board-unrecorded')).toBeNull();
  });

  it('entries missing the fields the notice needs are dropped, not rendered blank', () => {
    window.localStorage.setItem(
      BOARD_UNRECORDED_KEY,
      JSON.stringify([{ id: 'ok', title: 'T', actionId: 'done', reason: 'r', at: 1 }, { id: 42 }, null, 'x']),
    );
    expect(readBoardUnrecorded().map((u) => u.id)).toEqual(['ok']);
  });
});
