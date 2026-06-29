import { getJson, postJson } from './http';
import type {
  ChatHistoryResponse,
  ChatKind,
  ChatOpenResponse,
  ChatTurnResponse,
  IngestSubmitResponse,
  IngestTargetsResponse,
} from './types';
import type { IngestBody } from './schemas';

// BROWSER-side chat client. Talks ONLY to the same-origin BFF (`/api/chat/*`),
// never the transport directly — the BFF holds the peer token + relays the
// session token. Errors surface as `ApiError` (see ./http).

export const chatApi = {
  open: (): Promise<ChatOpenResponse> => postJson<ChatOpenResponse>('/api/chat/open', {}),
  turn: (
    sessionKey: string,
    message: string,
    kind: ChatKind = 'text',
  ): Promise<ChatTurnResponse> =>
    // `kind` defaults to "text"; transcript-originated sends pass "voice" so the
    // backend turn counter reflects voice-originated turns (decision H).
    postJson<ChatTurnResponse>('/api/chat/turn', {
      session_key: sessionKey,
      message,
      kind,
    }),
  history: (sessionKey: string): Promise<ChatHistoryResponse> =>
    getJson<ChatHistoryResponse>(`/api/chat/history/${encodeURIComponent(sessionKey)}`),
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
