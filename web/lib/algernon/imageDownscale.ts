/**
 * Client-side image downscaling for chat attachments (#82).
 *
 * WHY THIS EXISTS — the wedge. Anthropic applies a stricter per-image
 * dimension limit once a single request carries more than 20 image (and
 * document) blocks: every image must then be at most 2000px on either edge.
 * Over that, the request is rejected with an `invalid_request_error` naming
 * "many-image requests". A chat turn resends the whole conversation history,
 * so once enough oversized screenshots accumulate the 400 repeats on every
 * subsequent turn and the SESSION WEDGES — retrying cannot clear it, because
 * the offending image is in the history being resent. That is the live
 * incident this fixes (VERA, 2026-08-11, ~24 claims screenshots).
 *
 * WHY 1568 AND NOT 2000. 1568px is the max long edge the STANDARD resolution
 * tier actually consumes — Claude downscales anything larger to fit before
 * processing. VERA runs `claude-sonnet-4-6`, a standard-tier model, so a
 * 1568px long edge is not a quality compromise: it is precisely what the model
 * sees either way. It also leaves a wide margin under the 2000px many-image
 * limit rather than sitting on it.
 *
 * WHAT THIS DOES *NOT* BUY. It does not meaningfully cut image tokens on a
 * standard-tier model — the API caps those at 1568 visual tokens by
 * downscaling regardless. The wins are the wedge above, plus a much smaller
 * base64 payload (a 3000px JPG scan is several times the bytes of its 1568px
 * equivalent), which means faster uploads and more headroom under the 5 MiB
 * per-image and 32 MiB per-request caps. Do not describe this as a token
 * optimisation; on a high-resolution-tier model (Claude 4.7+) it would be one.
 *
 * NEVER BLOCKS THE USER. Every failure path returns the ORIGINAL file rather
 * than throwing: an unreadable image, a browser without canvas, a tainted
 * context, an encoder that returns null. Downscaling is an optimisation, and a
 * broken optimisation must not cost the operator their attachment — the
 * box-side guards still stand behind it.
 */

/** Max long edge, in pixels. The standard tier's native resolution. */
export const MAX_IMAGE_EDGE_PX = 1568;

/**
 * The dimension ceiling Anthropic imposes on requests carrying more than 20
 * image/document blocks. Not our target (we aim at MAX_IMAGE_EDGE_PX, well
 * under it) — it is here so the reason for the target is greppable from the
 * constant, and so a test can assert we stay below it.
 */
export const MANY_IMAGE_DIMENSION_LIMIT_PX = 2000;

/** Quality for re-encoded JPEG/WebP output. */
const REENCODE_QUALITY = 0.9;

export interface Dimensions {
  width: number;
  height: number;
}

/**
 * The dimensions an image should be resized to, preserving aspect ratio.
 *
 * Returns the input unchanged when it already fits — the common case for
 * phone photos and small screenshots, where re-encoding would only lose
 * quality for no benefit.
 *
 * Pure and canvas-free so it is testable under jsdom, which has no canvas
 * implementation (see the module's test file).
 */
export function targetDimensions(
  width: number,
  height: number,
  maxEdge: number = MAX_IMAGE_EDGE_PX,
): Dimensions {
  // Defensive: a decoder that reports a zero/NaN dimension must not produce
  // NaN geometry downstream — leave such an image alone.
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) {
    return { width, height };
  }
  const longEdge = Math.max(width, height);
  if (longEdge <= maxEdge) return { width, height };

  // Pin the long edge to maxEdge EXACTLY and derive only the short edge.
  // Scaling both by maxEdge/longEdge looks equivalent and is not: in floating
  // point `3000 * (1568/3000)` is 1567.999…, which floors to 1567 and leaves
  // the output one pixel under the cap on most inputs. Harmless for quality,
  // but it makes the geometry untestable against an exact value and quietly
  // wrong at the boundary.
  const scale = maxEdge / longEdge;
  // Clamp the short edge to >= 1: a very lopsided ratio (a 4000x1 strip) would
  // otherwise round it to 0 and produce an empty canvas.
  return width >= height
    ? { width: maxEdge, height: Math.max(1, Math.round(height * scale)) }
    : { width: Math.max(1, Math.round(width * scale)), height: maxEdge };
}

/** True when an image of these dimensions would trip the many-image limit. */
export function exceedsManyImageLimit(width: number, height: number): boolean {
  return Math.max(width, height) > MANY_IMAGE_DIMENSION_LIMIT_PX;
}

/**
 * The media type to re-encode as. GIF may be animated and PNG may carry
 * transparency that JPEG would flatten onto black, so both keep their own
 * format; everything else re-encodes as JPEG, which is far smaller for the
 * photographic and screenshot content this path actually carries.
 */
function reencodeType(sourceType: string): string {
  if (sourceType === 'image/png' || sourceType === 'image/gif') return sourceType;
  return 'image/jpeg';
}

async function decode(file: File): Promise<{ bitmap: ImageBitmap; close: () => void } | null> {
  if (typeof createImageBitmap !== 'function') return null;
  try {
    const bitmap = await createImageBitmap(file);
    return { bitmap, close: () => bitmap.close?.() };
  } catch {
    return null;
  }
}

export interface DownscaleResult {
  /** The file to upload — the original when no resize happened or one failed. */
  file: File;
  /** True when the returned file is a resized re-encode of the input. */
  resized: boolean;
  /** Source dimensions, when they could be read. Null when decoding failed. */
  source: Dimensions | null;
}

/**
 * Downscale `file` so neither edge exceeds `maxEdge`, re-encoding via canvas.
 *
 * Returns the original file (with `resized: false`) whenever the image already
 * fits or any step fails — see the module docstring on never blocking the user.
 */
export async function downscaleImage(
  file: File,
  maxEdge: number = MAX_IMAGE_EDGE_PX,
): Promise<DownscaleResult> {
  const decoded = await decode(file);
  if (!decoded) return { file, resized: false, source: null };

  const { bitmap, close } = decoded;
  const source = { width: bitmap.width, height: bitmap.height };
  try {
    const target = targetDimensions(source.width, source.height, maxEdge);
    if (target.width === source.width && target.height === source.height) {
      return { file, resized: false, source };
    }

    const canvas = document.createElement('canvas');
    canvas.width = target.width;
    canvas.height = target.height;
    const ctx = canvas.getContext('2d');
    if (!ctx) return { file, resized: false, source };
    ctx.drawImage(bitmap, 0, 0, target.width, target.height);

    const type = reencodeType(file.type);
    const blob = await new Promise<Blob | null>((resolve) => {
      if (typeof canvas.toBlob !== 'function') { resolve(null); return; }
      canvas.toBlob(resolve, type, REENCODE_QUALITY);
    });
    if (!blob) return { file, resized: false, source };

    return {
      file: new File([blob], file.name, { type: blob.type || type }),
      resized: true,
      source,
    };
  } catch {
    return { file, resized: false, source };
  } finally {
    close();
  }
}
