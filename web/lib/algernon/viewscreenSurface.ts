/**
 * The home shell's surface name — ONE spelling, shared by the page, the
 * stylesheet's scope, and the tests.
 *
 * Same three-independent-places problem `sensorSurface.ts` names: the `surface`
 * prop the page hands Layout, the `[data-surface='…']` scope in
 * styles/viewscreen.css, and the containment assertions. The stylesheet cannot
 * import a constant, so that spelling is pinned against this one by test rather
 * than by hope; the other two share this literal, so a rename cannot half-land.
 *
 * WHY HOME IS A REGISTER AT ALL. The board modules on this page already speak
 * the console token grammar — rings, slots, deck-dealt cards. Until now the page
 * CHROME around them stayed warm, so the operator's morning surface was two
 * design languages stacked. This name is the chrome joining what it already
 * contains.
 *
 * Deliberately NOT added to `KnownSurface` in consoleTokens.ts. That union is
 * the SHARED layer's closed set, and widening it without a matching
 * `EvidenceBody.SKIN` entry is a compile error by design. The ratified open-seam
 * contract is exactly this: a surface owns its name in its own module and its
 * skin in its own stylesheet, and the shared layer needs no entry.
 */
export const VIEWSCREEN_SURFACE = 'viewscreen';
