import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks push/public-key: method (405) → session (401) → configured (503) → the
// public key, plus the poller kick on GET (post-restart recovery).

const { mockResolveSessionToken } = vi.hoisted(() => ({ mockResolveSessionToken: vi.fn() }));
vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
  readDisplayIdentity: vi.fn(),
}));

const { mockReadVapidConfig } = vi.hoisted(() => ({ mockReadVapidConfig: vi.fn() }));
vi.mock('../lib/algernon/pushConfig', () => ({ readVapidConfig: mockReadVapidConfig }));

const { mockEnsurePushPoller } = vi.hoisted(() => ({ mockEnsurePushPoller: vi.fn() }));
vi.mock('../lib/algernon/pushNotifier', () => ({ ensurePushPoller: mockEnsurePushPoller }));

import handler from '../pages/api/push/public-key';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}
function req(method: string): NextApiRequest {
  return { method } as unknown as NextApiRequest;
}

beforeEach(() => {
  mockResolveSessionToken.mockReset().mockReturnValue('sess');
  mockReadVapidConfig.mockReset().mockReturnValue({ publicKey: 'BPubKey', privateKey: 'priv', subject: 'mailto:a@b.com' });
  mockEnsurePushPoller.mockReset();
});
afterEach(() => vi.restoreAllMocks());

describe('GET /api/push/public-key', () => {
  it('405 on a non-GET', async () => {
    const { res, status } = mockRes();
    await handler(req('POST'), res);
    expect(status).toHaveBeenCalledWith(405);
  });

  it('401 with no session', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status } = mockRes();
    await handler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(401);
  });

  it('503 when VAPID is not configured', async () => {
    mockReadVapidConfig.mockReturnValue(null);
    const { res, status } = mockRes();
    await handler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(503);
    expect(mockEnsurePushPoller).not.toHaveBeenCalled();
  });

  it('200 returns ONLY the public key and kicks the poller', async () => {
    const { res, status, json } = mockRes();
    await handler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ publicKey: 'BPubKey' });
    expect(mockEnsurePushPoller).toHaveBeenCalledTimes(1);
  });
});
