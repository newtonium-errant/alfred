import type { NextApiRequest, NextApiResponse } from 'next';
import { beforeEach, describe, expect, it, vi } from 'vitest';

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

import contactHandler from '../pages/api/day/contact';
import overrideHandler from '../pages/api/day/override';
import stateHandler from '../pages/api/day/state';

// The C4 BFF relays. Three gates in order — method, session, then upstream —
// and the pins that matter are the ones proving a gate ran BEFORE any upstream
// call, plus the deliberate ABSENCE of a vocabulary allowlist here (the
// transport is the single authority on rule and surface names; a copy in the
// BFF is a list that drifts, and a drift rejects legitimate contacts).

function req(method: string, body?: unknown): NextApiRequest {
  return { method, headers: {}, cookies: {}, query: {}, body } as unknown as NextApiRequest;
}

function mockRes() {
  const json = vi.fn();
  const status = vi.fn(() => ({ json }));
  const setHeader = vi.fn();
  return {
    res: { status, json, setHeader } as unknown as NextApiResponse,
    status,
    json,
    setHeader,
  };
}

beforeEach(() => {
  mockResolveSessionToken.mockReset().mockReturnValue('session-token');
  mockCallTransport.mockReset().mockResolvedValue({ status: 200, body: { ok: true } });
});

describe('GET /api/day/state', () => {
  it('405s a POST', async () => {
    const { res, status, json, setHeader } = mockRes();
    await stateHandler(req('POST'), res);
    expect(setHeader).toHaveBeenCalledWith('Allow', 'GET');
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('401s with no session, before reaching the transport', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await stateHandler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays the session token and passes the payload through verbatim', async () => {
    mockCallTransport.mockResolvedValue({
      status: 200,
      body: { configured: false, armed_rules: [] },
    });
    const { res, status, json } = mockRes();
    await stateHandler(req('GET'), res);
    expect(mockCallTransport).toHaveBeenCalledWith('GET', '/day/state', {
      sessionToken: 'session-token',
    });
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ configured: false, armed_rules: [] });
  });

  it('relays an upstream error status rather than inventing one', async () => {
    mockCallTransport.mockResolvedValue({ status: 403, body: { error: 'forbidden' } });
    const { res, status, json } = mockRes();
    await stateHandler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
  });
});

describe('POST /api/day/contact', () => {
  it('405s a GET', async () => {
    const { res, status, setHeader } = mockRes();
    await contactHandler(req('GET'), res);
    expect(setHeader).toHaveBeenCalledWith('Allow', 'POST');
    expect(status).toHaveBeenCalledWith(405);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('401s with no session', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status } = mockRes();
    await contactHandler(req('POST', { rule: 'default', surface: 'chat' }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it.each([
    [undefined],
    [{}],
    [{ rule: 'default' }],
    [{ surface: 'chat' }],
    [{ rule: 1, surface: 'chat' }],
  ])('400s a body missing a string rule/surface: %j', async (body) => {
    const { res, status, json } = mockRes();
    await contactHandler(req('POST', body), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_body' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('forwards ONLY rule and surface — never a client-supplied state blob', async () => {
    const { res } = mockRes();
    await contactHandler(
      req('POST', { rule: 'default', surface: 'chat', state: { hacked: true } }),
      res,
    );
    expect(mockCallTransport).toHaveBeenCalledWith('POST', '/day/contact', {
      sessionToken: 'session-token',
      body: { rule: 'default', surface: 'chat' },
    });
  });

  it('relays an UNKNOWN rule name to the transport rather than judging it', async () => {
    // The transport owns the vocabulary. A second allowlist here would reject
    // a legitimate contact the moment the two lists disagree.
    mockCallTransport.mockResolvedValue({ status: 400, body: { error: 'invalid_rule' } });
    const { res, status, json } = mockRes();
    await contactHandler(req('POST', { rule: 'from_the_future', surface: 'chat' }), res);
    expect(mockCallTransport).toHaveBeenCalled();
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_rule' });
  });
});

describe('POST /api/day/override', () => {
  it('405s a GET', async () => {
    const { res, status, setHeader } = mockRes();
    await overrideHandler(req('GET'), res);
    expect(setHeader).toHaveBeenCalledWith('Allow', 'POST');
    expect(status).toHaveBeenCalledWith(405);
  });

  it('401s with no session', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status } = mockRes();
    await overrideHandler(req('POST', { contact_id: 'c', surface: 'deck' }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it.each([[undefined], [{}], [{ contact_id: 'c' }], [{ surface: 'deck' }]])(
    '400s an incomplete body: %j',
    async (body) => {
      const { res, status, json } = mockRes();
      await overrideHandler(req('POST', body), res);
      expect(status).toHaveBeenCalledWith(400);
      expect(json).toHaveBeenCalledWith({ error: 'invalid_body' });
      expect(mockCallTransport).not.toHaveBeenCalled();
    },
  );

  it('relays the correction with its contact id', async () => {
    mockCallTransport.mockResolvedValue({
      status: 200,
      body: { recorded: true, patterns_surfaced: 1 },
    });
    const { res, status, json } = mockRes();
    await overrideHandler(req('POST', { contact_id: 'c-1', surface: 'deck' }), res);
    expect(mockCallTransport).toHaveBeenCalledWith('POST', '/day/override', {
      sessionToken: 'session-token',
      body: { contact_id: 'c-1', surface: 'deck' },
    });
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ recorded: true, patterns_surfaced: 1 });
  });

  it('relays a 404 for an unknown contact rather than swallowing it', async () => {
    mockCallTransport.mockResolvedValue({
      status: 404,
      body: { error: 'unknown_contact' },
    });
    const { res, status, json } = mockRes();
    await overrideHandler(req('POST', { contact_id: 'gone', surface: 'deck' }), res);
    expect(status).toHaveBeenCalledWith(404);
    expect(json).toHaveBeenCalledWith({ error: 'unknown_contact' });
  });
});
