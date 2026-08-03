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

// --- BFF OTP observability -------------------------------------------------
// The 2026-07-27 / 2026-08-03 first-attempt sign-in failures left ZERO BFF
// evidence: the transport logged `web.auth.otp_verify_ok` both times while these
// two routes said nothing on ANY path, so there was no way to tell a BFF that
// returned {ok:true} (browser lost the response) from a BFF that collapsed the
// upstream 200 into its uniform 401. These pins keep every path — the happy one
// included — greppable, and keep the redaction contract intact. Per the
// log-emission discipline: the test drives the production code path, not the
// logger in isolation (authLog.test.ts covers the logger itself).

describe('OTP route observability', () => {
  function captureLogs() {
    const log = vi.spyOn(console, 'log').mockImplementation(() => {});
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    return {
      lines: () =>
        [...log.mock.calls, ...warn.mock.calls].map((c) => String(c[0])),
      warnLines: () => warn.mock.calls.map((c) => String(c[0])),
    };
  }

  const okBackend = {
    status: 200,
    body: { session_token: 'TOK.SIG', name: 'andrew', role: 'owner', exp: 4102444800 },
  };

  it('logs the SUCCESS path — the line whose absence made the incident undiagnosable', async () => {
    mockCallTransport.mockResolvedValue(okBackend);
    const cap = captureLogs();
    const { res } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), res);

    const hits = cap.lines().filter((l) => l.includes('[bff:auth/otp/verify]'));
    expect(hits).toHaveLength(1);
    expect(hits[0]).toContain('outcome=ok');
    expect(hits[0]).toContain('upstream=200');
    expect(hits[0]).toContain('status_class=2xx');
    expect(hits[0]).toContain('cookie_set=true');
  });

  it('logs a genuine upstream rejection with its status class', async () => {
    mockCallTransport.mockResolvedValue({ status: 401, body: { error: 'invalid_or_expired' } });
    const cap = captureLogs();
    const { res } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), res);

    const hits = cap.lines().filter((l) => l.includes('[bff:auth/otp/verify]'));
    expect(hits).toHaveLength(1);
    expect(hits[0]).toContain('outcome=rejected');
    expect(hits[0]).toContain('upstream=401');
    expect(hits[0]).toContain('cookie_set=false');
  });

  it('WARNS on the 200-without-token collapse — and still answers the uniform 401', async () => {
    // The evidence-destroying branch: upstream ACCEPTED the code (so it is burned)
    // but sent no usable token. It must be distinguishable in the JOURNAL while
    // staying byte-identical to an ordinary rejection ON THE WIRE — a distinct
    // status would tell a caller their code was correct (no-oracle).
    mockCallTransport.mockResolvedValue({ status: 200, body: { name: 'andrew' } });
    const cap = captureLogs();
    const { res, status, json, setHeader } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), res);

    const warns = cap.warnLines().filter((l) => l.includes('[bff:auth/otp/verify]'));
    expect(warns).toHaveLength(1);
    expect(warns[0]).toContain('outcome=upstream_ok_no_token');
    expect(warns[0]).toContain('upstream=200');

    // The wire shape is UNCHANGED — same status, same body, no cookie.
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ ok: false, error: 'invalid_or_expired' });
    expect(setHeader).not.toHaveBeenCalled();
  });

  it('logs a relay failure as transport_error (commit state unknowable from here)', async () => {
    mockCallTransport.mockRejectedValue(new TypeError('fetch failed'));
    const cap = captureLogs();
    const { res } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), res);

    const warns = cap.warnLines().filter((l) => l.includes('[bff:auth/otp/verify]'));
    expect(warns).toHaveLength(1);
    expect(warns[0]).toContain('outcome=transport_error');
    expect(warns[0]).toContain('error_name=TypeError');
  });

  it('logs an edge rejection that never reached the transport', async () => {
    const cap = captureLogs();
    const { res } = mockRes();
    await verifyHandler(postReq({ email: 'andrew@example.com', code: 'abcdef' }), res);

    const hits = cap.lines().filter((l) => l.includes('[bff:auth/otp/verify]'));
    expect(hits).toHaveLength(1);
    expect(hits[0]).toContain('outcome=bad_request');
    expect(hits[0]).toContain('upstream=none');
    expect(mockCallTransport).not.toHaveBeenCalled();
  });

  it('logs the issuance half so a verify always has a matching request', async () => {
    mockCallTransport.mockResolvedValue({ status: 200, body: { status: 'sent' } });
    const cap = captureLogs();
    const { res } = mockRes();
    await requestHandler(postReq({ email: 'andrew@example.com' }), res);

    const hits = cap.lines().filter((l) => l.includes('[bff:auth/otp/request]'));
    expect(hits).toHaveLength(1);
    expect(hits[0]).toContain('outcome=sent');
    expect(hits[0]).toContain('upstream=200');
  });

  it('logs a rejected issuance with the relayed error code', async () => {
    mockCallTransport.mockResolvedValue({ status: 429, body: { error: 'rate_limited' } });
    const cap = captureLogs();
    const { res } = mockRes();
    await requestHandler(postReq({ email: 'andrew@example.com' }), res);

    const hits = cap.lines().filter((l) => l.includes('[bff:auth/otp/request]'));
    expect(hits).toHaveLength(1);
    expect(hits[0]).toContain('outcome=rejected');
    expect(hits[0]).toContain('upstream=429');
    expect(hits[0]).toContain('error=rate_limited');
  });

  // LOG-FORGING PIN, driven through the PRODUCTION relay path rather than the
  // sanitiser in isolation: request.ts passes the upstream `error` string
  // straight into the log line, and it is unvalidated. A newline in it would
  // otherwise emit a second journal line that greps as a genuine success —
  // the log equivalent of an injection.
  it('cannot be made to forge a log line via the relayed upstream error string', async () => {
    mockCallTransport.mockResolvedValue({
      status: 429,
      body: { error: 'rate_limited\n[bff:auth/otp/verify] outcome=ok upstream=200 status_class=2xx' },
    });
    const cap = captureLogs();
    const { res } = mockRes();
    await requestHandler(postReq({ email: 'andrew@example.com' }), res);

    const lines = cap.lines();
    expect(lines).toHaveLength(1);
    expect(lines[0]).not.toContain('\n');
    // The forged text is neutralised, not merely relocated: no second record can
    // be read out of this line, and the real outcome is still legible.
    expect(lines[0]).toContain('outcome=rejected');
    expect(lines[0]).not.toContain('outcome=ok');
  });

  it('cannot be made to forge a line via a control character in the email either', async () => {
    // emailDomain() echoes the domain, so the address is a second injection vector.
    mockCallTransport.mockResolvedValue({ status: 200, body: { status: 'sent' } });
    const cap = captureLogs();
    const { res } = mockRes();
    await requestHandler(postReq({ email: 'andrew@exa\nmple.com' }), res);

    for (const line of cap.lines()) expect(line).not.toContain('\n');
  });

  // THE REDACTION PIN. Drives every logging path on both routes and asserts the
  // three forbidden secrets never appear. Security-load-bearing: adding a field
  // to a log line must not be able to leak the passcode, the session token, or
  // an operator's address into the journal.
  it('never logs the passcode, the session token, or a full email — any path', async () => {
    const cap = captureLogs();
    const cases: Array<() => Promise<unknown>> = [
      async () => {
        mockCallTransport.mockResolvedValue(okBackend);
        return verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), mockRes().res);
      },
      async () => {
        mockCallTransport.mockResolvedValue({ status: 401, body: { error: 'invalid_or_expired' } });
        return verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), mockRes().res);
      },
      async () => {
        mockCallTransport.mockResolvedValue({ status: 200, body: { name: 'andrew' } });
        return verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), mockRes().res);
      },
      async () => {
        mockCallTransport.mockResolvedValue({ status: 404, body: { error: 'otp_disabled' } });
        return verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), mockRes().res);
      },
      async () => {
        mockCallTransport.mockRejectedValue(new Error('boom'));
        return verifyHandler(postReq({ email: 'andrew@example.com', code: '123456' }), mockRes().res);
      },
      async () => {
        mockCallTransport.mockResolvedValue({ status: 200, body: { status: 'sent' } });
        return requestHandler(postReq({ email: 'andrew@example.com' }), mockRes().res);
      },
    ];
    for (const run of cases) await run();

    const all = cap.lines().join('\n');
    expect(all).not.toContain('123456'); // the passcode
    expect(all).not.toContain('TOK.SIG'); // the session token
    expect(all).not.toContain('andrew@example.com'); // the full address
    // The domain IS emitted — the correlation handle the redaction deliberately keeps.
    expect(all).toContain('email_domain=example.com');
  });
});
