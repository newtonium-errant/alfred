// The formalized type scale + the shared inner-panel surface treatment
// (docs/design/design-language.md Part 2 "Type scale" + "Surface & depth", and
// audit finding #4). Defined ONCE as className constants so the primary heading
// surfaces stop drifting (text-2xl vs text-xl vs text-lg applied ad-hoc) and
// every nested panel reads with the same legibility.
//
// The rounded friendly font is a warmth asset — weights stay SOLID (bold /
// extrabold), never thin/light. Colors map to the design-doc roles.

// --- Type scale (named roles, mapped to color roles) -------------------------

// DISPLAY — the page title. The biggest, warmest text on a screen.
export const display = 'text-2xl font-extrabold text-honeydew-700';

// SECTION — a section / collapsible-section header (one level above a card
// title). Matches the size CollapsibleSection already uses, codified so the
// section headers across the app can't drift.
export const sectionTitle = 'text-xl font-bold text-honeydew-700';

// TITLE — a card / sub-card heading. Matches the existing CardTitle.
export const title = 'text-lg font-bold text-honeydew-700';

// BODY — default reading text.
export const body = 'text-base text-honeydew-900';

// SUBTLE — supporting / caption text. Matches the existing CardDescription.
export const subtle = 'text-sm text-honeydew-600/80';

// --- Surface & depth ---------------------------------------------------------

// INNER PANEL — a soft panel nested inside a card (the Phases / Tools / Parts /
// Cut-list / Build-plan boxes). Audit finding #4: at the old honeydew-100/60
// fill these read too faintly against the cream card and blur together. The fix
// is the FILL, not the border: solid honeydew-100 (dropping the /60 that let the
// cream show through) so the panel sits a clear step above the card surface,
// while the existing defining honeydew-300 border stays. Still soft and warm —
// no shadow, no hard contrast. Keep nesting shallow (card -> at most one inner
// panel).
export const innerPanel =
  'rounded-xl border border-honeydew-300 bg-honeydew-100 px-3 py-2';
