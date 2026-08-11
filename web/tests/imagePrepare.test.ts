import { describe, expect, it, vi, beforeEach } from 'vitest';

/**
 * #83 — the shared upload preparation, extracted from the chat Composer so the
 * bulk scan form runs the SAME sequence.
 *
 * Two properties are pinned, and they are exactly the two a second copy would
 * drift on: the ORDER (downscale BEFORE the size gate) and the SINGLE step-down
 * retry. `downscaleImage` is mocked so the composition is observable — under
 * jsdom the real one is a no-op (no canvas), so a test using it could not tell
 * "downscaled then gated" from "gated then downscaled".
 *
 * The Composer's own wiring pin lives in `composerDownscale.test.tsx`; this
 * file covers the helper both doors now share.
 */

const downscaleImage = vi.hoisted(() => vi.fn());

vi.mock('../lib/algernon/imageDownscale', () => ({
  downscaleImage: (file: File, edge?: number, quality?: number) =>
    downscaleImage(file, edge, quality),
  STEP_DOWN_EDGE_PX: 1800,
  STEP_DOWN_QUALITY: 0.72,
}));

import { prepareImageForUpload } from '../lib/algernon/imagePrepare';

const STEP_DOWN_EDGE_PX = 1800;
const STEP_DOWN_QUALITY = 0.72;

function sized(name: string, bytes: number): File {
  const f = new File([new Uint8Array(1)], name, { type: 'image/jpeg' });
  Object.defineProperty(f, 'size', { value: bytes });
  return f;
}

beforeEach(() => downscaleImage.mockReset());

describe('prepareImageForUpload', () => {
  it('downscales BEFORE gating, so a big-but-shrinkable scan is accepted', async () => {
    // THE order pin. A 9 MiB 4000px scan is over a 5 MiB cap at full size and
    // under it once capped at the tier resolution. Gating first would reject an
    // image that uploads perfectly well — the inversion this guards.
    const shrunk = sized('scan.jpg', 2_000_000);
    downscaleImage.mockResolvedValueOnce({ file: shrunk, resized: true, source: null });

    const r = await prepareImageForUpload(sized('scan.jpg', 9_000_000), 5_000_000);

    expect(downscaleImage).toHaveBeenCalledTimes(1);
    expect(r.file).toBe(shrunk);
    expect(r.withinBudget).toBe(true);
  });

  it('steps down ONCE, with the step-down edge and quality', async () => {
    // A dense full-page scan can clear the byte cap even at the tier edge.
    // Refusing over a budget we can re-encode into would break the module's
    // never-blocks-the-user principle.
    const stillBig = sized('dense.jpg', 6_000_000);
    const stepped = sized('dense.jpg', 3_000_000);
    downscaleImage
      .mockResolvedValueOnce({ file: stillBig, resized: true, source: null })
      .mockResolvedValueOnce({ file: stepped, resized: true, source: null });

    const r = await prepareImageForUpload(sized('dense.jpg', 9_000_000), 5_000_000);

    expect(downscaleImage).toHaveBeenCalledTimes(2);
    expect(downscaleImage.mock.calls[1][1]).toBe(STEP_DOWN_EDGE_PX);
    expect(downscaleImage.mock.calls[1][2]).toBe(STEP_DOWN_QUALITY);
    expect(r.file).toBe(stepped);
    expect(r.withinBudget).toBe(true);
  });

  it('does NOT step down when the first pass already fits', async () => {
    // One retry, not a loop — and not an unconditional second re-encode, which
    // would cost quality on every image for nothing.
    downscaleImage.mockResolvedValueOnce({
      file: sized('a.jpg', 100), resized: false, source: null,
    });
    await prepareImageForUpload(sized('a.jpg', 100), 5_000_000);
    expect(downscaleImage).toHaveBeenCalledTimes(1);
  });

  it('gives up after ONE retry and reports withinBudget=false', async () => {
    // The honest outcome: the caller refuses with words rather than uploading
    // something the box will reject after the fact. A loop here would make the
    // operator wait instead of telling them.
    const big = sized('huge.jpg', 8_000_000);
    downscaleImage
      .mockResolvedValueOnce({ file: big, resized: true, source: null })
      .mockResolvedValueOnce({ file: big, resized: true, source: null });

    const r = await prepareImageForUpload(sized('huge.jpg', 9_000_000), 5_000_000);

    expect(downscaleImage).toHaveBeenCalledTimes(2);
    expect(r.withinBudget).toBe(false);
  });

  it('accepts the ORIGINAL when downscaling failed but it already fits', async () => {
    // downscaleImage never throws — it hands back the original on any failure,
    // which then meets the same gate it would have met anyway.
    const original = sized('a.jpg', 100);
    downscaleImage.mockResolvedValueOnce({ file: original, resized: false, source: null });
    const r = await prepareImageForUpload(original, 5_000_000);
    expect(r.file).toBe(original);
    expect(r.withinBudget).toBe(true);
  });
});
