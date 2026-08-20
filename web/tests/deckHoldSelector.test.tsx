import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

// The AFFIRM-HOLD machine (affirm-with-hold-modifier, operator-ratified
// 2026-08-19) — the state built on the band geometry that holdPrimitive.test.ts
// pins. Technique inherited from deckHoldBand.test.tsx (this family's prior
// art): real pointer events against the real Deck + DeckCard, fake timers for
// the hold clock.
//
// The load-bearing pins:
//   * a plain → swipe commits the SUGGESTED verb (the ruling's first clause);
//   * holding the partial → opens the selector; CHOOSING IS THE AFFIRM — one
//     interaction, same delayed act, never choose-then-confirm (second clause);
//   * a ← swipe is "not now" — the quick defer, with copy that does not claim
//     a judgment (third clause);
//   * none of it exists on a card without a served choice group.

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { Deck } from '../components/feed/Deck';
import {
  GESTURE_HOLD_MS,
  STAMP_FADE_START,
  SWIPE_X_THRESHOLD,
  UNDO_MS,
} from '../lib/algernon/feedConstants';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

function sortCard(over: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
    id: 'sort_suggestion:task:task/Fix shed door.md',
    kind: 'sort_suggestion',
    instance: 'salem',
    title: 'Sort: Fix shed door',
    mode: 'fyi',
    attention: 'fyi',
    evidence: {
      origin: 'task',
      name: 'Fix shed door',
      path: 'task/Fix shed door.md',
      tier: 2,
      proposed_slot: 'duty',
      proposed_rule: 'default_duty',
      proposal_shape: 'task|due:n|t2',
    },
    actions: [],
    state: 'open',
    created_at: '2026-08-19T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  });
}

const ORIGIN_X = 100;
const ORIGIN_Y = 300;
// Derived, never typed — a threshold change moves these with it.
const IN_BAND_X = ORIGIN_X + Math.round((STAMP_FADE_START + SWIPE_X_THRESHOLD) / 2);
const PAST_BAND_X = ORIGIN_X + SWIPE_X_THRESHOLD + 40;

const card = () => screen.getAllByTestId('deck-card')[0];
const selectorOpen = () => screen.queryByTestId('deck-hold-selector') !== null;

function down() {
  fireEvent.pointerDown(card(), { clientX: ORIGIN_X, clientY: ORIGIN_Y, pointerId: 1 });
}
function move(x: number, y = ORIGIN_Y) {
  fireEvent.pointerMove(card(), { clientX: x, clientY: y, pointerId: 1 });
}
function up(x: number, y = ORIGIN_Y) {
  fireEvent.pointerUp(card(), { clientX: x, clientY: y, pointerId: 1 });
}

// jsdom ships no PointerEvent — the deckHoldBand polyfill, same reasons.
class TestPointerEvent extends MouseEvent {
  readonly pointerId: number;
  constructor(type: string, props: MouseEventInit & { pointerId?: number } = {}) {
    super(type, props);
    this.pointerId = props.pointerId ?? 1;
  }
}

const globalWithPointer = globalThis as unknown as Record<string, unknown>;
const elementProto = Element.prototype as unknown as Record<string, unknown>;
let originalPointerEvent: unknown;
let originalSetPointerCapture: unknown;

beforeEach(() => {
  mockAct.mockReset();
  mockAct.mockResolvedValue({ ok: true, status: 'sorted', render: { slot: 'duty', sorted: true } });
  vi.useFakeTimers();
  originalPointerEvent = globalWithPointer.PointerEvent;
  globalWithPointer.PointerEvent = TestPointerEvent;
  originalSetPointerCapture = elementProto.setPointerCapture;
  elementProto.setPointerCapture = function () {};
});
afterEach(() => {
  globalWithPointer.PointerEvent = originalPointerEvent;
  elementProto.setPointerCapture = originalSetPointerCapture;
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
});

// --- the plain gesture: affirm-as-suggested ------------------------------------

describe('the suggested affirm', () => {
  it('a full → swipe commits the PROPOSED verb through the delayed act', () => {
    render(<Deck items={[sortCard(), sortCard({ id: 'b', title: 'Bravo' })]} />);
    down();
    move(PAST_BAND_X);
    act(() => up(PAST_BAND_X));
    expect(selectorOpen()).toBe(false); // travelled through the band, no ambush
    act(() => vi.advanceTimersByTime(UNDO_MS + 1));
    expect(mockAct).toHaveBeenCalledWith(
      'sort_suggestion:task:task/Fix shed door.md', 'sort_duty',
    );
  });

  it('the face states the proposal and the strip names both gestures', () => {
    render(<Deck items={[sortCard()]} />);
    expect(screen.getByTestId('deck-suggested').textContent).toContain('Suggested');
    expect(screen.getByTestId('deck-suggested').textContent).toContain('Duty');
    expect(screen.getByTestId('deck-verb-affirm').textContent).toContain('Duty');
    expect(screen.getByTestId('deck-verb-reject').textContent).toContain('Not now');
  });
});

// --- the hold: the option selector ----------------------------------------------

describe('the affirm hold-selector', () => {
  it('holding a partial → opens the selector after GESTURE_HOLD_MS, suggested marked', () => {
    render(<Deck items={[sortCard()]} />);
    down();
    move(IN_BAND_X);
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS - 1));
    expect(selectorOpen()).toBe(false); // not a millisecond early
    act(() => vi.advanceTimersByTime(1));
    expect(selectorOpen()).toBe(true);
    // All three co-equal choices, the proposed one marked — never promoted.
    expect(screen.getByTestId('deck-hold-choice-sort_duty')).toBeTruthy();
    expect(screen.getByTestId('deck-hold-choice-sort_rhythm')).toBeTruthy();
    expect(screen.getByTestId('deck-hold-choice-sort_fuel')).toBeTruthy();
    expect(
      screen.getByTestId('deck-hold-choice-sort_duty').querySelector('[data-testid="deck-hold-suggested"]'),
    ).not.toBeNull();
    expect(
      screen.getByTestId('deck-hold-choice-sort_fuel').querySelector('[data-testid="deck-hold-suggested"]'),
    ).toBeNull();
  });

  it('ONE INTERACTION — a pick commits that verb with no confirm stage between', () => {
    render(<Deck items={[sortCard(), sortCard({ id: 'b', title: 'Bravo' })]} />);
    down();
    move(IN_BAND_X);
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS));
    act(() => {
      fireEvent.click(screen.getByTestId('deck-hold-choice-sort_fuel'));
    });
    // The pick IS the affirm: the selector closes, NO arm/confirm stage
    // appears, and the card has already advanced (optimistic, undoable).
    expect(selectorOpen()).toBe(false);
    expect(screen.queryByTestId('deck-confirm')).toBeNull();
    expect(screen.getByTestId('deck-count').textContent).toBe('1 card');
    // The undo window then flushes the CHOSEN verb — exactly one act.
    act(() => vi.advanceTimersByTime(UNDO_MS + 1));
    expect(mockAct).toHaveBeenCalledTimes(1);
    expect(mockAct).toHaveBeenCalledWith(
      'sort_suggestion:task:task/Fix shed door.md', 'sort_fuel',
    );
  });

  it('a pick is undoable exactly like a swipe — Undo cancels the POST', () => {
    render(<Deck items={[sortCard(), sortCard({ id: 'b', title: 'Bravo' })]} />);
    down();
    move(IN_BAND_X);
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS));
    act(() => {
      fireEvent.click(screen.getByTestId('deck-hold-choice-sort_rhythm'));
    });
    act(() => {
      fireEvent.click(screen.getByTestId('deck-toast-undo'));
    });
    act(() => vi.advanceTimersByTime(UNDO_MS * 2));
    expect(mockAct).not.toHaveBeenCalled(); // never an un-act — the act never fired
    expect(screen.getByTestId('deck-count').textContent).toBe('2 cards');
  });

  it('cancel springs the frozen card back and commits nothing', () => {
    render(<Deck items={[sortCard()]} />);
    const el = card();
    down();
    move(IN_BAND_X);
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS));
    expect(selectorOpen()).toBe(true);
    expect(el.style.transform).toContain(`${IN_BAND_X - ORIGIN_X}px`); // frozen at the held offset
    act(() => {
      fireEvent.click(screen.getByTestId('deck-hold-cancel'));
    });
    expect(selectorOpen()).toBe(false);
    expect(el.style.transform).toBe('');
    act(() => vi.advanceTimersByTime(UNDO_MS * 2));
    expect(mockAct).not.toHaveBeenCalled();
  });

  it('travelling on past the band cancels the hold (no ambush mid-swipe)', () => {
    render(<Deck items={[sortCard(), sortCard({ id: 'b', title: 'Bravo' })]} />);
    down();
    move(IN_BAND_X);
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS - 100));
    move(PAST_BAND_X); // still travelling toward the full swipe
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS * 3));
    expect(selectorOpen()).toBe(false);
  });

  it('lifting early cancels the hold — no selector after the hand is gone', () => {
    render(<Deck items={[sortCard()]} />);
    down();
    move(IN_BAND_X);
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS - 100));
    act(() => up(IN_BAND_X));
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS * 3));
    expect(selectorOpen()).toBe(false);
    expect(mockAct).not.toHaveBeenCalled(); // in-band release = null verdict
  });

  it('the on-face door opens the same selector — the keyboard/AT route', () => {
    render(<Deck items={[sortCard(), sortCard({ id: 'b', title: 'Bravo' })]} />);
    act(() => {
      fireEvent.click(screen.getByTestId('deck-hold-open'));
    });
    expect(selectorOpen()).toBe(true);
    act(() => {
      fireEvent.click(screen.getByTestId('deck-hold-choice-sort_rhythm'));
    });
    act(() => vi.advanceTimersByTime(UNDO_MS + 1));
    expect(mockAct).toHaveBeenCalledWith(
      'sort_suggestion:task:task/Fix shed door.md', 'sort_rhythm',
    );
  });

  it('never arms on a card without a served choice group — however long the finger sits', () => {
    // The kind gate, pinned as the OBSERVABLE property (the deckHoldBand
    // lesson): an email card held in the affirm band opens nothing, and its
    // face carries no suggestion line and no door.
    render(<Deck items={[sortCard({ id: 'e', kind: 'email_tier', mode: 'decide', attention: 'needs_you', evidence: {}, actions: [] })]} />);
    expect(screen.queryByTestId('deck-suggested')).toBeNull();
    expect(screen.queryByTestId('deck-hold-open')).toBeNull();
    down();
    move(IN_BAND_X);
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS * 5));
    expect(selectorOpen()).toBe(false);
  });

  it('an open selector blocks the gesture underneath it', () => {
    render(<Deck items={[sortCard(), sortCard({ id: 'b', title: 'Bravo' })]} />);
    down();
    move(IN_BAND_X);
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS));
    expect(selectorOpen()).toBe(true);
    // A fresh full swipe while the selector is open must not act on the card.
    down();
    move(PAST_BAND_X);
    act(() => up(PAST_BAND_X));
    act(() => vi.advanceTimersByTime(UNDO_MS + 1));
    expect(mockAct).not.toHaveBeenCalled();
  });
});

// --- the reject: not now ---------------------------------------------------------

describe('the not-now reject', () => {
  it('a ← swipe POSTs the quick defer and the toast does not claim a judgment', () => {
    render(<Deck items={[sortCard(), sortCard({ id: 'b', title: 'Bravo' })]} />);
    down();
    move(ORIGIN_X - (SWIPE_X_THRESHOLD + 40));
    act(() => up(ORIGIN_X - (SWIPE_X_THRESHOLD + 40)));
    // The honest sentence — a postponement, never "Rejected."
    expect(screen.getByTestId('deck-toast').textContent).toContain('Not now');
    expect(screen.getByTestId('deck-toast').textContent).not.toContain('Rejected');
    act(() => vi.advanceTimersByTime(UNDO_MS + 1));
    expect(mockAct).toHaveBeenCalledWith(
      'sort_suggestion:task:task/Fix shed door.md', 'defer',
    );
  });

  it('a ↑ on a sort card opens NO duration menu — its defer rungs are unbacked here', () => {
    // snoozeIsBacked is slot_suggestion-only; the sort card's ↑ stays the
    // session-local set-aside. A duration menu would promise persistence this
    // gesture does not have (the #48 property, on the new kind).
    render(<Deck items={[sortCard()]} />);
    down();
    move(ORIGIN_X, ORIGIN_Y - 60); // the ↑ hold band
    act(() => vi.advanceTimersByTime(GESTURE_HOLD_MS * 5));
    expect(screen.queryByTestId('deck-snooze-menu')).toBeNull();
    expect(selectorOpen()).toBe(false); // and not the selector either — wrong axis
  });
});
