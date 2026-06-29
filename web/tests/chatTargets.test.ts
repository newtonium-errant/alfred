import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  TransportConfigError,
  callChatTo,
  listCrossInstanceChatTargets,
  resolveChatTarget,
} from '../lib/algernon/transport';

// Locks the cross-instance CHAT target resolution (per-target env, fail-closed,
// distinct `web` peer token) + the relay header contract (Bearer target token +
// X-Alfred-Client + X-Alfred-User asserted name, NO session token).

const ORIG_ENV = { ...process.env };

function clearChatEnv() {
  for (const key of Object.keys(process.env)) {
    if (key.startsWith('ALFRED_WEB_CHAT_')) delete process.env[key];
  }
}

beforeEach(() => {
  clearChatEnv();
  process.env.ALFRED_WEB_TRANSPORT_URL = 'http://transport.test:8891/';
  process.env.ALFRED_WEB_PEER_TOKEN = 'home-chat-token';
});

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('listCrossInstanceChatTargets', () => {
  it('returns a target only when BOTH url and token are set (fail-closed)', () => {
    process.env.ALFRED_WEB_CHAT_KALLE_URL = 'http://127.0.0.1:8892';
    process.env.ALFRED_WEB_CHAT_KALLE_TOKEN = 'kalle-web';
    // HYPATIA has a URL but no token → must be skipped.
    process.env.ALFRED_WEB_CHAT_HYPATIA_URL = 'http://127.0.0.1:8893';

    const targets = listCrossInstanceChatTargets();
    expect(targets.map((t) => t.name)).toEqual(['KALLE']);
    expect(targets[0].label).toBe('KALLE'); // defaults to the name
  });

  it('uses the optional label, sorts by label, never leaks url/token', () => {
    process.env.ALFRED_WEB_CHAT_KALLE_URL = 'http://127.0.0.1:8892';
    process.env.ALFRED_WEB_CHAT_KALLE_TOKEN = 'k';
    process.env.ALFRED_WEB_CHAT_KALLE_LABEL = 'KAL-LE';
    process.env.ALFRED_WEB_CHAT_VERA_URL = 'http://127.0.0.1:8894';
    process.env.ALFRED_WEB_CHAT_VERA_TOKEN = 'v';
    process.env.ALFRED_WEB_CHAT_VERA_LABEL = 'VERA';

    const targets = listCrossInstanceChatTargets();
    expect(targets.map((t) => t.label)).toEqual(['KAL-LE', 'VERA']); // sorted
    const json = JSON.stringify(targets);
    expect(json).not.toContain('127.0.0.1');
    expect(json).not.toContain('http');
  });

  it('returns an empty array when nothing is configured', () => {
    expect(listCrossInstanceChatTargets()).toEqual([]);
  });
});

describe('resolveChatTarget', () => {
  it('resolves a configured target (trims trailing slash, carries the client)', () => {
    process.env.ALFRED_WEB_CHAT_KALLE_URL = 'http://127.0.0.1:8892/';
    process.env.ALFRED_WEB_CHAT_KALLE_TOKEN = 'kalle-web';
    const t = resolveChatTarget('KALLE');
    expect(t.baseUrl).toBe('http://127.0.0.1:8892');
    expect(t.token).toBe('kalle-web');
    expect(t.client).toBe('web');
  });

  it('throws when the env pair is missing', () => {
    expect(() => resolveChatTarget('KALLE')).toThrow(TransportConfigError);
  });

  it('throws on a malformed target name (no arbitrary env probing)', () => {
    expect(() => resolveChatTarget('a b')).toThrow(TransportConfigError);
    expect(() => resolveChatTarget('../secret')).toThrow(TransportConfigError);
  });
});

describe('callChatTo', () => {
  beforeEach(() => {
    process.env.ALFRED_WEB_CHAT_KALLE_URL = 'http://127.0.0.1:8892';
    process.env.ALFRED_WEB_CHAT_KALLE_TOKEN = 'kalle-web';
  });

  it('uses the TARGET token + X-Alfred-User (NOT a session token) + JSON body', async () => {
    const fetchMock = vi.fn(async () => ({ status: 200, json: async () => ({ reply: 'hi' }) }));
    vi.stubGlobal('fetch', fetchMock);

    const res = await callChatTo('KALLE', 'POST', '/chat/turn', {
      body: { session_key: 'k', message: 'hi' },
      userName: 'andrew',
    });

    expect(res.status).toBe(200);
    const [url, init] = fetchMock.mock.calls[0] as any[];
    expect(url).toBe('http://127.0.0.1:8892/chat/turn');
    expect(init.headers.Authorization).toBe('Bearer kalle-web'); // target token, not home token
    expect(init.headers['X-Alfred-Client']).toBe('web');
    expect(init.headers['X-Alfred-User']).toBe('andrew');
    expect(init.headers['X-Alfred-Session']).toBeUndefined(); // relay path: no session token
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(init.body).toBe(JSON.stringify({ session_key: 'k', message: 'hi' }));
  });

  it('omits Content-Type + body on a GET (history relay) but keeps the asserted user', async () => {
    const fetchMock = vi.fn(async () => ({ status: 200, json: async () => ({ turns: [] }) }));
    vi.stubGlobal('fetch', fetchMock);

    await callChatTo('KALLE', 'GET', '/chat/history/abc', { userName: 'andrew' });

    const [, init] = fetchMock.mock.calls[0] as any[];
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
    expect(init.headers['Content-Type']).toBeUndefined();
    expect(init.headers['X-Alfred-User']).toBe('andrew');
  });
});
