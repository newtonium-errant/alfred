import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// #90 — per-instance batch targets.
//
// The env family mirrors chat's, with one deliberate DIFFERENCE that these pin:
// chat excludes the home instance from its target list because home rides the
// SESSION path (a genuinely different auth mechanism). Batch has no such split
// — every target, home included, is reached with a `web_batch` peer token — so
// home IS resolvable here and is the default. One resolution path instead of
// two, available precisely because the auth story is uniform.
//
// Fail-closed throughout: a target counts as configured only when BOTH its URL
// and token are present, so a half-finished deploy never appears in the picker
// and never resolves.

const HOME = 'Salem';

function setEnv(vars: Record<string, string | undefined>) {
  for (const [k, v] of Object.entries(vars)) {
    if (v === undefined) delete process.env[k];
    else process.env[k] = v;
  }
}

const ENV_KEYS = [
  'NEXT_PUBLIC_INSTANCE_NAME',
  'ALFRED_WEB_TRANSPORT_URL',
  'ALFRED_WEB_BATCH_TOKEN',
  'ALFRED_WEB_BATCH_VERA_URL',
  'ALFRED_WEB_BATCH_VERA_TOKEN',
  'ALFRED_WEB_BATCH_VERA_LABEL',
  'ALFRED_WEB_BATCH_KALLE_URL',
  'ALFRED_WEB_BATCH_KALLE_TOKEN',
  'ALFRED_WEB_BATCH_SALEM_URL',
  'ALFRED_WEB_BATCH_SALEM_TOKEN',
];
const saved: Record<string, string | undefined> = {};

beforeEach(() => {
  vi.resetModules();
  for (const k of ENV_KEYS) saved[k] = process.env[k];
  for (const k of ENV_KEYS) delete process.env[k];
  process.env.NEXT_PUBLIC_INSTANCE_NAME = HOME;
});

afterEach(() => {
  setEnv(saved);
  vi.restoreAllMocks();
});

async function transport() {
  return import('../lib/algernon/transport');
}

describe('listBatchTargets', () => {
  it('is empty when nothing is wired', async () => {
    // A real answer, not an error — the page renders an explicit empty state.
    const { listBatchTargets } = await transport();
    expect(listBatchTargets()).toEqual([]);
  });

  it('lists the home instance when its unprefixed pair is set', async () => {
    setEnv({
      ALFRED_WEB_TRANSPORT_URL: 'http://127.0.0.1:8891',
      ALFRED_WEB_BATCH_TOKEN: 'home-token',
    });
    const { listBatchTargets } = await transport();
    expect(listBatchTargets()).toEqual([
      { name: HOME, label: HOME, home: true },
    ]);
  });

  it('adds a fully-configured cross-instance target', async () => {
    setEnv({
      ALFRED_WEB_TRANSPORT_URL: 'http://127.0.0.1:8891',
      ALFRED_WEB_BATCH_TOKEN: 'home-token',
      ALFRED_WEB_BATCH_VERA_URL: 'http://127.0.0.1:8893',
      ALFRED_WEB_BATCH_VERA_TOKEN: 'vera-token',
    });
    const { listBatchTargets } = await transport();
    expect(listBatchTargets().map((t) => t.name)).toEqual([HOME, 'VERA']);
  });

  it('puts home FIRST, then the rest by label', async () => {
    // Home is the default; a picker whose default is not the first option is
    // a control that argues with itself.
    setEnv({
      ALFRED_WEB_TRANSPORT_URL: 'u',
      ALFRED_WEB_BATCH_TOKEN: 't',
      ALFRED_WEB_BATCH_VERA_URL: 'u',
      ALFRED_WEB_BATCH_VERA_TOKEN: 't',
      ALFRED_WEB_BATCH_KALLE_URL: 'u',
      ALFRED_WEB_BATCH_KALLE_TOKEN: 't',
    });
    const { listBatchTargets } = await transport();
    expect(listBatchTargets().map((t) => t.name)).toEqual([HOME, 'KALLE', 'VERA']);
  });

  it('FAIL-CLOSED: a URL with no token is not a target', async () => {
    // A half-configured instance must never appear in the picker — choosing it
    // would fail at submit, after the operator scanned thirty pages.
    setEnv({ ALFRED_WEB_BATCH_VERA_URL: 'http://127.0.0.1:8893' });
    const { listBatchTargets } = await transport();
    expect(listBatchTargets()).toEqual([]);
  });

  it('FAIL-CLOSED: a token with no URL is not a target', async () => {
    setEnv({ ALFRED_WEB_BATCH_VERA_TOKEN: 'vera-token' });
    const { listBatchTargets } = await transport();
    expect(listBatchTargets()).toEqual([]);
  });

  it('honours a display label', async () => {
    setEnv({
      ALFRED_WEB_BATCH_VERA_URL: 'u',
      ALFRED_WEB_BATCH_VERA_TOKEN: 't',
      ALFRED_WEB_BATCH_VERA_LABEL: 'VERA (clinic)',
    });
    const { listBatchTargets } = await transport();
    expect(listBatchTargets()[0].label).toBe('VERA (clinic)');
  });

  it('drops a prefixed pair that names the HOME instance', async () => {
    // Two sources of truth for one target is how "which env did this use?"
    // stops having an answer. The unprefixed home pair wins.
    setEnv({
      ALFRED_WEB_TRANSPORT_URL: 'http://home',
      ALFRED_WEB_BATCH_TOKEN: 'home-token',
      ALFRED_WEB_BATCH_SALEM_URL: 'http://shadow',
      ALFRED_WEB_BATCH_SALEM_TOKEN: 'shadow-token',
    });
    const { listBatchTargets } = await transport();
    expect(listBatchTargets()).toEqual([
      { name: HOME, label: HOME, home: true },
    ]);
  });

  it('never leaks a URL or token in the metadata', async () => {
    setEnv({
      ALFRED_WEB_BATCH_VERA_URL: 'http://secret-host:9999',
      ALFRED_WEB_BATCH_VERA_TOKEN: 'super-secret-token',
    });
    const { listBatchTargets } = await transport();
    const serialised = JSON.stringify(listBatchTargets());
    expect(serialised).not.toContain('secret-host');
    expect(serialised).not.toContain('super-secret-token');
  });
});

describe('resolveBatchTarget', () => {
  it('resolves an empty name to the HOME pair', async () => {
    setEnv({
      ALFRED_WEB_TRANSPORT_URL: 'http://127.0.0.1:8891/',
      ALFRED_WEB_BATCH_TOKEN: 'home-token',
    });
    const { resolveBatchTarget } = await transport();
    const r = resolveBatchTarget('');
    expect(r.baseUrl).toBe('http://127.0.0.1:8891'); // trailing slash trimmed
    expect(r.token).toBe('home-token');
  });

  it('resolves the home NAME to the home pair, not a prefixed lookup', async () => {
    setEnv({
      ALFRED_WEB_TRANSPORT_URL: 'http://home',
      ALFRED_WEB_BATCH_TOKEN: 'home-token',
    });
    const { resolveBatchTarget } = await transport();
    expect(resolveBatchTarget(HOME).token).toBe('home-token');
    expect(resolveBatchTarget('salem').token).toBe('home-token'); // case-insensitive
  });

  it('resolves a cross-instance name to ITS OWN pair', async () => {
    // The whole point: VERA's batch must go to VERA with VERA's token. Sending
    // it to home with home's token would file another instance's scans into
    // this one's vault.
    setEnv({
      ALFRED_WEB_TRANSPORT_URL: 'http://home',
      ALFRED_WEB_BATCH_TOKEN: 'home-token',
      ALFRED_WEB_BATCH_VERA_URL: 'http://vera:8893',
      ALFRED_WEB_BATCH_VERA_TOKEN: 'vera-token',
    });
    const { resolveBatchTarget } = await transport();
    const r = resolveBatchTarget('VERA');
    expect(r.baseUrl).toBe('http://vera:8893');
    expect(r.token).toBe('vera-token');
  });

  it('throws on a target whose env is missing', async () => {
    const { resolveBatchTarget, TransportConfigError } = await transport();
    expect(() => resolveBatchTarget('VERA')).toThrow(TransportConfigError);
  });

  it('throws on a malformed target name rather than probing env', async () => {
    const { resolveBatchTarget, TransportConfigError } = await transport();
    expect(() => resolveBatchTarget('../../etc')).toThrow(TransportConfigError);
  });
});

// ---------------------------------------------------------------------------
// GET /api/batch/targets
// ---------------------------------------------------------------------------

function req(method: string): NextApiRequest {
  return { method, headers: {}, cookies: {}, query: {} } as unknown as NextApiRequest;
}

function mockRes() {
  const json = vi.fn();
  const status = vi.fn(() => ({ json }));
  const setHeader = vi.fn();
  return { res: { status, json, setHeader } as unknown as NextApiResponse, status, json };
}

describe('GET /api/batch/targets', () => {
  async function load(identity: unknown, session: string | null) {
    vi.doMock('../lib/algernon/identity', () => ({
      resolveSessionToken: () => session,
      readDisplayIdentity: () => identity,
    }));
    return (await import('../pages/api/batch/targets')).default;
  }

  it('405s a non-GET', async () => {
    const handler = await load({ name: 'A', role: 'owner' }, 'tok');
    const { res, status } = mockRes();
    handler(req('POST'), res);
    expect(status).toHaveBeenCalledWith(405);
  });

  it('401s when signed out', async () => {
    const handler = await load(null, null);
    const { res, status, json } = mockRes();
    handler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
  });

  it('403s a NON-OWNER', async () => {
    // A non-owner cannot submit, so listing the deployment's instance topology
    // to them would leak more than it serves.
    const handler = await load({ name: 'Guest', role: 'member' }, 'tok');
    const { res, status, json } = mockRes();
    handler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
  });

  it('returns the configured targets to the owner', async () => {
    setEnv({
      ALFRED_WEB_TRANSPORT_URL: 'http://home',
      ALFRED_WEB_BATCH_TOKEN: 'home-token',
    });
    const handler = await load({ name: 'Andrew', role: 'owner' }, 'tok');
    const { res, status, json } = mockRes();
    handler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({
      targets: [{ name: HOME, label: HOME, home: true }],
    });
  });

  it('an empty list is a 200, not an error', async () => {
    const handler = await load({ name: 'Andrew', role: 'owner' }, 'tok');
    const { res, status, json } = mockRes();
    handler(req('GET'), res);
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ targets: [] });
  });
});
