import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks GET /api/brief/audio — the session-gated RAW proxy of the day's briefing mp3.
// It forwards audio/mpeg bytes on a hit, a JSON ILB state otherwise, and — the raw
// proxy's own responsibility since callTransportRaw returns the status verbatim — maps
// the AMBIGUOUS upstream 401: wrong_peer → 502 (never a fake logout) vs invalid_session →
// relayed 401 (re-login). speed is clamped to 0.7-1.2 before it hits the query.

const { mockResolveSessionToken, mockCallTransportRaw } = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockCallTransportRaw: vi.fn(),
}));
vi.mock('../lib/algernon/identity', () => ({ resolveSessionToken: mockResolveSessionToken }));
vi.mock('../lib/algernon/transport', () => ({ callTransportRaw: mockCallTransportRaw }));

import handler from '../pages/api/brief/audio';

function req(query: Record<string, string> = {}, method = 'GET'): NextApiRequest {
  return { method, headers: {}, cookies: {}, query } as unknown as NextApiRequest;
}
function mockRes() {
  const json = vi.fn();
  const send = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json, send }));
  return { res: { status, json, send, setHeader } as unknown as NextApiResponse, status, json, send, setHeader };
}
// A fake upstream Response with only the members the proxy reads.
function upstream(opts: { status: number; contentType: string; jsonBody?: unknown; bytes?: Uint8Array; cache?: string }): Response {
  return {
    status: opts.status,
    headers: {
      get: (k: string) => {
        const key = k.toLowerCase();
        if (key === 'content-type') return opts.contentType;
        if (key === 'x-brief-audio-cache') return opts.cache ?? null;
        return null;
      },
    },
    arrayBuffer: async () => (opts.bytes ?? new Uint8Array()).buffer,
    json: async () => {
      if (opts.jsonBody === undefined) throw new Error('not json');
      return opts.jsonBody;
    },
  } as unknown as Response;
}

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockCallTransportRaw.mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe('GET /api/brief/audio', () => {
  it('401s with no session cookie (before any transport call)', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(req(), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransportRaw).not.toHaveBeenCalled();
  });

  it('forwards audio/mpeg bytes on a hit, with the cache header + verbatim status', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransportRaw.mockResolvedValue(upstream({ status: 200, contentType: 'audio/mpeg', bytes: new Uint8Array([1, 2, 3]), cache: 'hit' }));
    const { res, status, send, setHeader } = mockRes();
    await handler(req(), res);
    expect(setHeader).toHaveBeenCalledWith('Content-Type', 'audio/mpeg');
    expect(setHeader).toHaveBeenCalledWith('X-Brief-Audio-Cache', 'hit');
    expect(status).toHaveBeenCalledWith(200);
    expect(send).toHaveBeenCalled();
  });

  it('validates speed to the 0.7-1.2 clamp before the query (out-of-range omitted, in-range forwarded)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransportRaw.mockResolvedValue(upstream({ status: 200, contentType: 'application/json', jsonBody: { state: 'no_brief' } }));
    await handler(req({ speed: '9' }), mockRes().res); // out of range → omitted
    expect(mockCallTransportRaw).toHaveBeenCalledWith('GET', '/web/brief/audio', { sessionToken: 'tok' });
    mockCallTransportRaw.mockClear();
    await handler(req({ speed: '0.9' }), mockRes().res); // in range → forwarded
    expect(mockCallTransportRaw).toHaveBeenCalledWith('GET', '/web/brief/audio?speed=0.9', { sessionToken: 'tok' });
  });

  it('relays a JSON ILB state (no_brief) verbatim', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransportRaw.mockResolvedValue(upstream({ status: 200, contentType: 'application/json', jsonBody: { state: 'no_brief' } }));
    const { res, status, json } = mockRes();
    await handler(req(), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ state: 'no_brief' });
  });

  it('a wrong_peer upstream 401 → 502 (the raw proxy maps it; never a fake logout)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransportRaw.mockResolvedValue(upstream({ status: 401, contentType: 'application/json', jsonBody: { error: 'wrong_peer' } }));
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { res, status, json } = mockRes();
    await handler(req(), res);
    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'brief_upstream_unavailable' });
  });

  it('an invalid_session upstream 401 → RELAYED 401 (real expiry → re-login, not mapped)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransportRaw.mockResolvedValue(upstream({ status: 401, contentType: 'application/json', jsonBody: { error: 'invalid_session' } }));
    const { res, status, json } = mockRes();
    await handler(req(), res);
    expect(status).toHaveBeenCalledWith(401); // ← reddens if a blanket map breaks re-login
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
  });
});
