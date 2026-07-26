import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the BFF OTP routes (parity #23 — iOS PWA re-auth), security-load-bearing:
//
// verify.ts — THE COOKIE-JAR FIX: on a backend 200 the session token is stripped
// from the JSON and stored via the REAL setSessionCookies (httpOnly Set-Cookie on
// this same-origin response, i.e. the PWA's own jar); the response body NEVER
// contains the token. Every rejection relays the uniform invalid_or_expired.
//
// request.ts — uniform {ok:true} (no enumeration; the backend is uniform for
// known/unknown emails and this collapses 200 → ok:true), method/body edge guards.

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

// identity.ts is NOT mocked — the REAL setSessionCookies must produce the
// Set-Cookie header (mocking it would let the cookie-jar fix silently rot).
import requestHandler from '../pages/api/auth/otp/request';
import verifyHandler from '../pages/api/auth/otp/verify';
import { SESSION_COOKIE, IDENTITY_COOKIE } from '../lib/algernon/identity';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return {
    res: { status, setHeader, json } as unknown as NextApiResponse,
    status,
    json,
    setHeader,
  };
}

function postReq(body: unknown): NextApiRequest {
  return { method: 'POST', body } as unknown as NextApiRequest;
}

beforeEach(() => {
  mockCallTransport.mockReset();
  mockSendTransportError.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POST /api/auth/otp/request', () => {
  it('405 on a non-POST method (no relay)', async () => {
    const { res, status, json } = mockRes();
    await requestHandler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('400 email_required on a malformed body (no relay)', async () => {
    const { res, status, json } = mockRes();
    await requestHandler(postReq({ email: '' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ ok: false, error: 'email_required' });
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('relays {email} and returns the uniform {ok:true} on 200', async () => {
    mockCallTransport.mockResolvedValue({ status: 200, body: { status: 'sent' } });
    const { res, status, json } = mockRes();
    await requestHandler(postReq({ email: 'andrew@example.com' }), res);

    expect(mockCallTransport).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransport.mock.calls[0];
    expect(method).toBe('POST');
    expect(path).toBe('/auth/otp/request');
    expect(opts.body).toEqual({ email: 'andrew@example.com' });

    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ ok: true });
  });

  it('relays the disabled 404 so the form can degrade gracefully', async () => {
    mockCallTransport.mockResolvedValue({
      status: 404,
      body: { error: 'otp_disabled' },
    });
    const { res, status, json } = mockRes();
    await requestHandler(postReq({ email: 'andrew@example.com' }), res);
    expect(status).toHaveBeenCalledWith(404);
    expect(json).toHaveBeenCalledWith({ ok: false, error: 'otp_disabled' });
  });

  it('relays a 429 rate limit', async () => {
    mockCallTransport.mockResolvedValue({
      status: 429,
      body: { error: 'rate_limited' },
    });
    const { res, status, json } = mockRes();
    await requestHandler(postReq({ email: 'andrew@example.com' }), res);
    expect(status).toHaveBeenCalledWith(429);
    expect(json).toHaveBeenCalledWith({ ok: false, error: 'rate_limited' });
  });

  it('maps a transport failure through sendTransportError', async () => {
    const boom = new Error('nope');
    mockCallTransport.mockRejectedValue(boom);
    const { res } = mockRes();
    await requestHandler(postReq({ email: 'andrew@example.com' }), res);
    expect(mockSendTransportError).toHaveBeenCalledWith(res, 'auth/otp/request', boom);
  });
});

describe('POST /api/auth/otp/verify', () => {
  const okBackend = {
    status: 200,
    body: {
      session_token: 'TOK.SIG',
      name: 'andrew',
      role: 'owner',
      exp: 4102444800,
    },
  };

  it('405 on a non-POST method (no relay)', async () => {
    const { res, status } = mockRes();
    await verifyHandler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('sets the session cookie SERVER-SIDE and NEVER returns the token in JSON', async () => {
    mockCallTransport.mockResolvedValue(okBackend);
    const { res, status, json, setHeader } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), res);

    // Relay shape.
    const [method, path, opts] = mockCallTransport.mock.calls[0];
    expect(method).toBe('POST');
    expect(path).toBe('/auth/otp/verify');
    expect(opts.body).toEqual({ email: 'andrew@example.com', code: '123456' });

    // THE COOKIE-JAR FIX: the real setSessionCookies wrote httpOnly cookies on
    // THIS response — the token travels only in Set-Cookie, never in the body.
    expect(setHeader).toHaveBeenCalledTimes(1);
    const [headerName, cookies] = setHeader.mock.calls[0];
    expect(headerName).toBe('Set-Cookie');
    expect(Array.isArray(cookies)).toBe(true);
    const sessionCookie = (cookies as string[]).find((c) =>
      c.startsWith(`${SESSION_COOKIE}=`),
    );
    expect(sessionCookie).toBeDefined();
    expect(sessionCookie).toContain('TOK.SIG');
    expect(sessionCookie).toContain('HttpOnly');
    const identityCookie = (cookies as string[]).find((c) =>
      c.startsWith(`${IDENTITY_COOKIE}=`),
    );
    expect(identityCookie).toBeDefined();

    // Body: {ok:true} and NOTHING else — no session_token key, no token bytes.
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledTimes(1);
    const bodyArg = json.mock.calls[0][0];
    expect(bodyArg).toEqual({ ok: true });
    expect('session_token' in bodyArg).toBe(false);
    expect(JSON.stringify(bodyArg)).not.toContain('TOK.SIG');
  });

  it('401 uniform invalid_or_expired on a backend 401 — no cookie set', async () => {
    mockCallTransport.mockResolvedValue({
      status: 401,
      body: { error: 'invalid_or_expired' },
    });
    const { res, status, json, setHeader } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ ok: false, error: 'invalid_or_expired' });
    expect(setHeader).not.toHaveBeenCalled();
  });

  it('404 otp_disabled on a backend 404 (feature off)', async () => {
    mockCallTransport.mockResolvedValue({
      status: 404,
      body: { error: 'otp_disabled' },
    });
    const { res, status, json, setHeader } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), res);
    expect(status).toHaveBeenCalledWith(404);
    expect(json).toHaveBeenCalledWith({ ok: false, error: 'otp_disabled' });
    expect(setHeader).not.toHaveBeenCalled();
  });

  it.each([
    ['12345', 'too short'],
    ['1234567', 'too long'],
    ['abcdef', 'non-digit'],
    ['', 'empty'],
  ])('401 uniform on a malformed code (%s — %s), NO relay', async (bad) => {
    const { res, status, json, setHeader } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: bad }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ ok: false, error: 'invalid_or_expired' });
    expect(mockCallTransport).not.toHaveBeenCalled();
    expect(setHeader).not.toHaveBeenCalled();
  });

  it('does not set a cookie when a 200 arrives WITHOUT a session_token', async () => {
    // Defensive: a malformed upstream success must not mint an empty session.
    mockCallTransport.mockResolvedValue({ status: 200, body: { name: 'andrew' } });
    const { res, status, json, setHeader } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), res);
    expect(setHeader).not.toHaveBeenCalled();
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ ok: false, error: 'invalid_or_expired' });
  });

  it('maps a transport failure through sendTransportError', async () => {
    const boom = new Error('nope');
    mockCallTransport.mockRejectedValue(boom);
    const { res } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), res);
    expect(mockSendTransportError).toHaveBeenCalledWith(res, 'auth/otp/verify', boom);
  });
});
