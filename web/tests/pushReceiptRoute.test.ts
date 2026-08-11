import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the push/receipt gate ordering (method → session 401 → OWNER 403 →
// trial-running 404 → body 400 → store) and the push_id validator.
//
// The receipt is the trial's ONLY positive evidence: without it every unanswered
// slot reads as a delivery failure. So a dropped receipt is a 500, not a
// swallowed warning — a silently-lost tap would later be reported as a miss.

const { mockResolveSessionToken, mockReadDisplayIdentity } = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockReadDisplayIdentity: vi.fn(),
}));
vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
  readDisplayIdentity: mockReadDisplayIdentity,
}));

const { mockAppendTrialRow, mockIsTrialEnabled } = vi.hoisted(() => ({
  mockAppendTrialRow: vi.fn(),
  mockIsTrialEnabled: vi.fn(),
}));
vi.mock('../lib/algernon/pushTrial', () => ({
  appendTrialRow: mockAppendTrialRow,
  isTrialEnabled: mockIsTrialEnabled,
}));

import handler from '../pages/api/push/receipt';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}
function req(method: string, body?: unknown): NextApiRequest {
  return { method, body } as unknown as NextApiRequest;
}

beforeEach(() => {
  mockResolveSessionToken.mockReset().mockReturnValue('sess');
  mockReadDisplayIdentity.mockReset().mockReturnValue({ name: 'Andrew', role: 'owner' });
  mockAppendTrialRow.mockReset().mockResolvedValue(undefined);
  mockIsTrialEnabled.mockReset().mockReturnValue(true);
  vi.spyOn(console, 'log').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});
afterEach(() => vi.restoreAllMocks());

describe('gate ordering', () => {
  it('rejects a non-POST', async () => {
    const { res, status, setHeader } = mockRes();
    await handler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(405);
    expect(setHeader).toHaveBeenCalledWith('Allow', 'POST');
    expect(mockAppendTrialRow).not.toHaveBeenCalled();
  });

  it('fails closed with no session', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(req('POST', { push_id: 'trial-d1-w1' }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockAppendTrialRow).not.toHaveBeenCalled();
  });

  it('is owner-only', async () => {
    mockReadDisplayIdentity.mockReturnValue({ name: 'Guest', role: 'viewer' });
    const { res, status, json } = mockRes();
    await handler(req('POST', { push_id: 'trial-d1-w1' }), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
    expect(mockAppendTrialRow).not.toHaveBeenCalled();
  });

  it('404s when no trial is running', async () => {
    // Inert outside a trial window — the route cannot be used to grow a ledger.
    mockIsTrialEnabled.mockReturnValue(false);
    const { res, status, json } = mockRes();
    await handler(req('POST', { push_id: 'trial-d1-w1' }), res);
    expect(status).toHaveBeenCalledWith(404);
    expect(json).toHaveBeenCalledWith({ error: 'not_found' });
    expect(mockAppendTrialRow).not.toHaveBeenCalled();
  });

  it('session is checked BEFORE the trial gate', async () => {
    // Otherwise an unauthenticated caller could probe whether a trial is running.
    mockResolveSessionToken.mockReturnValue(null);
    mockIsTrialEnabled.mockReturnValue(false);
    const { res, status } = mockRes();
    await handler(req('POST', { push_id: 'trial-d1-w1' }), res);
    expect(status).toHaveBeenCalledWith(401);
  });
});

describe('push_id validation', () => {
  it('records a well-formed id', async () => {
    const { res, status, json } = mockRes();
    await handler(req('POST', { push_id: 'trial-d1-w2' }), res);
    expect(status).toHaveBeenCalledWith(201);
    expect(json).toHaveBeenCalledWith({ ok: true });
    const row = mockAppendTrialRow.mock.calls[0][0];
    expect(row.type).toBe('receipt');
    expect(row.push_id).toBe('trial-d1-w2');
    expect(row.received_ts).toBeTruthy();
  });

  it.each([
    ['missing', undefined],
    ['empty', ''],
    ['blank', '   '],
    ['non-string', 42],
    ['newline (ledger forge)', 'a\n{"type":"receipt","push_id":"b"}'],
    ['path-ish', '../../etc/passwd'],
    ['too long', 'a'.repeat(65)],
  ])('rejects a %s push_id', async (_label, value) => {
    const { res, status } = mockRes();
    await handler(req('POST', { push_id: value }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(mockAppendTrialRow).not.toHaveBeenCalled();
  });

  it('accepts the longest legal id (the boundary is not off by one)', async () => {
    const { res, status } = mockRes();
    await handler(req('POST', { push_id: 'a'.repeat(64) }), res);
    expect(status).toHaveBeenCalledWith(201);
  });

  it('tolerates a missing body', async () => {
    const { res, status } = mockRes();
    await handler(req('POST', undefined), res);
    expect(status).toHaveBeenCalledWith(400);
  });
});

describe('store failure', () => {
  it('is a 500, never a silent drop', async () => {
    // A lost receipt would later read as a delivery failure — the exact false
    // signal the trial exists to eliminate.
    mockAppendTrialRow.mockRejectedValue(new Error('disk full'));
    const { res, status, json } = mockRes();
    await handler(req('POST', { push_id: 'trial-d1-w1' }), res);
    expect(status).toHaveBeenCalledWith(500);
    expect(json).toHaveBeenCalledWith({ error: 'store_error' });
  });
});
