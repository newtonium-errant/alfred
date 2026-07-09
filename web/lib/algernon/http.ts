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

// Browser→BFF timeout (CONTRACT S8). Sized ABOVE the BFF→transport budget (~60s)
// so a wedged turn surfaces the BFF's own 504 gateway_timeout rather than racing
// it with a client-side abort. A `timeout` ApiError feeds the same recovery/
// reconcile path as a network error. The streaming path (chatApi.stream) uses a
// raw fetch and is EXEMPT — it's long-lived behind SSE keep-alive.
const DEFAULT_BROWSER_TIMEOUT_MS = 70000;

async function fetchWithTimeout(
  url: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (e) {
    if (controller.signal.aborted) {
      throw new ApiError(0, 'timeout', 'the request timed out');
    }
    // Network failure reaching our own BFF (offline, etc.).
    throw new ApiError(0, 'network_error', (e as Error).message);
  } finally {
    clearTimeout(timer);
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

export async function postJson<T>(
  url: string,
  payload: unknown,
  opts: { timeoutMs?: number } = {},
): Promise<T> {
  const res = await fetchWithTimeout(
    url,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    },
    opts.timeoutMs ?? DEFAULT_BROWSER_TIMEOUT_MS,
  );
  return parseOrThrow<T>(res);
}

export async function getJson<T>(url: string, opts: { timeoutMs?: number } = {}): Promise<T> {
  const res = await fetchWithTimeout(url, { method: 'GET' }, opts.timeoutMs ?? DEFAULT_BROWSER_TIMEOUT_MS);
  return parseOrThrow<T>(res);
}

// POST a binary blob (audio) with the blob's mime as Content-Type (NOT
// application/json), routing the response through the same parseOrThrow / ApiError
// machinery so STT edge errors (413/415/401/502) surface as the same ApiError the
// chat UI already understands. Used by the STT client (→ BFF /api/stt/transcribe).
export async function postBlob<T>(
  url: string,
  blob: Blob,
  contentType: string,
  opts: { timeoutMs?: number; headers?: Record<string, string> } = {},
): Promise<T> {
  // A GENEROUS bound (STT of a long note on flaky LTE is slow) so a legit long
  // transcribe completes, but a DEAD connection surfaces as a clean timeout →
  // retry affordance, instead of hanging on the OS TCP timeout for minutes.
  const res = await fetchWithTimeout(
    url,
    {
      method: 'POST',
      headers: { 'Content-Type': contentType, ...(opts.headers ?? {}) },
      body: blob,
    },
    opts.timeoutMs ?? DEFAULT_BROWSER_TIMEOUT_MS,
  );
  return parseOrThrow<T>(res);
}
