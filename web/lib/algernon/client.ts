import type {
  ApiErrorBody,
  ChatHistoryResponse,
  ChatOpenResponse,
  ChatTurnResponse,
} from './types';

// BROWSER-side client. Talks ONLY to the same-origin BFF (`/api/chat/*`), never
// the transport directly — the BFF holds the peer token + asserts identity. This
// wrapper just types the calls and normalises errors.

/** A failed chat call. `code` is the backend/BFF `error` code; `status` the HTTP status. */
export class ChatApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail?: string;

  constructor(status: number, code: string, detail?: string) {
    super(detail ? `${code}: ${detail}` : code);
    this.name = 'ChatApiError';
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

async function parseOrThrow<T>(res: Response): Promise<T> {
  let body: unknown = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) {
    const err = (body ?? {}) as Partial<ApiErrorBody>;
    throw new ChatApiError(res.status, err.error || 'request_failed', err.detail);
  }
  return body as T;
}

async function postJson<T>(url: string, payload: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    // Network failure reaching our own BFF (offline, etc.).
    throw new ChatApiError(0, 'network_error', (e as Error).message);
  }
  return parseOrThrow<T>(res);
}

async function getJson<T>(url: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, { method: 'GET' });
  } catch (e) {
    throw new ChatApiError(0, 'network_error', (e as Error).message);
  }
  return parseOrThrow<T>(res);
}

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
