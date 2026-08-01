import { describe, expect, it } from 'vitest';
import {
  arcPath,
  RING_GAP_DEG,
  RING_STROKE_CLASS,
  ringSegments,
  segmentStageClass,
  segmentStroke,
} from '../lib/algernon/ringGeometry';

// Pure ring geometry — ported from the operator-ratified deck sketch. These pin
// the segment layout (gap 26°, first gap straddling 12 o'clock), the arc-path
// math, the crowded-ring guard, and the three-colour mapping.

describe('ringSegments', () => {
  it('returns no segments for an empty ring (n = 0) → caller draws the red circle', () => {
    expect(ringSegments(0)).toEqual([]);
  });

  it('treats non-positive / non-finite counts as empty', () => {
    expect(ringSegments(-1)).toEqual([]);
    expect(ringSegments(Number.NaN)).toEqual([]);
    expect(ringSegments(Number.POSITIVE_INFINITY)).toEqual([]);
  });

  it('lays out a single item as one near-full arc, first gap centred on top', () => {
    // seg = (360 - 1*26)/1 = 334; a0 = 0*(334+26) + 26/2 = 13; a1 = 13 + 334 = 347.
    expect(ringSegments(1)).toEqual([{ a0: 13, a1: 347 }]);
  });

  it('lays out N equal segments separated by the gap (n = 3)', () => {
    // seg = (360 - 3*26)/3 = 94; stride = seg + gap = 120; offset = gap/2 = 13.
    expect(ringSegments(3)).toEqual([
      { a0: 13, a1: 107 },
      { a0: 133, a1: 227 },
      { a0: 253, a1: 347 },
    ]);
  });

  it('keeps every segment a positive sweep when the ring is crowded', () => {
    // n = 20 with a full 26° gap would give a negative sweep; the gap shrinks so
    // each segment keeps the 2° floor.
    const segs = ringSegments(20);
    expect(segs).toHaveLength(20);
    for (const s of segs) {
      expect(s.a1).toBeGreaterThan(s.a0);
    }
    // Uncrowded rings keep the full sketch gap.
    expect(RING_GAP_DEG).toBe(26);
  });

  it('floors a fractional count', () => {
    expect(ringSegments(2.9)).toHaveLength(2);
  });
});

describe('arcPath', () => {
  it('draws a quarter arc from the top to 3 o\'clock (small-arc flag)', () => {
    // a0 = 0° → top (17, 3); a1 = 90° → right (31, 17); sweep 90° ≤ 180 → large 0.
    expect(arcPath(17, 17, 14, 0, 90)).toBe('M 17.00 3.00 A 14 14 0 0 1 31.00 17.00');
  });

  it('sets the large-arc flag once the sweep passes 180°', () => {
    // The single-item ring's 334° sweep.
    expect(arcPath(17, 17, 14, 13, 347)).toContain('A 14 14 0 1 1');
  });
});

describe('segment colouring', () => {
  it('maps done → green, planned → amber, and empty → red (all distinct)', () => {
    expect(segmentStroke(true)).toBe(RING_STROKE_CLASS.done);
    expect(segmentStroke(false)).toBe(RING_STROKE_CLASS.planned);
    expect(RING_STROKE_CLASS.done).toBe('text-status-done-fg');
    expect(RING_STROKE_CLASS.planned).toBe('text-status-progress-fg');
    expect(RING_STROKE_CLASS.empty).toBe('text-danger');
    const all = new Set([RING_STROKE_CLASS.done, RING_STROKE_CLASS.planned, RING_STROKE_CLASS.empty]);
    expect(all.size).toBe(3);
  });

  it('C2 segmentStageClass maps all three stages to distinct colours (suggested = muted)', () => {
    expect(segmentStageClass('done')).toBe(RING_STROKE_CLASS.done);
    expect(segmentStageClass('planned')).toBe(RING_STROKE_CLASS.planned);
    expect(segmentStageClass('suggested')).toBe(RING_STROKE_CLASS.suggested);
    expect(RING_STROKE_CLASS.suggested).toBe('text-honeydew-400');
    // Four distinct classes across the full stage + empty set (no colour collision).
    const all = new Set([RING_STROKE_CLASS.done, RING_STROKE_CLASS.planned, RING_STROKE_CLASS.suggested, RING_STROKE_CLASS.empty]);
    expect(all.size).toBe(4);
  });

  it('segmentStroke(done) delegates to the stage class (2-way completion helper)', () => {
    expect(segmentStroke(true)).toBe(segmentStageClass('done'));
    expect(segmentStroke(false)).toBe(segmentStageClass('planned'));
  });
});
