import { z } from 'zod';

// zod validation at the BFF trust boundary. The browser is untrusted input even
// behind the session cookie — every BFF route parses its body before relaying to
// the transport, so a malformed request is rejected with a 400 at the edge
// rather than forwarded.

// A vault chat message can be long, but an unbounded body is a DoS surface; cap
// it generously. (The engine has its own limits; this is the edge guard.)
export const MAX_MESSAGE_CHARS = 8000;

// POST /api/chat/turn body.
export const chatTurnBodySchema = z.object({
  session_key: z.string().min(1),
  message: z.string().trim().min(1).max(MAX_MESSAGE_CHARS),
  // M1 is text-first; the field is accepted (forward-compat with M2 voice) but
  // defaults to "text". Anything other than "voice" normalises to "text".
  kind: z.enum(['text', 'voice']).optional(),
});

export type ChatTurnBody = z.infer<typeof chatTurnBodySchema>;

// A session_key path/param must be a non-empty string (the backend issues uuids).
export const sessionKeySchema = z.string().min(1).max(200);

// POST /api/auth/login body. Light edge guard; the backend is the authority on
// the uniform { status:"sent" } response (no account enumeration). We only ensure
// a non-empty string is present so we can return the contract's email_required.
export const loginBodySchema = z.object({
  email: z.string().trim().min(1).max(320),
});

// The magic-link token posted to /api/auth/verify (via the callback).
export const authTokenSchema = z.string().min(1).max(4096);

// --- Cross-instance ingest (BUILD_DECISIONS §3 / §5) ------------------------
// An ingested artifact's body is written VERBATIM and can be a whole document,
// so the cap is far larger than the chat cap — but still bounded (the chat path's
// 8000-char cap doesn't cover this DoS surface). Mirrors the backend
// `transport.ingest.max_body_chars` default (262144 = 256 KiB).
export const MAX_INGEST_CHARS = 262144;

// The MVP universal ingest record types (BUILD_DECISIONS decision B). Mirrors the
// backend code-level `WEB_INGEST_CREATE_TYPES = {document, note, source}`. This
// is an INTENTIONAL cross-instance constant (every target accepts the same set);
// per-instance type vocabularies are deferred.
export const INGEST_RECORD_TYPES = ['document', 'note', 'source'] as const;

// POST /api/ingest/submit body. `target` is the server-side env segment from
// GET /api/ingest/targets (validated against the configured set in the BFF before
// any env lookup). title/source bounds match the backend /vault/ingest contract.
export const ingestBodySchema = z.object({
  target: z.string().trim().min(1).max(64),
  record_type: z.enum(INGEST_RECORD_TYPES),
  title: z.string().trim().min(1).max(300),
  // The artifact body is written VERBATIM (CONTRACT §2) — do NOT trim/mutate it
  // (trimming would strip the artifact's own leading/trailing whitespace). Validate
  // non-empty-AFTER-trim via .refine() while relaying the ORIGINAL untrimmed value.
  body: z
    .string()
    .max(MAX_INGEST_CHARS)
    .refine((s) => s.trim().length > 0, { message: 'A body is required.' }),
  source: z.string().trim().min(1).max(500),
});

export type IngestBody = z.infer<typeof ingestBodySchema>;

// --- Web STT trust-boundary constants (BUILD_DECISIONS §4 / §5) -------------
// Co-located with the other edge constants even though the binary STT body is
// NOT zod-parsed (it's a raw audio Buffer) — the BFF route uses these for the
// 415 (mime allowlist) + 413 (size cap) edge guards, and the backend mirrors
// them. 25 MiB is Groq Whisper's upload limit. The base mime (params like
// `;codecs=opus` stripped) is what's matched. `application/octet-stream` is the
// last-resort fallback some browsers/file pickers send for audio.
export const MAX_AUDIO_BYTES = 25 * 1024 * 1024;

export const AUDIO_MIME_ALLOWLIST = [
  'audio/webm',
  'audio/ogg',
  'audio/mp4',
  'audio/mpeg',
  'audio/wav',
  'audio/x-wav',
  'audio/x-m4a',
  'audio/mp4a-latm',
  'audio/flac',
  'application/octet-stream',
] as const;

// Strip Content-Type parameters (`audio/webm;codecs=opus` → `audio/webm`) and
// lowercase, then test membership. Returns the normalised base mime when allowed,
// else null (→ the caller returns 415). Centralised so the BFF route + any test
// share one definition.
export function normaliseAudioMime(contentType: string | undefined | null): string | null {
  if (!contentType) return null;
  const base = contentType.split(';')[0].trim().toLowerCase();
  return (AUDIO_MIME_ALLOWLIST as readonly string[]).includes(base) ? base : null;
}
