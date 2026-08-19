import { MAX_INGEST_CHARS, MAX_INGEST_PDF_BYTES } from './schemas';

// File-upload preparation for the ingest body (#57 CSV half). The whole ingest
// pipeline is text-in-a-JSON-field — FileReader.readAsText → `body` → the box's
// POST /vault/ingest → a vault .md record — so a CSV rides it unchanged. What it
// needs is the affordance plus an honest presentation:
//
//   * a CSV lands FENCED in a ```csv block — content untouched (one
//     trailing-newline caveat, documented on `fenceCsv`) and greppable, never
//     parsed or reshaped (a transform is how tabular data quietly corrupts).
//   * .md / .txt keep landing exactly as before: raw, untouched.
//   * every silent-absence case gets words — an empty file, a file over the size
//     ceiling, and a file the browser could not read. None may reach the operator
//     as a disabled button, a dead picker, or a server bounce.
//
// Pure functions: the component renders what these return, so the rules are
// testable without a DOM.

/** Extensions the body picker accepts, as the `accept` attribute spells them. */
export const INGEST_UPLOAD_ACCEPT =
  '.md,.txt,.csv,.pdf,text/markdown,text/plain,text/csv,application/pdf';

// A PDF is the one upload the browser does NOT read as text (#57). Its bytes go
// up base64'd and the BOX extracts, using the same pypdf extractor the Telegram
// attachment path has used since 2026-06-06 — one extractor, so the same file
// yields the same text whichever door it comes through. Doing it in the browser
// would mean a second implementation (pdf.js) and therefore a second answer.
export function isPdfFilename(name: string): boolean {
  return name.trim().toLowerCase().endsWith('.pdf');
}

/**
 * Refusal for a PDF over the byte ceiling.
 *
 * Quotes BYTES, not characters, because that is the axis being enforced — a
 * PDF's character count is not known until the box extracts it, and naming the
 * wrong unit would send the operator off to shorten a document whose length was
 * never the problem.
 */
export function oversizePdfMessage(
  filename: string,
  bytes: number,
  limit: number = MAX_INGEST_PDF_BYTES,
): string {
  const mib = (n: number) => `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${filename} is ${mib(bytes)} — over the ${mib(limit)} PDF limit. Nothing was loaded; split it and upload the parts separately.`;
}

// The BOX-side refusals (`pdf_no_text_layer`, `pdf_encrypted`,
// `pdf_unreadable`, `pdf_support_unavailable`, …) get their words in
// `useIngest`'s code→message mapper, alongside every other relayed code.
// Deliberately NOT duplicated here: two homes for the same sentence is how the
// scanned-PDF copy ends up saying different things depending on which layer
// rendered it, and the operator ruling on that particular wording (a plain
// refusal, no promise of vision) is precisely the kind of thing that must have
// one owner.

/**
 * Base64-encode file bytes for the wire.
 *
 * Chunked rather than `String.fromCharCode(...bytes)` in one call: spreading a
 * 10 MiB array blows the argument limit and throws RangeError on every engine,
 * so the naive version would work in tests on small fixtures and fail on
 * exactly the real bank statements this feature exists for.
 */
export function bytesToBase64(bytes: Uint8Array): string {
  const CHUNK = 0x8000;
  let binary = '';
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

export type PreparedPdfUpload =
  | { ok: true; bodyB64: string; note: string; bytes: number }
  | { ok: false; reason: 'empty' | 'too_large'; message: string };

/**
 * Turn read PDF bytes into the base64 body to submit, or a refusal with words.
 *
 * Only the two checks the BROWSER can make: an empty file and an oversize one.
 * Everything else about a PDF — whether it has a text layer, whether it is
 * encrypted, whether it parses — is knowable only after extraction, which
 * happens on the box. Guessing here would mean a second, worse implementation
 * of the extractor's judgement.
 */
export function preparePdfUpload(
  filename: string,
  bytes: Uint8Array,
  limit: number = MAX_INGEST_PDF_BYTES,
): PreparedPdfUpload {
  if (bytes.length === 0) {
    return { ok: false, reason: 'empty', message: emptyUploadMessage(filename) };
  }
  if (bytes.length > limit) {
    return {
      ok: false,
      reason: 'too_large',
      message: oversizePdfMessage(filename, bytes.length, limit),
    };
  }
  const kb = (bytes.length / 1024).toFixed(0);
  return {
    ok: true,
    bodyB64: bytesToBase64(bytes),
    bytes: bytes.length,
    // Discloses the reshape, exactly as the CSV note does: the record will hold
    // extracted text, not the PDF, and the operator is owed that up front
    // rather than on discovering it in the vault.
    note: `${filename} — ${kb} KB PDF; its text will be extracted on the box and fenced in the body`,
  };
}

// Detection is by EXTENSION, not `File.type`: browsers disagree about the MIME
// for .csv (Windows commonly reports application/vnd.ms-excel via the registry),
// so the type field is not a reliable discriminator.
export function isCsvFilename(name: string): boolean {
  return name.trim().toLowerCase().endsWith('.csv');
}

/**
 * Rows in a CSV, counted as LINES — header included.
 *
 * Deliberately a newline count rather than a CSV parse: a quoted field may hold
 * an embedded newline, so this can exceed the number of logical records. The
 * rendered note says "newline count" for exactly that reason — an honest
 * approximation beats a parse this code has no business doing.
 */
export function countCsvRows(text: string): number {
  const normalized = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n');
  // A file ending in a newline has no trailing empty row.
  const body = normalized.endsWith('\n') ? normalized.slice(0, -1) : normalized;
  return body === '' ? 0 : body.split('\n').length;
}

/**
 * Wrap raw CSV text in a fenced code block without altering a byte of it.
 *
 * The fence is grown to one backtick longer than the longest backtick run in the
 * content, so a CSV field containing ``` cannot terminate the block early.
 * Line endings are left as-is (only `countCsvRows` normalizes). The single
 * exception to byte-for-byte fidelity is a newline added before the closing
 * fence when the file lacks a trailing one — a fenced block's closing fence must
 * begin a line, and a final newline inside a code block is not representable
 * distinctly in Markdown anyway.
 */
export function fenceCsv(text: string): string {
  const longestRun = (text.match(/`+/g) ?? []).reduce((m, r) => Math.max(m, r.length), 0);
  const fence = '`'.repeat(Math.max(3, longestRun + 1));
  const content = text.endsWith('\n') ? text : `${text}\n`;
  return `${fence}csv\n${content}${fence}\n`;
}

// "fenced in the body" rather than "fenced VERBATIM": `fenceCsv` adds a trailing
// newline when the file lacks one, so verbatim would overclaim by exactly that
// one character. The content itself is untouched — see `fenceCsv`.
export function csvUploadNote(filename: string, rows: number): string {
  return `${filename} — ${rows.toLocaleString()} rows (newline count, header included), fenced in the body`;
}

/**
 * THE CLOBBER GUARD — what separates text the operator TYPED from the file they
 * then attached, when both end up in one body.
 *
 * WHY THERE IS ANYTHING TO SEPARATE. The upload path used to do `setBody(...)`
 * WHOLESALE, so notes typed before attaching a file were silently replaced. The
 * operator hit this on 2026-08-18: he described what he wanted done with a CSV,
 * attached it, and the description never left the browser — the stored record
 * was the CSV alone. The form already guarded the MIRROR case (a text upload
 * un-stages a staged PDF) with a comment naming the exact principle — "the
 * operator's most recent, most deliberate action silently losing to the earlier
 * one" — and this direction was simply never covered.
 *
 * A HORIZONTAL RULE, not a labelled banner. The merged body is written verbatim
 * into a vault record, so anything added here is content the operator did not
 * write and will read forever afterwards. A rule is the smallest mark that
 * survives markdown rendering as a real division; a sentence explaining what the
 * form did belongs in the FORM, where it is read once and then gone, and that is
 * where it is (the upload note). Not at the top of the body: a leading `---`
 * would sit exactly where a YAML frontmatter fence goes.
 */
export const UPLOAD_MERGE_SEPARATOR = '\n\n---\n\n';

/**
 * Typed text first, then the file — because the typed text is almost always an
 * INSTRUCTION ABOUT the file ("summarise the Q3 column", "this is the budget
 * one"), which reads as a preamble and, at the top, is not buried under a
 * thousand-row CSV.
 */
export function mergeTypedWithUpload(typed: string, uploaded: string): string {
  return `${typed.trimEnd()}${UPLOAD_MERGE_SEPARATOR}${uploaded}`;
}

/**
 * What the form says when it kept both. The operator must be told, or a body
 * that quietly grew is its own small mystery — and this is the sentence that
 * lets them delete one half on purpose rather than wonder.
 */
export function mergedUploadNote(filename: string): string {
  return `${filename} was added BELOW the notes you had already typed — both are in the body. Delete either half if you meant to replace it.`;
}

export function emptyUploadMessage(filename: string): string {
  return `${filename} is empty — there is nothing to ingest. Pick a different file, or compose the body here.`;
}

/**
 * A file the browser could not read at all.
 *
 * `FileReader` can fail after the picker has already accepted the file — moved or
 * deleted between selection and read, a permissions change, a media error. That
 * lands on `onerror`, where doing nothing produces exactly the silence this
 * module exists to eliminate: the operator picked a file and the form simply did
 * not react. `reason` carries the DOMException name when the browser supplies one
 * rather than swallowing the only diagnostic on offer.
 */
export function readFailedMessage(filename: string, reason?: string): string {
  const because = reason ? ` (${reason})` : '';
  return `Could not read ${filename}${because} — it may have been moved, deleted, or be unreadable. Nothing was loaded; try selecting it again.`;
}

/**
 * The figure quoted is the BODY's, not the file's.
 *
 * For a CSV the fence is part of what the box will weigh, so a file a few
 * characters UNDER the limit can still be refused — and quoting the file's own
 * size there would make the sentence read as an arithmetic error ("262,141
 * characters — over the 262,144-character limit"). Naming the body and, for a
 * CSV, saying `once fenced` is what accounts for the difference out loud.
 */
export function oversizeUploadMessage(
  filename: string,
  bodyChars: number,
  limit: number = MAX_INGEST_CHARS,
  fenced = false,
): string {
  const measured = `a ${bodyChars.toLocaleString()}-character body${fenced ? ' once fenced' : ''}`;
  return `${filename} would make ${measured} — over the ${limit.toLocaleString()}-character ingest limit. Nothing was loaded; split it and upload the parts separately.`;
}

export type PreparedUpload =
  | { ok: true; body: string; note: string | null }
  | { ok: false; reason: 'empty' | 'too_large'; message: string };

/**
 * Turn a read file into the body to submit, or into a refusal with words.
 *
 * `limit` is measured against the body that would actually be SUBMITTED — for a
 * CSV that includes the fence — because the ceiling the box enforces
 * (`transport.ingest.max_body_chars`, mirrored here as `MAX_INGEST_CHARS`) is
 * applied to the relayed body. Counting the file alone would let a file just
 * under the limit produce an over-limit body and a 413 the operator never saw
 * coming.
 *
 * Character counts here are JS UTF-16 code units while the box counts Python
 * code points, so an astral-plane character (emoji) counts 2 here and 1 there
 * (measured, not assumed: `'😀'.repeat(10)` is length 20 in JS, len 10 in
 * Python). This client count is therefore never BELOW the box's, so the guard
 * errs toward refusing slightly early and never toward a surprise server
 * bounce — the safe direction, and pinned as such.
 *
 * RESIDUAL, stated rather than implied: `MAX_INGEST_CHARS` mirrors the box's
 * DEFAULT only. A target that lowers `transport.ingest.max_body_chars` below it
 * is invisible to the browser, and that operator CAN still meet the ceiling as a
 * relayed 413. Closing it needs the per-target limit on
 * `GET /api/ingest/targets` — which is why `limit` is a parameter here instead of
 * this module reading the constant inline: only the wire change is missing.
 * `tests/test_transport_config.py` pins the default pair against drift.
 */
export function prepareUpload(
  filename: string,
  text: string,
  limit: number = MAX_INGEST_CHARS,
): PreparedUpload {
  // Mirrors the box's `not body_text.strip()` → 400 empty_body, and the form's
  // own submit gate: a whitespace-only file is as unsubmittable as an empty one.
  if (text.trim() === '') {
    return { ok: false, reason: 'empty', message: emptyUploadMessage(filename) };
  }

  const csv = isCsvFilename(filename);
  const body = csv ? fenceCsv(text) : text;

  if (body.length > limit) {
    return {
      ok: false,
      reason: 'too_large',
      message: oversizeUploadMessage(filename, body.length, limit, csv),
    };
  }

  return {
    ok: true,
    body,
    // Only a CSV gets a note, because only a CSV's body was reshaped (fenced) —
    // the operator is owed that disclosure plus a sanity figure. A .md/.txt body
    // is verbatim and needs no annotation.
    note: csv ? csvUploadNote(filename, countCsvRows(text)) : null,
  };
}
