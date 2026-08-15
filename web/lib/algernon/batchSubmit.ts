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
  /**
   * True when the box REPLAYED an earlier answer for this idempotency key
   * rather than creating a batch (#100). The rest of the body is that original
   * receipt verbatim, so nothing else needs to branch on it — a retry after a
   * lost response shows the operator the batch that already exists.
   */
  deduped?: boolean;
}

// --- Wire-level idempotency (#100) -------------------------------------------
//
// THE FAILURE: a submit lands on the box, the box saves the scans and answers,
// and the ANSWER is lost. The operator sees "couldn't reach the instance" and
// presses Submit again — and without a key on the wire that second request is
// indistinguishable from a first, so a SECOND batch is minted and the drip pays
// for every scan twice. The content-hash dedupe inside `routes_batch` does not
// help: it dedupes within ONE submission, and these are two.
//
// MIRRORS THE CHAT TURN'S S6 CONTRACT in semantics — a client-minted key, the
// SAME key on a retry, and a server that replays its stored answer rather than
// re-acting. It rides a HEADER rather than the body because this submission is
// multipart and the BFF in front of it must not parse it (see the box's
// `alfred.batch.submit_keys` for the full contract, including why there is no
// "same key, different content" branch on this route).

// The SIBLING is `X-Alfred-Stt-Idempotency-Key` (#STT lost-message #2), and this
// follows its naming and its BFF discipline — allowlist ONLY this header, and
// relay it only when well-formed. It diverges on how the key is DERIVED: STT
// content-addresses (the SHA-256 of the audio blob), which needs no client state
// at all, but hashing a 128 MiB staged batch in the browser on every attempt
// would cost more than the whole feature saves. Hence a minted token, held
// against the staged signature below.

/** Request header carrying the key. Mirrors `routes_batch.BATCH_IDEMPOTENCY_HEADER`. */
export const BATCH_IDEMPOTENCY_HEADER = 'X-Alfred-Batch-Idempotency-Key';

/**
 * Well-formed keys: a bounded, header-safe token.
 *
 * Deliberately narrow — a UUID (the normal mint) matches, and so does the
 * fallback mint, while anything carrying whitespace, control characters or a
 * newline does not. The BFF relays a client-supplied value into an outbound
 * request header, and the cheapest way to keep that from ever being a header
 * injection is to allow only characters that cannot express one.
 */
const BATCH_IDEMPOTENCY_KEY_RE = /^[A-Za-z0-9_-]{1,200}$/;

/** True when a value may be relayed as an idempotency key. */
export function isBatchIdempotencyKey(value: unknown): value is string {
  return typeof value === 'string' && BATCH_IDEMPOTENCY_KEY_RE.test(value);
}

/** A minted key and the staged submission it belongs to. */
export interface StagedBatchKey {
  /** Signature of the submission this key was minted for. */
  sig: string;
  /** The key itself — sent on every attempt at THIS staged submission. */
  key: string;
}

/**
 * A cheap, stable signature of a staged submission.
 *
 * PER STAGED SET, NOT PER ATTEMPT is the whole requirement, and this is what
 * makes it self-maintaining: the key is reused for as long as the signature is
 * unchanged, so pressing Submit again after a failure resends the same key
 * without any component having to remember to hold it.
 *
 * Files are identified by name + size + last-modified rather than by content:
 * hashing megabytes on every keystroke would cost far more than it buys, and
 * the question being asked is only "is this the same staged set the operator
 * already tried to send?".
 *
 * The instruction and title are IN the signature deliberately. Editing them
 * makes a new submission, which must mint a new key — otherwise the box would
 * replay the first receipt and the operator's correction would be silently
 * discarded while the UI said it had been sent.
 */
export function stagedBatchSignature(opts: {
  target: string;
  instruction: string;
  files: File[];
  title?: string;
}): string {
  return JSON.stringify([
    opts.target,
    opts.instruction,
    opts.title ?? '',
    opts.files.map((f) => [f.name, f.size, f.lastModified]),
  ]);
}

/** Mint a key. Injectable so tests are deterministic and jsdom needs no crypto. */
function defaultMintKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  // Fallback for an environment without `crypto.randomUUID`. Uniqueness is what
  // matters here, not unpredictability: the key is a dedupe token the operator's
  // own session mints for itself, never a credential.
  return `batch-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * The key to send for this staged submission — the held one when nothing has
 * changed, a fresh one when it has.
 *
 * Callers keep the returned value (in a ref) and pass it back next time. The
 * comparison is what makes a retry idempotent and an edit-then-resend honest.
 */
export function keyForStagedBatch(
  held: StagedBatchKey | null,
  sig: string,
  mint: () => string = defaultMintKey,
): StagedBatchKey {
  if (held && held.sig === sig) return held;
  return { sig, key: mint() };
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
 * POST one staged batch. The single wire call both batch surfaces make.
 *
 * LIFTED HERE because there are two callers, not one: the standalone /batch
 * page and the unified composer's image chip. It was written out twice when the
 * composer arrived (#97) — same URL, same header, same three reasons below —
 * which is one place for the two to drift apart on a detail whose failure mode
 * is a body the box cannot parse. `buildBatchForm` above is the other half of
 * this call; they belong together.
 *
 * Three things about this request are load-bearing and none are obvious:
 *
 *   * The target rides the QUERY STRING. The body is raw multipart bytes the
 *     BFF relays without parsing, so a form field would force it to parse.
 *   * NO Content-Type header. The browser must set it so the multipart boundary
 *     is generated; setting it by hand produces a boundary-less body the box
 *     cannot read, and the failure looks like an empty batch rather than a
 *     malformed request.
 *   * The idempotency key (#100) rides a header, which is safe for exactly the
 *     reason Content-Type is not: only Content-Type carries the boundary.
 *
 * Throws `ApiError` so each caller maps the outcome its own way — the composer
 * marks one chip and leaves the others alone, the page shows a banner and
 * bounces a 401 to /login. Both spend `friendlyBatchError` for the words.
 */
export async function submitBatch(
  target: string,
  form: FormData,
  idempotencyKey?: string,
): Promise<BatchSubmitResponse> {
  const res = await fetch(`/api/batch/submit?target=${encodeURIComponent(target)}`, {
    method: 'POST',
    ...(idempotencyKey ? { headers: { [BATCH_IDEMPOTENCY_HEADER]: idempotencyKey } } : {}),
    body: form,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(res.status, body?.error ?? 'network_error');
  return body as BatchSubmitResponse;
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
