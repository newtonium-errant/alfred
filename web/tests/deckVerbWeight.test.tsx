import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

// PER-VERB WEIGHTS (#102 / D4) — the exposure fix, driven through the deck.
//
// THE BUG THIS CLOSES. Weight was one boolean per KIND, so `attribution` was
// declared light in both directions. Its LEFT swipe posts `reject`, and
// `vault/attribution.py::reject_marker` strips the marked line range out of the
// record body and drops its audit entry. A body-destroying edit therefore
// committed on a single motion, protected only by the undo window — while
// `proposal`, which merely creates a record, arms and asks twice.
//
// The unit half (weights, predicates, arm copy) is pinned in feedFoundation;
// this file pins the BEHAVIOUR through the real deck, because a weight table
// nothing consults is exactly the accepted-then-ignored shape.

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { Deck } from '../components/feed/Deck';
import { UNDO_MS } from '../lib/algernon/feedConstants';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

function item(over: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
    id: 'attribution:marker:inf-20260811-salem-abc123',
    kind: 'attribution',
    instance: 'salem',
    title: 'Salem inferred: the coop latch needs replacing',
    mode: 'decide',
    attention: 'needs_you',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-08-11T10:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  });
}

// The drag harness is the one deckHoldBand.test.tsx established: the deck
// renders a STACK of `deck-card` elements and only the first carries the
// imperative pointer listeners, and jsdom needs a PointerEvent that actually
// carries clientX/clientY (its fallback drops them, so every coordinate arrives
// NaN and no gesture ever resolves). Same harness, same reasons — the notes live
// in that file rather than being restated here.
const ORIGIN_X = 200;
const ORIGIN_Y = 300;
const card = () => screen.getAllByTestId('deck-card')[0];

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

/** A drag that resolves to the given horizontal verdict, and releases. */
function swipeX(toX: number) {
  fireEvent.pointerDown(card(), { clientX: ORIGIN_X, clientY: ORIGIN_Y, pointerId: 1 });
  fireEvent.pointerMove(card(), { clientX: toX, clientY: ORIGIN_Y, pointerId: 1 });
  act(() => {
    fireEvent.pointerUp(card(), { clientX: toX, clientY: ORIGIN_Y, pointerId: 1 });
  });
}
const swipeLeft = () => swipeX(ORIGIN_X - 160);
const swipeRight = () => swipeX(ORIGIN_X + 160);

beforeEach(() => {
  mockAct.mockReset();
  mockAct.mockResolvedValue({ ok: true, status: 'acted' });
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

describe('a heavy REJECT arms instead of committing', () => {
  it('ATTRIBUTION reject: one swipe writes NOTHING, even past the undo window', () => {
    // The regression pin. Before per-verb weights this posted `reject` and, once
    // the window lapsed, stripped the section out of the record.
    render(<Deck items={[item()]} />);
    swipeLeft();

    expect(screen.getByTestId('deck-confirm')).toBeTruthy();
    // Past the undo window and beyond — an armed verb has no pending POST to
    // flush, so time alone must never commit it.
    act(() => vi.advanceTimersByTime(UNDO_MS * 3));
    expect(mockAct).not.toHaveBeenCalled();
  });

  it('the arm says WHAT WILL BE WRITTEN — not "write this to the vault?"', () => {
    // D4. The old copy asked the same question for every heavy verb, and for a
    // reject that question described the opposite of what happens.
    render(<Deck items={[item()]} />);
    swipeLeft();

    const note = screen.getByTestId('deck-confirm-note').textContent ?? '';
    expect(note).toContain('Removes the marked section');
    expect(note).toContain('audit entry');
    // And the commit button names the verb it will fire.
    expect(screen.getByTestId('deck-confirm-yes').textContent).toContain('Reject');
  });

  it('the SECOND tap commits — and commits the REJECT, not the affirm', () => {
    // The direction is the whole point: an armed reject that committed
    // `confirm` would agree with a record the operator just refused.
    render(<Deck items={[item()]} />);
    swipeLeft();
    act(() => fireEvent.click(screen.getByTestId('deck-confirm-yes')));
    act(() => vi.advanceTimersByTime(UNDO_MS + 1));

    expect(mockAct).toHaveBeenCalledTimes(1);
    expect(mockAct).toHaveBeenCalledWith(
      'attribution:marker:inf-20260811-salem-abc123',
      'reject',
    );
  });

  it('CANCEL leaves the card and writes nothing', () => {
    render(<Deck items={[item()]} />);
    swipeLeft();
    act(() => fireEvent.click(screen.getByTestId('deck-confirm-cancel')));
    act(() => vi.advanceTimersByTime(UNDO_MS * 3));

    expect(mockAct).not.toHaveBeenCalled();
    expect(screen.queryByTestId('deck-confirm')).toBeNull();
  });
});

describe('POSITIVE CONTROL — the light direction of the SAME card still commits on one swipe', () => {
  it('attribution CONFIRM needs no second tap', () => {
    // Without this, every pin above passes identically against a deck that arms
    // EVERYTHING — which would be a different bug (every verb costing two taps)
    // reported as a fix. The asymmetry is the claim, so the asymmetry is tested.
    render(<Deck items={[item()]} />);
    swipeRight();

    expect(screen.queryByTestId('deck-confirm')).toBeNull();
    act(() => vi.advanceTimersByTime(UNDO_MS + 1));
    expect(mockAct).toHaveBeenCalledWith(
      'attribution:marker:inf-20260811-salem-abc123',
      'confirm',
    );
  });

  it('a light kind (email_tier) commits on one swipe in BOTH directions', () => {
    render(<Deck items={[item({ id: 'email_tier:x', kind: 'email_tier' })]} />);
    swipeLeft();
    expect(screen.queryByTestId('deck-confirm')).toBeNull();
    act(() => vi.advanceTimersByTime(UNDO_MS + 1));
    expect(mockAct).toHaveBeenCalledWith('email_tier:x', 'spam');
  });
});

describe('the undo window is visible, not just long (D8)', () => {
  it('a committed light verb shows a draining bar whose duration IS UNDO_MS', () => {
    // The bar reads its duration from the constant the flush timer uses. Two
    // numbers here would be a bar that empties at a rate unrelated to the
    // deadline it depicts — worse than no bar, because it looks like information.
    render(<Deck items={[item({ id: 'email_tier:x', kind: 'email_tier' })]} />);
    swipeLeft();

    const bar = screen.getByTestId('deck-toast-bar');
    expect((bar as HTMLElement).style.animationDuration).toBe(`${UNDO_MS}ms`);
  });

  it('the window is SIX seconds — the ruled value, not the shipped 3.5', () => {
    expect(UNDO_MS).toBe(6000);
  });

  it('an ARMED heavy verb shows no toast and no bar — nothing has been done yet', () => {
    // ILB in the other direction: a countdown over a verb that has not fired
    // would promise a deadline that does not exist.
    render(<Deck items={[item()]} />);
    swipeLeft();
    expect(screen.queryByTestId('deck-toast-bar')).toBeNull();
  });
});
