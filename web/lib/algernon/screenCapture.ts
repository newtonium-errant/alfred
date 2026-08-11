/**
 * Screen capture for the in-app bug reporter (#95).
 *
 * THE ONLY MODULE IN THE APP THAT KNOWS html2canvas EXISTS. Everything else
 * talks to `captureScreen()`, which is why the component tests can mock this
 * file and never load a canvas library at all — and why replacing the capture
 * engine is a change to one file rather than a change to the reporter.
 *
 * That seam is not hypothetical. The two prior ports of this feature used two
 * different engines: honeydew-much uses html2canvas; the mixtape-remix port
 * reimplemented capture from scratch over an SVG `<foreignObject>` with no
 * dependency at all. The capture engine is the part of this pattern that has
 * NOT generalised, so it is isolated here on purpose.
 *
 * BEST-EFFORT, ALWAYS. Every failure path resolves to `null` rather than
 * throwing: a bug report must never be blocked by the machinery that was only
 * trying to make it easier to file. A reporter with no screenshot still has
 * words, and words are the part that matters.
 *
 * WHY THE TIMEOUT IS NOT OPTIONAL. html2canvas walks the entire DOM and can
 * choke or hang outright — modern CSS colour functions (`oklch`), cross-origin
 * images, and very large documents are the known offenders. The capture is
 * therefore RACED against a deadline, and a lost race resolves `null`. The work
 * itself is not cancellable; it simply stops being waited on. Without this the
 * button would appear to do nothing, which is a bad first impression for the
 * feature you reach for when things are already going wrong.
 */

import { downscaleImage } from './imageDownscale';

/**
 * Hard ceiling on one capture attempt. Generous enough for a large page on a
 * slow phone, short enough that a wedged capture doesn't look like a wedged app.
 */
export const CAPTURE_TIMEOUT_MS = 8000;

/**
 * Elements carrying this attribute are excluded from the capture.
 *
 * The reporter's own UI (the button and the dialog) wears it, so the picture
 * shows the screen the operator is complaining about rather than the thing they
 * are complaining through.
 */
export const CAPTURE_IGNORE_ATTR = 'data-report-ignore';

/**
 * Capture the current page as a PNG blob, or `null` on any failure.
 *
 * The result is passed through `downscaleImage` (#82) before being returned, so
 * a retina phone's 3x-scaled page capture is bounded before it is ever base64'd
 * for the wire. That bound is the difference between a routine report and one
 * the box refuses for size, and it costs the reporter nothing.
 */
export async function captureScreen(): Promise<Blob | null> {
  if (typeof document === 'undefined' || typeof window === 'undefined') return null;

  const capture = (async (): Promise<Blob | null> => {
    // Dynamic import keeps html2canvas out of the main bundle: it becomes its
    // own chunk, fetched the first time somebody actually reports something.
    // Most sessions never load it at all.
    const html2canvas = (await import('html2canvas')).default;
    const canvas = await html2canvas(document.body, {
      ignoreElements: (el: Element) =>
        typeof (el as HTMLElement).hasAttribute === 'function'
        && (el as HTMLElement).hasAttribute(CAPTURE_IGNORE_ATTR),
      logging: false,
      useCORS: true,
      backgroundColor: null,
      // Cap the device-pixel multiplier at 2. A 3x phone would otherwise
      // produce a capture with nine times the pixels of the CSS layout, which
      // is a lot of bytes for no extra legibility.
      scale: Math.min(window.devicePixelRatio || 1, 2),
    });
    const blob = await new Promise<Blob | null>((resolve) => {
      canvas.toBlob((b: Blob | null) => resolve(b), 'image/png');
    });
    if (!blob) return null;
    return boundShot(blob);
  })();

  const deadline = new Promise<null>((resolve) => {
    setTimeout(() => resolve(null), CAPTURE_TIMEOUT_MS);
  });

  try {
    return await Promise.race([capture, deadline]);
  } catch {
    // CORS taint, out of memory, unsupported CSS — no shot, no exception.
    return null;
  }
}

/**
 * Bound a capture through the shared downscaler, returning the ORIGINAL on any
 * failure.
 *
 * `downscaleImage` takes and returns a `File`, so the blob is wrapped and
 * unwrapped here. A `File` IS a `Blob`, so the return type is honest either
 * way, and the caller never has to care which path it took.
 */
async function boundShot(blob: Blob): Promise<Blob> {
  try {
    const asFile = new File([blob], 'screen.png', { type: 'image/png' });
    const { file } = await downscaleImage(asFile);
    return file;
  } catch {
    // Downscaling is an optimisation. A broken optimisation must not cost the
    // reporter their screenshot — the box's own size cap is the real backstop.
    return blob;
  }
}

/**
 * Base64-encode a blob for the JSON wire, or `null` when it can't be encoded or
 * would exceed `maxBytes` DECODED.
 *
 * The cap is applied to the blob's real size BEFORE encoding, not to the base64
 * length after: the caller's limit is expressed in image bytes (it mirrors the
 * box's), and checking the inflated string against it would refuse images that
 * are comfortably inside the actual limit.
 */
export async function blobToBase64(blob: Blob, maxBytes: number): Promise<string | null> {
  if (blob.size > maxBytes) return null;
  try {
    const buf = await blob.arrayBuffer();
    const bytes = new Uint8Array(buf);
    let binary = '';
    // Chunked so a multi-megabyte screenshot cannot blow the argument limit of
    // String.fromCharCode with a single spread of a million elements.
    const CHUNK = 0x8000;
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
    }
    return typeof btoa === 'function' ? btoa(binary) : null;
  } catch {
    return null;
  }
}
