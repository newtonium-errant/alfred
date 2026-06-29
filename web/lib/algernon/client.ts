import { getJson, postJson } from './http';
import type {
  ChatHistoryResponse,
  ChatKind,
  ChatOpenResponse,
  ChatTargetsResponse,
  ChatTurnResponse,
  IngestSubmitResponse,
  IngestTargetsResponse,
} from './types';
import type { IngestBody } from './schemas';

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
    }),
  history: (sessionKey: string, instance?: string): Promise<ChatHistoryResponse> =>
    getJson<ChatHistoryResponse>(
      `/api/chat/history/${encodeURIComponent(sessionKey)}` +
        (instance ? `?instance=${encodeURIComponent(instance)}` : ''),
    ),
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
