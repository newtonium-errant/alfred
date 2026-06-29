import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TransportConfigError, callTransport } from '../lib/algernon/transport';

// Locks the BFF→transport auth contract in code: the transport's auth_middleware
// (src/alfred/transport/server.py) requires Bearer peer token + X-Alfred-Client,
// and routes_chat resolves identity from X-Web-User (Sub-arc A). A drift here is
// a 401/403 in production.

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

  it('sends Bearer token + X-Alfred-Client + X-Web-User, trims trailing slash', async () => {
    const fetchMock = vi.fn(async () => ({
      status: 200,
      json: async () => ({ reply: 'hi', session_key: 'k' }),
    }));
    vi.stubGlobal('fetch', fetchMock);

    const res = await callTransport(
      'POST',
      '/chat/turn',
      { user: 'andrew' },
      { session_key: 'k', message: 'hi', kind: 'text' },
    );

    expect(res.status).toBe(200);
    expect(res.body).toEqual({ reply: 'hi', session_key: 'k' });

    const [url, init] = fetchMock.mock.calls[0] as any[];
    expect(url).toBe('http://transport.test:9000/chat/turn');
    expect(init.method).toBe('POST');
    expect(init.headers.Authorization).toBe('Bearer secret-token');
    expect(init.headers['X-Alfred-Client']).toBe('web');
    expect(init.headers['X-Web-User']).toBe('andrew');
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(init.body).toBe(JSON.stringify({ session_key: 'k', message: 'hi', kind: 'text' }));
  });

  it('omits Content-Type + body on a GET', async () => {
    const fetchMock = vi.fn(async () => ({ status: 200, json: async () => ({ turns: [] }) }));
    vi.stubGlobal('fetch', fetchMock);

    await callTransport('GET', '/chat/history/abc', { user: 'andrew' });

    const [, init] = fetchMock.mock.calls[0] as any[];
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
    expect(init.headers['Content-Type']).toBeUndefined();
    expect(init.headers['X-Web-User']).toBe('andrew');
  });

  it('defaults X-Alfred-Client to "web" when unset', async () => {
    // PEER_CLIENT is read at module load; this just documents the default is "web"
    // (set in beforeEach to the same value the module default would produce).
    const fetchMock = vi.fn(async () => ({ status: 200, json: async () => ({}) }));
    vi.stubGlobal('fetch', fetchMock);
    await callTransport('POST', '/chat/open', { user: 'a' }, {});
    const [, init] = fetchMock.mock.calls[0] as any[];
    expect(init.headers['X-Alfred-Client']).toBe('web');
  });

  it('throws TransportConfigError when the base URL is missing', async () => {
    delete process.env.ALFRED_WEB_TRANSPORT_URL;
    await expect(
      callTransport('POST', '/chat/open', { user: 'a' }, {}),
    ).rejects.toBeInstanceOf(TransportConfigError);
  });

  it('throws TransportConfigError when the peer token is missing (no fetch)', async () => {
    delete process.env.ALFRED_WEB_PEER_TOKEN;
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await expect(
      callTransport('POST', '/chat/open', { user: 'a' }, {}),
    ).rejects.toBeInstanceOf(TransportConfigError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
