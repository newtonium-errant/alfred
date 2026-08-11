/**
 * #82 — client-side image downscale.
 *
 * The geometry is tested as a pure function because jsdom has no canvas: it is
 * the part that has to be right, and testing it through a canvas stub would
 * only assert the stub. The orchestration around it is tested for the property
 * that actually matters operationally — it NEVER throws and never loses the
 * user's file, whatever the browser does.
 */
import { describe, expect, it, vi } from 'vitest';

import {
  MANY_IMAGE_DIMENSION_LIMIT_PX,
  MAX_IMAGE_EDGE_PX,
  downscaleImage,
  exceedsManyImageLimit,
  targetDimensions,
} from '../lib/algernon/imageDownscale';

function imageFile(name = 'shot.png', type = 'image/png', size = 32): File {
  const bytes = new Uint8Array(size);
  bytes.set([0x89, 0x50, 0x4e, 0x47], 0); // \x89PNG
  return new File([bytes], name, { type });
}

describe('targetDimensions', () => {
  it('leaves an image that already fits untouched', () => {
    // The common case (phone photos, small screenshots). Re-encoding here
    // would cost quality for no benefit, so the caller checks for equality to
    // decide whether to skip the canvas round-trip entirely.
    expect(targetDimensions(800, 600)).toEqual({ width: 800, height: 600 });
    expect(targetDimensions(MAX_IMAGE_EDGE_PX, 100)).toEqual({
      width: MAX_IMAGE_EDGE_PX,
      height: 100,
    });
  });

  it('scales the long edge down to the cap, preserving aspect ratio', () => {
    // A 4000x2667 desktop scan — above the cap on both tiers.
    const out = targetDimensions(4000, 2667);
    expect(out.width).toBe(MAX_IMAGE_EDGE_PX);
    // 2667 * (2576/4000) = 1717.5 -> round
    expect(out.height).toBe(1718);
    // Aspect ratio preserved within a pixel of rounding.
    expect(Math.abs(out.width / out.height - 4000 / 2667)).toBeLessThan(0.01);
  });

  it('scales by the LONG edge regardless of orientation', () => {
    // Portrait: the height is the constraint. Getting this backwards would
    // leave portrait scans oversized — the exact images a claims workflow has.
    const out = targetDimensions(2667, 4000);
    expect(out.height).toBe(MAX_IMAGE_EDGE_PX);
    expect(out.width).toBe(1718);
  });

  it('caps every output at the tier resolution, whatever comes in', () => {
    // Note what this does NOT claim: outputs are NOT under the 2000px
    // many-image limit, and deliberately so — 2576 is above it. The wedge is
    // held by the history trim bounding the image COUNT to 12 (threshold >20),
    // not by this constant. See the module docstring.
    for (const [w, h] of [
      [4000, 2667], [8000, 8000], [3001, 10], [10, 3001], [4000, 1],
    ]) {
      const out = targetDimensions(w, h);
      expect(Math.max(out.width, out.height)).toBeLessThanOrEqual(MAX_IMAGE_EDGE_PX);
    }
  });

  it('never produces a zero edge on an extreme aspect ratio', () => {
    // 4000x1 would floor the short edge to 0 and yield an empty canvas.
    const out = targetDimensions(4000, 1);
    expect(out.height).toBeGreaterThanOrEqual(1);
    expect(out.width).toBe(MAX_IMAGE_EDGE_PX);
  });

  it('passes through nonsense dimensions rather than emitting NaN', () => {
    // A decoder reporting 0 or NaN must not produce NaN geometry downstream.
    expect(targetDimensions(0, 0)).toEqual({ width: 0, height: 0 });
    expect(targetDimensions(NaN, 100)).toEqual({ width: NaN, height: 100 });
  });

  it('honours an explicit maxEdge', () => {
    expect(targetDimensions(4000, 2000, 1000)).toEqual({ width: 1000, height: 500 });
  });
});

describe('exceedsManyImageLimit', () => {
  it('is the documented 2000px threshold, on either edge', () => {
    expect(MANY_IMAGE_DIMENSION_LIMIT_PX).toBe(2000);
    expect(exceedsManyImageLimit(2000, 2000)).toBe(false);
    expect(exceedsManyImageLimit(2001, 10)).toBe(true);
    expect(exceedsManyImageLimit(10, 2001)).toBe(true);
  });

  it('is BELOW our downscale target — the trim, not this cap, holds the wedge', () => {
    // This assertion was inverted in the first ship, when the target was 1568
    // and the reasoning was "stay under the many-image ceiling". That framing
    // was wrong twice over: it cost the high-res-tier instances ~1.64x linear
    // resolution, and it credited the wrong guard. The >20-block condition is
    // unreachable because conversation.MAX_HISTORY_IMAGE_BLOCKS trims every
    // request to 12 images total, so the dimension ceiling never applies and
    // the target is free to sit at the tier's native resolution.
    expect(MAX_IMAGE_EDGE_PX).toBeGreaterThan(MANY_IMAGE_DIMENSION_LIMIT_PX);
  });

  it('pins the tier resolution as an exact value', () => {
    // Exact-value pin: 2576 is the HIGH-RESOLUTION tier's long edge (Claude
    // 4.7+, which KAL-LE and Hypatia run). Lossless on the standard tier too,
    // which self-downscales to 1568. Revisit only when a tier above 2576 ships.
    expect(MAX_IMAGE_EDGE_PX).toBe(2576);
  });
});

describe('downscaleImage — never costs the user their attachment', () => {
  it('returns the original when the browser has no createImageBitmap', async () => {
    // jsdom's baseline. Also the real state of older browsers.
    vi.stubGlobal('createImageBitmap', undefined);
    try {
      const file = imageFile();
      const out = await downscaleImage(file);
      expect(out.file).toBe(file);
      expect(out.resized).toBe(false);
      expect(out.source).toBeNull();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('returns the original when decoding throws', async () => {
    // A corrupt or unsupported image must degrade to "send it as-is" and let
    // the server decide — not throw out of addFiles and lose the whole batch.
    vi.stubGlobal('createImageBitmap', vi.fn().mockRejectedValue(new Error('bad')));
    try {
      const file = imageFile();
      const out = await downscaleImage(file);
      expect(out.file).toBe(file);
      expect(out.resized).toBe(false);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('returns the original when the image already fits (no canvas needed)', async () => {
    // Proves the early return happens BEFORE any canvas work — under jsdom a
    // canvas round-trip would fail, so reaching this assertion is the evidence.
    vi.stubGlobal(
      'createImageBitmap',
      vi.fn().mockResolvedValue({ width: 800, height: 600, close: () => {} }),
    );
    try {
      const file = imageFile();
      const out = await downscaleImage(file);
      expect(out.file).toBe(file);
      expect(out.resized).toBe(false);
      expect(out.source).toEqual({ width: 800, height: 600 });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('returns the original when the canvas 2d context is unavailable', async () => {
    // jsdom's actual behaviour for an oversized image: getContext returns null.
    // This is the path a real browser hits with a tainted or blocked canvas.
    vi.stubGlobal(
      'createImageBitmap',
      vi.fn().mockResolvedValue({ width: 3000, height: 2000, close: () => {} }),
    );
    try {
      const file = imageFile();
      const out = await downscaleImage(file);
      expect(out.file).toBe(file);
      expect(out.resized).toBe(false);
      // Dimensions were still read — the caller can log what it saw.
      expect(out.source).toEqual({ width: 3000, height: 2000 });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('closes the decoded bitmap even when the resize path bails', async () => {
    // ImageBitmaps hold decoded pixels; leaking one per attachment is a real
    // memory cost in a session with dozens of scans.
    const close = vi.fn();
    vi.stubGlobal(
      'createImageBitmap',
      vi.fn().mockResolvedValue({ width: 3000, height: 2000, close }),
    );
    try {
      await downscaleImage(imageFile());
      expect(close).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
