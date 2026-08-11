import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// #95 — POST /api/bugreport/submit, the BFF relay.
//
// The gate ORDER is the thing under test as much as the gates themselves. A
// deploy that hasn't added the token yet must answer "not switched on", not
// "you're not allowed" — those send an operator looking in two different
// places, and only one of them is the right one.
//
// The other pin that matters: the browser must never be able to reach any token
// but `web_bugreport`. The box peer-pins that name, so a BFF that relayed the
// chat token would produce a 401 that looks like an auth problem rather than
// like the wiring mistake it is.

const { mockCall, mockIsConfigured, mockIdentity, mockSession } = vi.hoisted(() => ({
  mockCall: vi.fn(),
  mockIsConfigured: vi.fn(),
  mockIdentity: vi.fn(),
  mockSession: vi.fn(),
}));

vi.mock('../lib/algernon/transport', async () => {
  const actual = await vi.importActual<typeof import('../lib/algernon/transport')>(
    '../lib/algernon/transport',
  );
  return {
    ...actual,
    callTransportBugReport: mockCall,
    isBugReportConfigured: mockIsConfigured,
  };
});

vi.mock('../lib/algernon/identity', () => ({
  readDisplayIdentity: mockIdentity,
  resolveSessionToken: mockSession,
}));

import handler from '../pages/api/bugreport/submit';
import { TransportConfigError, TransportTimeoutError } from '../lib/algernon/transport';

function mockRes() {
  const res: any = { statusCode: 0, payload: undefined, headers: {} as Record<string, string> };
  res.status = (code: number) => {
    res.statusCode = code;
    return res;
  };
  res.json = (p: unknown) => {
    res.payload = p;
    return res;
  };
  res.setHeader = (k: string, v: string) => {
    res.headers[k] = v;
  };
  return res;
}

function validBody(over: Record<string, unknown> = {}) {
  return {
    description: 'The Send button did nothing.',
    context: {
      route: '/chat',
      instance: 'Salem',
      user_agent: 'Mozilla/5.0',
      viewport_w: 390,
      viewport_h: 844,
      app_version: 'abc1234',
      ts: '2026-08-11T12:00:00.000Z',
    },
    ...over,
  };
}

async function call(body: unknown, method = 'POST') {
  const req: any = { method, body };
  const res = mockRes();
  await handler(req, res);
  return res;
}

beforeEach(() => {
  mockCall.mockReset();
  mockIsConfigured.mockReset().mockReturnValue(true);
  mockIdentity.mockReset().mockReturnValue({ name: 'Andrew', role: 'owner' });
  mockSession.mockReset().mockReturnValue('session-token');
  mockCall.mockResolvedValue({
    status: 200,
    body: { status: 'filed', report_id: 'r1', instance: 'Salem' },
  });
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('method + configuration gates', () => {
  it('refuses a non-POST with Allow', async () => {
    const res = await call(validBody(), 'GET');
    expect(res.statusCode).toBe(405);
    expect(res.headers.Allow).toBe('POST');
  });

  it('answers not_configured BEFORE checking the session', async () => {
    // Deploy-inert: the feature ships dark. Checking auth first would tell an
    // operator with no token that they are signed out, which is false.
    mockIsConfigured.mockReturnValue(false);
    mockSession.mockReturnValue(null);

    const res = await call(validBody());
    expect(res.statusCode).toBe(503);
    expect(res.payload).toEqual({ error: 'bugreport_not_configured' });
    expect(mockCall).not.toHaveBeenCalled();
  });

  it('refuses an unauthenticated caller fail-closed', async () => {
    mockSession.mockReturnValue(null);
    const res = await call(validBody());
    expect(res.statusCode).toBe(401);
    expect(res.payload).toEqual({ error: 'invalid_session' });
    expect(mockCall).not.toHaveBeenCalled();
  });

  it('does NOT require the owner role', async () => {
    // Deliberate divergence from ingest/submit. Anyone who can sign in can hit
    // a bug; gating the report button on a role would silence exactly the
    // users most likely to find one.
    mockIdentity.mockReturnValue({ name: 'Guest', role: 'member' });
    const res = await call(validBody());
    expect(res.statusCode).toBe(200);
    expect(mockCall).toHaveBeenCalledTimes(1);
  });

  it('still files when there is no display identity at all', async () => {
    mockIdentity.mockReturnValue(null);
    const res = await call(validBody());
    expect(res.statusCode).toBe(200);
    expect(mockCall.mock.calls[0][1].user).toBe(undefined);
    expect(mockCall.mock.calls[0][1].body.reported_by).toBe('');
  });
});

describe('body validation', () => {
  it('refuses an empty description at the edge', async () => {
    const res = await call(validBody({ description: '   ' }));
    expect(res.statusCode).toBe(400);
    expect(res.payload.error).toBe('invalid_request');
    expect(res.payload.detail).toContain('A description is required.');
    expect(mockCall).not.toHaveBeenCalled();
  });

  it('refuses an over-long description before relaying it onward', async () => {
    const res = await call(validBody({ description: 'x'.repeat(5001) }));
    expect(res.statusCode).toBe(400);
    expect(mockCall).not.toHaveBeenCalled();
  });

  it('refuses an oversize screenshot at the edge, not at the box', async () => {
    // The base64-inflated cap: the point is that the BFF does not buffer a
    // 20 MB string onward just to have the box refuse it.
    const res = await call(validBody({
      screenshot_b64: 'A'.repeat(Math.ceil((5 * 1024 * 1024 * 4) / 3) + 8192),
      screenshot_media_type: 'image/png',
    }));
    expect(res.statusCode).toBe(400);
    expect(mockCall).not.toHaveBeenCalled();
  });

  it('refuses a media type sent with no image', async () => {
    const res = await call(validBody({ screenshot_media_type: 'image/png' }));
    expect(res.statusCode).toBe(400);
    expect(res.payload.detail).toContain('without any screenshot data');
  });

  it('refuses a media type outside the allowlist', async () => {
    const res = await call(validBody({
      screenshot_b64: 'QUJD',
      screenshot_media_type: 'image/heic',
    }));
    expect(res.statusCode).toBe(400);
    expect(mockCall).not.toHaveBeenCalled();
  });

  it('refuses a malformed context block', async () => {
    const res = await call({ description: 'ok', context: { viewport_w: -3 } });
    expect(res.statusCode).toBe(400);
  });
});

describe('relay', () => {
  it('asserts the verified name as provenance and relays the report', async () => {
    const res = await call(validBody({
      screenshot_b64: 'QUJD',
      screenshot_media_type: 'image/png',
    }));

    expect(res.statusCode).toBe(200);
    const [path, opts] = mockCall.mock.calls[0];
    expect(path).toBe('/vault/bugreport');
    expect(opts.user).toBe('Andrew');
    expect(opts.body.description).toBe('The Send button did nothing.');
    expect(opts.body.context.route).toBe('/chat');
    expect(opts.body.screenshot_b64).toBe('QUJD');
    expect(opts.body.reported_by).toBe('Andrew');
  });

  it('omits the screenshot keys entirely when none was sent', async () => {
    await call(validBody());
    const sent = mockCall.mock.calls[0][1].body;
    expect('screenshot_b64' in sent).toBe(false);
    expect('screenshot_media_type' in sent).toBe(false);
  });

  it('defaults the media type to png when only the data is sent', async () => {
    await call(validBody({ screenshot_b64: 'QUJD' }));
    expect(mockCall.mock.calls[0][1].body.screenshot_media_type).toBe('image/png');
  });

  it('relays a backend refusal VERBATIM so the modal can map the code', async () => {
    // Collapsing these into a generic 502 would cost the reporter the one piece
    // of information that tells them what to do differently.
    mockCall.mockResolvedValue({
      status: 413,
      body: { error: 'screenshot_too_large', max_bytes: 5242880 },
    });
    const res = await call(validBody({ screenshot_b64: 'QUJD' }));
    expect(res.statusCode).toBe(413);
    expect(res.payload).toEqual({ error: 'screenshot_too_large', max_bytes: 5242880 });
  });

  it('relays the box’s wrong_peer refusal rather than swallowing it', async () => {
    mockCall.mockResolvedValue({ status: 401, body: { error: 'wrong_peer' } });
    const res = await call(validBody());
    expect(res.statusCode).toBe(401);
    expect(res.payload).toEqual({ error: 'wrong_peer' });
  });

  it('maps a misconfigured transport to a 500 that leaks no topology', async () => {
    mockCall.mockRejectedValue(new TransportConfigError('ALFRED_WEB_BUGREPORT_TOKEN is not set'));
    const res = await call(validBody());
    expect(res.statusCode).toBe(500);
    expect(res.payload).toEqual({ error: 'transport_misconfigured' });
    expect(JSON.stringify(res.payload)).not.toContain('ALFRED_WEB_BUGREPORT_TOKEN');
  });

  it('maps a timeout to 504 and an unreachable box to 502', async () => {
    mockCall.mockRejectedValue(new TransportTimeoutError('timed out'));
    expect((await call(validBody())).statusCode).toBe(504);

    mockCall.mockRejectedValue(new Error('ECONNREFUSED'));
    expect((await call(validBody())).statusCode).toBe(502);
  });

  it('tolerates a null upstream body', async () => {
    mockCall.mockResolvedValue({ status: 200, body: null });
    const res = await call(validBody());
    expect(res.statusCode).toBe(200);
    expect(res.payload).toEqual({});
  });
});
