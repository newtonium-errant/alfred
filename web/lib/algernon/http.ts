import type { ApiErrorBody } from './types';

// BROWSER-side fetch helpers shared by the chat + auth clients. Both talk ONLY to
// the same-origin BFF (`/api/*`), never the transport directly — the BFF holds
// the peer token + relays identity.

/** A failed API call. `code` is the backend/BFF `error` code; `status` the HTTP status. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail?: string;

  constructor(status: number, code: string, detail?: string) {
    super(detail ? `${code}: ${detail}` : code);
    this.name = 'ApiError';
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
    throw new ApiError(res.status, err.error || 'request_failed', err.detail);
  }
  return body as T;
}

export async function postJson<T>(url: string, payload: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  } catch (e) {
    // Network failure reaching our own BFF (offline, etc.).
    throw new ApiError(0, 'network_error', (e as Error).message);
  }
  return parseOrThrow<T>(res);
}

export async function getJson<T>(url: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, { method: 'GET' });
  } catch (e) {
    throw new ApiError(0, 'network_error', (e as Error).message);
  }
  return parseOrThrow<T>(res);
}

// POST a binary blob (audio) with the blob's mime as Content-Type (NOT
// application/json), routing the response through the same parseOrThrow / ApiError
// machinery so STT edge errors (413/415/401/502) surface as the same ApiError the
// chat UI already understands. Used by the STT client (→ BFF /api/stt/transcribe).
export async function postBlob<T>(url: string, blob: Blob, contentType: string): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': contentType },
      body: blob,
    });
  } catch (e) {
    throw new ApiError(0, 'network_error', (e as Error).message);
  }
  return parseOrThrow<T>(res);
}
