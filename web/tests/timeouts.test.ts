import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { getJson, postJson, ApiError } from '../lib/algernon/http';
import { TransportTimeoutError, callTransport } from '../lib/algernon/transport';

// Locks the resilience timeouts (CONTRACT S8): browser→BFF aborts surface a
// `timeout` ApiError; BFF→transport aborts surface a TransportTimeoutError
// (→ 504 gateway_timeout). A fetch that never resolves until its signal aborts
// stands in for a wedged hop; a tiny timeout keeps the test fast.

const ORIG_ENV = { ...process.env };

// A fetch that hangs until its AbortSignal fires, then rejects like undici does.
function hangingFetch() {
  return vi.fn(
    (_url: string, init: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        const signal = init.signal;
        if (signal) {
          signal.addEventListener('abort', () => {
            reject(new DOMException('The operation was aborted.', 'AbortError'));
          });
        }
      }),
  );
}

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('http browser→BFF timeout', () => {
  it('postJson throws ApiError("timeout") when the BFF call exceeds the budget', async () => {
    vi.stubGlobal('fetch', hangingFetch());
    await expect(postJson('/api/chat/turn', {}, { timeoutMs: 15 })).rejects.toMatchObject({
      code: 'timeout',
      status: 0,
    });
  });

  it('getJson throws ApiError("timeout") on a hung GET', async () => {
    vi.stubGlobal('fetch', hangingFetch());
    const err = await getJson('/api/chat/targets', { timeoutMs: 15 }).catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('timeout');
  });
});

describe('transport BFF→transport timeout', () => {
  beforeEach(() => {
    process.env.ALFRED_WEB_TRANSPORT_URL = 'http://transport.test:8891';
    process.env.ALFRED_WEB_PEER_TOKEN = 'tok';
    process.env.ALFRED_WEB_TRANSPORT_TIMEOUT_MS = '15';
  });

  it('callTransport throws TransportTimeoutError when the transport hangs', async () => {
    vi.stubGlobal('fetch', hangingFetch());
    await expect(
      callTransport('POST', '/chat/turn', { body: { x: 1 }, sessionToken: 's' }),
    ).rejects.toBeInstanceOf(TransportTimeoutError);
  });
});
