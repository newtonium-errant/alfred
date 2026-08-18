// Pure geometry + colouring for the segmented "balanced day" rings (B3-4),
// ported from the operator-ratified deck sketch (scratchpad/sketch1-deck.html).
// DOM-free + deterministic so the arc math and segment layout are unit-pinned
// without an SVG renderer — the RingsHeader component is a thin render over this.

// Ring canvas — matches the sketch's 34x34 viewBox / centre 17 / r 14 idiom.
export const RING_VIEWBOX = 34;
export const RING_CENTER = RING_VIEWBOX / 2; // 17
export const RING_RADIUS = 14;
export const RING_STROKE_WIDTH = 3.5;

// Blank degrees between adjacent segments (the sketch's gap = 26°). MIN_SEG_DEG
// is a floor so a crowded bucket can't collapse a segment to a non-positive
// sweep — the sketch had no guard, but real tier buckets can hold many items.
export const RING_GAP_DEG = 26;
const MIN_SEG_DEG = 2;

export interface RingSegment {
  /** Segment start angle, degrees clockwise from 12 o'clock. */
  a0: number;
  /** Segment end angle, degrees clockwise from 12 o'clock. */
  a1: number;
}

/**
 * The arcs for a ring holding `n` items: `n` equal segments separated by
 * RING_GAP_DEG gaps, positioned so the first gap straddles 12 o'clock (the
 * sketch's `start = i*(seg+gap) + gap/2`). n <= 0 → [] (the caller renders the
 * empty red circle instead). When the full gap would leave a non-positive sweep
 * (a very crowded ring), the gap shrinks so every segment keeps MIN_SEG_DEG.
 */
export function ringSegments(n: number): RingSegment[] {
  if (!Number.isFinite(n) || n <= 0) return [];
  const count = Math.floor(n);
  let gap = RING_GAP_DEG;
  let seg = (360 - count * gap) / count;
  if (seg < MIN_SEG_DEG) {
    // Shrink the gap so each of the `count` segments keeps a MIN_SEG_DEG sweep.
    gap = Math.max((360 - count * MIN_SEG_DEG) / count, 0);
    seg = (360 - count * gap) / count;
  }
  const out: RingSegment[] = [];
  for (let i = 0; i < count; i++) {
    const a0 = i * (seg + gap) + gap / 2;
    out.push({ a0, a1: a0 + seg });
  }
  return out;
}

/**
 * SVG path `d` for an arc on the ring circle from a0 to a1 (degrees, clockwise
 * from 12 o'clock). Ported verbatim from the sketch's arcPath: the -90° rotation
 * puts 0° at the top; the large-arc flag trips once the sweep passes 180°.
 */
export function arcPath(cx: number, cy: number, r: number, a0: number, a1: number): string {
  const r0 = ((a0 - 90) * Math.PI) / 180;
  const r1 = ((a1 - 90) * Math.PI) / 180;
  const x0 = cx + r * Math.cos(r0);
  const y0 = cy + r * Math.sin(r0);
  const x1 = cx + r * Math.cos(r1);
  const y1 = cy + r * Math.sin(r1);
  const large = a1 - a0 > 180 ? 1 : 0;
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

// Segment / ring colouring, mapped to the PWA's EXISTING status token idiom (the
// deck already uses text-status-progress-fg / text-danger). Applied as each
// element's text colour with stroke="currentColor" so a path can carry its own
// colour. These literals live in lib/, which the tailwind content glob scans.
//   done      → green  (a completed item)
//   planned   → amber  (a committed, not-yet-done item — on today's plan)
//   suggested → muted  (C2: an auto-surfaced candidate, not yet committed — a
//               third visual class, tentative; rendered as a segment for legibility
//               but excluded from the done/total COUNT. Colour-only for v1; a dashed
//               stroke is a possible enhancement, operator eyes on first cut.)
//   snoozed   → caution (a DELAYED item — acted on, but pushed to a later day and
//               due back. See below for why it paints a segment at all.)
//   empty     → red    (an empty ring: nothing in the bucket)
//
// WHY SNOOZED PAINTS A SEGMENT RATHER THAN NOTHING (the ruling, in one sentence):
// dropping it would make a morning where three duties were PUSHED look identical
// to one where they never existed, which erases the operator's own decision from
// the only glance surface he has — so the segment stays and says *moved*, never
// *finished*. (The item leaves on its own the next day, via
// `ringItemVisibleToday`'s acted_at check; this is its one visible day.)
//
// WHY `caution` AND NOT A NEW HUE: styles/console.css states the role contract
// outright — "`caution` deliberately covers BOTH the defer family (swipe-up,
// snooze) and the heavy-verb arm stage … Do not split them into two hues without
// re-ratifying". Snooze IS the defer verb, so amber is the ratified answer and
// picking a fresh colour here would be an unratified split.
//
// The consequence, stated rather than left to be discovered: `caution` (#c99a4c)
// and `planned`'s `status-progress-fg` (#92611a) are the same amber FAMILY, which
// is semantically right (both mean "not finished") and is a legibility question on
// a 3.5px stroke. They differ substantially in luminance so they are separable,
// and the distinctness pin in tests/ringGeometry.test.ts asserts all five classes
// are distinct STRINGS — a string pin cannot speak for the eye. Flagged for
// operator eyes on first cut, exactly as `suggested` was.
export type SegmentStatus = 'done' | 'planned' | 'suggested' | 'snoozed';

export const RING_STROKE_CLASS = {
  done: 'text-status-done-fg',
  planned: 'text-status-progress-fg',
  suggested: 'text-honeydew-400',
  snoozed: 'text-caution',
  empty: 'text-danger',
} as const;

/**
 * The colour class for one segment given its stage (done → green, planned → amber,
 * suggested → muted, snoozed → caution amber).
 *
 * TOTAL over `SegmentStatus` by construction: it indexes the record rather than
 * branching, so a new stage cannot be added to the union without a colour — the
 * type checker names the omission at this line. That totality is the reason a
 * snoozed segment could not silently inherit another stage's paint.
 */
export function segmentStageClass(stage: SegmentStatus): string {
  return RING_STROKE_CLASS[stage];
}

/**
 * The colour class for one segment given its completion (done → green, else amber).
 * Delegates to `segmentStageClass` — the 2-way completion helper kept for callers
 * that only know done/not-done; C2 stage-aware callers use `segmentStageClass`.
 */
export function segmentStroke(done: boolean): string {
  return segmentStageClass(done ? 'done' : 'planned');
}
