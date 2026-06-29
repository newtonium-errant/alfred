import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the BFF ingest gate ordering: method → session (401) → OWNER-ONLY (403)
// → body shape (400) → known target (400) → relay. The identity cookie role is a
// defence-in-depth BFF guard (the peer token is the real write authority).

const {
  mockResolveSessionToken,
  mockReadDisplayIdentity,
  mockCallTransportTo,
  mockListIngestTargets,
} = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockReadDisplayIdentity: vi.fn(),
  mockCallTransportTo: vi.fn(),
  mockListIngestTargets: vi.fn(),
}));

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
  readDisplayIdentity: mockReadDisplayIdentity,
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransportTo: mockCallTransportTo,
  listIngestTargets: mockListIngestTargets,
  TransportConfigError: class TransportConfigError extends Error {},
}));

import handler from '../pages/api/ingest/submit';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

const validBody = {
  target: 'SALEM',
  record_type: 'document',
  title: 'A clear unique title',
  body: 'The verbatim body.',
  source: 'paste',
};

function postReq(body: unknown): NextApiRequest {
  return { method: 'POST', body } as unknown as NextApiRequest;
}

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockReadDisplayIdentity.mockReset();
  mockCallTransportTo.mockReset();
  mockListIngestTargets.mockReset();
  mockListIngestTargets.mockReturnValue([
    { name: 'SALEM', label: 'Salem', recordTypes: ['document', 'note', 'source'] },
  ]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POST /api/ingest/submit', () => {
  it('405 on a non-POST method', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('401 when there is no session (fail-closed, no relay)', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('403 for a signed-in NON-owner (owner-only)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'ben', role: 'ops' });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('403 when there is a session token but no identity cookie', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue(null);
    const { res, status } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('400 invalid_request on a malformed body (owner)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
    const { res, status, json } = mockRes();
    await handler(postReq({ ...validBody, title: '' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0].error).toBe('invalid_request');
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('400 unknown_target when the target is not configured', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
    const { res, status, json } = mockRes();
    await handler(postReq({ ...validBody, target: 'GHOST' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'unknown_target' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('relays an owner submit to the chosen target with provenance + ingest-user header', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
    mockCallTransportTo.mockResolvedValue({
      status: 201,
      body: { status: 'created', path: 'document/A clear unique title.md', record_type: 'document', instance: 'Salem' },
    });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);

    expect(mockCallTransportTo).toHaveBeenCalledTimes(1);
    const [targetName, method, path, opts] = mockCallTransportTo.mock.calls[0];
    expect(targetName).toBe('SALEM');
    expect(method).toBe('POST');
    expect(path).toBe('/vault/ingest');
    expect(opts.body.record_type).toBe('document');
    expect(opts.body.title).toBe('A clear unique title');
    expect(opts.body.body).toBe('The verbatim body.');
    expect(opts.body.ingested_by).toBe('andrew');
    expect(opts.body.set_fields.ingested_via).toBe('web');
    expect(typeof opts.body.ingested_at).toBe('string');
    expect(opts.headers['X-Alfred-Ingest-User']).toBe('andrew');

    expect(status).toHaveBeenCalledWith(201);
    expect(json).toHaveBeenCalledWith({
      status: 'created',
      path: 'document/A clear unique title.md',
      record_type: 'document',
      instance: 'Salem',
    });
  });

  it('relays a backend 409 title_collision through verbatim', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
    mockCallTransportTo.mockResolvedValue({
      status: 409,
      body: { error: 'title_collision', path: 'document/Existing.md' },
    });
    const { res, status, json } = mockRes();
    await handler(postReq(validBody), res);
    expect(status).toHaveBeenCalledWith(409);
    expect(json.mock.calls[0][0].error).toBe('title_collision');
  });
});
