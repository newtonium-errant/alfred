import { createUnrecordedLedger, type UnrecordedAct } from './unrecordedLedger';

// The DAY BOARD's unrecorded-completion ledger — the ✓ taps the operator made
// on home that the server refused, held until they have actually seen them.
//
// ## Why the board needs one at all
//
// `useBoardGrace` is a knowing second copy of the deck's deferred-act quartet: a
// ✓ marks the row done on screen and HOLDS the write for the undo window, then
// commits it when the window closes, when the next ✓ lands, or from the unmount
// cleanup when the operator navigates off home mid-window. That last door is the
// one that has no component behind it — the POST fires from a dead closure, and
// before this ledger existed its refusal reached nothing at all. Same mechanism,
// same silence, same surface the 2026-08-15 incident was about: a completion the
// operator gave, recorded nowhere, never reported.
//
// ## Its OWN key, not the deck's
//
// The notice's single Acknowledge control clears the whole list, so sharing a
// key with the deck would let a tap on /deck settle a debt the operator has
// never been shown on home — and the two notices name different things ("your
// verdict", "your completion"). One key per surface; see `unrecordedLedger` for
// the rest of the mechanism and the incident it comes from.

/** The `localStorage` key. Exported so a test can seed it the way the board does. */
export const BOARD_UNRECORDED_KEY = 'algernon_board_unrecorded';

/**
 * One completion the server refused.
 *
 * Carries no field beyond the shared base, and deliberately so: the board's
 * grace window commits exactly one verb (`done`), which `actionId` already
 * records. The deck's `verdict` exists because a swipe has four meanings; a ✓
 * has one, and storing a constant would only invite the two shapes to drift.
 */
export type UnrecordedCompletion = UnrecordedAct;

const ledger = createUnrecordedLedger<UnrecordedCompletion>(BOARD_UNRECORDED_KEY);

/** The ledger, oldest first. Empty when storage is unreadable — never invented. */
export const readBoardUnrecorded = ledger.read;
/** Record one refused completion, replacing any earlier entry for the same row. */
export const recordBoardUnrecorded = ledger.record;
/** Drop one row's entry — the ✓ was given again and landed. */
export const clearBoardUnrecorded = ledger.clear;
/** The operator has SEEN the list. Returns the new (empty) ledger. */
export const clearAllBoardUnrecorded = ledger.clearAll;
