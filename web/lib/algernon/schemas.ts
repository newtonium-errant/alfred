import { z } from 'zod';
import {
  ALLOWED_SHOT_MIME,
  MAX_BUGREPORT_DESCRIPTION_CHARS,
  MAX_BUGREPORT_SCREENSHOT_BYTES,
} from './bugReport';

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

// --- Image-carry (parity #29) — MIRRORS the backend authority ----------------
// The backend (src/alfred/web/routes_chat.py) is the FAIL-LOUD authority on
// these caps (ALLOWED_IMAGE_MEDIA_TYPES / MAX_IMAGE_BYTES / MAX_IMAGES_PER_TURN);
// these FE mirrors are UX-only — they reject early with inline copy so a user
// isn't surprised by a 400 after the round-trip. Keep them IDENTICAL to the
// backend constants; a drift means the FE accepts what the backend rejects (or
// vice-versa). The wire shape is `{ media_type, data }` where `data` is base64
// WITHOUT the `data:<mime>;base64,` prefix — the caller strips it.
export const ALLOWED_IMAGE_MEDIA_TYPES = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
] as const;
export const MAX_IMAGE_BYTES = 5 * 1024 * 1024; // 5 MiB decoded, per image
export const MAX_IMAGES_PER_TURN = 4;

// The decoded byte length of a standard base64 string (no data: prefix), derived
// from length + '=' padding WITHOUT allocating the decoded buffer (a 5 MiB image
// is ~6.7 MiB of base64 — decoding just to measure it would be wasteful).
export function base64DecodedBytes(b64: string): number {
  const len = b64.length;
  if (len === 0) return 0;
  const padding = b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0;
  return Math.floor((len * 3) / 4) - padding;
}

// One carried image. `data` is bare standard base64 (the `data:` URI prefix
// stripped by the composer): non-empty, base64-alphabet only (mirrors the
// backend's `b64decode(validate=True)`), decoding to ≤ MAX_IMAGE_BYTES.
export const imageAttachmentSchema = z.object({
  media_type: z.enum(ALLOWED_IMAGE_MEDIA_TYPES),
  data: z
    .string()
    .min(1)
    .regex(/^[A-Za-z0-9+/]+={0,2}$/, {
      message: 'image data must be bare base64 (strip the data: prefix)',
    })
    .refine((s) => base64DecodedBytes(s) <= MAX_IMAGE_BYTES, {
      message: `each image must be ≤ ${MAX_IMAGE_BYTES} bytes (5 MiB) decoded`,
    }),
});

export type ImageAttachment = z.infer<typeof imageAttachmentSchema>;

// Player-ask on-screen primer (C3c) — the two-key context the /player ask carries so the
// answer resolves against the PAUSED slide. Relayed VERBATIM to the backend, which is the
// validity authority (src/alfred/brief/player_primer.py PlayerContextPrimer.valid): an
// ISO-bad date or an unknown section_id ⟹ the backend answers UN-GROUNDED, never rejects
// the turn. So this edge guard only BOUNDS the strings (DoS) — it deliberately does NOT
// enforce the ISO format / the known-section set, which would turn the backend's fail-soft
// into a 400 and break the "answer un-grounded" contract. Same relay-verbatim discipline
// as `images` (the backend re-validates as the authority).
export const playerPrimerSchema = z.object({
  brief_date: z.string().max(32),
  section_id: z.string().max(64),
});

export type PlayerPrimerBody = z.infer<typeof playerPrimerSchema>;

// --- Learned-vocabulary capture (#54) ----------------------------------------
// The STT transcript AS INSERTED into the composer, carried alongside the SENT
// message so the backend can diff the two and learn what it mis-heard
// (src/alfred/telegram/stt_vocab_learning.py). Optional and additive: absent ⇒
// byte-identical to the pre-feature body, exactly like `images` and `primer`.
//
// THE BOUND, and why it is the MESSAGE cap rather than something audio-derived.
// The STT route bounds the AUDIO it accepts (MAX_AUDIO_BYTES) but places no cap
// on the transcript it returns, so there is no upstream number to mirror. The
// honest bound comes from what the field is FOR: the backend diffs it word-for-
// word against `message`, which itself cannot exceed MAX_MESSAGE_CHARS. A
// transcript longer than the longest sendable message cannot yield a meaningful
// correction out of that diff — it degenerates into one giant delete, which the
// extractor ignores by design — so accepting more would only widen the DoS
// surface for no learning.
//
// The composer DROPS an over-long transcript rather than sending it (see
// `useComposerText.takeTranscript` — that is where the drop lives now that the
// legacy Composer.tsx is deleted): capture is telemetry and must never cost the
// operator their turn, which a 400 here would. This bound is therefore the trust-boundary
// guard against a hostile or buggy client, not the operator's everyday limit —
// the same edge-guard-vs-UX split `images` already uses.
export const MAX_TRANSCRIPT_CHARS = MAX_MESSAGE_CHARS;

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
  // Optional carried screenshots (parity #29) — relayed VERBATIM to the backend
  // (which re-validates as the authority). Bounded to MAX_IMAGES_PER_TURN.
  images: z.array(imageAttachmentSchema).max(MAX_IMAGES_PER_TURN).optional(),
  // Player-ask on-screen context (C3c) — relayed VERBATIM; the backend validity-gates
  // (invalid ⟹ answer un-grounded, never a 400). Only the /player ask sends it.
  primer: playerPrimerSchema.optional(),
  // The STT transcript as inserted (#54) — relayed VERBATIM; the backend records
  // the (transcript, sent) pair only on a voice-kind real send. Trimmed + bounded
  // like `message`; see MAX_TRANSCRIPT_CHARS for why the bound is the message cap.
  transcript: z.string().trim().min(1).max(MAX_TRANSCRIPT_CHARS).optional(),
});

export type ChatTurnBody = z.infer<typeof chatTurnBodySchema>;

// POST /api/chat/open body — only the (optional) instance selector. BFF-only.
export const chatOpenBodySchema = z.object({
  instance: chatInstanceSchema.optional(),
});

// A session_key path/param must be a non-empty string (the backend issues uuids).
export const sessionKeySchema = z.string().min(1).max(200);

// --- Notifications (parity #22, poll slice) ----------------------------------
// Typed mirror of the backend notification entry
// (src/alfred/web/notify_state.py — the backend is the authority). Used to
// parse-validate the polled list on the client; `ticket_uid` / `issue_url`
// are '' when the notice wasn't ticket-shaped, and tolerated as absent for
// forward-compat.
export const notificationSchema = z.object({
  id: z.string().min(1),
  text: z.string(),
  precedence: z.string(),
  source: z.string(),
  ticket_uid: z.string().optional(),
  // #22 XSS defense-in-depth: a peer-supplied issue_url renders into an
  // <a href>; sanitize any non-http(s) scheme (javascript:/data:) to undefined
  // so it never reaches the href. Backend (_safe_http_url) is the authority;
  // this mirrors it. Transform (not refine) so one bad item can't fail the
  // whole notifications array and nuke the tray.
  issue_url: z
    .string()
    .optional()
    .transform((u) => (u && /^https?:\/\//i.test(u.trim()) ? u : undefined)),
  // #76 — the ticket body the intake sent with the notice, so the card can
  // expand. Plain TEXT, rendered as escaped React children and never as
  // markup (the #22 stored-XSS precedent: this is reporter-authored text that
  // crossed a peer protocol). Optional throughout — a tray holding pre-#76
  // entries must keep rendering rather than failing the parse and emptying
  // itself on rollout.
  ticket_body: z.string().optional(),
  ticket_body_truncated: z.boolean().optional(),
  issue_number: z.number().optional(),
  ts: z.string(),
  read: z.boolean(),
  // #86 — optional for the same reason ticket_body is: a tray holding pre-#86
  // entries must keep parsing rather than failing and emptying itself on
  // rollout. The backend already filters dismissed entries out of the list, so
  // this arriving true would be a backend bug, not a rendering case.
  dismissed: z.boolean().optional(),
});

export type Notification = z.infer<typeof notificationSchema>;

// GET /chat/notifications → { notifications: [...], unread }.
export const notificationsResponseSchema = z.object({
  notifications: z.array(notificationSchema),
  unread: z.number(),
});

// POST /api/chat/notifications/ack body — the BFF trust-boundary guard.
// Bounded (mirrors the backend MAX_ACK_IDS=200); non-empty string ids only.
export const MAX_NOTIFICATION_ACK_IDS = 200;

export const notificationsAckBodySchema = z.object({
  ids: z.array(z.string().min(1).max(64)).min(1).max(MAX_NOTIFICATION_ACK_IDS),
});

export type NotificationsAckBody = z.infer<typeof notificationsAckBodySchema>;

// POST /api/chat/notifications/dismiss body (#86). Deliberately its OWN schema
// object rather than an alias of the ack one: they validate the same shape
// today, and an alias would quietly couple two endpoints whose rules are free
// to diverge (dismiss is the less reversible of the two). The shared BOUND is
// the constant above, which is the part that must not drift.
export const notificationsDismissBodySchema = z.object({
  ids: z.array(z.string().min(1).max(64)).min(1).max(MAX_NOTIFICATION_ACK_IDS),
});

export type NotificationsDismissBody = z.infer<
  typeof notificationsDismissBodySchema
>;

// POST /api/auth/login body. Light edge guard; the backend is the authority on
// the uniform { status:"sent" } response (no account enumeration). We only ensure
// a non-empty string is present so we can return the contract's email_required.
export const loginBodySchema = z.object({
  email: z.string().trim().min(1).max(320),
  // Optional post-auth redirect target, relayed to the backend which embeds it in
  // the magic link (?next=…). NOT sanitised here — the backend's safe_next_path is
  // the authority, and auth/callback re-guards it via safeNextPath before redirect.
  // Bounded as an edge guard (an unbounded value is a DoS surface).
  next: z.string().max(2048).optional(),
});

// The magic-link token posted to /api/auth/verify (via the callback).
export const authTokenSchema = z.string().min(1).max(4096);

// --- OTP re-auth (parity #23 — iOS PWA cookie-jar fix) -----------------------
// POST /api/auth/otp/request body. Same edge-guard role as loginBodySchema; the
// backend is the authority on the uniform { status:"sent" } (no enumeration).
export const otpRequestBodySchema = z.object({
  email: z.string().trim().min(1).max(320),
});

// The exact passcode wire shape — mirrors the backend's ^[0-9]{6}$ authority.
export const OTP_CODE_REGEX = /^[0-9]{6}$/;

// POST /api/auth/otp/verify body. A malformed shape is rejected at the edge
// with the SAME uniform invalid_or_expired the backend returns (no oracle).
export const otpVerifyBodySchema = z.object({
  email: z.string().trim().min(1).max(320),
  code: z.string().regex(OTP_CODE_REGEX),
});

// --- Cross-instance ingest (BUILD_DECISIONS §3 / §5) ------------------------
// An ingested artifact's body is written VERBATIM and can be a whole document,
// so the cap is far larger than the chat cap — but still bounded (the chat path's
// 8000-char cap doesn't cover this DoS surface). Mirrors the backend
// `transport.ingest.max_body_chars` default (262144 = 256 KiB).
export const MAX_INGEST_CHARS = 262144;

// The PDF byte ceiling (#57). A DIFFERENT AXIS from MAX_INGEST_CHARS, and the
// distinction is load-bearing: a 10 MiB bank statement is an ordinary file
// whose extracted text sits comfortably under the character cap, so one number
// cannot govern both. The form checks bytes before upload and characters are
// the box's business after extraction. Mirrors
// `alfred.documents.pdf.MAX_PDF_BYTES`, ratified 2026-06-06 for the Telegram
// attachment path and re-ratified 2026-08-07 for web ingest — ONE number
// across both doors, held by a drift pin in tests/test_transport_config.py.
export const MAX_INGEST_PDF_BYTES = 10 * 1024 * 1024;

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
  // Exactly one of `body` (text) or `body_b64` (#57, a PDF) is supplied — see
  // the .superRefine below. `body` stays required-shaped here so every
  // pre-#57 caller and its error messages are unchanged.
  body: z
    .string()
    .max(MAX_INGEST_CHARS)
    .refine((s) => s.trim().length > 0, { message: 'A body is required.' })
    .optional(),
  source: z.string().trim().min(1).max(500),
  // #57 PDF half. The bytes ride base64 over the SAME peer-pinned
  // deterministic-create route rather than a second endpoint: the peer-pin,
  // provenance, collision handling and error taxonomy all already live there,
  // and a parallel route is how two of them drift.
  body_format: z.enum(['text', 'pdf']).optional(),
  // Bounded by the base64-inflated byte cap (4/3) so an oversize upload is
  // refused at the edge rather than buffered onward to the box.
  body_b64: z
    .string()
    .max(Math.ceil((MAX_INGEST_PDF_BYTES * 4) / 3) + 4096)
    .optional(),
}).superRefine((val, ctx) => {
  const isPdf = val.body_format === 'pdf';
  if (isPdf && !val.body_b64) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['body_b64'],
      message: 'A PDF upload is required.',
    });
  }
  if (!isPdf && val.body === undefined) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['body'],
      message: 'A body is required.',
    });
  }
});

export type IngestBody = z.infer<typeof ingestBodySchema>;

// --- iOS Shortcuts device-token ingest (bearer-only /api/ingest/shortcut) -----
// A deliberately NARROWER subset of the ingest surface for the Shortcuts tendril
// (Action Button → Dictate → POST). `text` is the record BODY; note-only for v1;
// title optional (the route derives one when absent); target optional (the route
// defaults it). No free-form record_type, no client-supplied `source` — the route
// stamps provenance itself. The 8000-char cap mirrors the chat edge cap (a voice
// capture is short; a whole-document paste belongs on ingest/submit).
export const SHORTCUT_MAX_TEXT_CHARS = 8000;
export const SHORTCUT_MAX_TITLE_CHARS = 300;

// v1 accepts ONLY 'note'. z.enum leaves the door open for a widened set later; a
// value outside it is a 400 at the edge (mirrors the backend type gate).
export const SHORTCUT_RECORD_TYPES = ['note'] as const;

export const shortcutIngestBodySchema = z.object({
  // Written VERBATIM as the record body; validate non-empty-after-trim while
  // relaying the ORIGINAL value (mirrors ingestBodySchema.body).
  text: z
    .string()
    .max(SHORTCUT_MAX_TEXT_CHARS)
    .refine((s) => s.trim().length > 0, { message: 'A text capture is required.' }),
  title: z.string().trim().min(1).max(SHORTCUT_MAX_TITLE_CHARS).optional(),
  record_type: z.enum(SHORTCUT_RECORD_TYPES).optional(),
  target: z.string().trim().min(1).max(64).optional(),
});

export type ShortcutIngestBody = z.infer<typeof shortcutIngestBodySchema>;

// --- Feed surface (Feed Phase B) --------------------------------------------
// POST /api/feed/act body → relayed VERBATIM to the transport POST /feed/act.
// `id` is the feed item id (`<kind>:<stable_key>` — carries a colon and often a
// record path, hence the generous bound); `action_id` is the deck/feed verb
// (confirm/reject/high/…/ack). The transport's (kind, action) map is the
// capability ceiling — the BFF only relays. `z.object` STRIPS unknown keys (no
// `.strict()`), so a client can't smuggle extra fields into the transport body
// (the transport contract defines only {id, action_id}).
// `correction_target` (#13) is the routine item a rejected completion actually
// meant, sent only with the routine_match `correct` action. Bounded like any
// other client string; its CONTENT is never trusted here — the transport hands
// it to the resolver, which refuses anything that isn't a live routine item.
// One validation site, not two that can drift.
export const FEED_CORRECTION_TARGET_MAX_CHARS = 500;
// #72 item 4 — bound for the tapped summary heading. Sized off the vocabulary
// rather than picked round: the field only ever legitimately carries one of the
// eight `CONTEST_SECTIONS` headings, so anything materially longer is a client
// that has stopped speaking the contract and should be refused here rather than
// relayed. Headroom over the longest heading leaves room for a ninth without a
// schema change.
export const CONTEST_SECTION_MAX_CHARS = 64;
export const feedActBodySchema = z.object({
  id: z.string().trim().min(1).max(512),
  action_id: z.string().trim().min(1).max(64),
  correction_target: z
    .string()
    .trim()
    .min(1)
    .max(FEED_CORRECTION_TARGET_MAX_CHARS)
    .optional(),
  // #72 item 4 — the summary heading the operator tapped when contesting an
  // attribution inference. Bounded like every other relayed string; the VALUE
  // is not validated here. The transport normalises an unrecognised heading to
  // "unknown" server-side (`_normalise_contested_section`), and duplicating
  // that vocabulary check at the BFF would put a second gate on a value whose
  // one authority is the Python renderer — a heading added there would then be
  // refused here until someone remembered this file.
  contested_section: z
    .string()
    .trim()
    .min(1)
    .max(CONTEST_SECTION_MAX_CHARS)
    .optional(),
});

export type FeedActBody = z.infer<typeof feedActBodySchema>;

// GET /api/feed/list allowlisted query filters, passed through to the transport
// GET /feed/items. Optional + AND-combined server-side; any other query key is
// DROPPED (allowlist — a client can't forward arbitrary query to the transport).
export const FEED_LIST_FILTER_KEYS = ['state', 'mode', 'kind'] as const;

// POST /api/feed/composer-log body (B3-3). CAPTURE-ONLY home-composer telemetry
// appended to a server-side JSONL — never relayed to the transport, never carries
// evidence content. All fields bounded (a log write is a DoS surface too):
// `rule`/`event` are closed enums; `path` is the client route string (≤200,
// stored as DATA only — it NEVER influences the append path, which is server-
// fixed); `dwell_ms` a non-negative int with a 24h sanity ceiling. `z.object`
// STRIPS unknown keys so a client can't smuggle extra fields into the log line.
export const COMPOSER_LOG_MAX_PATH_CHARS = 200;
export const composerLogBodySchema = z.object({
  rule: z.enum(['brief', 'checkin', 'feed']),
  event: z.enum(['composed', 'navigated_away']),
  dwell_ms: z.number().int().min(0).max(24 * 60 * 60 * 1000).optional(),
  path: z.string().max(COMPOSER_LOG_MAX_PATH_CHARS).optional(),
});

export type ComposerLogBody = z.infer<typeof composerLogBodySchema>;

// POST /api/push/subscribe body (B4) — a browser PushSubscription, stored server-
// side so the poller can send to it. All fields bounded; `z.object` STRIPS unknown
// keys. `endpoint` MUST be a URL (the push service origin) — validated but never
// used as a filesystem path. The keys are the base64url ECDH/auth secrets web-push
// needs; bounded generously (p256dh ~88 / auth ~24 chars in practice).
export const PUSH_ENDPOINT_MAX_CHARS = 2048;
// A push endpoint MUST be an https URL. `.url()` alone would accept `http://` and
// even `javascript:` (both parse as URLs), so pin the scheme — a non-https
// endpoint is never a real push service and is rejected 400 at the edge.
export const pushEndpointSchema = z
  .string()
  .url()
  .max(PUSH_ENDPOINT_MAX_CHARS)
  .refine((u) => u.startsWith('https://'), { message: 'endpoint must be an https URL' });

export const pushSubscriptionSchema = z.object({
  endpoint: pushEndpointSchema,
  expirationTime: z.number().nullable().optional(),
  keys: z.object({
    p256dh: z.string().min(1).max(512),
    auth: z.string().min(1).max(512),
  }),
});
export type PushSubscriptionBody = z.infer<typeof pushSubscriptionSchema>;

// DELETE /api/push/subscribe body — remove the subscription with this endpoint.
export const pushUnsubscribeBodySchema = z.object({
  endpoint: pushEndpointSchema,
});
export type PushUnsubscribeBody = z.infer<typeof pushUnsubscribeBodySchema>;

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

// STT idempotency (lost-message #2): a CONTENT-ADDRESSED key — the SHA-256 hex of
// the audio bytes — sent by the browser and RELAYED by the BFF to the backend,
// which dedups (same key → cached transcript, no re-transcribe / no double-charge).
// Because the key is derived from the blob content and VoiceCapture retains the blob
// across a retry, a resend of the SAME audio hashes to the SAME key ⇒ a natural
// cache hit with NO client-side state to mint or hold. The BFF allowlists ONLY this
// header (never arbitrary client headers) and relays it only when it's a well-formed
// 64-char lowercase hex digest.
export const STT_IDEMPOTENCY_HEADER = 'X-Alfred-Stt-Idempotency-Key';

export function isSttIdempotencyKey(v: unknown): v is string {
  return typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
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
  // Cross-instance selector (multi-instance voice switcher). Absent / the home name
  // ⇒ the existing same-instance session path. BFF-only — stripped before relay
  // (mirror chatTurnBodySchema.instance).
  instance: chatInstanceSchema.optional(),
});

export type VoiceOfferBody = z.infer<typeof voiceOfferBodySchema>;

// The server-minted 32-hex session id echoed back to /voice/close. Bounded edge
// guard (the backend is the authority on the exact format); absent ⇒ 400.
export const voiceCloseBodySchema = z.object({
  voice_session_id: z.string().min(1).max(128),
  // Cross-instance selector — routes the close to the session's OWN instance.
  // BFF-only, stripped before relay (the close must reach the backend that minted
  // the session, not the currently-selected one).
  instance: chatInstanceSchema.optional(),
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

// --- In-app bug reporting (#95) ---------------------------------------------
// The BFF's edge validation for POST /api/bugreport/submit. Bounds are IMPORTED
// from `bugReport.ts` rather than re-typed, so the client's textarea cap, this
// schema and the refusal copy can never disagree with each other — three
// literals that must match is three chances to drift, and the one that drifts
// silently refuses reports the box would have accepted.
//
// The box re-validates every one of these. This layer exists so an oversize or
// malformed payload is refused at the edge rather than relayed onward, not
// because the box trusts it.

export const bugReportBodySchema = z.object({
  description: z
    .string()
    .max(MAX_BUGREPORT_DESCRIPTION_CHARS)
    .refine((s) => s.trim().length > 0, { message: 'A description is required.' }),
  context: z.object({
    route: z.string().max(512).default(''),
    instance: z.string().max(120).default(''),
    user_agent: z.string().max(512).default(''),
    // Non-negative and integral; the client already floors these, and a
    // negative viewport is a malformed client rather than a small screen.
    viewport_w: z.number().int().min(0).max(100000).default(0),
    viewport_h: z.number().int().min(0).max(100000).default(0),
    app_version: z.string().max(120).default(''),
    ts: z.string().max(64).default(''),
  }),
  // Bounded by the base64-inflated byte cap (4/3 plus padding slack) so an
  // oversize screenshot is refused HERE rather than buffered onward to the box.
  screenshot_b64: z
    .string()
    .max(Math.ceil((MAX_BUGREPORT_SCREENSHOT_BYTES * 4) / 3) + 4096)
    .optional(),
  screenshot_media_type: z.enum(ALLOWED_SHOT_MIME).optional(),
}).superRefine((val, ctx) => {
  // A media type with no image, or an image with no media type, is a client
  // bug — and one that would otherwise be stored with a GUESSED extension.
  // Refusing the pair outright is better than filing a report whose attachment
  // has a name that disagrees with its bytes.
  if (val.screenshot_media_type && !val.screenshot_b64) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['screenshot_b64'],
      message: 'A screenshot media type was sent without any screenshot data.',
    });
  }
});

export type BugReportBody = z.infer<typeof bugReportBodySchema>;
