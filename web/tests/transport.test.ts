import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TransportConfigError, callTransport } from '../lib/algernon/transport';

// Locks the BFF→transport auth contract in code. The transport's auth_middleware
// (src/alfred/transport/server.py) requires Bearer peer token + X-Alfred-Client
// on EVERY route; /chat/* identity (B3 live contract) is the verified
// X-Alfred-Session token. A drift here is a 401 in production.

const ORIG_ENV = { ...process.env };

describe('callTransport', () => {
  beforeEach(() => {
    process.env.ALFRED_WEB_TRANSPORT_URL = 'http://transport.test:9000/';
    process.env.ALFRED_WEB_PEER_TOKEN = 'secret-token';
    process.env.ALFRED_WEB_PEER_CLIENT = 'web';
  });

  afterEach(() => {
    process.env = { ...ORIG_ENV };
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('sends Bearer peer token + X-Alfred-Client + X-Alfred-Session; trims slash; no X-Web-User', async () => {
    const fetchMock = vi.fn(async () => ({
      status: 200,
      json: async () => ({ reply: 'hi', session_key: 'k' }),
    }));
    vi.stubGlobal('fetch', fetchMock);

    const res = await callTransport('POST', '/chat/turn', {
      body: { session_key: 'k', message: 'hi', kind: 'text' },
      sessionToken: 'sess-token-abc',
    });

    expect(res.status).toBe(200);
    expect(res.body).toEqual({ reply: 'hi', session_key: 'k' });

    const [url, init] = fetchMock.mock.calls[0] as any[];
    expect(url).toBe('http://transport.test:9000/chat/turn');
    expect(init.method).toBe('POST');
    expect(init.headers.Authorization).toBe('Bearer secret-token');
    expect(init.headers['X-Alfred-Client']).toBe('web');
    expect(init.headers['X-Alfred-Session']).toBe('sess-token-abc');
    expect(init.headers['X-Web-User']).toBeUndefined();
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(init.body).toBe(JSON.stringify({ session_key: 'k', message: 'hi', kind: 'text' }));
  });

  it('omits Content-Type + body on a GET, still sends the session token', async () => {
    const fetchMock = vi.fn(async () => ({ status: 200, json: async () => ({ turns: [] }) }));
    vi.stubGlobal('fetch', fetchMock);

    await callTransport('GET', '/chat/history/abc', { sessionToken: 'tok' });

    const [, init] = fetchMock.mock.calls[0] as any[];
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
    expect(init.headers['Content-Type']).toBeUndefined();
    expect(init.headers['X-Alfred-Session']).toBe('tok');
  });

  it('omits X-Alfred-Session for token-less /auth/* calls but keeps peer auth', async () => {
    const fetchMock = vi.fn(async () => ({ status: 200, json: async () => ({ status: 'sent' }) }));
    vi.stubGlobal('fetch', fetchMock);

    await callTransport('POST', '/auth/login', { body: { email: 'a@b.c' } });

    const [, init] = fetchMock.mock.calls[0] as any[];
    expect(init.headers.Authorization).toBe('Bearer secret-token');
    expect(init.headers['X-Alfred-Client']).toBe('web');
    expect(init.headers['X-Alfred-Session']).toBeUndefined();
    expect(init.body).toBe(JSON.stringify({ email: 'a@b.c' }));
  });

  it('throws TransportConfigError when the base URL is missing', async () => {
    delete process.env.ALFRED_WEB_TRANSPORT_URL;
    await expect(
      callTransport('POST', '/chat/open', { body: {}, sessionToken: 'tok' }),
    ).rejects.toBeInstanceOf(TransportConfigError);
  });

  it('throws TransportConfigError when the peer token is missing (no fetch)', async () => {
    delete process.env.ALFRED_WEB_PEER_TOKEN;
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await expect(
      callTransport('POST', '/chat/open', { body: {}, sessionToken: 'tok' }),
    ).rejects.toBeInstanceOf(TransportConfigError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
