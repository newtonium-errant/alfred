import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the targets route gate: method (405) → session (401) → list. The list is
// metadata only (no secrets) — that property is covered in transportExtras.test.

const { mockResolveSessionToken, mockListIngestTargets } = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockListIngestTargets: vi.fn(),
}));

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
}));

vi.mock('../lib/algernon/transport', () => ({
  listIngestTargets: mockListIngestTargets,
  TransportConfigError: class TransportConfigError extends Error {},
}));

import handler from '../pages/api/ingest/targets';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockListIngestTargets.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('GET /api/ingest/targets', () => {
  it('405 on a non-GET method', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'POST' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockListIngestTargets).not.toHaveBeenCalled();
  });

  it('401 when there is no session', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockListIngestTargets).not.toHaveBeenCalled();
  });

  it('returns the configured targets for a signed-in user', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockListIngestTargets.mockReturnValue([
      { name: 'SALEM', label: 'Salem', recordTypes: ['document', 'note', 'source'] },
    ]);
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({
      targets: [{ name: 'SALEM', label: 'Salem', recordTypes: ['document', 'note', 'source'] }],
    });
  });
});
