import { DECK_CARD_BASE_Z } from './DeckCard';

// Overlays (the snoozed drill, the re-tier picker, the hold-selector) must sit
// ABOVE the whole card stack — the top card is at DECK_CARD_BASE_Z, so an
// overlay below it opens invisibly behind the card and reads as a dead tap
// (the #28 re-tier bug). Derived from the card base so it can't drift below.
// Inline style (not a Tailwind z-class) at each consumer so the ordering is
// jsdom-testable. Extracted to its own module when the hold-selector became a
// second FILE needing it — Deck.tsx and HoldSelector.tsx must share one
// derivation, and HoldSelector importing Deck for it would be a cycle.
export const OVERLAY_Z_INDEX = DECK_CARD_BASE_Z + 10;
