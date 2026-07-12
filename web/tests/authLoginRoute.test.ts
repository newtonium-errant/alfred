import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the BFF /api/auth/login gate: method → body shape (400 email_required) →
// relay to transport POST /auth/login. The `next` deep-link target is relayed
// ONLY when present (backward-compatible with the no-deep-link body) and is NOT
// sanitised here — the backend's safe_next_path is the authority.

const { mockCallTransport, mockSendTransportError } = vi.hoisted(() => ({
  mockCallTransport: vi.fn(),
  mockSendTransportError: vi.fn(),
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransport: mockCallTransport,
}));

vi.mock('../lib/algernon/bffError', () => ({
  sendTransportError: mockSendTransportError,
}));

import handler from '../pages/api/auth/login';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

function postReq(body: unknown): NextApiRequest {
  return { method: 'POST', body } as unknown as NextApiRequest;
}

beforeEach(() => {
  mockCallTransport.mockReset();
  mockSendTransportError.mockReset();
  mockCallTransport.mockResolvedValue({ status: 200, body: { status: 'sent' } });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POST /api/auth/login', () => {
  it('405 on a non-POST method (no relay)', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('400 email_required on a malformed body (no relay)', async () => {
    const { res, status, json } = mockRes();
    await handler(postReq({ email: '' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'email_required' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays { email } only when no next is present (unchanged body)', async () => {
    const { res, status, json } = mockRes();
    await handler(postReq({ email: 'andrew@example.com' }), res);

    expect(mockCallTransport).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransport.mock.calls[0];
    expect(method).toBe('POST');
    expect(path).toBe('/auth/login');
    expect(opts.body).toEqual({ email: 'andrew@example.com' });
    expect('next' in opts.body).toBe(false);

    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ status: 'sent' });
  });

  it('relays { email, next } when next is present (deep-link)', async () => {
    const { res } = mockRes();
    await handler(postReq({ email: 'andrew@example.com', next: '/chat' }), res);

    expect(mockCallTransport).toHaveBeenCalledTimes(1);
    const [, , opts] = mockCallTransport.mock.calls[0];
    expect(opts.body).toEqual({ email: 'andrew@example.com', next: '/chat' });
  });

  it('does NOT sanitise next — an unsafe value is relayed verbatim (backend is the authority)', async () => {
    const { res } = mockRes();
    await handler(postReq({ email: 'andrew@example.com', next: 'https://evil.com' }), res);

    const [, , opts] = mockCallTransport.mock.calls[0];
    expect(opts.body.next).toBe('https://evil.com');
  });

  it('400 email_required when next exceeds the 2048-char edge cap (no relay)', async () => {
    const { res, status, json } = mockRes();
    await handler(postReq({ email: 'andrew@example.com', next: '/' + 'a'.repeat(2048) }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'email_required' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });
});
