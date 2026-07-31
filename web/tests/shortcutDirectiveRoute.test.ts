import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Route-level pins for voice-directive routing on POST /api/ingest/shortcut.
// (The pre-directive behavior is pinned by shortcutIngestRoute.test.ts, which
// stays untouched — the no-directive path here re-confirms byte-identical relay.)

const { mockCallTransportTo, mockListIngestTargets } = vi.hoisted(() => ({
  mockCallTransportTo: vi.fn(),
  mockListIngestTargets: vi.fn(),
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransportTo: mockCallTransportTo,
  listIngestTargets: mockListIngestTargets,
  TransportConfigError: class TransportConfigError extends Error {},
  TransportTimeoutError: class TransportTimeoutError extends Error {},
}));

import handler, { __resetShortcutRateLimitForTest } from '../pages/api/ingest/shortcut';

const TOKEN = 'DUMMY_SHORTCUT_TOKEN_1234';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json };
}

function req(body: unknown): NextApiRequest {
  return {
    method: 'POST',
    headers: { authorization: `Bearer ${TOKEN}` },
    body,
  } as unknown as NextApiRequest;
}

function relayed() {
  return mockCallTransportTo.mock.calls[0];
}

beforeEach(() => {
  mockCallTransportTo.mockReset();
  mockListIngestTargets.mockReset();
  // SALEM (default) + HYPATIA configured; VERA deliberately NOT configured.
  mockListIngestTargets.mockReturnValue([
    { name: 'SALEM', label: 'Salem', recordTypes: ['note'] },
    { name: 'HYPATIA', label: 'Hypatia', recordTypes: ['note'] },
  ]);
  mockCallTransportTo.mockResolvedValue({ status: 201, body: { ok: true, path: 'note/x.md' } });
  __resetShortcutRateLimitForTest();
  process.env.SHORTCUT_INGEST_TOKEN = TOKEN;
  delete process.env.SHORTCUT_DIRECTIVE_ALIASES;
});

afterEach(() => {
  vi.restoreAllMocks();
  delete process.env.SHORTCUT_INGEST_TOKEN;
  delete process.env.SHORTCUT_DIRECTIVE_ALIASES;
});

describe('directive routing — re-homes to a configured instance + strips + provenance', () => {
  it('form 1 "message for Hypatia, …" routes to HYPATIA with the prefix stripped', async () => {
    const { res } = mockRes();
    await handler(req({ text: 'message for Hypatia, add to my story idea' }), res);
    const [targetName, , , opts] = relayed();
    expect(targetName).toBe('HYPATIA');
    expect(opts.body.body).toBe('add to my story idea');
    expect(opts.body.set_fields.routed_via).toBe('directive');
    expect(opts.body.source).toContain('message for Hypatia');
  });

  it('form 2 "for Hypatia: …" routes to HYPATIA', async () => {
    const { res } = mockRes();
    await handler(req({ text: 'for Hypatia: buy milk' }), res);
    const [targetName, , , opts] = relayed();
    expect(targetName).toBe('HYPATIA');
    expect(opts.body.body).toBe('buy milk');
  });

  it('form 3 "Hypatia: …" routes to HYPATIA', async () => {
    const { res } = mockRes();
    await handler(req({ text: 'Hypatia: buy milk' }), res);
    expect(relayed()[0]).toBe('HYPATIA');
    expect(relayed()[3].body.body).toBe('buy milk');
  });

  it('an STT alias ("high patia") resolves to HYPATIA', async () => {
    const { res } = mockRes();
    await handler(req({ text: 'high patia: note this down' }), res);
    expect(relayed()[0]).toBe('HYPATIA');
    expect(relayed()[3].body.body).toBe('note this down');
  });

  it('the derived title comes from the STRIPPED body, not the directive', async () => {
    const { res } = mockRes();
    await handler(req({ text: 'message for Hypatia: alpha beta gamma delta' }), res);
    expect(relayed()[3].body.title as string).toMatch(/^alpha beta gamma delta —/);
  });
});

describe('directive routing — fail-safe (text kept INTACT to the default)', () => {
  it('an unknown name is not a directive → SALEM default, body fully intact, no routed_via', async () => {
    const { res } = mockRes();
    await handler(req({ text: 'message for Bob: hello there' }), res);
    const [targetName, , , opts] = relayed();
    expect(targetName).toBe('SALEM');
    expect(opts.body.body).toBe('message for Bob: hello there'); // intact — no words lost
    expect(opts.body.set_fields.routed_via).toBeUndefined();
    expect(opts.body.source).toBe('iOS Shortcut');
  });

  it('a recognised-but-UNCONFIGURED name keeps text intact to default + logs the intent', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const { res } = mockRes();
    await handler(req({ text: 'message for Vera: the driver called' }), res);
    const [targetName, , , opts] = relayed();
    expect(targetName).toBe('SALEM'); // VERA isn't configured → default
    expect(opts.body.body).toBe('message for Vera: the driver called'); // INTACT
    expect(opts.body.set_fields.routed_via).toBeUndefined();
    expect(warn).toHaveBeenCalledWith(
      expect.stringContaining('directive_target_unconfigured target=vera'),
    );
  });

  it('a no-directive capture is byte-identical relay (SALEM, verbatim, no routed_via)', async () => {
    const { res } = mockRes();
    await handler(req({ text: 'buy milk tomorrow' }), res);
    const [targetName, , , opts] = relayed();
    expect(targetName).toBe('SALEM');
    expect(opts.body.body).toBe('buy milk tomorrow');
    expect(opts.body.source).toBe('iOS Shortcut');
    expect(opts.body.set_fields.routed_via).toBeUndefined();
  });

  it('a directive with no remainder ("Hypatia:") is kept intact (never strips to empty)', async () => {
    const { res } = mockRes();
    await handler(req({ text: 'Hypatia:' }), res);
    const [targetName, , , opts] = relayed();
    expect(targetName).toBe('SALEM');
    expect(opts.body.body).toBe('Hypatia:');
  });
});

describe('directive routing — garbage-safe config', () => {
  it('a malformed SHORTCUT_DIRECTIVE_ALIASES falls back to defaults (Hypatia still routes)', async () => {
    process.env.SHORTCUT_DIRECTIVE_ALIASES = '{broken json';
    const { res } = mockRes();
    await handler(req({ text: 'Hypatia: still works' }), res);
    expect(relayed()[0]).toBe('HYPATIA');
    expect(relayed()[3].body.body).toBe('still works');
  });
});
