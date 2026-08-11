/**
 * Client-side image downscaling for chat attachments (#82).
 *
 * WHY THIS EXISTS — the wedge. Anthropic applies a stricter per-image
 * dimension limit once a single request carries more than 20 image blocks
 * (document blocks share that count on some platforms, though not on this
 * one): every image must then be at most 2000px on either edge.
 * Over that, the request is rejected with an `invalid_request_error` naming
 * "many-image requests". A chat turn resends the whole conversation history,
 * so once enough oversized screenshots accumulate the 400 repeats on every
 * subsequent turn and the SESSION WEDGES — retrying cannot clear it, because
 * the offending image is in the history being resent. That is the live
 * incident this fixes (VERA, 2026-08-11, ~24 claims screenshots).
 *
 * WHY 2576 — AND WHY THE ANSWER IS PER-TIER. Claude has two vision resolution
 * tiers, and THIS ONE COMPOSER FEEDS INSTANCES ON BOTH, so a single constant
 * has to be judged against each:
 *
 *   HIGH-RESOLUTION tier (Claude 4.7 and later) — 2576px long edge, up to 4784
 *     visual tokens. KAL-LE and Hypatia run `claude-opus-4-7`. A 2576px input
 *     is consumed at full fidelity.
 *   STANDARD tier (everything else) — 1568px long edge, capped at 1568 visual
 *     tokens. Salem/VERA runs `claude-sonnet-4-6`. A 2576px input is
 *     downscaled BY THE API to 1568 before processing.
 *
 * So 2576 is lossless on both tiers today: high-res gets its native
 * resolution, standard gets exactly what it would have got from a 1568px
 * upload because the API does that downscale itself. 1568 was NOT neutral —
 * it silently discarded ~1.64x linear resolution (2576/1568) from KAL-LE's and
 * Hypatia's dense scans, which is the whole reason this constant moved.
 *
 * REVISIT WHEN: a tier above 2576px ships. Then this becomes the old mistake
 * pointed the other way — a cap set to yesterday's top tier, quietly throwing
 * away detail for whoever runs the newer models. The check is the long-edge
 * column of the vision docs' resolution-tier table, not this comment.
 *
 * TOKENS: this is not a token optimisation on the standard tier (the API caps
 * those at 1568 regardless of what we send) and raising the cap to 2576 does
 * increase tokens on the HIGH-RES tier, up to 4784/image — that is the cost of
 * the fidelity, paid deliberately. What downscaling always buys is a much
 * smaller base64 payload than a raw 3000-4000px scan: faster uploads, and
 * headroom under the 5 MiB per-image and 32 MiB per-request caps.
 *
 * WHY THE 2000px MANY-IMAGE CAP IS NOT A CONSTRAINT ON THIS VALUE. 2576 is
 * above 2000, which would matter only if a request could ever carry more than
 * 20 image blocks. It cannot: `conversation.MAX_HISTORY_IMAGE_BLOCKS` trims
 * every outgoing request to 12 image blocks TOTAL — the trim runs after the
 * new turn is appended, so the current turn's images are counted too — and on
 * this platform document blocks contribute nothing to that count. 12 against a
 * threshold of >20 is the margin, and it is the trim, not this constant, that
 * holds it.
 *
 * NEVER BLOCKS THE USER. Every failure path returns the ORIGINAL file rather
 * than throwing: an unreadable image, a browser without canvas, a tainted
 * context, an encoder that returns null. Downscaling is an optimisation, and a
 * broken optimisation must not cost the operator their attachment. The real
 * backstop is the history trim named above — it bounds the image COUNT, which
 * is what makes the dimension limit unreachable no matter what any client
 * uploads. The byte and per-turn-count caps in `schemas.ts` are input
 * validation, not a wedge guard; don't mistake them for one.
 */

/**
 * Max long edge, in pixels: the HIGH-RESOLUTION tier's native resolution.
 * Lossless on both tiers today — see the per-tier note above before changing.
 */
export const MAX_IMAGE_EDGE_PX = 2576;

/**
 * The dimension ceiling Anthropic imposes on requests carrying more than 20
 * image blocks (document blocks share the count on some platforms, though not
 * on this one). Kept as a named constant so the threshold this whole fix is
 * about is greppable — but note MAX_IMAGE_EDGE_PX is deliberately ABOVE it,
 * because the history trim makes the >20 condition unreachable. See the
 * per-tier note in the module docstring.
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
