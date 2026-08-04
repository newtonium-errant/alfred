import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

// Locks the composer-log gate ordering (method → session 401 → OWNER 403 → body
// 400 → append) and the capture-only safety properties: server-FIXED append path
// (never client-derived), strip-unknowns, newline-forgery escaping, and
// swallow-on-append-failure (telemetry never breaks the page). Writes to a real
// temp file (builtin fs is not mocked) so the actual append behaviour is pinned.

const { mockResolveSessionToken, mockReadDisplayIdentity } = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockReadDisplayIdentity: vi.fn(),
}));
vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
  readDisplayIdentity: mockReadDisplayIdentity,
}));

import handler from '../pages/api/feed/composer-log';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}
function postReq(body: unknown): NextApiRequest {
  return { method: 'POST', body } as unknown as NextApiRequest;
}

let tmpDir: string;
let logPath: string;
let counter = 0;

beforeEach(() => {
  mockResolveSessionToken.mockReset().mockReturnValue('sess-token');
  mockReadDisplayIdentity.mockReset().mockReturnValue({ name: 'Andrew', role: 'owner' });
  tmpDir = mkdtempSync(join(tmpdir(), 'composer-log-'));
  logPath = join(tmpDir, `log-${counter++}.jsonl`);
  process.env.ALFRED_WEB_COMPOSER_LOG = logPath;
});
afterEach(() => {
  delete process.env.ALFRED_WEB_COMPOSER_LOG;
  try {
    rmSync(tmpDir, { recursive: true, force: true });
  } catch {
    /* best-effort temp cleanup */
  }
  vi.restoreAllMocks();
});

function readRecords(): Record<string, unknown>[] {
  return readFileSync(logPath, 'utf8')
    .split('\n')
    .filter(Boolean)
    .map((l) => JSON.parse(l) as Record<string, unknown>);
}

describe('POST /api/feed/composer-log', () => {
  it('405 on a non-POST method', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(existsSync(logPath)).toBe(false);
  });

  it('401 when there is no session', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(postReq({ rule: 'brief', event: 'composed' }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(existsSync(logPath)).toBe(false);
  });

  it('403 when the identity is not the owner', async () => {
    mockReadDisplayIdentity.mockReturnValue({ name: 'Guest', role: 'member' });
    const { res, status, json } = mockRes();
    await handler(postReq({ rule: 'brief', event: 'composed' }), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
    expect(existsSync(logPath)).toBe(false);
  });

  it('403 when there is no identity cookie at all', async () => {
    mockReadDisplayIdentity.mockReturnValue(null);
    const { res, status } = mockRes();
    await handler(postReq({ rule: 'brief', event: 'composed' }), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(existsSync(logPath)).toBe(false);
  });

  it('400 on an invalid rule / event enum', async () => {
    const { res, status, json } = mockRes();
    await handler(postReq({ rule: 'nope', event: 'composed' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0].error).toBe('invalid_request');
    expect(existsSync(logPath)).toBe(false);
  });

  it('appends one JSONL line with server-stamped at/user + the body fields, and 200s', async () => {
    const { res, status, json } = mockRes();
    await handler(postReq({ rule: 'checkin', event: 'navigated_away', dwell_ms: 4200, path: '/deck' }), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ ok: true, logged: true });
    const recs = readRecords();
    expect(recs).toHaveLength(1);
    expect(recs[0]).toMatchObject({
      rule: 'checkin',
      event: 'navigated_away',
      dwell_ms: 4200,
      path: '/deck',
      user: 'Andrew', // server-stamped from the verified identity
    });
    expect(typeof recs[0].at).toBe('string');
  });

  it('writes to the SERVER-FIXED path, never a client-supplied one', async () => {
    const { res } = mockRes();
    // A hostile `path` field must never influence WHERE we write — only WHAT we store.
    await handler(postReq({ rule: 'feed', event: 'composed', path: '/etc/passwd' }), res);
    expect(existsSync(logPath)).toBe(true);
    expect(readRecords()[0].path).toBe('/etc/passwd');
  });

  it('strips unknown keys (they never reach the log line)', async () => {
    const { res } = mockRes();
    await handler(postReq({ rule: 'brief', event: 'composed', evil: 'x', dwell_ms: 1 }), res);
    const rec = readRecords()[0];
    expect('evil' in rec).toBe(false);
    expect(rec.rule).toBe('brief');
  });

  it('a newline in `path` cannot forge a second JSONL record', async () => {
    const { res } = mockRes();
    await handler(postReq({ rule: 'brief', event: 'composed', path: 'a\n{"forged":true}' }), res);
    // Exactly one record — the embedded newline is JSON-escaped, not a delimiter.
    const raw = readFileSync(logPath, 'utf8');
    expect(raw.split('\n').filter(Boolean)).toHaveLength(1);
    expect(readRecords()[0].path).toBe('a\n{"forged":true}');
  });

  it('swallows an append failure — 200 logged:false, never throws', async () => {
    // Point the log at an existing DIRECTORY → appendFile fails (EISDIR).
    process.env.ALFRED_WEB_COMPOSER_LOG = tmpDir;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { res, status, json } = mockRes();
    await handler(postReq({ rule: 'brief', event: 'composed' }), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ ok: true, logged: false });
    expect(warn).toHaveBeenCalled();
  });
});

// --- spool-path degradation (#33) -------------------------------------------
// The stake here is higher than a provenance string: `||` only catches '', so a
// whitespace-only env is TRUTHY and became the literal FILENAME appended to.

describe('POST /api/feed/composer-log — spool path degradation', () => {
  const STRAY = '   ';

  afterEach(() => {
    // Never leave whitespace-named debris behind, even if an assertion failed.
    // A build WITHOUT the trim creates two shapes, and mutation-testing this pin
    // hits both: the stray file, and — from a leading-space path, which stops
    // being absolute — a whole bogus directory tree rooted at "  " in the cwd
    // (measured: `web/  /tmp/composer-log-*/log-N.jsonl  `). Clean both so a red
    // run can't litter the working tree.
    for (const name of [STRAY, '  ']) {
      try {
        rmSync(name, { recursive: true, force: true });
      } catch {
        /* best-effort */
      }
    }
  });

  it('a whitespace-only spool env does NOT become a whitespace FILENAME', async () => {
    process.env.ALFRED_WEB_COMPOSER_LOG = STRAY;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { res, status } = mockRes();
    await handler(postReq({ rule: 'brief', event: 'composed' }), res);

    expect(status).toHaveBeenCalledWith(200);
    // Pre-fix this created a file literally named "   " in the cwd, and the real
    // spool was never written to again.
    expect(existsSync(STRAY)).toBe(false);
    expect(warn).toHaveBeenCalledWith(
      '[bff:feed/composer-log] env_degraded var=ALFRED_WEB_COMPOSER_LOG reason=blank',
    );
  });

  it('a configured path is honoured, with surrounding whitespace trimmed off', async () => {
    process.env.ALFRED_WEB_COMPOSER_LOG = `  ${logPath}  `;
    const { res, status } = mockRes();
    await handler(postReq({ rule: 'brief', event: 'composed' }), res);

    expect(status).toHaveBeenCalledWith(200);
    expect(existsSync(logPath)).toBe(true);
    expect(readRecords()).toHaveLength(1);
  });

  it('UNSET is silent — the default spool is not a misconfiguration', async () => {
    delete process.env.ALFRED_WEB_COMPOSER_LOG;
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { res, status } = mockRes();
    await handler(postReq({ rule: 'brief', event: 'composed' }), res);

    expect(status).toHaveBeenCalledWith(200);
    expect(
      warn.mock.calls.filter((c) => String(c[0]).includes('env_degraded')),
    ).toEqual([]);
  });
});
