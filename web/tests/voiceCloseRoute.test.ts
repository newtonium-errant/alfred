import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks POST /api/voice/close: method (405) → session (401) → zod (400) → verbatim
// relay of the idempotent {closed} / {closed:false, reason} → error mapping.

const {
  mockResolveSessionToken,
  mockCallTransport,
  mockCallChatTo,
  mockGate,
  TransportConfigError,
  TransportTimeoutError,
} = vi.hoisted(() => {
  class TransportConfigError extends Error {}
  class TransportTimeoutError extends Error {}
  return {
    mockResolveSessionToken: vi.fn(),
    mockCallTransport: vi.fn(),
    mockCallChatTo: vi.fn(),
    mockGate: vi.fn(),
    TransportConfigError,
    TransportTimeoutError,
  };
});

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransport: mockCallTransport,
  callChatTo: mockCallChatTo,
  TransportConfigError,
  TransportTimeoutError,
}));

vi.mock('../lib/algernon/chatRouting', () => ({
  isHomeInstance: (i?: string) => !i || i.toUpperCase() === 'ALGERNON',
  gateCrossInstance: mockGate,
}));

import handler from '../pages/api/voice/close';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

function postReq(body: unknown): NextApiRequest {
  return { method: 'POST', body, headers: {} } as unknown as NextApiRequest;
}

const validBody = { voice_session_id: 'a'.repeat(32) };

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockCallTransport.mockReset();
  mockCallChatTo.mockReset();
  mockGate.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POST /api/voice/close', () => {
  it('405 on a non-POST method', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('401 when there is no session (no relay)', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('400 invalid_request on a missing voice_session_id', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await handler(postReq({}), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0].error).toBe('invalid_request');
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays a successful owner close verbatim', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: { closed: true } });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);

    expect(mockCallTransport).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransport.mock.calls[0];
    expect(method).toBe('POST');
    expect(path).toBe('/voice/close');
    expect(opts.body.voice_session_id).toBe(validBody.voice_session_id);
    expect(opts.sessionToken).toBe('tok');
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ closed: true });
  });

  it('relays the idempotent not_found result verbatim', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({
      status: 200,
      body: { closed: false, reason: 'not_found' },
    });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ closed: false, reason: 'not_found' });
  });

  it('maps TransportConfigError → 500 transport_misconfigured', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockRejectedValue(new TransportConfigError('missing url'));
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(500);
    expect(json).toHaveBeenCalledWith({ error: 'transport_misconfigured' });
  });

  it('maps a generic error → 502 transport_unreachable', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockRejectedValue(new Error('ECONNREFUSED'));
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'transport_unreachable' });
  });

  it('cross-instance: routes the close to the SESSION\'s instance via callChatTo, instance stripped', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockGate.mockReturnValue({ ok: true, targetName: 'HYPATIA', userName: 'andrew' });
    mockCallChatTo.mockResolvedValue({ status: 200, body: { closed: true } });
    const { res, status, json } = mockRes();
    await handler(postReq({ ...validBody, instance: 'HYPATIA' }), res);

    expect(mockCallTransport).not.toHaveBeenCalled();
    expect(mockCallChatTo).toHaveBeenCalledTimes(1);
    const [targetName, method, path, opts] = mockCallChatTo.mock.calls[0];
    expect(targetName).toBe('HYPATIA');
    expect(method).toBe('POST');
    expect(path).toBe('/voice/close');
    expect(opts.userName).toBe('andrew');
    expect(opts.body.voice_session_id).toBe(validBody.voice_session_id);
    expect(opts.body).not.toHaveProperty('instance'); // stripped before relay
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ closed: true });
  });

  it('cross-instance: a failed gate (403) short-circuits before any relay', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockGate.mockReturnValue({ ok: false, status: 403, body: { error: 'forbidden' } });
    const { res, status, json } = mockRes();
    await handler(postReq({ ...validBody, instance: 'HYPATIA' }), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
    expect(mockCallChatTo).not.toHaveBeenCalled();
    expect(mockCallTransport).not.toHaveBeenCalled();
  });
});
