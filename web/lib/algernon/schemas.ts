import { z } from 'zod';

// zod validation at the BFF trust boundary. The browser is untrusted input even
// behind the session cookie — every BFF route parses its body before relaying to
// the transport, so a malformed request is rejected with a 400 at the edge
// rather than forwarded.

// A vault chat message can be long, but an unbounded body is a DoS surface; cap
// it generously. (The engine has its own limits; this is the edge guard.)
export const MAX_MESSAGE_CHARS = 8000;

// An instance selector — the routing token from GET /api/chat/targets (home
// display name or a cross-instance env segment). Bounded; absent ⇒ home instance.
export const chatInstanceSchema = z.string().trim().min(1).max(64);

// A client-minted idempotency key (UUID per logical turn, resent on retry). The
// backend dedups the last (key, message-hash) → cached result so a retry of a
// turn that already ran does NOT double-act (e.g. a vault write). Bounded edge
// guard (CONTRACT S6); absent ⇒ no dedup.
export const idempotencyKeySchema = z.string().min(1).max(200);

// POST /api/chat/turn body.
export const chatTurnBodySchema = z.object({
  session_key: z.string().min(1),
  message: z.string().trim().min(1).max(MAX_MESSAGE_CHARS),
  // M1 is text-first; the field is accepted (forward-compat with M2 voice) but
  // defaults to "text". Anything other than "voice" normalises to "text".
  kind: z.enum(['text', 'voice']).optional(),
  // Cross-instance selector (multi-instance switcher). Absent / the home name ⇒
  // the existing same-instance session path. BFF-only — stripped before relay.
  instance: chatInstanceSchema.optional(),
  // Retry-safety (CONTRACT S6). Relayed verbatim to the transport.
  idempotency_key: idempotencyKeySchema.optional(),
});

export type ChatTurnBody = z.infer<typeof chatTurnBodySchema>;

// POST /api/chat/open body — only the (optional) instance selector. BFF-only.
export const chatOpenBodySchema = z.object({
  instance: chatInstanceSchema.optional(),
});

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

// --- Web voice (V0) trust-boundary constants + schemas (CONTRACT §7) ---------
// The WebRTC signalling offer/close bodies cross the BFF trust boundary like every
// other route — parsed here before relay. A vanilla-ICE offer (all candidates
// embedded, host-only) is a few KB; 64 KiB is the DoS edge guard, mirrored by the
// backend's own 131072-byte cap (the backend measures BYTES, we cap CHARS — a
// lower ceiling, still comfortably above a real offer). The optional `session_key`
// is a V0 forward-hook (bound to a chat session in V1): length-capped here, logged
// + ignored server-side. Do NOT `.strict()` these — zod's default strips unknown
// keys, and the contract requires we accept-and-drop extras rather than 400 them.
export const MAX_SDP_CHARS = 65536;

export const voiceOfferBodySchema = z.object({
  sdp: z.string().min(1).max(MAX_SDP_CHARS),
  type: z.literal('offer'),
  session_key: z.string().min(1).max(128).optional(),
});

export type VoiceOfferBody = z.infer<typeof voiceOfferBodySchema>;

// The server-minted 32-hex session id echoed back to /voice/close. Bounded edge
// guard (the backend is the authority on the exact format); absent ⇒ 400.
export const voiceCloseBodySchema = z.object({
  voice_session_id: z.string().min(1).max(128),
});

export type VoiceCloseBody = z.infer<typeof voiceCloseBodySchema>;

// --- Web voice (V1) datachannel wire protocol (VOICE-V1-CONTRACT §1.1) --------
// DELIBERATE CONVENTION DEVIATION: zod normally guards only the browser→BFF
// request boundary; server→client JSON (SSE frames, API responses) uses
// safeJson<T> + a types.ts interface. The voice datachannel is a NEW inbound
// parse surface — untrusted server text driving a client state machine over a
// direct browser↔server channel that never passes through the BFF — so it gets
// the same bounded, discriminated validation the request boundary does. This is
// the CANONICAL D2 turn-plane vocabulary (the design facet's assumed schema was
// rejected). `v:1` rides EVERY frame in BOTH directions. Non-strict per member
// (zod strips unknown keys) so V2 can add fields/events without breaking V1.
export const VOICE_DC_PROTOCOL_VERSION = 1;
// Per-frame text cap (partials, finals, one reply sentence chunk). The DoS edge
// guard for a channel with no BFF in front of it.
export const MAX_DC_TEXT_CHARS = 8192;
// turn_final carries the WHOLE persisted reply (the trigger for history-reconcile)
// — a much larger ceiling than a single chunk so a long reply never fails the
// union and silently drops the reconcile trigger.
export const MAX_DC_REPLY_CHARS = 100_000;

const dcVersion = z.literal(VOICE_DC_PROTOCOL_VERSION);

export const voiceDcEventSchema = z.discriminatedUnion('type', [
  // Lifecycle/control. `ready` additionally carries the bound session ids.
  z.object({
    v: dcVersion,
    type: z.literal('state'),
    state: z.enum(['ready', 'superseded', 'turn_cancelled']),
    chat_session_key: z.string().optional(),
    voice_session_id: z.string().optional(),
    turn_id: z.string().optional(),
  }),
  // stt_final IS the end-of-utterance marker (there is no separate `utterance`).
  z.object({
    v: dcVersion,
    type: z.literal('stt_partial'),
    utterance_id: z.string(),
    text: z.string().max(MAX_DC_TEXT_CHARS),
    ts: z.union([z.string(), z.number()]).optional(),
  }),
  z.object({
    v: dcVersion,
    type: z.literal('stt_final'),
    utterance_id: z.string(),
    text: z.string().max(MAX_DC_TEXT_CHARS),
    ts: z.union([z.string(), z.number()]).optional(),
  }),
  z.object({ v: dcVersion, type: z.literal('turn_started'), turn_id: z.string() }),
  z.object({
    v: dcVersion,
    type: z.literal('turn_text'),
    turn_id: z.string(),
    seq: z.number(),
    text: z.string().max(MAX_DC_TEXT_CHARS),
  }),
  z.object({
    v: dcVersion,
    type: z.literal('turn_tool'),
    turn_id: z.string().optional(),
    tool: z.string().max(128).optional(),
  }),
  z.object({
    v: dcVersion,
    type: z.literal('turn_final'),
    turn_id: z.string(),
    reply: z.string().max(MAX_DC_REPLY_CHARS),
    ts: z.string().optional(),
    user_ts: z.string().optional(),
    reply_chars: z.number().optional(),
    truncated: z.boolean().optional(),
  }),
  // --- V2 streaming TTS talk-back (ADDITIVE — VOICE-V2-CONTRACT §1.1). Old V1
  //     clients console.debug-drop these three (harmless: tts only fires when the
  //     server has it enabled). The shipped `state` enum stays UNTOUCHED — these
  //     are their OWN types, not state-enum extensions.
  z.object({ v: dcVersion, type: z.literal('speaking_started'), turn_id: z.string() }),
  z.object({
    v: dcVersion,
    type: z.literal('speaking_done'),
    turn_id: z.string(),
    // Opaque bounded reason ('drained'|'cancelled'|'error' expected) — NOT z.enum,
    // so a future value (e.g. a V3 'barged_in') degrades to an unknown string
    // rather than a dropped frame.
    reason: z.string().max(64).optional(),
  }),
  // Half-duplex: an utterance final arriving while the assistant is speaking is
  // discarded server-side; this is the honest "heard you, hold on" notice.
  z.object({ v: dcVersion, type: z.literal('utterance_discarded'), utterance_id: z.string() }),
  // No `stt_error` — an unrecoverable STT death is error{code:'stt_unavailable'}.
  // A non-fatal TTS degrade is error{code:'tts_unavailable'} (its own FE branch).
  z.object({
    v: dcVersion,
    type: z.literal('error'),
    code: z.string().max(128),
    detail: z.string().max(1024).optional(),
    turn_id: z.string().optional(),
    utterance_id: z.string().optional(),
  }),
]);

export type VoiceDcEvent = z.infer<typeof voiceDcEventSchema>;

// Client→server frames (exactly hello + cancel, both carrying v:1). Serialized
// here so the wire version is set in one place.
export function voiceHelloFrame(): string {
  return JSON.stringify({ v: VOICE_DC_PROTOCOL_VERSION, type: 'hello' });
}

export function voiceCancelFrame(turnId?: string): string {
  return JSON.stringify({
    v: VOICE_DC_PROTOCOL_VERSION,
    type: 'cancel',
    ...(turnId ? { turn_id: turnId } : {}),
  });
}
