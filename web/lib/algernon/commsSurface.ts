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

/**
 * The transcript's two hulls — the panel a turn is spoken from.
 *
 * The QUOTATION (above) reached the assistant's words while the hull they sit
 * on stayed warm: `bg-cream` under monospace phosphor, which is what the
 * operator photographed and called bright. These adopt the hull, and they are
 * deliberately NOT part of the quotation — they read the surface's ordinary
 * depth tokens, never `--comms-quoted-*`, so the boundary that says exactly one
 * selector may borrow the CRT voice stays true by construction rather than by
 * remembering.
 *
 * TWO CLASSES, BECAUSE THE DISTINCTION IS THE POINT. Operator and computer must
 * stay tellable apart, and the existing grammar carries that in the TEXT FACE —
 * proportional for the operator, monospace-phosphor for the computer. That is
 * untouched. The hull carries the same distinction on the register's own depth
 * axis: the computer speaks from a pane face, the operator from the control
 * resting on it. Flattening both to one hull would leave the register legible
 * and the conversation harder to read at a glance.
 */
export const COMMS_TURN_ASSISTANT_CLASS = 'comms-turn-assistant';
export const COMMS_TURN_OPERATOR_CLASS = 'comms-turn-operator';
