import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks GET /api/feed/list — the session+owner-gated BFF read of the HOME
// transport GET /feed/items via the server-side `web_feed` peer token.
// Gate order: method (405) → session (401) → OWNER-ONLY (403) → configured
// (503) → relay. Query filters state/mode/kind pass through (allowlisted);
// unknown query keys are dropped. Transport status relays verbatim.

const {
  mockResolveSessionToken,
  mockReadDisplayIdentity,
  mockCallTransportFeed,
  mockIsFeedConfigured,
} = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockReadDisplayIdentity: vi.fn(),
  mockCallTransportFeed: vi.fn(),
  mockIsFeedConfigured: vi.fn(),
}));

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
  readDisplayIdentity: mockReadDisplayIdentity,
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransportFeed: mockCallTransportFeed,
  isFeedConfigured: mockIsFeedConfigured,
  // bffError imports these from the (now mocked) transport module.
  TransportConfigError: class TransportConfigError extends Error {},
  TransportTimeoutError: class TransportTimeoutError extends Error {},
}));

import handler from '../pages/api/feed/list';
import { TransportTimeoutError } from '../lib/algernon/transport';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

function getReq(query: Record<string, string | string[]> = {}): NextApiRequest {
  return { method: 'GET', query, cookies: {} } as unknown as NextApiRequest;
}

function asOwner() {
  mockResolveSessionToken.mockReturnValue('tok');
  mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
}

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockReadDisplayIdentity.mockReset();
  mockCallTransportFeed.mockReset();
  mockIsFeedConfigured.mockReset();
  mockIsFeedConfigured.mockReturnValue(true);
  mockCallTransportFeed.mockResolvedValue({ status: 200, body: { items: [], count: 0 } });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('GET /api/feed/list — gates', () => {
  it('405 on a non-GET method', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'POST', query: {} } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('401 when there is no session (fail-closed, no relay)', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(getReq(), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('403 for a signed-in NON-owner', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'ben', role: 'ops' });
    const { res, status, json } = mockRes();
    await handler(getReq(), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('403 when there is a session but no identity cookie', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue(null);
    const { res, status } = mockRes();
    await handler(getReq(), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('503 not_configured when the feed token env is absent (deploy-inert)', async () => {
    asOwner();
    mockIsFeedConfigured.mockReturnValue(false);
    const { res, status, json } = mockRes();
    await handler(getReq(), res);
    expect(status).toHaveBeenCalledWith(503);
    expect(json).toHaveBeenCalledWith({ error: 'not_configured' });
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });
});

describe('GET /api/feed/list — filter passthrough', () => {
  it('forwards state/mode/kind verbatim as query filters', async () => {
    asOwner();
    const { res } = mockRes();
    await handler(getReq({ state: 'open', mode: 'decide', kind: 'email_tier' }), res);
    expect(mockCallTransportFeed).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransportFeed.mock.calls[0];
    expect(method).toBe('GET');
    expect(path).toBe('/feed/items');
    expect(opts.query).toEqual({ state: 'open', mode: 'decide', kind: 'email_tier' });
  });

  it('drops unknown query keys (allowlist — no arbitrary query relay)', async () => {
    asOwner();
    const { res } = mockRes();
    await handler(getReq({ state: 'open', evil: 'DROP me', id: 'x' }), res);
    const [, , opts] = mockCallTransportFeed.mock.calls[0];
    expect(opts.query).toEqual({ state: 'open' });
  });

  it('drops array-valued (repeated) query params', async () => {
    asOwner();
    const { res } = mockRes();
    await handler(getReq({ state: ['open', 'acted'] }), res);
    const [, , opts] = mockCallTransportFeed.mock.calls[0];
    expect(opts.query).toEqual({});
  });
});

describe('GET /api/feed/list — relay', () => {
  it('relays the folded feed store (200) verbatim', async () => {
    asOwner();
    mockCallTransportFeed.mockResolvedValue({
      status: 200,
      body: { items: [{ id: 'email_tier:note/A.md', kind: 'email_tier' }], count: 1 },
    });
    const { res, status, json } = mockRes();
    await handler(getReq(), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json.mock.calls[0][0].count).toBe(1);
    expect(json.mock.calls[0][0].items[0].id).toBe('email_tier:note/A.md');
  });

  it('relays a wrong-token transport 401 feed_wrong_peer (NOT a silent success)', async () => {
    // A SET-but-WRONG web_feed token clears isFeedConfigured() but the transport
    // rejects it — the BFF must surface the error, never a 200.
    asOwner();
    mockCallTransportFeed.mockResolvedValue({ status: 401, body: { error: 'feed_wrong_peer' } });
    const { res, status, json } = mockRes();
    await handler(getReq(), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'feed_wrong_peer' });
    expect(status).not.toHaveBeenCalledWith(200);
  });

  it('maps a transport timeout to 504 WITHOUT retrying', async () => {
    asOwner();
    mockCallTransportFeed.mockRejectedValue(new TransportTimeoutError('timed out'));
    const { res, status, json } = mockRes();
    await handler(getReq(), res);
    expect(status).toHaveBeenCalledWith(504);
    expect(json).toHaveBeenCalledWith({ error: 'gateway_timeout' });
    expect(mockCallTransportFeed).toHaveBeenCalledTimes(1); // no retry
  });

  it('maps an unreachable transport to 502 (no topology leak)', async () => {
    asOwner();
    mockCallTransportFeed.mockRejectedValue(new Error('ECONNREFUSED 127.0.0.1:8790'));
    const { res, status, json } = mockRes();
    await handler(getReq(), res);
    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'transport_unreachable' });
  });
});
