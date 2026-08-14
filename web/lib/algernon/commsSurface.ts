/**
 * The chat surface's name — the third pillar (deck = tactical, feed = sensors,
 * chat = comms).
 *
 * ONE spelling for the page's `surface` prop, the `[data-surface='…']` scope in
 * styles/comms.css, and the containment assertions — the same three-places
 * problem `sensorSurface.ts` names.
 *
 * THE QUOTED TRANSCRIPT, AND WHY IT IS NOT A SECOND SURFACE. Salem's replies
 * render in monospace phosphor — the ship's computer speaking — which is a
 * QUOTATION of the CRT register, not adoption of it. The precedent is
 * TimelineView setting its own nested `data-surface`: a region may borrow
 * another register's voice without the page changing identity. So the chrome,
 * the composer, and every voice state stay comms-grade, and exactly one region
 * speaks CRT.
 *
 * That split is what the containment pin has to encode, and it is why a naive
 * "no CRT tokens on comms" assertion would be wrong: the quoted region is a
 * DECLARED exception, named in the pin rather than silently excluded from it.
 *
 * Same open-seam contract as its siblings — not in `KnownSurface`.
 */
export const COMMS_SURFACE = 'comms';

/**
 * The class that carries the CRT quotation on an assistant turn.
 *
 * Named here beside the surface it belongs to (not in comms.css alone) so the
 * markup and the stylesheet share one literal, and so the containment pin can
 * name the exception by importing it rather than by restating a string.
 */
export const COMMS_QUOTED_CLASS = 'comms-quoted';
