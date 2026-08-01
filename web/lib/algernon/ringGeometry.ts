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
//   empty     → red    (an empty ring: nothing in the bucket)
export type SegmentStatus = 'done' | 'planned' | 'suggested';

export const RING_STROKE_CLASS = {
  done: 'text-status-done-fg',
  planned: 'text-status-progress-fg',
  suggested: 'text-honeydew-400',
  empty: 'text-danger',
} as const;

/** The colour class for one segment given its C2 stage (done → green, planned → amber, suggested → muted). */
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
