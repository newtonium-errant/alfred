import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the multi-instance BFF routing for /api/chat/{turn,open,history}:
//   - absent / home selector → the existing session-token path (callTransport),
//     UNCHANGED, no owner gate;
//   - cross-instance selector → gate session (401) → owner-only (403) → known
//     target (400) → relay via callChatTo with the asserted X-Alfred-User.
// chatRouting is REAL here (its identity + transport deps are mocked) so the gate
// ordering is exercised end-to-end.

const {
  mockResolveSessionToken,
  mockReadDisplayIdentity,
  mockCallTransport,
  mockCallChatTo,
  mockListCrossInstanceChatTargets,
} = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockReadDisplayIdentity: vi.fn(),
  mockCallTransport: vi.fn(),
  mockCallChatTo: vi.fn(),
  mockListCrossInstanceChatTargets: vi.fn(),
}));

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
  readDisplayIdentity: mockReadDisplayIdentity,
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransport: mockCallTransport,
  callChatTo: mockCallChatTo,
  listCrossInstanceChatTargets: mockListCrossInstanceChatTargets,
  TransportConfigError: class TransportConfigError extends Error {},
  TransportTimeoutError: class TransportTimeoutError extends Error {},
}));

import turnHandler from '../pages/api/chat/turn';
import openHandler from '../pages/api/chat/open';
import historyHandler from '../pages/api/chat/history/[key]';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

function postReq(body: unknown): NextApiRequest {
  return { method: 'POST', body } as unknown as NextApiRequest;
}

const HOME_OK = { status: 200, body: { reply: 'home', session_key: 'k', ts: '', user_ts: '' } };
const RELAY_OK = { status: 200, body: { reply: 'kalle', session_key: 'k', ts: '', user_ts: '' } };

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockReadDisplayIdentity.mockReset();
  mockCallTransport.mockReset();
  mockCallChatTo.mockReset();
  mockListCrossInstanceChatTargets.mockReset();
  mockListCrossInstanceChatTargets.mockReturnValue([{ name: 'KALLE', label: 'KAL-LE' }]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POST /api/chat/turn — routing', () => {
  it('405 on a non-POST method', async () => {
    const { res, status } = mockRes();
    await turnHandler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(mockCallTransport).not.toHaveBeenCalled();
    expect(mockCallChatTo).not.toHaveBeenCalled();
  });

  it('401 when there is no session (fail-closed)', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await turnHandler(postReq({ session_key: 'k', message: 'hi' }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallChatTo).not.toHaveBeenCalled();
  });

  it('400 invalid_request on a malformed body', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await turnHandler(postReq({ session_key: 'k' }), res); // no message
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0].error).toBe('invalid_request');
  });

  it('HOME (no instance) → session path, no owner gate', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue(HOME_OK);
    const { res, status, json } = mockRes();
    await turnHandler(postReq({ session_key: 'k', message: 'hi' }), res);

    expect(mockCallTransport).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransport.mock.calls[0];
    expect(method).toBe('POST');
    expect(path).toBe('/chat/turn');
    expect(opts.sessionToken).toBe('tok');
    expect(opts.body.message).toBe('hi');
    expect(mockCallChatTo).not.toHaveBeenCalled();
    expect(mockReadDisplayIdentity).not.toHaveBeenCalled(); // home path has no owner gate
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith(HOME_OK.body);
  });

  it('HOME relays idempotency_key but never the instance field', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue(HOME_OK);
    const { res } = mockRes();
    await turnHandler(
      postReq({ session_key: 'k', message: 'hi', idempotency_key: 'idk-1' }),
      res,
    );
    const [, , opts] = mockCallTransport.mock.calls[0];
    expect(opts.body.idempotency_key).toBe('idk-1');
    expect(opts.body.instance).toBeUndefined();
  });

  it('CROSS non-owner → 403, no relay', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'ben', role: 'ops' });
    const { res, status, json } = mockRes();
    await turnHandler(postReq({ session_key: 'k', message: 'hi', instance: 'KALLE' }), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
    expect(mockCallChatTo).not.toHaveBeenCalled();
  });

  it('CROSS owner + unknown target → 400 unknown_target, no relay', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
    mockListCrossInstanceChatTargets.mockReturnValue([]); // nothing configured
    const { res, status, json } = mockRes();
    await turnHandler(postReq({ session_key: 'k', message: 'hi', instance: 'KALLE' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'unknown_target' });
    expect(mockCallChatTo).not.toHaveBeenCalled();
  });

  it('CROSS owner + known target → relay with asserted X-Alfred-User name', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
    mockCallChatTo.mockResolvedValue(RELAY_OK);
    const { res, status, json } = mockRes();
    await turnHandler(
      postReq({ session_key: 'k', message: 'hi', instance: 'KALLE', idempotency_key: 'idk-2' }),
      res,
    );

    expect(mockCallTransport).not.toHaveBeenCalled();
    expect(mockCallChatTo).toHaveBeenCalledTimes(1);
    const [targetName, method, path, opts] = mockCallChatTo.mock.calls[0];
    expect(targetName).toBe('KALLE');
    expect(method).toBe('POST');
    expect(path).toBe('/chat/turn');
    expect(opts.userName).toBe('andrew');
    expect(opts.body.message).toBe('hi');
    expect(opts.body.idempotency_key).toBe('idk-2');
    expect(opts.body.instance).toBeUndefined(); // BFF-only, never relayed
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith(RELAY_OK.body);
  });
});

describe('POST /api/chat/open — routing', () => {
  it('HOME → session path', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: { session_key: 'new' } });
    const { res, status, json } = mockRes();
    await openHandler(postReq({}), res);
    expect(mockCallTransport).toHaveBeenCalledWith('POST', '/chat/open', {
      body: {},
      sessionToken: 'tok',
    });
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ session_key: 'new' });
  });

  it('CROSS owner + known target → relay', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
    mockCallChatTo.mockResolvedValue({ status: 200, body: { session_key: 'new' } });
    const { res, status } = mockRes();
    await openHandler(postReq({ instance: 'KALLE' }), res);
    const [targetName, method, path, opts] = mockCallChatTo.mock.calls[0];
    expect(targetName).toBe('KALLE');
    expect(method).toBe('POST');
    expect(path).toBe('/chat/open');
    expect(opts.userName).toBe('andrew');
    expect(status).toHaveBeenCalledWith(200);
  });

  it('CROSS non-owner → 403', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'ben', role: 'ops' });
    const { res, status, json } = mockRes();
    await openHandler(postReq({ instance: 'KALLE' }), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
    expect(mockCallChatTo).not.toHaveBeenCalled();
  });

  it('400 invalid_request on a malformed body (zod boundary-validated)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await openHandler(postReq({ instance: 123 }), res); // instance not a string
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0].error).toBe('invalid_request');
    expect(mockCallTransport).not.toHaveBeenCalled();
    expect(mockCallChatTo).not.toHaveBeenCalled();
  });
});

describe('GET /api/chat/history/[key] — routing', () => {
  function getReq(key: string, instance?: string): NextApiRequest {
    return {
      method: 'GET',
      query: instance ? { key, instance } : { key },
    } as unknown as NextApiRequest;
  }

  it('HOME → session path', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: { turns: [] } });
    const { res, status, json } = mockRes();
    await historyHandler(getReq('abc'), res);
    expect(mockCallTransport).toHaveBeenCalledWith('GET', '/chat/history/abc', {
      sessionToken: 'tok',
    });
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ turns: [] });
  });

  it('CROSS owner + known target → relay with asserted user', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
    mockCallChatTo.mockResolvedValue({ status: 200, body: { turns: [] } });
    const { res, status } = mockRes();
    await historyHandler(getReq('abc', 'KALLE'), res);
    const [targetName, method, path, opts] = mockCallChatTo.mock.calls[0];
    expect(targetName).toBe('KALLE');
    expect(method).toBe('GET');
    expect(path).toBe('/chat/history/abc');
    expect(opts.userName).toBe('andrew');
    expect(status).toHaveBeenCalledWith(200);
  });

  it('400 invalid_session_key on an empty key', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await historyHandler(getReq(''), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session_key' });
  });
});
