import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks POST /api/voice/offer: method (405) → session (401) → zod (400) → verbatim
// relay (incl. the relayed 429 too_many_sessions) → sendTransportError mapping.

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

import handler from '../pages/api/voice/offer';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

function postReq(body: unknown): NextApiRequest {
  return { method: 'POST', body, headers: {} } as unknown as NextApiRequest;
}

const validBody = { sdp: 'v=0\r\no=- 1 1 IN IP4 0.0.0.0', type: 'offer' };

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockCallTransport.mockReset();
  mockCallChatTo.mockReset();
  mockGate.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POST /api/voice/offer', () => {
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

  it('400 invalid_request on a missing sdp', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await handler(postReq({ type: 'offer' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0].error).toBe('invalid_request');
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('400 invalid_request on a wrong type literal', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status } = mockRes();
    await handler(postReq({ sdp: 'v=0', type: 'answer' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays the parsed offer verbatim with the session token', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({
      status: 200,
      body: { voice_session_id: 'abc123', sdp: 'answer-sdp', type: 'answer', expires_at: 'z' },
    });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);

    expect(mockCallTransport).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransport.mock.calls[0];
    expect(method).toBe('POST');
    expect(path).toBe('/voice/offer');
    expect(opts.body.sdp).toBe(validBody.sdp);
    expect(opts.body.type).toBe('offer');
    expect(opts.sessionToken).toBe('tok');
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({
      voice_session_id: 'abc123',
      sdp: 'answer-sdp',
      type: 'answer',
      expires_at: 'z',
    });
  });

  it('relays a backend 429 too_many_sessions through verbatim', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({
      status: 429,
      body: { error: 'too_many_sessions', max_sessions: 2 },
    });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(429);
    expect(json.mock.calls[0][0].error).toBe('too_many_sessions');
  });

  it('maps TransportTimeoutError → 504 gateway_timeout', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockRejectedValue(new TransportTimeoutError('timed out'));
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(504);
    expect(json).toHaveBeenCalledWith({ error: 'gateway_timeout' });
  });

  it('maps a generic error → 502 transport_unreachable', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockRejectedValue(new Error('ECONNREFUSED'));
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'transport_unreachable' });
  });

  it('strips the BFF-only instance selector before relaying (home path)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: { voice_session_id: 'x' } });
    const { res } = mockRes();
    await handler(postReq({ ...validBody, instance: 'Algernon' }), res);
    const [, , opts] = mockCallTransport.mock.calls[0];
    expect(opts.body).not.toHaveProperty('instance'); // never forwarded upstream
    expect(opts.body.sdp).toBe(validBody.sdp);
  });

  it('cross-instance: gates then relays via callChatTo (peer token + X-Alfred-User), instance stripped', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockGate.mockReturnValue({ ok: true, targetName: 'HYPATIA', userName: 'andrew' });
    mockCallChatTo.mockResolvedValue({
      status: 200,
      body: { voice_session_id: 'vs-h', sdp: 'answer', type: 'answer', expires_at: 'z' },
    });
    const { res, status } = mockRes();
    await handler(postReq({ ...validBody, instance: 'HYPATIA' }), res);

    expect(mockCallTransport).not.toHaveBeenCalled();
    expect(mockCallChatTo).toHaveBeenCalledTimes(1);
    const [targetName, method, path, opts] = mockCallChatTo.mock.calls[0];
    expect(targetName).toBe('HYPATIA');
    expect(method).toBe('POST');
    expect(path).toBe('/voice/offer');
    expect(opts.userName).toBe('andrew');
    expect(opts.body).not.toHaveProperty('instance'); // stripped before relay
    expect(opts.body.sdp).toBe(validBody.sdp);
    expect(status).toHaveBeenCalledWith(200);
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
