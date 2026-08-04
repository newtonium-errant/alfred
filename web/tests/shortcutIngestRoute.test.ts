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

import handler, {
  __resetShortcutRateLimitForTest,
  __shortcutRateLimitBucketCountForTest,
  __shortcutRateLimitMaxForTest,
  __shortcutSourceLabelForTest,
} from '../pages/api/ingest/shortcut';

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
  delete process.env.SHORTCUT_INGEST_SOURCE_LABEL;
  delete process.env.SHORTCUT_INGEST_USER;
  delete process.env.SHORTCUT_INGEST_DEFAULT_TARGET;
  delete process.env.NEXT_PUBLIC_INSTANCE_NAME;
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  delete process.env.SHORTCUT_INGEST_TOKEN;
  delete process.env.SHORTCUT_INGEST_RATE_MAX;
  delete process.env.SHORTCUT_INGEST_SOURCE_LABEL;
  delete process.env.SHORTCUT_INGEST_USER;
  delete process.env.SHORTCUT_INGEST_DEFAULT_TARGET;
  delete process.env.NEXT_PUBLIC_INSTANCE_NAME;
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

// --- provenance label (SHORTCUT_INGEST_SOURCE_LABEL) --------------------------
// This door is bearer-POST, not iOS-specific: an Android Tasker recipe hits the
// SAME route. Pre-parameterisation it stamped every capture 'iOS Shortcut', so
// an Android capture carried a FALSE origin on the one field the operator is
// meant to trust. These assert the relayed body — the wire, not a helper.

describe('POST /api/ingest/shortcut — source label', () => {
  async function relayedSource(): Promise<string> {
    const { res } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'buy milk' } }), res);
    return mockCallTransportTo.mock.calls[0][3].body.source as string;
  }

  it("default preserved: an UNSET env still stamps 'iOS Shortcut' (behaviour-identical)", async () => {
    expect(process.env.SHORTCUT_INGEST_SOURCE_LABEL).toBeUndefined();
    expect(await relayedSource()).toBe('iOS Shortcut');
  });

  it('override honoured: the configured label rides the relayed body', async () => {
    process.env.SHORTCUT_INGEST_SOURCE_LABEL = 'Android (Tasker)';
    expect(await relayedSource()).toBe('Android (Tasker)');
  });

  it('a whitespace-only label degrades to the default (never stamps BLANK provenance)', async () => {
    process.env.SHORTCUT_INGEST_SOURCE_LABEL = '   ';
    expect(await relayedSource()).toBe('iOS Shortcut');
  });

  it('a surrounding-whitespace label is trimmed, not stamped raw', async () => {
    process.env.SHORTCUT_INGEST_SOURCE_LABEL = '  Android (Tasker)  ';
    expect(await relayedSource()).toBe('Android (Tasker)');
  });

  it('garbage SHORTCUT_INGEST_SOURCE_LABEL falls back to the default (never empty)', () => {
    for (const garbage of ['', '   ', '\t\n']) {
      process.env.SHORTCUT_INGEST_SOURCE_LABEL = garbage;
      expect(__shortcutSourceLabelForTest()).toBe('iOS Shortcut');
    }
    // Controls: a real value is honoured; absent → default.
    process.env.SHORTCUT_INGEST_SOURCE_LABEL = 'Desktop hotkey';
    expect(__shortcutSourceLabelForTest()).toBe('Desktop hotkey');
    delete process.env.SHORTCUT_INGEST_SOURCE_LABEL;
    expect(__shortcutSourceLabelForTest()).toBe('iOS Shortcut');
  });
});

// --- whitespace degradation across the WHOLE env family (#29 R-b) ------------
// #27 fixed the source label; a bare `||` only catches '', so a whitespace-only
// value is truthy and sails through. These pin the same contract on every
// remaining operator-tunable field — all asserted on the relayed body/headers
// (the wire), never on a helper.

describe('POST /api/ingest/shortcut — env whitespace degradation', () => {
  async function relayed() {
    const { res } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'buy milk' } }), res);
    return mockCallTransportTo.mock.calls[0][3];
  }

  it('a whitespace-only SHORTCUT_INGEST_USER does not blank the attribution', async () => {
    // The exposed one: this rides BOTH the relayed field and the
    // X-Alfred-Ingest-User asserted-identity header on a privileged relay.
    process.env.SHORTCUT_INGEST_USER = '   ';
    const opts = await relayed();
    expect(opts.body.ingested_by).toBe('Andrew (shortcut)');
    expect(opts.headers['X-Alfred-Ingest-User']).toBe('Andrew (shortcut)');
  });

  it('a real SHORTCUT_INGEST_USER is honoured on both the field AND the header', async () => {
    process.env.SHORTCUT_INGEST_USER = 'Sam (tasker)';
    const opts = await relayed();
    expect(opts.body.ingested_by).toBe('Sam (tasker)');
    expect(opts.headers['X-Alfred-Ingest-User']).toBe('Sam (tasker)');
  });

  it('the field and the header can never disagree (one resolution per request)', async () => {
    process.env.SHORTCUT_INGEST_USER = '  Sam (tasker)  ';
    const opts = await relayed();
    expect(opts.headers['X-Alfred-Ingest-User']).toBe(opts.body.ingested_by);
    expect(opts.body.ingested_by).toBe('Sam (tasker)'); // trimmed, not raw
  });

  it('a whitespace-only NEXT_PUBLIC_INSTANCE_NAME does not blank origin_instance', async () => {
    process.env.NEXT_PUBLIC_INSTANCE_NAME = '   ';
    const opts = await relayed();
    expect(opts.body.set_fields.origin_instance).toBe('Algernon');
  });

  it('a whitespace-only default target routes to the built-in default, not a 400', async () => {
    // BEHAVIOUR CHANGE, deliberate: pre-fix this trimmed to '' and 400'd
    // unknown_target, DESTROYING the capture (the share/voice source is already
    // gone by then). This route's stated philosophy is that a bad name never
    // costs you the words — the directive parser keeps text intact rather than
    // rejecting it — so a garbage default degrades to the same place an UNSET
    // one goes, and the capture survives.
    process.env.SHORTCUT_INGEST_DEFAULT_TARGET = '   ';
    const { res, status } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'buy milk' } }), res);
    expect(status).toHaveBeenCalledWith(201);
    expect(mockCallTransportTo.mock.calls[0][0]).toBe('SALEM');
  });

  it('unset env is byte-identical to the pre-fix module-const behaviour', async () => {
    const opts = await relayed();
    expect(opts.body.ingested_by).toBe('Andrew (shortcut)');
    expect(opts.body.set_fields.origin_instance).toBe('Algernon');
    expect(opts.headers['X-Alfred-Ingest-User']).toBe('Andrew (shortcut)');
  });
});

// --- token trim symmetry (#29 R-b, builder judgment) -------------------------
// A secret gets NO fallback default — the fail-closed 503 is the point. But
// extractBearer TRIMS the token it parses off the header, so an untrimmed
// expected value could never match it.

describe('POST /api/ingest/shortcut — token whitespace symmetry', () => {
  it('a stored token with stray trailing whitespace still authenticates', async () => {
    // Routine in .env files, copy-paste and mounted secrets. Pre-fix this was a
    // PERMANENT 401 that reads exactly like a wrong credential — the worst kind
    // of misconfiguration to diagnose.
    process.env.SHORTCUT_INGEST_TOKEN = `${TOKEN}  \n`;
    const { res, status } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(201);
  });

  it('a leading-whitespace stored token also authenticates', async () => {
    process.env.SHORTCUT_INGEST_TOKEN = `   ${TOKEN}`;
    const { res, status } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(201);
  });

  it('trimming does NOT weaken the gate — a wrong token is still 401', async () => {
    process.env.SHORTCUT_INGEST_TOKEN = `${TOKEN}  `;
    const { res, status } = mockRes();
    await handler(req({ authorization: 'Bearer DUMMY_WRONG_TOKEN_zzzz', body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(mockCallTransportTo).not.toHaveBeenCalled();
  });

  it('a whitespace-only token is still 503 not_configured (no accidental default)', async () => {
    process.env.SHORTCUT_INGEST_TOKEN = '   ';
    const { res, status, json } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'hi' } }), res);
    expect(status).toHaveBeenCalledWith(503);
    expect(json).toHaveBeenCalledWith({ error: 'not_configured' });
  });
});

// --- degraded env is AUDIBLE (#29 WARN-1) ------------------------------------
// Degrading a garbage value keeps the capture, but doing it SILENTLY is our own
// intentionally-left-blank doctrine violated: the operator goes on believing a
// broken setting is honoured. Every fallback announces itself. All driven
// through the real handler.

describe('POST /api/ingest/shortcut — degraded env announces itself', () => {
  /** Collect console.warn text for the duration of a case. */
  function captureWarns(): string[] {
    const seen: string[] = [];
    vi.spyOn(console, 'warn').mockImplementation((...args: unknown[]) => {
      seen.push(args.map(String).join(' '));
    });
    return seen;
  }

  const degraded = (warns: string[]) => warns.filter((m) => m.includes('env_degraded'));

  async function post() {
    const { res } = mockRes();
    await handler(req({ authorization: `Bearer ${TOKEN}`, body: { text: 'buy milk' } }), res);
  }

  it('a blank default target warns — the one whose degradation changes ROUTING', async () => {
    process.env.SHORTCUT_INGEST_DEFAULT_TARGET = '   ';
    const warns = captureWarns();
    await post();
    expect(degraded(warns)).toEqual([
      '[bff:ingest/shortcut] env_degraded var=SHORTCUT_INGEST_DEFAULT_TARGET reason=blank',
    ]);
  });

  it('a blank ingest user warns (blank attribution + blank asserted-identity header)', async () => {
    process.env.SHORTCUT_INGEST_USER = '   ';
    const warns = captureWarns();
    await post();
    expect(degraded(warns)).toEqual([
      '[bff:ingest/shortcut] env_degraded var=SHORTCUT_INGEST_USER reason=blank',
    ]);
  });

  it('a blank source label warns', async () => {
    process.env.SHORTCUT_INGEST_SOURCE_LABEL = '   ';
    const warns = captureWarns();
    await post();
    expect(degraded(warns)).toEqual([
      '[bff:ingest/shortcut] env_degraded var=SHORTCUT_INGEST_SOURCE_LABEL reason=blank',
    ]);
  });

  it('a blank instance name warns', async () => {
    process.env.NEXT_PUBLIC_INSTANCE_NAME = '   ';
    const warns = captureWarns();
    await post();
    expect(degraded(warns)).toEqual([
      '[bff:ingest/shortcut] env_degraded var=NEXT_PUBLIC_INSTANCE_NAME reason=blank',
    ]);
  });

  it('a non-positive rate max warns with its OWN reason code (not "blank")', async () => {
    process.env.SHORTCUT_INGEST_RATE_MAX = '0';
    const warns = captureWarns();
    await post();
    expect(degraded(warns)).toEqual([
      '[bff:ingest/shortcut] env_degraded var=SHORTCUT_INGEST_RATE_MAX reason=not_positive_int',
    ]);
  });

  it('UNSET env is SILENT — a stock deploy must not shout on every request', async () => {
    // The pin that keeps this signal meaningful. Unset is the documented
    // default, not a degradation; warning on it would bury the real ones.
    const warns = captureWarns();
    await post();
    expect(degraded(warns)).toEqual([]);
  });

  it('a VALID override is silent too (only garbage is noisy)', async () => {
    process.env.SHORTCUT_INGEST_USER = 'Sam (tasker)';
    process.env.SHORTCUT_INGEST_SOURCE_LABEL = 'Android (Tasker)';
    process.env.SHORTCUT_INGEST_RATE_MAX = '10';
    const warns = captureWarns();
    await post();
    expect(degraded(warns)).toEqual([]);
  });

  it('several blank vars each get their own line (no swallowing after the first)', async () => {
    process.env.SHORTCUT_INGEST_USER = '   ';
    process.env.SHORTCUT_INGEST_SOURCE_LABEL = '  ';
    const warns = captureWarns();
    await post();
    expect(degraded(warns).sort()).toEqual([
      '[bff:ingest/shortcut] env_degraded var=SHORTCUT_INGEST_SOURCE_LABEL reason=blank',
      '[bff:ingest/shortcut] env_degraded var=SHORTCUT_INGEST_USER reason=blank',
    ]);
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

  it('distinct wrong tokens never populate the limiter Map (anti-flood, #38 invariant)', async () => {
    // The real flood vector: DISTINCT wrong tokens (distinct hashes). If the
    // limiter ran BEFORE auth, each would create its own bucket and grow the
    // Map. Because auth precedes the limiter, every one is 401'd first and the
    // Map stays empty. Reddens iff limiter-before-auth is (re)introduced.
    for (let i = 0; i < 25; i++) {
      const { res, status } = mockRes();
      await handler(req({ authorization: `Bearer DUMMY_WRONG_TOKEN_${i}`, body: { text: 'hi' } }), res);
      expect(status).toHaveBeenCalledWith(401);
    }
    expect(__shortcutRateLimitBucketCountForTest()).toBe(0);
  });

  it('garbage SHORTCUT_INGEST_RATE_MAX falls back to the default 30 (never 0/NaN/negative)', async () => {
    for (const garbage of ['0', 'abc', '-5']) {
      process.env.SHORTCUT_INGEST_RATE_MAX = garbage;
      expect(__shortcutRateLimitMaxForTest()).toBe(30);
    }
    // Controls: a valid positive value is honoured; absent → default.
    process.env.SHORTCUT_INGEST_RATE_MAX = '5';
    expect(__shortcutRateLimitMaxForTest()).toBe(5);
    delete process.env.SHORTCUT_INGEST_RATE_MAX;
    expect(__shortcutRateLimitMaxForTest()).toBe(30);
  });
});
