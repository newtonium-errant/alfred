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

  it('segmentStageClass maps every stage to a distinct colour (suggested = muted, snoozed = caution)', () => {
    expect(segmentStageClass('done')).toBe(RING_STROKE_CLASS.done);
    expect(segmentStageClass('planned')).toBe(RING_STROKE_CLASS.planned);
    expect(segmentStageClass('suggested')).toBe(RING_STROKE_CLASS.suggested);
    expect(segmentStageClass('snoozed')).toBe(RING_STROKE_CLASS.snoozed);
    expect(RING_STROKE_CLASS.suggested).toBe('text-honeydew-400');
    // `caution` is the RATIFIED role for the defer family, not a fresh hue picked
    // here: styles/console.css states it "deliberately covers BOTH the defer
    // family (swipe-up, snooze) and the heavy-verb arm stage … Do not split them
    // into two hues without re-ratifying". Snooze IS the defer verb.
    expect(RING_STROKE_CLASS.snoozed).toBe('text-caution');
    // FIVE distinct classes across the full stage + empty set. Parametrized over
    // the WHOLE family rather than bolted onto the new member: the guard added
    // for snoozed has to cover the incumbents it was not written for.
    const all = new Set(Object.values(RING_STROKE_CLASS));
    expect(all.size).toBe(Object.keys(RING_STROKE_CLASS).length);
    expect(all.size).toBe(5);
  });

  it('a snoozed segment is NOT the done colour — the ring stops painting a delay green', () => {
    // The glance-surface half of the 2026-08-16 report. The panel below the ring
    // said "3/3 done"; the ring itself painted three GREEN segments, because every
    // non-accept verb reached `segmentStageClass('done')`.
    expect(segmentStageClass('snoozed')).not.toBe(segmentStageClass('done'));
    // And it is not silently borrowing planned's paint either — "moved" and "still
    // owed today" are different facts and the ring has to be able to say both.
    expect(segmentStageClass('snoozed')).not.toBe(segmentStageClass('planned'));
  });

  it('segmentStroke(done) delegates to the stage class (2-way completion helper)', () => {
    expect(segmentStroke(true)).toBe(segmentStageClass('done'));
    expect(segmentStroke(false)).toBe(segmentStageClass('planned'));
  });
});
