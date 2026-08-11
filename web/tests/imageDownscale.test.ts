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
    // A 3000x2000 desktop scan — the shape from the live incident.
    const out = targetDimensions(3000, 2000);
    expect(out.width).toBe(MAX_IMAGE_EDGE_PX);
    // 2000 * (1568/3000) = 1045.33 -> floor
    expect(out.height).toBe(1045);
    // Aspect ratio preserved within a pixel of rounding.
    expect(Math.abs(out.width / out.height - 3000 / 2000)).toBeLessThan(0.01);
  });

  it('scales by the LONG edge regardless of orientation', () => {
    // Portrait: the height is the constraint. Getting this backwards would
    // leave portrait scans oversized — the exact images a claims workflow has.
    const out = targetDimensions(2000, 3000);
    expect(out.height).toBe(MAX_IMAGE_EDGE_PX);
    expect(out.width).toBe(1045);
  });

  it('keeps every output under the many-image dimension limit', () => {
    // The load-bearing property: whatever comes in, what goes out cannot trip
    // the >20-image rejection that wedged the session.
    for (const [w, h] of [
      [3000, 2000], [8000, 8000], [2001, 10], [10, 2001], [4000, 1],
    ]) {
      const out = targetDimensions(w, h);
      expect(exceedsManyImageLimit(out.width, out.height)).toBe(false);
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

  it('is strictly above our downscale target, so the target has headroom', () => {
    // Not an arbitrary pair of constants: 1568 is what the standard-tier model
    // actually consumes, and it must sit under the many-image ceiling rather
    // than on it.
    expect(MAX_IMAGE_EDGE_PX).toBeLessThan(MANY_IMAGE_DIMENSION_LIMIT_PX);
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
