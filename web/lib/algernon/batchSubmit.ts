// Bulk scan upload — client-side preparation, caps, and refusal copy (#83).
//
// The operator picks a set of scans and writes ONE instruction that applies to
// all of them. This module owns what the browser does before the bytes leave:
// enforce the caps, downscale each image, and refuse — with words, naming the
// cap that was hit — anything that cannot be sent.
//
// THE CAPS ARE MIRRORED, NOT INVENTED. Each one has a twin on the box
// (`src/alfred/transport/routes_batch.py` / `transport/config.py`), and the BOX
// is the authority: these exist so the operator is told which cap they hit at
// the moment they pick the file, instead of after uploading 128 MiB. A
// front-end cap that is LOWER than the box's silently forbids something the
// system allows; one that is HIGHER produces an upload that is refused after
// the fact. Either way the fix is to move both, which is why each constant
// names its backend twin.

import { ApiError } from './http';

/** Per image. Mirrors `transport.config.DEFAULT_BATCH_MAX_IMAGE_BYTES`. */
export const MAX_BATCH_IMAGE_BYTES = 5 * 1024 * 1024;

/** Per batch, by count. Mirrors `transport.config.DEFAULT_BATCH_MAX_IMAGES`. */
export const MAX_BATCH_IMAGES = 60;

/** Per batch, total bytes. Mirrors `DEFAULT_BATCH_MAX_TOTAL_BYTES`. */
export const MAX_BATCH_TOTAL_BYTES = 128 * 1024 * 1024;

/** Mirrors `DEFAULT_BATCH_MAX_INSTRUCTION_CHARS`. */
export const MAX_BATCH_INSTRUCTION_CHARS = 4000;

/** Mirrors the box's `ALLOWED_SCAN_MEDIA_TYPES`. */
export const ALLOWED_BATCH_MEDIA_TYPES = [
  'image/jpeg',
  'image/png',
  'image/gif',
  'image/webp',
] as const;

/** The `accept` attribute for the picker — same set, in input syntax. */
export const BATCH_UPLOAD_ACCEPT = ALLOWED_BATCH_MEDIA_TYPES.join(',');

const MIB = 1024 * 1024;

/** Human-readable MiB, for copy that must state a real number. */
export function mib(bytes: number): number {
  return Math.round(bytes / MIB);
}

export interface PreparedBatch {
  /** Files to upload, in the order picked. */
  files: File[];
  /** Total bytes after preparation — what the total cap is measured against. */
  totalBytes: number;
  /**
   * One refusal, naming the cap that was hit and the file that hit it. Null
   * when everything was accepted. Files before the refusal are still returned:
   * a partial pick is better than silently discarding the whole selection.
   */
  error: string | null;
  /** Count of picked files that were skipped for their own stated reason. */
  skipped: number;
}

/**
 * Prepare a picked file list for submission.
 *
 * HONEST AT THE EDGE, which is the requirement that shapes this: the Nth
 * over-cap image is refused by NAME and the message says WHICH cap it hit.
 * "Some images could not be added" is the operator-facing form of silence —
 * "send fewer", "send a smaller one" and "split the batch" are three different
 * actions, and a single message for all three cannot be acted on.
 *
 * `prepare` is the shared downscale-then-step-down helper, injected so this
 * stays testable under jsdom (which has no canvas). The default is the real
 * one; the chat Composer runs the identical preparation.
 */
export async function prepareBatch(
  picked: File[],
  prepare: (file: File, maxBytes: number) => Promise<{ file: File; withinBudget: boolean }>,
  opts: {
    maxImages?: number;
    maxImageBytes?: number;
    maxTotalBytes?: number;
    alreadyPicked?: number;
    alreadyBytes?: number;
  } = {},
): Promise<PreparedBatch> {
  const maxImages = opts.maxImages ?? MAX_BATCH_IMAGES;
  const maxImageBytes = opts.maxImageBytes ?? MAX_BATCH_IMAGE_BYTES;
  const maxTotalBytes = opts.maxTotalBytes ?? MAX_BATCH_TOTAL_BYTES;

  const files: File[] = [];
  let totalBytes = opts.alreadyBytes ?? 0;
  let count = opts.alreadyPicked ?? 0;
  let error: string | null = null;
  let skipped = 0;

  for (const file of picked) {
    // COUNT cap. Checked first and reported with the number, so "send fewer"
    // is an instruction rather than a guess.
    if (count >= maxImages) {
      error = `That would be more than ${maxImages} scans in one batch — “${file.name}” and anything after it were not added. Submit these, then start another batch.`;
      break;
    }

    const mime = (file.type || '').toLowerCase();
    if (!(ALLOWED_BATCH_MEDIA_TYPES as readonly string[]).includes(mime)) {
      error = `“${file.name}” is not an image this can read — PNG, JPEG, GIF and WebP only.`;
      skipped += 1;
      continue;
    }

    const { file: readyFile, withinBudget } = await prepare(file, maxImageBytes);
    if (!withinBudget) {
      // PER-IMAGE cap. Distinct remedy: this one image is too heavy even after
      // being downscaled, so the fix is a smaller scan, not a smaller batch.
      error = `“${file.name}” is still over ${mib(maxImageBytes)} MiB after being resized, so it was not added. Try a lower-resolution scan of that page.`;
      skipped += 1;
      continue;
    }

    // TOTAL cap. Third remedy again: nothing is wrong with this image or the
    // count — the batch as a whole is too big.
    if (totalBytes + readyFile.size > maxTotalBytes) {
      error = `Adding “${file.name}” would put this batch over ${mib(maxTotalBytes)} MiB in total, so it and anything after it were not added. Submit these, then start another batch.`;
      break;
    }

    files.push(readyFile);
    totalBytes += readyFile.size;
    count += 1;
  }

  return { files, totalBytes, error, skipped };
}

export interface BatchSubmitResponse {
  /** "queued" = a run was kicked; "saved" = nothing is configured to process it. */
  status: 'queued' | 'saved';
  batch_id: string;
  images: number;
  bytes: number;
  path: string;
  instance: string;
  duplicates?: number;
}

/** Build the multipart body. Field names match the box's part names exactly. */
export function buildBatchForm(instruction: string, files: File[], title = ''): FormData {
  const form = new FormData();
  form.append('instruction', instruction);
  if (title.trim()) form.append('title', title.trim());
  for (const file of files) {
    form.append('images', file, file.name);
  }
  return form;
}

/**
 * Map a relayed refusal to the sentence the operator reads.
 *
 * EXPORTED so the suite can pin the wording, the same reason `friendlyError`
 * in `useIngest` is exported: copy guarded only by a comment survives exactly
 * until someone edits the string.
 *
 * Every cap gets its OWN sentence with its OWN remedy. The three size codes are
 * three different actions and must never collapse into one message — that is
 * the whole reason the box emits three codes rather than one.
 */
export function friendlyBatchError(e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.code) {
      case 'invalid_session':
        return 'Your session has ended — please sign in again.';
      case 'forbidden':
        return 'Bulk upload is owner-only on this instance.';
      case 'image_too_large':
        return `One of those scans is over ${mib(MAX_BATCH_IMAGE_BYTES)} MiB. Try a lower-resolution scan of that page.`;
      case 'too_many_images':
        return `That is more than ${MAX_BATCH_IMAGES} scans in one batch. Send them as two batches.`;
      case 'batch_too_large':
        return `That batch is over ${mib(MAX_BATCH_TOTAL_BYTES)} MiB in total. Split it and send the parts separately.`;
      case 'unsupported_media_type':
        return 'One of those files is not a PNG, JPEG, GIF or WebP image.';
      case 'empty_image':
        return 'One of those files was empty — it may have been moved or deleted since you picked it.';
      case 'no_images':
        return 'No scans were attached — pick at least one before submitting.';
      case 'empty_instruction':
        return 'Write the instruction that applies to every scan in this batch.';
      case 'instruction_too_long':
        return `That instruction is over ${MAX_BATCH_INSTRUCTION_CHARS.toLocaleString()} characters. It is sent with every scan, so it needs to be short.`;
      case 'not_multipart':
        return "That upload didn't arrive in a form the instance could read. Try again — if it keeps happening that's a bug, not something you did.";
      case 'batch_not_configured':
        return "This instance isn't set up to receive bulk uploads yet.";
      case 'vault_not_configured':
        return "This instance isn't set up to receive documents yet.";
      case 'wrong_peer':
        // Same doctrine as the ingest copy: the browser never holds this
        // credential, so telling the operator to sign in again would send them
        // through a logout that cannot fix it. Names no token or check.
        return "That instance refused the request for identity reasons. That's a configuration problem on the server, not your sign-in — signing in again won't change it.";
      case 'batch_create_failed':
      case 'transport_unreachable':
      case 'network_error':
        return "Couldn't reach the instance right now. Your scans were not submitted — try again shortly.";
      case 'transport_misconfigured':
        return 'Bulk upload isn’t configured on this instance yet.';
      default:
        return 'Something went wrong. Please try again.';
    }
  }
  return 'Something went wrong. Please try again.';
}

/**
 * The success line.
 *
 * "queued" and "saved" get DIFFERENT words because they are different facts.
 * A batch that is saved with no campaign enabled will sit at 0 of N forever,
 * and telling the operator it is being processed would send them off to wait
 * for results that cannot arrive. The box distinguishes them; so does this.
 */
export function batchSuccessMessage(result: BatchSubmitResponse): string {
  const n = result.images;
  const scans = n === 1 ? '1 scan' : `${n} scans`;
  const dupes = result.duplicates
    ? ` ${result.duplicates} duplicate${result.duplicates === 1 ? '' : 's'} were folded into one.`
    : '';
  if (result.status === 'saved') {
    return `Batch ${result.batch_id} saved with ${scans}, but nothing is set up to process it yet — the results section will stay empty until the batch campaign is enabled.${dupes}`;
  }
  return `Batch ${result.batch_id} submitted — processing ${scans}. Progress appears in the record and in your brief.${dupes}`;
}
