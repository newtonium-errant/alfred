import { getJson, postJson } from './http';
import type {
  ChatHistoryResponse,
  ChatKind,
  ChatOpenResponse,
  ChatTargetsResponse,
  ChatTurnResponse,
  IngestSubmitResponse,
  IngestTargetsResponse,
  NotificationsAckResponse,
  NotificationsResponse,
} from './types';
import type { ImageAttachment, IngestBody } from './schemas';
import type { PlayerPrimer } from './player';

// BROWSER-side chat client. Talks ONLY to the same-origin BFF (`/api/chat/*`),
// never the transport directly — the BFF holds the peer token + relays the
// session token (home) or the asserted user (cross-instance). Errors surface as
// `ApiError` (see ./http).
//
// `instance` (multi-instance switcher) is the routing selector: omit / the home
// name ⇒ the existing same-instance session path; any other configured target ⇒
// the BFF relays to that instance. `idempotencyKey` (retry-safety) is minted per
// logical turn by useChat and resent on retry.

export interface ChatTurnOptions {
  kind?: ChatKind;
  instance?: string;
  idempotencyKey?: string;
  // Carried screenshots (parity #29). Sent as `images` on the turn/stream body;
  // absent / empty ⇒ a text-only turn (byte-identical to the pre-feature path).
  images?: ImageAttachment[];
  // Player-ask on-screen context (C3c). Sent as `primer` on the turn body — the BFF
  // relays it verbatim; the backend validity-gates it (PlayerContextPrimer) and grounds
  // the answer on the slide, or answers un-grounded if invalid. Only the /player ask
  // sends it; a normal /chat turn omits it (byte-identical to the pre-feature path).
  primer?: PlayerPrimer;
}

export const chatApi = {
  targets: (): Promise<ChatTargetsResponse> =>
    getJson<ChatTargetsResponse>('/api/chat/targets'),
  open: (instance?: string): Promise<ChatOpenResponse> =>
    postJson<ChatOpenResponse>('/api/chat/open', instance ? { instance } : {}),
  turn: (
    sessionKey: string,
    message: string,
    opts: ChatTurnOptions = {},
  ): Promise<ChatTurnResponse> =>
    // `kind` defaults to "text"; transcript-originated sends pass "voice" so the
    // backend turn counter reflects voice-originated turns (decision H).
    postJson<ChatTurnResponse>('/api/chat/turn', {
      session_key: sessionKey,
      message,
      kind: opts.kind ?? 'text',
      ...(opts.instance ? { instance: opts.instance } : {}),
      ...(opts.idempotencyKey ? { idempotency_key: opts.idempotencyKey } : {}),
      ...(opts.images && opts.images.length ? { images: opts.images } : {}),
      // Player-ask primer (C3c) — non-streamed turn only (the answer is one text card).
      ...(opts.primer ? { primer: opts.primer } : {}),
    }),
  history: (sessionKey: string, instance?: string): Promise<ChatHistoryResponse> =>
    getJson<ChatHistoryResponse>(
      `/api/chat/history/${encodeURIComponent(sessionKey)}` +
        (instance ? `?instance=${encodeURIComponent(instance)}` : ''),
    ),
  // Notification tray (parity #22, POLL slice — home instance only). The empty
  // tray is the backend's ILB { notifications: [], unread: 0 }, never an error.
  notifications: (): Promise<NotificationsResponse> =>
    getJson<NotificationsResponse>('/api/chat/notifications'),
  ackNotifications: (ids: string[]): Promise<NotificationsAckResponse> =>
    postJson<NotificationsAckResponse>('/api/chat/notifications/ack', { ids }),
  // Opens the SSE turn stream and returns the RAW Response so useChat can read
  // res.body.getReader() (the success signal is the terminal `done` frame, not a
  // JSON body). Validation errors come back as JSON BEFORE the stream begins —
  // useChat checks res.ok / content-type before reading. `signal` lets the caller
  // abort the fetch (e.g. on unmount). NOTE: a non-network failure surfaces via
  // the response status, not a throw; only a true network error throws here.
  stream: (
    sessionKey: string,
    message: string,
    opts: ChatTurnOptions & { signal?: AbortSignal } = {},
  ): Promise<Response> =>
    fetch('/api/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
      body: JSON.stringify({
        session_key: sessionKey,
        message,
        kind: opts.kind ?? 'text',
        ...(opts.instance ? { instance: opts.instance } : {}),
        ...(opts.idempotencyKey ? { idempotency_key: opts.idempotencyKey } : {}),
        ...(opts.images && opts.images.length ? { images: opts.images } : {}),
      }),
      signal: opts.signal,
    }),
};

// BROWSER-side ingest client. Same-origin BFF only (`/api/ingest/*`) — the BFF
// resolves the chosen target's peer token server-side and relays to that
// instance's transport /vault/ingest. Errors surface as `ApiError`.
export const ingestApi = {
  targets: (): Promise<IngestTargetsResponse> =>
    getJson<IngestTargetsResponse>('/api/ingest/targets'),
  submit: (payload: IngestBody): Promise<IngestSubmitResponse> =>
    postJson<IngestSubmitResponse>('/api/ingest/submit', payload),
};
