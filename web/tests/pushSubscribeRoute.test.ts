import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the push/subscribe gate ordering (method → session 401 → OWNER 403 →
// configured 503 → body 400 → store) for both POST (add, deduped) and DELETE
// (remove), plus the strip-unknowns property and the poller kick.

const { mockResolveSessionToken, mockReadDisplayIdentity } = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockReadDisplayIdentity: vi.fn(),
}));
vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
  readDisplayIdentity: mockReadDisplayIdentity,
}));

const { mockIsPushConfigured } = vi.hoisted(() => ({ mockIsPushConfigured: vi.fn() }));
vi.mock('../lib/algernon/pushConfig', () => ({ isPushConfigured: mockIsPushConfigured }));

const { mockAddSubscription, mockRemoveSubscription } = vi.hoisted(() => ({
  mockAddSubscription: vi.fn(),
  mockRemoveSubscription: vi.fn(),
}));
vi.mock('../lib/algernon/pushStore', () => ({
  addSubscription: mockAddSubscription,
  removeSubscription: mockRemoveSubscription,
}));

const { mockEnsurePushPoller } = vi.hoisted(() => ({ mockEnsurePushPoller: vi.fn() }));
vi.mock('../lib/algernon/pushNotifier', () => ({ ensurePushPoller: mockEnsurePushPoller }));

import handler from '../pages/api/push/subscribe';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}
function req(method: string, body?: unknown): NextApiRequest {
  return { method, body } as unknown as NextApiRequest;
}

const validSub = {
  endpoint: 'https://push.example.com/abc',
  keys: { p256dh: 'p256key', auth: 'authkey' },
};

beforeEach(() => {
  mockResolveSessionToken.mockReset().mockReturnValue('sess');
  mockReadDisplayIdentity.mockReset().mockReturnValue({ name: 'Andrew', role: 'owner' });
  mockIsPushConfigured.mockReset().mockReturnValue(true);
  mockAddSubscription.mockReset().mockResolvedValue(undefined);
  mockRemoveSubscription.mockReset().mockResolvedValue(true);
  mockEnsurePushPoller.mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe('push/subscribe gates', () => {
  it('405 on GET', async () => {
    const { res, status } = mockRes();
    await handler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(405);
  });
  it('401 with no session', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status } = mockRes();
    await handler(req('POST', validSub), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(mockAddSubscription).not.toHaveBeenCalled();
  });
  it('403 for a non-owner', async () => {
    mockReadDisplayIdentity.mockReturnValue({ name: 'G', role: 'member' });
    const { res, status } = mockRes();
    await handler(req('POST', validSub), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(mockAddSubscription).not.toHaveBeenCalled();
  });
  it('503 when push is not configured', async () => {
    mockIsPushConfigured.mockReturnValue(false);
    const { res, status } = mockRes();
    await handler(req('POST', validSub), res);
    expect(status).toHaveBeenCalledWith(503);
    expect(mockAddSubscription).not.toHaveBeenCalled();
  });
});

describe('push/subscribe POST', () => {
  it('400 on a malformed subscription', async () => {
    const { res, status } = mockRes();
    await handler(req('POST', { endpoint: 'not-a-url' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(mockAddSubscription).not.toHaveBeenCalled();
  });

  it('201 stores the subscription (owner-stamped) and kicks the poller', async () => {
    const { res, status, json } = mockRes();
    await handler(req('POST', validSub), res);
    expect(status).toHaveBeenCalledWith(201);
    expect(json).toHaveBeenCalledWith({ ok: true });
    expect(mockAddSubscription).toHaveBeenCalledTimes(1);
    const stored = mockAddSubscription.mock.calls[0][0];
    expect(stored).toMatchObject({
      user: 'Andrew',
      endpoint: 'https://push.example.com/abc',
      p256dh: 'p256key',
      auth: 'authkey',
    });
    expect(mockEnsurePushPoller).toHaveBeenCalledTimes(1);
  });

  it('strips unknown keys — they never reach the store', async () => {
    const { res } = mockRes();
    await handler(req('POST', { ...validSub, evil: 'x', role: 'owner' }), res);
    const stored = mockAddSubscription.mock.calls[0][0];
    expect('evil' in stored).toBe(false);
    expect('role' in stored).toBe(false);
  });

  it('500 when the store write fails', async () => {
    mockAddSubscription.mockRejectedValue(new Error('EACCES'));
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { res, status } = mockRes();
    await handler(req('POST', validSub), res);
    expect(status).toHaveBeenCalledWith(500);
    expect(warn).toHaveBeenCalled();
  });
});

describe('push/subscribe DELETE', () => {
  it('200 removes by endpoint', async () => {
    const { res, status, json } = mockRes();
    await handler(req('DELETE', { endpoint: 'https://push.example.com/abc' }), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ ok: true, removed: true });
    expect(mockRemoveSubscription).toHaveBeenCalledWith('https://push.example.com/abc');
  });

  it('400 on a malformed endpoint', async () => {
    const { res, status } = mockRes();
    await handler(req('DELETE', { endpoint: 'nope' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(mockRemoveSubscription).not.toHaveBeenCalled();
  });
});
