// The ✓-hold when-selector's shared words — ONE owner for the three board
// surfaces (FeedRow, SlotBoard, RingsHeader) so the same gesture reads the
// same way everywhere (the house one-gesture-one-meaning rule; a drifted copy
// per surface is the same-concept-divergent-constants trap in copy form).
//
// The NOTE overrides the HoldSelector default deliberately: the board's picks
// POST straight away with a real reversing Undo on the row — not the deck's
// delayed-act cancel window — and the footer must promise the way back that
// is true where it renders.

/** Sheet title — names the decision the family answers. */
export const WHEN_SELECTOR_TITLE = 'Done when?';

/** Footer — the board surfaces' undo promise. */
export const WHEN_SELECTOR_NOTE =
  'Choosing records it straight away — Undo takes it back.';
