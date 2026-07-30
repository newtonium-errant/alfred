import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks POST /api/ingest/shortcut — the bearer-only iOS Shortcuts capture door.
// Security-critical outward-facing surface (#23/#38 seriousness):
//   method (405) → env configured (503) → constant-time bearer (401) →
//   rate limit (429) → body shape (400) → target (400) → relay.
// The relay is mocked; we assert scope-by-construction provenance rides the body.

const { mockCallTransportTo, mockListIngestTargets } = vi.hoisted(() => ({
  mockCallTransportTo: vi.fn(),
  mockListIngestTargets: vi.fn(),
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransportTo: mockCallTransportTo,
  listIngestTargets: mockListIngestTargets,
  // bffError imports these from the (now mocked) transport module.
  TransportConfigError: class TransportConfigError extends Error {},
  TransportTimeoutError: class TransportTimeoutError extends Error {},
}));

import handler, { __resetShortcutRateLimitForTest } from '../pages/api/ingest/shortcut';

const TOKEN = 'DUMMY_SHORTCUT_TOKEN_1234';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

function req(opts: {
  method?: string;
  authorization?: string;
  body?: unknown;
}): NextApiRequest {
  const headers: Record<string, string> = {};
  if (opts.authorization !== undefined) headers.authorization = opts.authorization;
  return { method: opts.method ?? 'POST', headers, body: opts.body } as unknown as NextApiRequest;
}

beforeEach(() => {
  mockCallTransportTo.mockReset();
  mockListIngestTargets.mockReset();
  mockListIngestTargets.mockReturnValue([{ name: 'SALEM', label: 'Salem', recordTypes: ['note'] }]);
  mockCallTransportTo.mockResolvedValue({ status: 201, body: { ok: true, path: 'note/x.md' } });
  __resetShortcutRateLimitForTest();
  process.env.SHORTCUT_INGEST_TOKEN = TOKEN;
  delete process.env.SHORTCUT_INGEST_RATE_MAX;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  delete process.env.SHORTCUT_INGEST_TOKEN;
  delete process.env.SHORTCUT_INGEST_RATE_MAX;
});

// --- method + config gates ---------------------------------------------------

describe('POST /api/ingest/shortcut — gates', () => {
  it('405 on a non-POST method', async () => {
    const { res, status, json } = mockRes();
    await handler(req({ method: 'GET' }), res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('503 not_configured when SHORTCUT_INGEST_TOKEN is absent (deploy-inert)', async () => {
    delete process.env.SHORTCUT_INGEST_TOKEN;
    const { res, status, json } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(503);
    expect(json).toHaveBeenCalledWith({ error: 'not_configured' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('503 when the token env is set but blank', async () => {
    process.env.SHORTCUT_INGEST_TOKEN = '   ';
    const { res, status, json } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(503);
    expect(json).toHaveBeenCalledWith({ error: 'not_configured' });
  });
});

// --- constant-time bearer auth ----------------------------------------------

describe('POST /api/ingest/shortcut — bearer auth', () => {
  it('401 when the Authorization header is missing', async () => {
    const { res, status, json } = mockRes();
    await handler(req({ body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_token' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('401 on a wrong token (constant-time compare path)', async () => {
    const { res, status, json } = mockRes();
    await handler(req({ authorization: 'Bearer DUMMY_WRONG_TOKEN_zzzz', body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_token' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('401 on a short token (length must not leak — hashed compare)', async () => {
    const { res, status, json } = mockRes();
    await handler(req({ authorization: 'Bearer x', body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_token' });
  });

  it('401 on a malformed Authorization header (no Bearer scheme)', async () => {
    const { res, status } = mockRes();
    await handler(req({ authorization: TOKEN, body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(401);
  });

  it('accepts the correct token (case-insensitive Bearer scheme)', async () => {
    const { res, status } = mockRes();
    await handler(req({ authorization: `bearer ${TOKEN}`, body: { text: 'hi there' } }), res);
    expect(status).toHaveBeenCalledWith(201);
    expect(mockCallTransportTo).toHaveBeenCalledTimes(1);
  });
});

// --- body validation ---------------------------------------------------------

describe('POST /api/ingest/shortcut — body validation', () => {
  it('400 when text exceeds 8000 chars', async () => {
    const { res, status, json } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'a'.repeat(8001) } }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0]).toMatchObject({ error: 'invalid_request' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('400 when text is blank after trim', async () => {
    const { res, status } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: '   ' } }), res);
    expect(status).toHaveBeenCalledWith(400);
  });

  it('400 when record_type is not "note" (v1 note-only)', async () => {
    const { res, status, json } = mockRes();
    await handler(
      req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi', record_type: 'document' } }),
      res,
    );
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0]).toMatchObject({ error: 'invalid_request' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('accepts an explicit record_type of "note"', async () => {
    const { res, status } = mockRes();
    await handler(
      req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi', record_type: 'note' } }),
      res,
    );
    expect(status).toHaveBeenCalledWith(201);
  });
});

// --- target resolution -------------------------------------------------------

describe('POST /api/ingest/shortcut — target', () => {
  it('400 unknown_target when no ingest target is configured', async () => {
    mockListIngestTargets.mockReturnValue([]);
    const { res, status, json } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'unknown_target' });
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('defaults to "salem" and matches the configured "SALEM" case-insensitively', async () => {
    const { res, status } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(201);
    expect(mockCallTransportTo).toHaveBeenCalledWith('SALEM', 'POST', '/vault/ingest', expect.anything());
  });

  it('400 unknown_target for a target that is not configured', async () => {
    const { res, status, json } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi', target: 'ghost' } }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'unknown_target' });
  });
});

// --- relay shape + provenance (scope-by-construction) ------------------------

describe('POST /api/ingest/shortcut — relay', () => {
  it('relays note + provenance to the web_ingest peer, echoes upstream status', async () => {
    mockCallTransportTo.mockResolvedValue({ status: 201, body: { ok: true, path: 'note/y.md' } });
    const { res, status, json } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'buy milk tomorrow' } }), res);

    expect(status).toHaveBeenCalledWith(201);
    expect(json).toHaveBeenCalledWith({ ok: true, path: 'note/y.md' });

    const [targetName, method, path, opts] = mockCallTransportTo.mock.calls[0];
    expect(targetName).toBe('SALEM');
    expect(method).toBe('POST');
    expect(path).toBe('/vault/ingest');
    expect(opts.body).toMatchObject({
      record_type: 'note',
      body: 'buy milk tomorrow',
      source: 'iOS Shortcut',
      ingested_by: 'Andrew (shortcut)',
      set_fields: { ingested_via: 'shortcut' },
    });
    expect(opts.body.set_fields.origin_instance).toBeTypeOf('string');
    expect(typeof opts.body.correlation_id).toBe('string');
    expect(opts.headers['X-Alfred-Ingest-User']).toBe('Andrew (shortcut)');
  });

  it('relays a provided title verbatim', async () => {
    const { res } = mockRes();
    await handler(
      req({ authorization: `Bearer ${TOKEN}`, body: { text: 'body here', title: 'My Explicit Title' } }),
      res,
    );
    expect(mockCallTransportTo.mock.calls[0][3].body.title).toBe('My Explicit Title');
  });

  it('derives a title from the first words + Halifax stamp when title is absent', async () => {
    const { res } = mockRes();
    await handler(
      req({
        authorization: `Bearer ${TOKEN}`,
        body: { text: 'one two three four five six seven eight nine ten' },
      }),
      res,
    );
    const relayedTitle = mockCallTransportTo.mock.calls[0][3].body.title as string;
    // first ~8 words, then the voice-capture stamp (YYYY-MM-DD HHMM).
    expect(relayedTitle).toMatch(/^one two three four five six seven eight — voice capture \d{4}-\d{2}-\d{2} \d{4}$/);
  });

  it('maps a transport failure through sendTransportError (no topology leak)', async () => {
    mockCallTransportTo.mockRejectedValue(new Error('ECONNREFUSED 127.0.0.1:8790'));
    const { res, status, json } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'transport_unreachable' });
  });
});

// --- rate limiting -----------------------------------------------------------

describe('POST /api/ingest/shortcut — rate limit', () => {
  it('trips at N+1 within the window and recovers in the next window', async () => {
    process.env.SHORTCUT_INGEST_RATE_MAX = '2';
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-30T12:00:00Z'));

    const send = async () => {
      const { res, status } = mockRes();
      await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi' } }), res);
      return status;
    };

    expect(await send()).toHaveBeenCalledWith(201); // 1
    expect(await send()).toHaveBeenCalledWith(201); // 2
    expect(await send()).toHaveBeenCalledWith(429); // 3 → over ceiling
    expect(mockCallTransportTo).toHaveBeenCalledTimes(2); // the 429 never relayed

    // Advance past the 1-hour window → the bucket resets.
    vi.setSystemTime(new Date('2026-07-30T13:00:01Z'));
    expect(await send()).toHaveBeenCalledWith(201); // recovered
    expect(mockCallTransportTo).toHaveBeenCalledTimes(3);
  });

  it('a wrong token never touches the limiter (auth precedes it)', async () => {
    process.env.SHORTCUT_INGEST_RATE_MAX = '1';
    // Exhaust nothing with bad tokens, then a good request still succeeds.
    for (let i = 0; i < 5; i++) {
      const { res } = mockRes();
      await handler(req({ authorization: 'Bearer WRONG', body: { text: 'hi' } }), res);
    }
    const { res, status } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(201);
  });
});
