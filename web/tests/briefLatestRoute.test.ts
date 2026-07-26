import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the BFF outbound-read relay (#30 READ-ON-OPEN): the kind allowlist
// (brief | daily_sync → 400 unknown_kind BEFORE any transport call, since the
// kind is interpolated into the transport path), the session gate (401), and
// the verbatim pass-through of the backend's ILB empty 200 {date:null}.

const { mockResolveSessionToken, mockCallTransport } = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockCallTransport: vi.fn(),
}));

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransport: mockCallTransport,
}));

import handler, { OUTBOUND_KINDS } from '../pages/api/brief/latest';

function briefReq(kind?: string, method = 'GET'): NextApiRequest {
  return {
    method,
    headers: {},
    cookies: {},
    query: kind === undefined ? {} : { kind },
  } as unknown as NextApiRequest;
}

function mockRes() {
  const json = vi.fn();
  const status = vi.fn(() => ({ json }));
  const setHeader = vi.fn();
  const res = { status, json, setHeader } as unknown as NextApiResponse;
  return { res, status, json, setHeader };
}

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockCallTransport.mockReset();
});

afterEach(() => vi.restoreAllMocks());

describe('GET /api/brief/latest (#30)', () => {
  it('exports exactly the brief + daily_sync kinds', () => {
    expect([...OUTBOUND_KINDS]).toEqual(['brief', 'daily_sync']);
  });

  it('405s a non-GET method', async () => {
    const { res, status } = mockRes();
    await handler(briefReq('brief', 'POST'), res);
    expect(status).toHaveBeenCalledWith(405);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('401s when there is no session cookie', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(briefReq('brief'), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('400s an unknown kind BEFORE any transport call', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await handler(briefReq('ticket'), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'unknown_kind' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('400s a missing kind', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status } = mockRes();
    await handler(briefReq(undefined), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays kind=brief with the session token and passes the body through', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const payload = { kind: 'brief', date: '2026-07-19', markdown: '# Hi\n' };
    mockCallTransport.mockResolvedValue({ status: 200, body: payload });
    const { res, status, json } = mockRes();
    await handler(briefReq('brief'), res);
    expect(mockCallTransport).toHaveBeenCalledWith('GET', '/web/outbound/brief/latest', {
      sessionToken: 'tok',
    });
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith(payload);
  });

  it('relays kind=daily_sync and passes the ILB empty 200 through verbatim', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const empty = { kind: 'daily_sync', date: null, markdown: null };
    mockCallTransport.mockResolvedValue({ status: 200, body: empty });
    const { res, status, json } = mockRes();
    await handler(briefReq('daily_sync'), res);
    expect(mockCallTransport).toHaveBeenCalledWith('GET', '/web/outbound/daily_sync/latest', {
      sessionToken: 'tok',
    });
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith(empty);
  });

  it('maps a backend 401 through to the client (401→401)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 401, body: { error: 'invalid_session' } });
    const { res, status, json } = mockRes();
    await handler(briefReq('brief'), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
  });
});
