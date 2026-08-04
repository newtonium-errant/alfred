import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks POST /api/feed/act — the session+owner-gated BFF relay of a deck/feed
// act to the HOME transport POST /feed/act via the server-side `web_feed` token.
// Gate order: method (405) → session (401) → OWNER-ONLY (403) → configured
// (503) → body (400) → relay. The transport (kind,action) map is the capability
// ceiling — the BFF only relays. Provenance (via=deck + user) is LOGGED, never
// added to the relay body. NO retry on timeout. Transport status relays verbatim.

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
  TransportConfigError: class TransportConfigError extends Error {},
  TransportTimeoutError: class TransportTimeoutError extends Error {},
}));

import handler from '../pages/api/feed/act';
import { TransportTimeoutError } from '../lib/algernon/transport';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

function postReq(body: unknown): NextApiRequest {
  return { method: 'POST', body, cookies: {} } as unknown as NextApiRequest;
}

function asOwner() {
  mockResolveSessionToken.mockReturnValue('tok');
  mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
}

const validBody = { id: 'email_tier:note/Email1.md', action_id: 'confirm' };

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockReadDisplayIdentity.mockReset();
  mockCallTransportFeed.mockReset();
  mockIsFeedConfigured.mockReset();
  mockIsFeedConfigured.mockReturnValue(true);
  mockCallTransportFeed.mockResolvedValue({
    status: 200,
    body: { ok: true, status: 'acted', detail: 'email tier calibrated → medium', id: validBody.id },
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POST /api/feed/act — gates', () => {
  it('405 on a non-POST method', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('401 when there is no session', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('403 for a signed-in NON-owner', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'ben', role: 'ops' });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('403 when there is a session but no identity cookie', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue(null);
    const { res, status } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('503 not_configured when the feed token env is absent (deploy-inert)', async () => {
    asOwner();
    mockIsFeedConfigured.mockReturnValue(false);
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(503);
    expect(json).toHaveBeenCalledWith({ error: 'not_configured' });
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });
});

describe('POST /api/feed/act — body validation', () => {
  it('400 invalid_request when id is missing', async () => {
    asOwner();
    const { res, status, json } = mockRes();
    await handler(postReq({ action_id: 'confirm' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0].error).toBe('invalid_request');
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('400 invalid_request when action_id is missing', async () => {
    asOwner();
    const { res, status, json } = mockRes();
    await handler(postReq({ id: 'email_tier:x' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0].error).toBe('invalid_request');
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('400 invalid_request on an empty id / action_id', async () => {
    asOwner();
    const { res, status } = mockRes();
    await handler(postReq({ id: '  ', action_id: '  ' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('STRIPS unknown body fields — the relay carries ONLY {id, action_id}', async () => {
    asOwner();
    const { res } = mockRes();
    await handler(
      postReq({ ...validBody, acted_via: 'deck', evil: 'x', evidence: { secret: 1 } }),
      res,
    );
    const [, , opts] = mockCallTransportFeed.mock.calls[0];
    expect(opts.body).toEqual({ id: validBody.id, action_id: 'confirm' });
  });
});

describe('POST /api/feed/act — relay per status class', () => {
  it('relays a 200 acted verbatim', async () => {
    asOwner();
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(mockCallTransportFeed).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransportFeed.mock.calls[0];
    expect(method).toBe('POST');
    expect(path).toBe('/feed/act');
    expect(opts.body).toEqual({ id: validBody.id, action_id: 'confirm' });
    expect(status).toHaveBeenCalledWith(200);
    expect(json.mock.calls[0][0].status).toBe('acted');
  });

  it('relays a 400 invalid_action verbatim', async () => {
    asOwner();
    mockCallTransportFeed.mockResolvedValue({ status: 400, body: { status: 'invalid_action' } });
    const { res, status, json } = mockRes();
    await handler(postReq({ ...validBody, action_id: 'reject' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0].status).toBe('invalid_action');
  });

  it('relays a 409 stale_item verbatim', async () => {
    asOwner();
    mockCallTransportFeed.mockResolvedValue({ status: 409, body: { status: 'stale_item' } });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(409);
    expect(json.mock.calls[0][0].status).toBe('stale_item');
  });

  it('relays a 422 resolver error verbatim', async () => {
    asOwner();
    mockCallTransportFeed.mockResolvedValue({
      status: 422,
      body: { status: 'error', detail: 'record no longer exists' },
    });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(422);
    expect(json.mock.calls[0][0].detail).toBe('record no longer exists');
  });

  it('maps a wrong-token upstream 401 to 502 feed_upstream_unavailable + warns (NOT a bare 401)', async () => {
    // A SET-but-WRONG web_feed token → transport 401 feed_wrong_peer. Post-auth
    // this is server-side misconfig, not a client session failure — mapped to
    // 502 so it can't trip the PWA bare-401 → login redirect, with the operator's
    // greppable upstream_auth_misconfig warn. Never a 200, never a relayed 401.
    asOwner();
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    mockCallTransportFeed.mockResolvedValue({ status: 401, body: { error: 'feed_wrong_peer' } });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'feed_upstream_unavailable' });
    expect(status).not.toHaveBeenCalledWith(200);
    expect(status).not.toHaveBeenCalledWith(401);
    expect(warnSpy).toHaveBeenCalledWith('[bff:feed/act] upstream_auth_misconfig');
    // A misconfig-rejected act never happened → it is NOT logged as an act.
    expect(logSpy).not.toHaveBeenCalled();
  });
});

describe('POST /api/feed/act — timeout + errors', () => {
  it('maps a transport timeout to 504 WITHOUT retrying (a timed-out act may have dispatched)', async () => {
    asOwner();
    mockCallTransportFeed.mockRejectedValue(new TransportTimeoutError('timed out'));
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(504);
    expect(json).toHaveBeenCalledWith({ error: 'gateway_timeout' });
    expect(mockCallTransportFeed).toHaveBeenCalledTimes(1); // never resent
  });

  it('maps an unreachable transport to 502 (no topology leak)', async () => {
    asOwner();
    mockCallTransportFeed.mockRejectedValue(new Error('ECONNREFUSED 127.0.0.1:8790'));
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'transport_unreachable' });
  });
});

describe('POST /api/feed/act — provenance log', () => {
  it('logs id/action_id/via/outcome/user and no token material', async () => {
    asOwner();
    const logSpy = vi.spyOn(console, 'log').mockImplementation(() => {});
    const { res } = mockRes();
    await handler(postReq(validBody), res);
    expect(logSpy).toHaveBeenCalledTimes(1);
    const line = String(logSpy.mock.calls[0][0]);
    expect(line).toContain('[bff:feed/act]');
    expect(line).toContain(`id=${validBody.id}`);
    expect(line).toContain('action_id=confirm');
    expect(line).toContain('via=deck');
    expect(line).toContain('outcome=acted');
    expect(line).toContain('user=andrew');
    // No token/credential material ever reaches the route (the token is injected
    // in the transport lib), so the log can never leak one.
    expect(line).not.toMatch(/Bearer|token/i);
  });
});

describe('POST /api/feed/act — correction_target relay (#13)', () => {
  const correctBody = {
    id: 'routine_match:clean hammer|Weekly',
    action_id: 'correct',
    correction_target: 'Tidy the workshop',
  };

  it('relays correction_target through to the transport', async () => {
    asOwner();
    const { res } = mockRes();
    await handler(postReq(correctBody), res);
    const [, path, opts] = mockCallTransportFeed.mock.calls[0];
    expect(path).toBe('/feed/act');
    expect(opts.body).toEqual(correctBody);
  });

  it('trims the target — a padded pick must match the vault item server-side', async () => {
    asOwner();
    const { res } = mockRes();
    await handler(postReq({ ...correctBody, correction_target: '  Tidy the workshop  ' }), res);
    const [, , opts] = mockCallTransportFeed.mock.calls[0];
    expect(opts.body.correction_target).toBe('Tidy the workshop');
  });

  it('omits the key entirely when absent — every other action relays the old shape', async () => {
    asOwner();
    const { res } = mockRes();
    await handler(postReq(validBody), res);
    const [, , opts] = mockCallTransportFeed.mock.calls[0];
    expect(opts.body).toEqual({ id: validBody.id, action_id: 'confirm' });
    expect('correction_target' in opts.body).toBe(false);
  });

  it('rejects a non-string target at the body gate (400, nothing relayed)', async () => {
    asOwner();
    const { res, status } = mockRes();
    await handler(postReq({ ...correctBody, correction_target: { evil: 1 } }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('rejects an over-long target rather than forwarding it', async () => {
    asOwner();
    const { res, status } = mockRes();
    await handler(postReq({ ...correctBody, correction_target: 'x'.repeat(501) }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(mockCallTransportFeed).not.toHaveBeenCalled();
  });

  it('still strips unknown fields alongside a valid target', async () => {
    asOwner();
    const { res } = mockRes();
    await handler(postReq({ ...correctBody, evil: 'x', evidence: { secret: 1 } }), res);
    const [, , opts] = mockCallTransportFeed.mock.calls[0];
    expect(opts.body).toEqual(correctBody);
  });
});
