/**
 * Turning picked images into carried chat attachments — the caps, the copy, and
 * the preparation, in ONE place (#97).
 *
 * Lifted verbatim out of the legacy chat composer's `addFiles` when the unified
 * composer became a second door onto the same conversation route. That legacy
 * door has since been deleted and `UnifiedComposer` is the only one left, so
 * the drift this module was extracted to prevent is no longer live — but the
 * consolidation is kept rather than inlined, because the caps and their refusal
 * sentences are the answer to "which images may ride a message" and that
 * question is asked from more than the composer. It is the same argument
 * `imagePrepare` makes one layer down, and the one `batchSubmit` names when it
 * says the composer runs the identical preparation.
 *
 * The REFUSAL SENTENCES live here too, not just the numbers. A cap enforced in
 * two places with two wordings is a cap the operator learns twice, and the
 * wording is the part that says which of the three remedies applies.
 *
 * These FE caps are UX-only — `routes_chat.py` re-validates as the fail-loud
 * authority. They exist so a refusal arrives at the picker rather than after the
 * round-trip.
 */

import {
  ALLOWED_IMAGE_MEDIA_TYPES,
  MAX_IMAGE_BYTES,
  MAX_IMAGES_PER_TURN,
  base64DecodedBytes,
  type ImageAttachment,
} from './schemas';
import { prepareImageForUpload } from './imagePrepare';
import { readAsBase64 } from './fileRead';

/** MiB, as the copy states it. */
function mib(bytes: number): number {
  return Math.round(bytes / (1024 * 1024));
}

export function tooManyImagesMessage(max: number = MAX_IMAGES_PER_TURN): string {
  return `You can attach at most ${max} images.`;
}

export const UNSUPPORTED_IMAGE_MESSAGE =
  'Only PNG, JPEG, GIF, or WebP images are supported.';

export function oversizeImageMessage(maxBytes: number = MAX_IMAGE_BYTES): string {
  return `Each image must be under ${mib(maxBytes)} MiB.`;
}

export const UNREADABLE_IMAGE_MESSAGE = 'Could not read that image.';

export interface CollectedImages {
  /** Attachments that passed every gate, in the order picked. */
  accepted: ImageAttachment[];
  /**
   * ONE refusal naming the cap that was hit. Null when everything was accepted.
   * Files accepted before the refusal are still returned — a partial pick beats
   * silently discarding the whole selection.
   */
  error: string | null;
}

export interface CollectOptions {
  /** Attachments already staged — the count cap is judged against the total. */
  alreadyCount?: number;
  maxImages?: number;
  maxImageBytes?: number;
  /** Injected for tests (jsdom has no canvas); defaults to the real helper. */
  prepare?: (file: File, maxBytes: number) => Promise<{ file: File; withinBudget: boolean }>;
  /** Injected for tests; defaults to the real FileReader wrapper. */
  read?: (file: Blob) => Promise<string>;
}

/**
 * Prepare picked images for the conversation route.
 *
 * The ORDER of the gates is load-bearing and matches what shipped: count cap
 * first (and it BREAKS — everything after is refused with it), then the mime
 * allowlist, then downscale-then-step-down, then the decoded-byte gate.
 *
 * `size` is the DECODED byte length — the same quantity the backend caps — so
 * that is the primary size gate; the base64 recheck is belt-and-braces against
 * a reader that inflates.
 */
export async function collectImageAttachments(
  files: File[],
  opts: CollectOptions = {},
): Promise<CollectedImages> {
  const maxImages = opts.maxImages ?? MAX_IMAGES_PER_TURN;
  const maxImageBytes = opts.maxImageBytes ?? MAX_IMAGE_BYTES;
  const prepare = opts.prepare ?? prepareImageForUpload;
  const read = opts.read ?? readAsBase64;

  const accepted: ImageAttachment[] = [];
  let error: string | null = null;
  let count = opts.alreadyCount ?? 0;

  for (const file of files) {
    if (count >= maxImages) {
      error = tooManyImagesMessage(maxImages);
      break;
    }
    const mime = (file.type || '').toLowerCase();
    if (!(ALLOWED_IMAGE_MEDIA_TYPES as readonly string[]).includes(mime)) {
      error = UNSUPPORTED_IMAGE_MESSAGE;
      continue;
    }
    try {
      const { file: prepared, withinBudget } = await prepare(file, maxImageBytes);
      if (!withinBudget) {
        error = oversizeImageMessage(maxImageBytes);
        continue;
      }
      const data = await read(prepared);
      if (base64DecodedBytes(data) > maxImageBytes) {
        error = oversizeImageMessage(maxImageBytes);
        continue;
      }
      // The re-encode can change the media type (a WebP becomes a JPEG), so the
      // block must carry the PREPARED file's type, not the picked one — a
      // mismatch here is what makes the model see a corrupt image.
      const preparedMime = (prepared.type || mime).toLowerCase();
      const outMime = (ALLOWED_IMAGE_MEDIA_TYPES as readonly string[]).includes(preparedMime)
        ? preparedMime
        : mime;
      accepted.push({ media_type: outMime as ImageAttachment['media_type'], data });
      count += 1;
    } catch {
      error = UNREADABLE_IMAGE_MESSAGE;
    }
  }

  return { accepted, error };
}
