import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the BFF notification relays (parity #22 POLL slice): the session gate
// (401 before any transport call), the verbatim pass-through of the backend's
// ILB empty 200 { notifications: [], unread: 0 }, and the ack body's
// trust-boundary validation (400 BEFORE any transport call).

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

import listHandler from '../pages/api/chat/notifications';
import ackHandler from '../pages/api/chat/notifications/ack';
import dismissHandler from '../pages/api/chat/notifications/dismiss';

function req(method: string, body?: unknown): NextApiRequest {
  return {
    method,
    headers: {},
    cookies: {},
    query: {},
    body,
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

describe('GET /api/chat/notifications (#22)', () => {
  it('405s a non-GET method', async () => {
    const { res, status } = mockRes();
    await listHandler(req('POST'), res);
    expect(status).toHaveBeenCalledWith(405);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('401s when there is no session cookie', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await listHandler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays with the session token and passes the tray through', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const payload = {
      notifications: [
        {
          id: 'abc123',
          text: 'New ticket [bug] Login broken — filed as issue #7',
          precedence: 'R',
          source: 'kal-le',
          ticket_uid: 'vera-20260719-0001',
          issue_url: 'https://github.com/acme/site/issues/7',
          ts: '2026-07-19T12:00:00+00:00',
          read: false,
        },
      ],
      unread: 1,
    };
    mockCallTransport.mockResolvedValue({ status: 200, body: payload });
    const { res, status, json } = mockRes();
    await listHandler(req('GET'), res);
    expect(mockCallTransport).toHaveBeenCalledWith('GET', '/chat/notifications', {
      sessionToken: 'tok',
    });
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith(payload);
  });

  it('passes the ILB empty 200 through verbatim', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const empty = { notifications: [], unread: 0 };
    mockCallTransport.mockResolvedValue({ status: 200, body: empty });
    const { res, status, json } = mockRes();
    await listHandler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith(empty);
  });

  it('maps a backend 401 through to the client (401→401)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({
      status: 401,
      body: { error: 'invalid_session' },
    });
    const { res, status, json } = mockRes();
    await listHandler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
  });

  it('maps a backend 403 (recipient-pin) through to the client', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({
      status: 403,
      body: { error: 'forbidden' },
    });
    const { res, status, json } = mockRes();
    await listHandler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
  });
});

describe('POST /api/chat/notifications/ack (#22)', () => {
  it('405s a non-POST method', async () => {
    const { res, status } = mockRes();
    await ackHandler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(405);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('401s when there is no session cookie', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status } = mockRes();
    await ackHandler(req('POST', { ids: ['abc'] }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it.each([
    [{}, 'ids absent'],
    [{ ids: 'not-a-list' }, 'non-array'],
    [{ ids: [] }, 'empty list'],
    [{ ids: [1, 2] }, 'non-string ids'],
    [{ ids: [''] }, 'empty id'],
    [{ ids: Array.from({ length: 201 }, (_, i) => `id${i}`) }, 'over cap'],
  ])('400s a malformed body BEFORE any transport call (%j)', async (body, _label) => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await ackHandler(req('POST', body), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith(
      expect.objectContaining({ error: 'invalid_request' }),
    );
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays valid ids with the session token and passes {acked, unread} through', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({
      status: 200,
      body: { acked: 1, unread: 0 },
    });
    const { res, status, json } = mockRes();
    await ackHandler(req('POST', { ids: ['abc123'] }), res);
    expect(mockCallTransport).toHaveBeenCalledWith(
      'POST',
      '/chat/notifications/ack',
      { body: { ids: ['abc123'] }, sessionToken: 'tok' },
    );
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ acked: 1, unread: 0 });
  });
});


describe('POST /api/chat/notifications/dismiss (#86)', () => {
  // The clearing door. Every refusal pin asserts the transport was NEVER
  // CALLED, not just the status code: a 401/405 is also what a broken import
  // or a typo'd handler produces, and "nothing was relayed" is the fact that
  // separates a working gate from a coincidence.

  it('405s a non-POST method', async () => {
    const { res, status } = mockRes();
    await dismissHandler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(405);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('401s when there is no session cookie', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await dismissHandler(req('POST', { ids: ['abc123'] }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('400s a malformed id list BEFORE any transport call', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    for (const body of [{}, { ids: [] }, { ids: [''] }, { ids: 'nope' }]) {
      const { res, status } = mockRes();
      await dismissHandler(req('POST', body), res);
      expect(status).toHaveBeenCalledWith(400);
    }
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('400s an over-long id list at the trust boundary', async () => {
    // Bounded here as well as at the backend: the BFF is the trust boundary,
    // and relaying 10,000 ids to find out they are too many is the wrong shape.
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status } = mockRes();
    await dismissHandler(
      req('POST', { ids: Array.from({ length: 201 }, (_, i) => `id${i}`) }),
      res,
    );
    expect(status).toHaveBeenCalledWith(400);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays valid ids and passes {dismissed, unread} through', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({
      status: 200,
      body: { dismissed: 2, unread: 0 },
    });
    const { res, status, json } = mockRes();
    await dismissHandler(req('POST', { ids: ['a1', 'b2'] }), res);
    expect(mockCallTransport).toHaveBeenCalledWith(
      'POST',
      '/chat/notifications/dismiss',
      { body: { ids: ['a1', 'b2'] }, sessionToken: 'tok' },
    );
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ dismissed: 2, unread: 0 });
  });

  it('hits the DISMISS path, not the ack path', async () => {
    // The two doors differ in reversibility, so a copy-paste that pointed this
    // handler at /ack would turn every clear into a no-op the operator would
    // read as "dismiss is broken" — while the ack tests stayed green.
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: {} });
    const { res } = mockRes();
    await dismissHandler(req('POST', { ids: ['a1'] }), res);
    expect(mockCallTransport.mock.calls[0][1]).toBe('/chat/notifications/dismiss');
  });

  it('defaults an empty backend body to an explicit zero', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: null });
    const { res, json } = mockRes();
    await dismissHandler(req('POST', { ids: ['a1'] }), res);
    expect(json).toHaveBeenCalledWith({ dismissed: 0, unread: 0 });
  });
});
