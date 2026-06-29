import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks GET /api/chat/targets: method (405) → session (401) → list. The HOME
// instance is ALWAYS present (default, home:true); cross-instance relay targets
// come from env. Metadata only (no secrets).

const { mockResolveSessionToken, mockListCrossInstanceChatTargets } = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockListCrossInstanceChatTargets: vi.fn(),
}));

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
}));

vi.mock('../lib/algernon/transport', () => ({
  listCrossInstanceChatTargets: mockListCrossInstanceChatTargets,
  TransportConfigError: class TransportConfigError extends Error {},
}));

import handler from '../pages/api/chat/targets';
import { HOME_INSTANCE_NAME } from '../lib/algernon/instance';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockListCrossInstanceChatTargets.mockReset();
  mockListCrossInstanceChatTargets.mockReturnValue([]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('GET /api/chat/targets', () => {
  it('405 on a non-GET method', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'POST' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockListCrossInstanceChatTargets).not.toHaveBeenCalled();
  });

  it('401 when there is no session', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockListCrossInstanceChatTargets).not.toHaveBeenCalled();
  });

  it('returns the home instance as the default even with no cross-instance targets', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({
      targets: [{ name: HOME_INSTANCE_NAME, label: HOME_INSTANCE_NAME, home: true }],
    });
  });

  it('prepends home and appends cross-instance targets (no secrets)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockListCrossInstanceChatTargets.mockReturnValue([
      { name: 'KALLE', label: 'KAL-LE' },
      { name: 'VERA', label: 'VERA' },
    ]);
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({
      targets: [
        { name: HOME_INSTANCE_NAME, label: HOME_INSTANCE_NAME, home: true },
        { name: 'KALLE', label: 'KAL-LE', home: false },
        { name: 'VERA', label: 'VERA', home: false },
      ],
    });
  });

  it('drops an env target that collides with the home name (home wins via session path)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockListCrossInstanceChatTargets.mockReturnValue([
      { name: HOME_INSTANCE_NAME.toUpperCase(), label: 'dup' },
      { name: 'KALLE', label: 'KAL-LE' },
    ]);
    const { res, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    const payload = json.mock.calls[0][0] as { targets: { name: string }[] };
    expect(payload.targets.map((t) => t.name)).toEqual([HOME_INSTANCE_NAME, 'KALLE']);
  });
});
