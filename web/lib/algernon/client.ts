import { getJson, postJson } from './http';
import type {
  ChatHistoryResponse,
  ChatOpenResponse,
  ChatTurnResponse,
} from './types';

// BROWSER-side chat client. Talks ONLY to the same-origin BFF (`/api/chat/*`),
// never the transport directly — the BFF holds the peer token + relays the
// session token. Errors surface as `ApiError` (see ./http).

export const chatApi = {
  open: (): Promise<ChatOpenResponse> => postJson<ChatOpenResponse>('/api/chat/open', {}),
  turn: (sessionKey: string, message: string): Promise<ChatTurnResponse> =>
    // M1 always sends kind: "text" (voice = M2).
    postJson<ChatTurnResponse>('/api/chat/turn', {
      session_key: sessionKey,
      message,
      kind: 'text',
    }),
  history: (sessionKey: string): Promise<ChatHistoryResponse> =>
    getJson<ChatHistoryResponse>(`/api/chat/history/${encodeURIComponent(sessionKey)}`),
};
