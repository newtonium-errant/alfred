/**
 * The unified composer's send: preparing each attachment for the route its chip
 * names, running those routes, and composing the turn that follows (#97).
 *
 * PARTIAL-FAILURE HONESTY is the requirement that shapes this file. One
 * attachment's refusal must not sink the others: every job runs inside its own
 * try/catch and produces its own outcome, so a batch the instance refused sits
 * beside a document that filed perfectly, each with its own sentence. The
 * failure this replaces is the one where a fan-out throws on job 2 and jobs 3
 * and 4 never run — with nothing on screen to say which of the four happened.
 *
 * EACH ROUTE'S OWN CAPS AND REFUSAL COPY ARE REUSED, NOT RE-WRITTEN.
 * `prepareUpload` / `preparePdfUpload` (#57), `prepareBatch` (#83) and
 * `friendlyError` / `friendlyBatchError` are imported and called — the caps are
 * named contracts, and a second wording of "that PDF has no selectable text" is
 * a second thing to keep in sync with an operator ruling.
 *
 * Pure except where a `File` must actually be read; the network calls arrive as
 * injected dependencies, so the fan-out's ordering and isolation are testable
 * without a server.
 */

import {
  MAX_MESSAGE_CHARS,
  type IngestBody,
} from './schemas';
import {
  isCsvFilename,
  prepareUpload,
  preparePdfUpload,
  readFailedMessage,
  type PreparedUpload,
} from './ingestUpload';
import {
  batchSuccessMessage,
  friendlyBatchError,
  type BatchSubmitResponse,
} from './batchSubmit';
import { friendlyError as friendlyIngestError } from './useIngest';
import { readAsBytes, readAsText, readErrorName } from './fileRead';
import { isPdf } from './composerRouting';
import type { IngestSubmitResponse } from './types';

// --- attachment preparation -------------------------------------------------

/**
 * A document read and prepared ONCE, at attach time, for both routes it might
 * take.
 *
 * Reading at attach rather than at send is what lets a refusal appear beside the
 * affordance that refused it — the operator learns a 300 KB CSV is over the
 * ingest limit while looking at the picker, not after pressing Send. It is also
 * the only way the Discuss chip can honestly know whether the text FITS in a
 * message before the operator picks it.
 */
export interface PreparedTextDoc {
  ok: true;
  format: 'text';
  /** Body text for the vault write — CSV fenced per #57, .md/.txt verbatim. */
  body: string;
  /** Disclosure of any reshape (the CSV row count), or null. */
  note: string | null;
}

export interface PreparedPdfDoc {
  ok: true;
  format: 'pdf';
  /** Base64 bytes; the BOX extracts the text (never the browser). */
  bodyB64: string;
  note: string | null;
  bytes: number;
}

export interface UnpreparedDoc {
  ok: false;
  message: string;
}

export type PreparedDoc = PreparedTextDoc | PreparedPdfDoc | UnpreparedDoc;

/** Read + prepare one picked document. Never throws — a read failure is words. */
export async function prepareDoc(file: File): Promise<PreparedDoc> {
  // Normalise the name ONCE: CSV detection trims, so deriving the title and
  // source from the untrimmed name would let a padded filename be treated as a
  // CSV while the source field kept the padding. One string, one answer.
  const name = file.name.trim();
  try {
    if (isPdf(name)) {
      const prepared = preparePdfUpload(name, await readAsBytes(file));
      if (!prepared.ok) return { ok: false, message: prepared.message };
      return {
        ok: true,
        format: 'pdf',
        bodyB64: prepared.bodyB64,
        note: prepared.note,
        bytes: prepared.bytes,
      };
    }
    const prepared: PreparedUpload = prepareUpload(name, await readAsText(file));
    if (!prepared.ok) return { ok: false, message: prepared.message };
    return { ok: true, format: 'text', body: prepared.body, note: prepared.note };
  } catch (e) {
    // A read can fail AFTER the picker accepted the file (moved, deleted,
    // permissions, media error). Without words here the operator picks a file
    // and the composer does nothing at all — the one silence this path exists
    // to remove, and the hardest to diagnose because it looks like a no-op.
    return { ok: false, message: readFailedMessage(name, readErrorName(e)) };
  }
}

/** Text a transcribed recording contributes — the same shape a .txt would. */
export function preparedFromTranscript(
  transcript: string,
): PreparedTextDoc | UnpreparedDoc {
  const body = transcript.trim();
  if (!body) {
    return {
      ok: false,
      message:
        'Nothing was transcribed from that recording — there is no text to file. Try recording again, or type it yourself.',
    };
  }
  return { ok: true, format: 'text', body, note: null };
}

// --- what rides the conversation --------------------------------------------

/**
 * Headroom kept for the operator's own words (and the filed-record line) when
 * judging whether a document can be QUOTED into a message.
 *
 * A document that exactly filled the message cap would leave the operator no
 * room to say what they wanted asked about it — a technically-sendable turn
 * that is useless. Reserving a fixed slice means the Discuss chip is offered
 * only when there is a conversation left to have.
 */
export const INLINE_RESERVE_CHARS = 1000;

/** The largest document body that may be quoted into a turn. */
export const MAX_INLINE_DOC_CHARS = MAX_MESSAGE_CHARS - INLINE_RESERVE_CHARS;

/**
 * Refusal for a document too long to quote — with the remedy that is actually
 * available, which is the OTHER chip rather than "trim it".
 */
export function inlineTooLongMessage(filename: string, chars: number): string {
  return `${filename} is ${chars.toLocaleString()} characters — too long to quote into a message (the limit is ${MAX_INLINE_DOC_CHARS.toLocaleString()}). File it to the vault instead; this message can still ask about it.`;
}

/**
 * One document, as it appears inside the turn.
 *
 * A CSV arrives already fenced (`prepareUpload` did it, with the fence grown
 * past any backtick run in the content) so it renders as a block rather than as
 * a wall of commas — the same treatment #85's read side gives it. Everything
 * else is quoted as-is under its filename, because a .md that gets wrapped in a
 * fence stops being markdown.
 */
export function inlineDocBlock(filename: string, body: string): string {
  return `${filename}:\n\n${body}`;
}

/** Whether a prepared text doc is short enough to quote. */
export function canInline(doc: PreparedDoc): boolean {
  return doc.ok === true && doc.format === 'text' && doc.body.length <= MAX_INLINE_DOC_CHARS;
}

/** How many paths the filed-record line names before it summarises. */
export const FILED_CONTEXT_MAX_PATHS = 10;

/**
 * The line that tells the assistant what just landed in the vault.
 *
 * It rides IN the message rather than in a side-channel field, which is a
 * deliberate choice twice over: no new wire contract is needed (the ratified
 * shape says no new transport routes), and the operator SEES it in their own
 * bubble. A hidden payload attached to a message the operator thinks they wrote
 * is exactly the kind of thing that later reads as the assistant knowing
 * something it was never told.
 */
export function buildFiledContextLine(paths: string[]): string {
  if (paths.length === 0) return '';
  const shown = paths.slice(0, FILED_CONTEXT_MAX_PATHS);
  const rest = paths.length - shown.length;
  const list = shown.map((p) => `\`${p}\``).join(', ');
  const more = rest > 0 ? `, and ${rest} more` : '';
  const noun = paths.length === 1 ? 'this' : 'these';
  return `(Just filed to the vault: ${list}${more} — read ${noun} if it helps answer.)`;
}

export interface ComposedTurn {
  /** The message to send. Empty when there is nothing to say. */
  message: string;
  /** False when the filed-record line did not fit and must wait for a shorter turn. */
  filedCarried: boolean;
  /** True when the operator's own content alone is over the cap — refuse the send. */
  overLimit: boolean;
}

/**
 * Build the message the turn actually carries.
 *
 * ORDER OF SACRIFICE, stated because it is a judgement and not an accident: the
 * operator's words and their quoted documents are never trimmed to make room —
 * if those alone exceed the cap the whole send is refused, with words, before
 * anything is submitted. It is the FILED-RECORD LINE that gives way, because it
 * is the only part that can be carried by a later turn without loss (the caller
 * keeps the paths and offers them to the next message).
 */
export function composeTurnMessage(opts: {
  text: string;
  inlineBlocks?: string[];
  filedPaths?: string[];
  limit?: number;
}): ComposedTurn {
  const limit = opts.limit ?? MAX_MESSAGE_CHARS;
  const parts = [opts.text.trim(), ...(opts.inlineBlocks ?? [])].filter((p) => p.length > 0);
  const base = parts.join('\n\n');
  if (base.length > limit) {
    return { message: base, filedCarried: false, overLimit: true };
  }
  const paths = opts.filedPaths ?? [];
  if (paths.length === 0) return { message: base, filedCarried: false, overLimit: false };
  const line = buildFiledContextLine(paths);
  const candidate = base ? `${base}\n\n${line}` : line;
  if (candidate.length <= limit) {
    return { message: candidate, filedCarried: true, overLimit: false };
  }
  return { message: base, filedCarried: false, overLimit: false };
}

/** Refusal when the operator's own content is over the turn cap. */
export function turnOverLimitMessage(chars: number, limit: number = MAX_MESSAGE_CHARS): string {
  return `That message is ${(chars - limit).toLocaleString()} characters over the ${limit.toLocaleString()}-character limit — nothing was sent. Shorten it, or file the attached text to the vault instead of quoting it.`;
}

/** What the operator is told when a filed path could not ride this turn. */
export const FILED_CONTEXT_DEFERRED_MESSAGE =
  'Filed — but this message was too full to name the new record in it. Your next message will carry it.';

// --- the fan-out ------------------------------------------------------------

export type FanoutJob =
  | { id: string; route: 'file'; payload: IngestBody }
  | {
      id: string;
      route: 'batch';
      target: string;
      instruction: string;
      files: File[];
      title?: string;
      /**
       * Wire-level idempotency key for this batch (#100) — minted per STAGED
       * SET by the composer and resent unchanged on a retry, so a submit whose
       * response was lost does not mint a second batch.
       *
       * NOTE that `id` is NOT this: `id` is the CHIP's identifier, used as the
       * key of the outcomes map so each chip gets its own sentence. It is
       * generated per chip and means nothing to the box.
       */
      idempotencyKey?: string;
    };

export interface FanoutOutcome {
  status: 'done' | 'failed';
  /** The sentence shown on the chip. Never empty (intentionally-left-blank). */
  message: string;
  /** The created record path, when one was created. */
  path?: string;
}

export interface FanoutDeps {
  submitIngest: (payload: IngestBody) => Promise<IngestSubmitResponse>;
  submitBatch: (job: {
    target: string;
    instruction: string;
    files: File[];
    title?: string;
    idempotencyKey?: string;
  }) => Promise<BatchSubmitResponse>;
}

/** The success line for a filed document — the ingest page's own words. */
export function ingestSuccessMessage(r: IngestSubmitResponse): string {
  const verb = r.status === 'exists' ? 'Already present' : 'Ingested';
  return `${verb} in ${r.instance} as ${r.record_type} — ${r.path}`;
}

export interface FanoutResult {
  outcomes: Record<string, FanoutOutcome>;
  /** Paths created by this fan-out, in job order, for the turn's context line. */
  filedPaths: string[];
}

/**
 * Run every non-conversation job, one after another.
 *
 * SEQUENTIAL, deliberately. A batch submission is up to 128 MiB; firing several
 * at once would have them contend for the same uplink and turn one slow upload
 * into several. It also makes the filed-path order match the chip order the
 * operator is looking at, which is what the turn's context line then names.
 *
 * Every job is isolated: a throw becomes that job's own failure sentence and the
 * next job still runs. That isolation is the whole contract of this function —
 * a version that let one rejection escape would pass every per-route test and
 * still lose three attachments to the first bad one.
 */
export async function runFanout(
  jobs: FanoutJob[],
  deps: FanoutDeps,
): Promise<FanoutResult> {
  const outcomes: Record<string, FanoutOutcome> = {};
  const filedPaths: string[] = [];

  for (const job of jobs) {
    try {
      if (job.route === 'file') {
        const r = await deps.submitIngest(job.payload);
        outcomes[job.id] = {
          status: 'done',
          message: ingestSuccessMessage(r),
          path: r.path,
        };
        if (r.path) filedPaths.push(r.path);
      } else {
        const r = await deps.submitBatch({
          target: job.target,
          instruction: job.instruction,
          files: job.files,
          title: job.title,
          idempotencyKey: job.idempotencyKey,
        });
        outcomes[job.id] = {
          status: 'done',
          message: batchSuccessMessage(r),
          path: r.path,
        };
        // A batch record is NOT offered to the turn as readable context: it is
        // a manifest whose results arrive later, on the drip schedule, so
        // pointing the assistant at it now would invite it to report on an
        // empty record as though the work were done.
      }
    } catch (e) {
      outcomes[job.id] = {
        status: 'failed',
        message:
          job.route === 'file' ? friendlyIngestError(e) : friendlyBatchError(e),
      };
    }
  }

  return { outcomes, filedPaths };
}

/** The ingest payload for one prepared document. */
export function ingestPayloadFor(opts: {
  target: string;
  recordType: string;
  title: string;
  source: string;
  doc: Extract<PreparedDoc, { ok: true }>;
}): IngestBody {
  const base = {
    target: opts.target,
    record_type: opts.recordType as IngestBody['record_type'],
    title: opts.title.trim(),
    source: opts.source.trim(),
  };
  // Spread rather than always-send so a text upload's request shape is
  // byte-identical to the ingest page's — an added `body_format: undefined` key
  // still serialises differently, and that is the kind of quiet wire change
  // that costs a debugging session later.
  return opts.doc.format === 'pdf'
    ? { ...base, body_format: 'pdf', body_b64: opts.doc.bodyB64 }
    : { ...base, body: opts.doc.body };
}

/** Whether a filename will be fenced when filed (drives the chip's note). */
export { isCsvFilename };
