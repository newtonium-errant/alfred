// Typed mirror of the FROZEN backend /chat contract (src/alfred/web/routes_chat.py).
// These shapes are the cross-agent contract; do not drift them without a sync
// from the team-lead (the backend owns the contract).

export type ChatRole = 'user' | 'assistant';
export type ChatKind = 'text' | 'voice';

// One turn in the flattened web history — matches the backend's
// `_flatten_transcript_for_web` output ({role, text, ts}). `ts` is the turn's
// stamp ('' for pre-stamp records).
export interface HistoryTurn {
  role: ChatRole;
  text: string;
  ts: string;
}

// POST /chat/open  → { session_key }
export interface ChatOpenResponse {
  session_key: string;
}

// POST /chat/turn  { session_key, message, kind } → { reply, session_key, ts, user_ts }
// `ts` = assistant turn stamp, `user_ts` = user turn stamp (both ISO-8601 UTC).
// Team-lead-APPROVED additive contract bump (BUILD_DECISIONS §1 / decision G):
// both default to '' on the backend so the fields are always present.
export interface ChatTurnResponse {
  reply: string;
  session_key: string;
  ts: string;
  user_ts: string;
  // Retry-safety (CONTRACT S6): true when the backend returned a cached result
  // for a repeated idempotency_key instead of re-running the turn. Optional —
  // absent on a fresh turn and on old backends.
  deduped?: boolean;
}

// --- Streamed turn (SSE) — CONTRACT §1 --------------------------------------
// The typed mirror of the /chat/stream SSE frames. `status` frames surface tool
// activity on the in-flight turn; the terminal `done` frame's data IS a
// ChatTurnResponse (byte-identical to /chat/turn); `error` is the standard
// envelope. Keep-alive comment frames carry no event (see ./sse).
export interface StreamStatusEvent {
  phase: string;
  tool?: string;
  iteration?: number;
}
export type StreamDoneEvent = ChatTurnResponse;
export interface StreamErrorEvent {
  error: string;
  detail?: string;
  // The server's retryability verdict (#94), present when the engine-error
  // classifier recognised the failure. Absent means it abstained — which is
  // NOT the same as false; the client falls back to its own code list.
  retryable?: boolean;
}

// GET /chat/active → the caller's live session key, or null when there is none
// (#94c). An explicit null, NOT a 404: "you have no session" is a normal answer
// to a normal question, and a 404 would push the client back toward treating
// absence as an error — the reflex that produced the open-storm.
export interface ChatActiveResponse {
  session_key: string | null;
  turns: number;
}

// GET /chat/history/{session_key} → { turns: [...] }
export interface ChatHistoryResponse {
  turns: HistoryTurn[];
}

// --- Cross-instance chat (multi-instance switcher, Model B) ------------------
// One instance the owner can chat with. `name` is the routing selector that the
// picker round-trips (the home instance's display name, or a cross-instance env
// segment like KALLE); `label` is the display name; `home` flags the default
// session-path instance. NO secrets — the BFF holds every target URL/token
// server-side; the browser only sees {name, label, home}.
export interface ChatTarget {
  name: string;
  label: string;
  home?: boolean;
}

// GET /api/chat/targets → the configured chat instances (home + cross-instance).
export interface ChatTargetsResponse {
  targets: ChatTarget[];
}

// --- Notifications (parity #22, poll slice) ----------------------------------
// Typed mirror of the backend notification tray
// (src/alfred/web/routes_notify.py — the backend owns the contract). One
// entry per KAL-LE ticket-created notice (web_notify-tagged peer notices in
// general); `ticket_uid`/`issue_url` are '' for non-ticket notices.
export interface NotificationItem {
  id: string;
  text: string;
  precedence: string;
  source: string;
  ticket_uid?: string;
  issue_url?: string;
  // #76 — the ticket's content, carried on the notice so the card can expand
  // and be READ. Optional: entries written before #76 simply have none.
  ticket_body?: string;
  ticket_body_truncated?: boolean;
  issue_number?: number;
  ts: string;
  read: boolean;
  // #86 — terminal for rendering. A dismissed entry never reaches the client
  // (the backend filters it), so this is present for the audit/debug shape
  // rather than for the tray, and is optional because pre-#86 entries lack it.
  dismissed?: boolean;
}

// GET /api/chat/notifications → the caller's own tray, newest-first. An empty
// tray is the backend's ILB 200 { notifications: [], unread: 0 } — never a 404.
export interface NotificationsResponse {
  notifications: NotificationItem[];
  unread: number;
}

// POST /api/chat/notifications/ack { ids } → { acked, unread }. Idempotent —
// re-acking acks 0 and never errors.
export interface NotificationsAckResponse {
  acked: number;
  unread: number;
}

// POST /api/chat/notifications/dismiss { ids } → { dismissed, unread } (#86).
// Clearing, not reading: the entry stops being listed anywhere (tray AND the
// daily brief) while staying in the store for audit. Idempotent — re-dismissing
// dismisses 0 and never errors.
// GET /api/batch/targets → the instances a scan batch can be sent to (#90).
// `home` marks the deployment's own instance (the default). Metadata only — no
// URL or token ever reaches the browser.
export interface BatchTarget {
  name: string;
  label: string;
  home: boolean;
}

export interface BatchTargetsResponse {
  targets: BatchTarget[];
}

export interface NotificationsDismissResponse {
  dismissed: number;
  unread: number;
}

// The backend error envelope: { error: <code>, detail?: <string> }.
export interface ApiErrorBody {
  error: string;
  detail?: string;
}

// A message as the chat UI tracks it: a HistoryTurn plus a stable client-side id
// (React key). No server round-trip needed to render it.
export interface ChatMessage {
  id: string;
  role: ChatRole;
  text: string;
  ts: string;
}

// The signed-in user, DISPLAY ONLY. Authorization is the instance-signed session
// token (never this); the name/role here are just what the UI shows. Mirrors the
// non-secret fields of POST /auth/verify's response.
export interface SessionUser {
  name: string;
  role: string;
}

// POST /auth/verify → { session_token, name, role, exp }. `session_token` is the
// instance-signed credential the BFF stores httpOnly + relays as X-Alfred-Session.
export interface AuthVerifyResponse {
  session_token: string;
  name: string;
  role: string;
  exp?: number;
}

// --- Cross-instance ingest (BUILD_DECISIONS §2 / §3) -------------------------
// Typed mirror of the backend transport POST /vault/ingest contract + the BFF
// ingest routes. The BFF holds each target's peer token server-side; the browser
// talks ONLY to the same-origin BFF and never sees a target URL or token.

// One configurable ingest target the operator can write to. `name` is the
// server-side env segment (round-trips back as the submit `target`); `label` is
// the display name; `recordTypes` constrains the type picker. NO secrets.
export interface IngestTarget {
  name: string;
  label: string;
  recordTypes: string[];
}

// GET /api/ingest/targets → the configured targets (data-driven from env).
export interface IngestTargetsResponse {
  targets: IngestTarget[];
}

// POST /api/ingest/submit → the backend /vault/ingest result (verbatim relay).
export interface IngestSubmitResponse {
  status: string;
  path: string;
  record_type: string;
  instance: string;
}

// --- Web STT (BUILD_DECISIONS §4) -------------------------------------------
// POST /stt/transcribe response. The editable transcript is the human-in-the-loop
// correction surface (self-correcting design); low_confidence/empty/degraded are
// explicit signals (intentionally-left-blank) so a fallible transcript is never
// silently auto-committed. `transcript` is always present (may be '').
export interface SttTranscribeResponse {
  transcript: string;
  backend_used?: string;
  fell_back?: boolean;
  tier?: string;
  low_confidence?: boolean;
  empty?: boolean;
  degraded?: boolean;
}

// --- Web voice (V0) — typed mirror of the FROZEN backend contract (CONTRACT §2)
// The backend (src/alfred/web/routes_voice.py) owns these shapes; do not drift
// them without a team-lead sync. The BFF relays them verbatim — the browser talks
// ONLY to the same-origin /api/voice/* routes and never sees a peer token.

// One ICE server the browser should use in its RTCPeerConfiguration. `urls` is a
// list (STUN/TURN); TURN entries additionally carry username/credential. V0 with a
// public-IP host-candidate server usually returns an EMPTY list.
export interface VoiceIceServer {
  urls: string[];
  username?: string;
  credential?: string;
}

// One of the CALLER'S OWN live sessions (yours-scoped — never a global registry,
// security W9). Surfaced by GET /voice/config so the UI could reconcile a stale
// call; V0 uses it only to know a prior session is still open.
export interface VoiceSessionSummary {
  voice_session_id: string;
  connection_state: string;
  age_seconds: number;
}

// GET /voice/config → the capability/ICE/own-sessions probe. `available` false
// (reason 'aiortc_missing') means the routes are mounted but the engine is missing
// — the UI treats it exactly like disabled (no voice affordance).
export interface VoiceConfigResponse {
  available: boolean;
  reason: string | null;
  ice_servers: VoiceIceServer[];
  max_sessions: number;
  yours: VoiceSessionSummary[];
  // V1 (CONTRACT §17b, additive): which server pipeline is active. Only
  // 'assistant' streams to cloud STT and emits the dictation `ready` — so only
  // then is a missing/dead dictation channel FATAL. Absent (older backend) or
  // 'echo' ⇒ the benign dictation-unavailable path. Tolerate absence.
  pipeline?: 'echo' | 'assistant';
}

// POST /voice/offer → the SDP answer + server-minted id. `expires_at` is
// now + max_session_seconds (ISO-8601). Media flows DIRECT browser↔server after
// this; no further signalling until close.
export interface VoiceOfferResponse {
  voice_session_id: string;
  sdp: string;
  type: 'answer';
  expires_at: string;
}

// POST /voice/close → idempotent + owner-bound (CONTRACT §1.5). `closed:false`
// with reason 'not_found' covers unknown / already-closed / another user's id —
// indistinguishable by design (no existence leak).
export interface VoiceCloseResponse {
  closed: boolean;
  reason?: string;
}
