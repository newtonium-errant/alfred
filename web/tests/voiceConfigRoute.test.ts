import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks GET /api/voice/config: method (405) → session (401) → verbatim relay,
// incl. the voice-unmounted 404 pass-through and the sendTransportError mapping.

const { mockResolveSessionToken, mockCallTransport, TransportConfigError, TransportTimeoutError } =
  vi.hoisted(() => {
    class TransportConfigError extends Error {}
    class TransportTimeoutError extends Error {}
    return {
      mockResolveSessionToken: vi.fn(),
      mockCallTransport: vi.fn(),
      TransportConfigError,
      TransportTimeoutError,
    };
  });

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransport: mockCallTransport,
  TransportConfigError,
  TransportTimeoutError,
}));

import handler from '../pages/api/voice/config';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockCallTransport.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('GET /api/voice/config', () => {
  it('405 on a non-GET method', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'POST' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('401 when there is no session (no relay)', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays the config verbatim with the session token', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({
      status: 200,
      body: { available: true, reason: null, ice_servers: [], max_sessions: 2, yours: [] },
    });
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);

    expect(mockCallTransport).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransport.mock.calls[0];
    expect(method).toBe('GET');
    expect(path).toBe('/voice/config');
    expect(opts.sessionToken).toBe('tok');
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({
      available: true,
      reason: null,
      ice_servers: [],
      max_sessions: 2,
      yours: [],
    });
  });

  it('relays a voice-unmounted 404 through verbatim', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 404, body: {} });
    const { res, status } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(404);
  });

  it('maps TransportConfigError → 500 transport_misconfigured', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockRejectedValue(new TransportConfigError('ALFRED_WEB_PEER_TOKEN is not set'));
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(500);
    expect(json).toHaveBeenCalledWith({ error: 'transport_misconfigured' });
  });

  it('maps TransportTimeoutError → 504 gateway_timeout', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockRejectedValue(new TransportTimeoutError('timed out'));
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(504);
    expect(json).toHaveBeenCalledWith({ error: 'gateway_timeout' });
  });

  it('maps a generic error → 502 transport_unreachable', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockRejectedValue(new Error('ECONNREFUSED'));
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'transport_unreachable' });
  });
});
