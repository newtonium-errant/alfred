import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen } from '@testing-library/react';

// The hold-band gesture MACHINE (#48, reviewer-a27's WARN-2 on #14).
//
// `inSnoozeHoldBand`'s geometry is pinned in feedConstants.test.ts. This file
// pins the state machine built on top of it in Deck.tsx — the part that has to
// be driven with a real pointer and a real clock:
//
//   arm-on-enter · cancel-on-leave · cancel-on-early-up · re-anchor-on-drift ·
//   hold-within-tolerance · freeze-at-offset · release-does-not-unfreeze ·
//   spring-back-on-dismiss · teardown-cancels
//
// Every one of these was undefended: the reviewer removed `clearHold()` from the
// leave-band branch and all 76 deck tests stayed green, while the built app
// would have opened a menu under a finger still travelling toward a full swipe.
// Each test below names the mutation that reds it.
//
// Driven with synthetic pointer events against the REAL Deck + DeckCard (the
// listeners are attached imperatively to the card element, not via React props,
// so `fireEvent` reaches them exactly as a browser would) plus fake timers for
// the hold clock. There is no device testing in this lane; this is the only
// place the properties can be caught.

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { Deck } from '../components/feed/Deck';
import {
  SNOOZE_HOLD_MOVE_TOLERANCE,
  SNOOZE_HOLD_MS,
  SNOOZE_X_TOLERANCE,
  SNOOZE_Y_THRESHOLD,
  STAMP_FADE_START,
  verdictForDrag,
} from '../lib/algernon/feedConstants';
import type { FeedItem } from '../lib/algernon/feed';

// A snooze-capable card — the only kind that offers durations, so the only kind
// whose hold band is live (`durationsAvailable` in the drag effect).
function slotItem(over: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'slot_suggestion:task:task/Pay Steph.md',
    kind: 'slot_suggestion',
    instance: 'salem',
    title: 'T1: Pay Steph',
    mode: 'decide',
    attention: 'needs_you',
    evidence: { tier: 1, origin: 'task', path: 'task/Pay Steph.md', name: 'Pay Steph', candidate: true },
    actions: [],
    state: 'open',
    created_at: '2026-08-05T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  };
}

// The gesture origin. Upward drag = DECREASING clientY, so `at(up)` converts a
// "how many px up" into the clientY a browser would report.
const ORIGIN_X = 100;
const ORIGIN_Y = 300;
const at = (up: number) => ORIGIN_Y - up;

// Two points inside the band and one comfortably past it. Derived from the
// constants rather than typed as literals, so a threshold change moves these
// with it instead of silently making the tests describe the wrong gesture.
const IN_BAND = Math.round((STAMP_FADE_START + SNOOZE_Y_THRESHOLD) / 2); // 60
const PAST_BAND = SNOOZE_Y_THRESHOLD + 40; // 120 — full-swipe territory
const BELOW_BAND = STAMP_FADE_START - 20; // 20 — stamp not yet showing

// The TOP card. The deck renders a stack (up to three `deck-card` elements),
// and only the top one carries the drag listeners — `getByTestId` would throw
// on ambiguity the moment a test uses more than one item. Deck pushes `current`
// at depth 0 first, so DOM order puts the interactive card first.
const card = () => screen.getAllByTestId('deck-card')[0];
const menuOpen = () => screen.queryByTestId('deck-snooze-menu') !== null;

function down(x = ORIGIN_X, y = ORIGIN_Y) {
  fireEvent.pointerDown(card(), { clientX: x, clientY: y, pointerId: 1 });
}
function move(x: number, y: number) {
  fireEvent.pointerMove(card(), { clientX: x, clientY: y, pointerId: 1 });
}
function up(x = ORIGIN_X, y = ORIGIN_Y) {
  fireEvent.pointerUp(card(), { clientX: x, clientY: y, pointerId: 1 });
}

// jsdom ships no PointerEvent, and Testing Library's fallback for an unknown
// constructor drops clientX/clientY — every coordinate arrived as NaN, so the
// drag maths produced `translate(NaNpx, NaNpx)` and no gesture ever resolved.
// Extending MouseEvent is what carries the coordinates through; pointerId is the
// only other field the drag effect touches (setPointerCapture).
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
  mockAct.mockResolvedValue({ ok: true, status: 'acted' });
  vi.useFakeTimers();
  originalPointerEvent = globalWithPointer.PointerEvent;
  globalWithPointer.PointerEvent = TestPointerEvent;
  // jsdom implements neither pointer capture method; the drag effect calls
  // setPointerCapture on pointerdown, so without a stub every gesture throws
  // before the machine is reached. Same class of gap as the scrollIntoView stub
  // in vitest.setup.ts, but scoped here — only the deck drags a pointer.
  originalSetPointerCapture = elementProto.setPointerCapture;
  elementProto.setPointerCapture = function () {};
});
afterEach(() => {
  globalWithPointer.PointerEvent = originalPointerEvent;
  elementProto.setPointerCapture = originalSetPointerCapture;
  vi.runOnlyPendingTimers();
  vi.useRealTimers();
});

// --- the harness itself must be honest ---------------------------------------

describe('hold-band harness', () => {
  it('the band constants really do sit where these tests assume', () => {
    // If this drifts, every "in band" / "past band" below is describing a
    // different gesture than its name claims, and the suite would keep passing
    // while testing nothing. Cheap insurance on a file built out of coordinates.
    expect(IN_BAND).toBeGreaterThan(STAMP_FADE_START);
    expect(IN_BAND).toBeLessThan(SNOOZE_Y_THRESHOLD);
    expect(PAST_BAND).toBeGreaterThan(SNOOZE_Y_THRESHOLD);
    expect(BELOW_BAND).toBeLessThan(STAMP_FADE_START);
  });

  it('a full ↑ swipe still snoozes — the machine did not break the verdict path', () => {
    render(<Deck items={[slotItem(), slotItem({ id: 'b', title: 'Bravo' })]} />);
    down();
    move(ORIGIN_X, at(PAST_BAND));
    act(() => up(ORIGIN_X, at(PAST_BAND)));
    expect(menuOpen()).toBe(false);
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS * 10));
    expect(mockAct).toHaveBeenCalledWith('slot_suggestion:task:task/Pay Steph.md', 'snooze_until_i_say');
  });
});

// --- arm / cancel -------------------------------------------------------------

describe('Deck hold band — arming and cancelling', () => {
  it('holding still IN the band opens the duration menu after SNOOZE_HOLD_MS', () => {
    render(<Deck items={[slotItem()]} />);
    down();
    move(ORIGIN_X, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS - 1));
    expect(menuOpen()).toBe(false); // not a millisecond early
    act(() => vi.advanceTimersByTime(1));
    expect(menuOpen()).toBe(true);
  });

  it('ACCEPTANCE — leaving the band UPWARD cancels the hold (no ambush)', () => {
    // The reviewer's case. A finger travelling toward a full swipe passes
    // THROUGH the band; if the timer survives that, the menu opens under a
    // gesture the operator meant as a swipe — the exact behaviour the code
    // comment claims the cancel prevents.
    //
    // Mutation: delete `clearHold()` from the `!inSnoozeHoldBand` branch in
    // Deck.tsx's onMove → the timer armed on the first move survives, fires
    // here, and this fails.
    render(<Deck items={[slotItem()]} />);
    down();
    move(ORIGIN_X, at(IN_BAND)); // arms
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS - 100));
    move(ORIGIN_X, at(PAST_BAND)); // travels on past the band
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS * 3));
    expect(menuOpen()).toBe(false);
  });

  it('dropping back BELOW the band cancels the hold too', () => {
    // The other exit. Below STAMP_FADE_START nothing is drawn on the card, so a
    // menu appearing there would arrive with no visible cause at all.
    render(<Deck items={[slotItem()]} />);
    down();
    move(ORIGIN_X, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS - 100));
    move(ORIGIN_X, at(BELOW_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS * 3));
    expect(menuOpen()).toBe(false);
  });

  it('drifting SIDEWAYS out of the band cancels the hold', () => {
    // The band carries the same |dx| tolerance as the ↑ verdict, so a diagonal
    // must not open a menu the release wouldn't have honoured either.
    render(<Deck items={[slotItem()]} />);
    down();
    move(ORIGIN_X, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS - 100));
    move(ORIGIN_X + SNOOZE_X_TOLERANCE + 10, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS * 3));
    expect(menuOpen()).toBe(false);
  });

  it('lifting the finger EARLY cancels the hold — no menu after the hand is gone', () => {
    // Mutation: delete `clearHold()` from the top of onUp → the menu opens
    // ~100ms after the operator has already let go, over whatever card is
    // showing, and this fails.
    render(<Deck items={[slotItem()]} />);
    down();
    move(ORIGIN_X, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS - 100));
    act(() => up(ORIGIN_X, at(IN_BAND)));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS * 3));
    expect(menuOpen()).toBe(false);
    // ...and the short drag resolved to nothing, so no act went out either.
    expect(verdictForDrag(0, -IN_BAND)).toBeNull(); // states WHY none is expected
    expect(mockAct).not.toHaveBeenCalled();
  });

  it('the card changing under an armed hold cancels it', () => {
    // Effect teardown. Without the `clearHold()` in the cleanup, a hold armed on
    // one card would open a menu over its SUCCESSOR — a menu whose durations
    // would then apply to a card the operator never touched.
    //
    // Mutation: delete `clearHold()` from the drag effect's cleanup → fails.
    render(<Deck items={[slotItem(), slotItem({ id: 'b', title: 'Bravo' })]} />);
    down();
    move(ORIGIN_X, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS - 100));
    // Advance the deck by another route entirely (the ✓ button), which swaps the
    // top card and tears the drag effect down mid-hold.
    act(() => fireEvent.click(screen.getByTestId('deck-btn-affirm')));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS * 3));
    expect(menuOpen()).toBe(false);
  });
});

// --- re-anchor ----------------------------------------------------------------

describe('Deck hold band — re-anchoring on drift', () => {
  it('drifting past the tolerance RESTARTS the clock instead of firing', () => {
    // A finger creeping up through the band is still travelling. Re-anchoring is
    // what stops a slow full-swipe from being read as a hold.
    //
    // Mutation: in the `holdTimerRef.current !== null` branch, always `return`
    // (drop the tolerance check and the re-arm) → the original timer survives,
    // fires at the 100ms advance below, and the first assert fails.
    render(<Deck items={[slotItem()]} />);
    down();
    move(ORIGIN_X, at(STAMP_FADE_START + 5)); // arms near the bottom of the band
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS - 50));
    move(ORIGIN_X, at(STAMP_FADE_START + 5 + SNOOZE_HOLD_MOVE_TOLERANCE + 8)); // drifts up, still in band
    act(() => vi.advanceTimersByTime(100)); // past the ORIGINAL deadline
    expect(menuOpen()).toBe(false); // ...but the clock restarted
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS));
    expect(menuOpen()).toBe(true); // and it still fires once held again
  });

  it('jitter WITHIN the tolerance does not restart the clock', () => {
    // The other half, and the one a naive "re-anchor on every move" fix breaks:
    // a thumb is not a tripod, and a hold that never completes because of 3px of
    // tremor is a feature the operator cannot use.
    //
    // Mutation: re-anchor unconditionally (delete the tolerance comparison) →
    // the menu never opens on this sequence and the final assert fails.
    render(<Deck items={[slotItem()]} />);
    down();
    move(ORIGIN_X, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS - 50));
    move(ORIGIN_X + SNOOZE_HOLD_MOVE_TOLERANCE - 2, at(IN_BAND)); // a wobble, not a move
    act(() => vi.advanceTimersByTime(50)); // the ORIGINAL deadline arrives
    expect(menuOpen()).toBe(true);
  });
});

// --- freeze / release ---------------------------------------------------------

describe('Deck hold band — freeze at the held offset', () => {
  it('the card KEEPS its held transform while the menu is open', () => {
    // The ruling: the menu reads as attached to the gesture. That is the whole
    // reason the hold fires mid-drag rather than on release.
    //
    // Mutation: call resetVisual() when the hold fires → the card snaps home the
    // instant the menu appears and this fails.
    render(<Deck items={[slotItem()]} />);
    const el = card();
    down();
    move(ORIGIN_X, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS));
    expect(menuOpen()).toBe(true);
    expect(el.style.transform).toContain(`-${IN_BAND}px`);
  });

  it('RELEASING under the open menu does not unfreeze the card', async () => {
    // The release must not spring the card out from under an open menu.
    //
    // Two corrections to what the shipped comment claims, both found by
    // mutating rather than reading, and both reported at the #48 ship:
    //
    //  1. It says the release "must not also verdict". Geometrically it never
    //     could — every in-band coordinate resolves to a null verdict (asserted
    //     below), because the band's own |dx| and dy bounds sit strictly inside
    //     every threshold verdictForDrag tests. What the guard really protects
    //     is onUp's `resetVisual()`, i.e. the FREEZE.
    //  2. `gestureConsumedRef` is REDUNDANT with the `dragging = false` set two
    //     lines above it in the same callback: onUp's `if (!dragging) return`
    //     already short-circuits. Deleting either guard alone leaves this test
    //     green; deleting BOTH reds it. So the mutation recorded for this pin is
    //     the combined one — claiming the single-guard mutation would be
    //     claiming a red that does not happen.
    //
    // The pointerup is dispatched after the timer fires but BEFORE React commits
    // the menu render, which is the production race verbatim: the hold fires,
    // the re-render that tears the drag listeners down has not landed, and the
    // finger lifts in that gap.
    render(<Deck items={[slotItem()]} />);
    const el = card();
    down();
    move(ORIGIN_X, at(IN_BAND));
    // Fire the hold WITHOUT letting React commit the menu render, then lift the
    // finger in that gap — this is the production race verbatim (the timer sets
    // state; the re-render that tears the drag listeners down has not landed
    // yet; the operator's finger comes up). Advancing inside act() instead would
    // flush the re-render first, the listeners would already be gone, and onUp
    // would never run — which is exactly how the first draft of this test passed
    // against a build with the guard deleted.
    vi.advanceTimersByTime(SNOOZE_HOLD_MS);
    up(ORIGIN_X, at(IN_BAND));
    // Let React catch up. It has to be the ASYNC act + async timer advance:
    // React's scheduler does not run on a plain fake timer here, so a sync
    // act(() => …) leaves the pending render in the queue and the menu never
    // appears (which is how the first draft of this test managed to pass while
    // asserting nothing about the guard at all).
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(menuOpen()).toBe(true);
    expect(el.style.transform).toContain(`-${IN_BAND}px`);
    expect(verdictForDrag(0, -IN_BAND)).toBeNull(); // the in-band null-verdict claim
    expect(mockAct).not.toHaveBeenCalled();
  });

  it('dismissing the menu springs the card back', () => {
    // Reached through the real gesture rather than the ↑ button, so this covers
    // the path an operator actually takes to a frozen card.
    render(<Deck items={[slotItem()]} />);
    const el = card();
    down();
    move(ORIGIN_X, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS));
    expect(el.style.transform).toContain(`-${IN_BAND}px`);

    act(() => fireEvent.click(screen.getByTestId('deck-snooze-cancel')));
    expect(menuOpen()).toBe(false);
    expect(el.style.transform).toBe('');
    expect(screen.getByTestId('deck-card')).toBeTruthy(); // still the same card
    expect(mockAct).not.toHaveBeenCalled();
  });

  it('picking a duration from a held gesture commits THAT rung', () => {
    // End to end, gesture to POST: the path the feature exists for.
    render(<Deck items={[slotItem(), slotItem({ id: 'b', title: 'Bravo' })]} />);
    down();
    move(ORIGIN_X, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS));
    act(() => fireEvent.click(screen.getByTestId('deck-snooze-choice-snooze_3d')));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS * 10));
    expect(mockAct).toHaveBeenCalledWith('slot_suggestion:task:task/Pay Steph.md', 'snooze_3d');
  });
});

// --- the kind gate ------------------------------------------------------------

describe('Deck hold band — only live where durations mean something', () => {
  it('an unbacked kind never opens a duration menu, however long the finger sits there', () => {
    // DEFENDED IN DEPTH, and this pin is honest about that: the property has TWO
    // independent guards — `durationsAvailable` in the drag effect (the hold
    // never arms) and `snoozeIsBacked(current)` on the JSX (the menu never
    // renders). Removing either one alone leaves this green, so there is no
    // single-guard mutation to name here; removing BOTH reds it, which is the
    // mutation recorded in the ship report.
    //
    // Pinning the OBSERVABLE property rather than one of its two guards is the
    // right level anyway: what must never happen is an email card offering
    // "3 days" with no store behind it.
    render(<Deck items={[slotItem({ id: 'e', kind: 'email_tier', title: 'Email A' })]} />);
    down();
    move(ORIGIN_X, at(IN_BAND));
    act(() => vi.advanceTimersByTime(SNOOZE_HOLD_MS * 5));
    expect(menuOpen()).toBe(false);
  });
});
