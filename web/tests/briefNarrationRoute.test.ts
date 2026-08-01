import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks GET /api/brief/narration — the session-gated BFF relay of the day's narration
// JSON. The load-bearing pins are the AMBIGUOUS upstream-401 pair: wrong_peer → 502
// (peer misconfig, never a fake logout) vs invalid_session → relayed 401 (real expiry →
// re-login). A blanket map (like feed/*) would break re-login here, so we discriminate.

const { mockResolveSessionToken, mockCallTransport } = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockCallTransport: vi.fn(),
}));
vi.mock('../lib/algernon/identity', () => ({ resolveSessionToken: mockResolveSessionToken }));
vi.mock('../lib/algernon/transport', () => ({ callTransport: mockCallTransport }));

import handler from '../pages/api/brief/narration';

function req(method = 'GET'): NextApiRequest {
  return { method, headers: {}, cookies: {}, query: {} } as unknown as NextApiRequest;
}
function mockRes() {
  const json = vi.fn();
  const status = vi.fn(() => ({ json }));
  const setHeader = vi.fn();
  return { res: { status, json, setHeader } as unknown as NextApiResponse, status, json, setHeader };
}

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockCallTransport.mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe('GET /api/brief/narration', () => {
  it('405s a non-GET', async () => {
    const { res, status } = mockRes();
    await handler(req('POST'), res);
    expect(status).toHaveBeenCalledWith(405);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('401s with no session cookie (the BFF\'s own gate, before any transport call)', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(req(), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays the narration dict on 200 with the session token', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const payload = { brief_date: '2026-08-01', segments: [], total_words: 0, empty: true };
    mockCallTransport.mockResolvedValue({ status: 200, body: payload });
    const { res, status, json } = mockRes();
    await handler(req(), res);
    expect(mockCallTransport).toHaveBeenCalledWith('GET', '/web/brief/narration', { sessionToken: 'tok' });
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith(payload);
  });

  it('relays the ILB no_brief 200 verbatim', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: { state: 'no_brief' } });
    const { res, status, json } = mockRes();
    await handler(req(), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ state: 'no_brief' });
  });

  it('a wrong_peer upstream 401 → 502 (peer misconfig, never a fake logout)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 401, body: { error: 'wrong_peer' } });
    vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { res, status, json } = mockRes();
    await handler(req(), res);
    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'brief_upstream_unavailable' });
  });

  it('an invalid_session upstream 401 → RELAYED 401 (real expiry → re-login, not mapped)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 401, body: { error: 'invalid_session' } });
    const { res, status, json } = mockRes();
    await handler(req(), res);
    expect(status).toHaveBeenCalledWith(401); // ← reddens if a blanket map breaks re-login
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
  });
});
