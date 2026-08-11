/**
 * Intent resolution for the unified composer (#97).
 *
 * The operator asked for ONE interface: "each of these three does similar work
 * with the distinction made between what type of file will be received. That's
 * not good ux." So the chat composer takes everything the three pages jointly
 * accepted, and what used to be a choice of PAGE becomes a visible chip per
 * attachment set.
 *
 * The three pipelines behind it are UNCHANGED — chat-context (#82's image
 * path), vault ingest (#57's deterministic-create), batch drip (#83). Nothing
 * here submits anything; it decides which of those three an attachment set is
 * headed for, and says so out loud.
 *
 * PROPOSE-THEN-APPROVE, NEVER SILENT ROUTING. The default is computed from type
 * and count, rendered as a chip the operator can see before pressing Send, and
 * flippable in one tap. A default that is merely *usually right* would still be
 * wrong silently, and the wrong answer here is a document that lands in a chat
 * turn instead of the vault (or thirty scans that wedge a conversation).
 *
 * Pure and DOM-free on purpose — the component renders what these return, so
 * the rules are testable without a render (the convention `ingestUpload.ts`
 * sets out).
 */

import {
  ALLOWED_IMAGE_MEDIA_TYPES,
  AUDIO_MIME_ALLOWLIST,
  MAX_IMAGES_PER_TURN,
} from './schemas';
import { MAX_BATCH_IMAGES } from './batchSubmit';

/** What kind of thing was attached — the axis the default routing reads. */
export type AttachmentKind = 'image' | 'doc' | 'audio';

/** Where an attachment set is headed. */
export type ComposerIntent =
  /** Into the conversation (the #82 image path / inline text). */
  | 'discuss'
  /** Into a bulk scan batch, processed on the drip campaign's schedule (#83). */
  | 'batch'
  /** Into the vault as a record, written verbatim (#57 ingest). */
  | 'file';

/** Document extensions the ingest body picker has always accepted. */
export const DOC_EXTENSIONS = ['.md', '.txt', '.csv', '.pdf'] as const;

/**
 * Audio extensions, as a FALLBACK to the mime check.
 *
 * The mime allowlist is the primary test, but a picker can hand over a file
 * with an empty `type` (some Android file providers do), and refusing a .m4a
 * because the browser declined to name it would be a silence the operator
 * cannot act on. `.webm` is deliberately absent: it is as often video as audio,
 * and a video file's mime is what distinguishes them.
 */
export const AUDIO_EXTENSIONS = [
  '.m4a', '.mp3', '.wav', '.ogg', '.oga', '.opus', '.flac', '.aac', '.mp4a',
] as const;

/**
 * The `accept` attribute for the one universal picker — the union of what the
 * three pages accepted separately.
 *
 * Built from the same constants each route enforces rather than typed out, so a
 * media type added to one of them cannot be silently missing from the door.
 */
export const UNIVERSAL_ACCEPT = [
  ...ALLOWED_IMAGE_MEDIA_TYPES,
  ...DOC_EXTENSIONS,
  'text/markdown',
  'text/plain',
  'text/csv',
  'application/pdf',
  'audio/*',
].join(',');

function extensionOf(name: string): string {
  const trimmed = name.trim().toLowerCase();
  const i = trimmed.lastIndexOf('.');
  return i > 0 ? trimmed.slice(i) : '';
}

/** Strip the extension — the filename-as-title default. */
export function stripExtension(name: string): string {
  const i = name.lastIndexOf('.');
  return i > 0 ? name.slice(0, i) : name;
}

export function isPdf(name: string): boolean {
  return extensionOf(name) === '.pdf';
}

/**
 * Which of the three kinds this file is — or null when no route accepts it.
 *
 * MIME FIRST, extension as the fallback. Detection by extension alone is what
 * `ingestUpload` warns about for CSV (Windows reports application/vnd.ms-excel
 * via the registry); detection by mime alone loses the file whose provider sent
 * an empty type. Both, in that order, is what accepts the real inputs.
 */
export function classifyAttachment(file: { name: string; type?: string }): AttachmentKind | null {
  const mime = (file.type || '').toLowerCase().split(';')[0].trim();
  const ext = extensionOf(file.name);
  if ((ALLOWED_IMAGE_MEDIA_TYPES as readonly string[]).includes(mime)) return 'image';
  if ((DOC_EXTENSIONS as readonly string[]).includes(ext)) return 'doc';
  if (mime.startsWith('audio/') || (AUDIO_MIME_ALLOWLIST as readonly string[]).includes(mime)) {
    // `application/octet-stream` is in the STT allowlist as a last-resort
    // fallback for audio, but it is ALSO what a browser calls an unknown
    // binary — so it only counts as audio when the extension agrees.
    if (mime.startsWith('audio/')) return 'audio';
    if ((AUDIO_EXTENSIONS as readonly string[]).includes(ext)) return 'audio';
  }
  if ((AUDIO_EXTENSIONS as readonly string[]).includes(ext)) return 'audio';
  return null;
}

/** The refusal for a file no route accepts. Names what IS accepted. */
export function unsupportedAttachmentMessage(filename: string): string {
  return `“${filename}” isn’t something this can take — attach an image (PNG, JPEG, GIF, WebP), a document (.md, .txt, .csv, .pdf), or an audio recording.`;
}

/**
 * The image count at which the default flips from Discuss to Batch.
 *
 * DERIVED from the conversation's own per-turn cap rather than written as 5, so
 * the two cannot drift into a gap (a set that is too big to discuss and too
 * small to default to batch) or an overlap. The ratified rule — 1–4 discuss,
 * 5+ batch — is exactly "more than a turn can carry goes to the batch door",
 * and that is what this expresses.
 *
 * The 12-block history cap is why a bulk set must NOT enter the conversation at
 * all: thirty scans in context is the wedge #82 was about.
 */
export const BATCH_DEFAULT_THRESHOLD = MAX_IMAGES_PER_TURN + 1;

/** The deterministic default for a set of `count` images. */
export function defaultImageIntent(count: number): ComposerIntent {
  return count >= BATCH_DEFAULT_THRESHOLD ? 'batch' : 'discuss';
}

/** The deterministic default for a document or an audio recording. */
export function defaultDocIntent(): ComposerIntent {
  return 'file';
}

/**
 * The intents offered for an attachment set, in display order — the first is
 * the default.
 *
 * A PDF gets `file` only. The browser never reads a PDF's text (the box
 * extracts it with the one pypdf extractor, so one file yields one answer
 * whichever door it arrives through), so there is nothing to inline into a
 * turn. That is not a loss: filing it hands the created record path to the same
 * turn, and the talker reads it with `vault_read` — which is discussing it,
 * with the extraction done by the layer that owns extraction.
 */
export function availableIntents(
  kind: AttachmentKind,
  opts: { count?: number; filename?: string } = {},
): ComposerIntent[] {
  if (kind === 'image') {
    const count = opts.count ?? 0;
    return count > MAX_IMAGES_PER_TURN ? ['batch'] : ['discuss', 'batch'];
  }
  if (kind === 'doc' && isPdf(opts.filename ?? '')) return ['file'];
  return ['file', 'discuss'];
}

/**
 * Why an intent is NOT on offer for this set — or null when it is.
 *
 * A disabled chip with no sentence beside it is the greyed-out-button silence
 * this whole surface is shaped against.
 */
export function intentBlockedReason(
  kind: AttachmentKind,
  intent: ComposerIntent,
  opts: { count?: number; filename?: string } = {},
): string | null {
  if (availableIntents(kind, opts).includes(intent)) return null;
  if (kind === 'image' && intent === 'discuss') {
    const count = opts.count ?? 0;
    return `A message can carry ${MAX_IMAGES_PER_TURN} images and this set has ${count}. Send it as a batch, or remove ${count - MAX_IMAGES_PER_TURN} of them to discuss the rest.`;
  }
  if (kind === 'doc' && intent === 'discuss') {
    return 'A PDF’s text is extracted on the instance, not in the browser — so it can’t be quoted into a message. File it, and the reply can read it straight from the vault.';
  }
  return 'That isn’t available for this attachment.';
}

/** The chip's label. */
export function intentLabel(intent: ComposerIntent): string {
  switch (intent) {
    case 'discuss':
      return 'Discuss';
    case 'batch':
      return 'Batch';
    case 'file':
      return 'File to vault';
  }
}

/** One line under the chip saying what pressing Send will actually do. */
export function intentDescription(intent: ComposerIntent, instanceLabel: string): string {
  switch (intent) {
    case 'discuss':
      return `Rides your message into the conversation with ${instanceLabel}.`;
    case 'batch':
      return `Saved to ${instanceLabel} and read one scan at a time on the batch schedule — not into this conversation.`;
    case 'file':
      return `Written to ${instanceLabel}'s vault verbatim; this message can then ask about it.`;
  }
}

/**
 * Match the CHAT instance the operator has selected to a target of another
 * route family, case-insensitively.
 *
 * The three families name their targets differently — chat's home target is the
 * display name (`Salem`), while ingest and batch names are env-key segments
 * (`SALEM`). Matching on case would make a perfectly configured deploy look
 * unconfigured. Returns the target ITSELF so the caller round-trips that
 * family's exact `name`, never the chat spelling.
 */
export function matchTarget<T extends { name: string }>(
  instance: string,
  targets: readonly T[],
): T | null {
  const want = (instance || '').trim().toUpperCase();
  if (!want) return null;
  return targets.find((t) => t.name.trim().toUpperCase() === want) ?? null;
}

/**
 * The refusal when the selected instance has no target for this route.
 *
 * NAMES THE INSTANCE and says nothing was sent. The alternative — quietly
 * routing to whichever instance IS configured — is the silent routing this
 * whole surface exists to remove, and it would put the operator's document in
 * the wrong vault without ever saying so.
 *
 * It deliberately does not point at the standalone page: that page reads the
 * same env, so it would refuse identically and the operator would have spent a
 * navigation to learn nothing.
 */
export function missingTargetMessage(intent: ComposerIntent, instanceLabel: string): string {
  if (intent === 'batch') {
    return `${instanceLabel} isn’t set up to receive bulk scans on this deployment, so nothing was sent. Pick a different instance above, or discuss them instead.`;
  }
  return `${instanceLabel} isn’t set up to receive documents on this deployment, so nothing was filed. Pick a different instance above.`;
}

/**
 * The count cap, restated for a set that is already over it before any send.
 *
 * `prepareBatch` refuses the (N+1)th file at PICK time with its own sentence,
 * so this only speaks for a set assembled some other way (a flip from discuss
 * after the picker's own cap was never consulted). Same number, same remedy.
 */
export function overBatchCapMessage(count: number): string {
  return `That is ${count} scans — more than the ${MAX_BATCH_IMAGES} one batch can hold. Remove some, or submit these and start another batch.`;
}
