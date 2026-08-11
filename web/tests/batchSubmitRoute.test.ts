import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { Readable } from 'stream';

// #83 item 6 — the BFF relay door.
//
// This route is a BYTE PIPE with auth in front of it, so the pins are about the
// auth and about the bytes surviving unchanged. Every refusal asserts that
// NOTHING WAS RELAYED, not just the status code: a 401/403 is also what a
// broken import or a typo'd handler returns, and "the transport was never
// called" is the fact that distinguishes a working gate from a coincidence.

const identity = vi.hoisted(() => ({
  resolveSessionToken: vi.fn(),
  readDisplayIdentity: vi.fn(),
}));
const transport = vi.hoisted(() => ({
  callTransportBatch: vi.fn(),
  listBatchTargets: vi.fn(),
}));

vi.mock('../lib/algernon/identity', () => identity);
vi.mock('../lib/algernon/transport', async () => {
  const actual = await vi.importActual<Record<string, unknown>>(
    '../lib/algernon/transport',
  );
  return { ...actual, ...transport };
});

import handler from '../pages/api/batch/submit';

function req(body: Buffer, over: Record<string, unknown> = {}) {
  const stream = Readable.from([body]) as unknown as Record<string, unknown>;
  stream.method = 'POST';
  stream.query = {};
  stream.headers = { 'content-type': 'multipart/form-data; boundary=----abc' };
  Object.assign(stream, over);
  return stream as never;
}

function res() {
  const out: { status?: number; body?: unknown; headers: Record<string, string> } = {
    headers: {},
  };
  const r = {
    status(code: number) {
      out.status = code;
      return r;
    },
    json(payload: unknown) {
      out.body = payload;
      return r;
    },
    setHeader(k: string, v: string) {
      out.headers[k] = v;
    },
  };
  return { r: r as never, out };
}

beforeEach(() => {
  identity.resolveSessionToken.mockReturnValue('session-token');
  identity.readDisplayIdentity.mockReturnValue({ name: 'Andrew', role: 'owner' });
  transport.listBatchTargets.mockReturnValue([
    { name: 'Salem', label: 'Salem', home: true },
    { name: 'VERA', label: 'VERA', home: false },
  ]);
  transport.callTransportBatch.mockResolvedValue({
    status: 200,
    body: { status: 'queued', batch_id: 'b1' },
  });
});

afterEach(() => vi.clearAllMocks());

describe('POST /api/batch/submit', () => {
  it('relays the raw multipart body VERBATIM with its boundary', async () => {
    // The BFF must not parse or re-encode: a second multipart implementation
    // here would have limits that could disagree with the box's, and the box
    // is the only layer a client cannot skip.
    const body = Buffer.from('------abc\r\nContent-Disposition: form-data\r\n\r\nx');
    const { r, out } = res();
    await handler(req(body), r);

    expect(out.status).toBe(200);
    expect(transport.callTransportBatch).toHaveBeenCalledTimes(1);
    const call = transport.callTransportBatch.mock.calls[0];
    expect(call[0]).toBe('/vault/batch');
    expect(Buffer.compare(call[1].body, body)).toBe(0);
    expect(call[1].contentType).toBe('multipart/form-data; boundary=----abc');
  });

  it('carries the VERIFIED name as provenance, never the client claim', async () => {
    identity.readDisplayIdentity.mockReturnValue({ name: 'Andrew', role: 'owner' });
    const { r } = res();
    await handler(req(Buffer.from('x'), { headers: {
      'content-type': 'multipart/form-data; boundary=b',
      'x-alfred-batch-user': 'attacker',
    } }), r);
    expect(transport.callTransportBatch.mock.calls[0][1].user).toBe('Andrew');
  });

  it('relays the backend status and body unchanged', async () => {
    transport.callTransportBatch.mockResolvedValue({
      status: 413,
      body: { error: 'batch_too_large', max_bytes: 134217728 },
    });
    const { r, out } = res();
    await handler(req(Buffer.from('x')), r);
    expect(out.status).toBe(413);
    expect(out.body).toEqual({ error: 'batch_too_large', max_bytes: 134217728 });
  });

  it('refuses a non-POST', async () => {
    const { r, out } = res();
    await handler(req(Buffer.from('x'), { method: 'GET' }), r);
    expect(out.status).toBe(405);
    expect(out.headers.Allow).toBe('POST');
    expect(transport.callTransportBatch).not.toHaveBeenCalled();
  });

  it('refuses a signed-out request and relays nothing', async () => {
    identity.resolveSessionToken.mockReturnValue(null);
    const { r, out } = res();
    await handler(req(Buffer.from('x')), r);
    expect(out.status).toBe(401);
    expect(out.body).toEqual({ error: 'invalid_session' });
    expect(transport.callTransportBatch).not.toHaveBeenCalled();
  });

  it('refuses a NON-OWNER and relays nothing', async () => {
    // Defence in depth over the box's peer pin: the cookie is provenance, the
    // peer token is the authority. This gate stops a signed-in guest before a
    // single byte reaches the vault.
    identity.readDisplayIdentity.mockReturnValue({ name: 'Guest', role: 'member' });
    const { r, out } = res();
    await handler(req(Buffer.from('x')), r);
    expect(out.status).toBe(403);
    expect(out.body).toEqual({ error: 'forbidden' });
    expect(transport.callTransportBatch).not.toHaveBeenCalled();
  });

  it('refuses when no identity cookie can be read at all', async () => {
    identity.readDisplayIdentity.mockReturnValue(null);
    const { r, out } = res();
    await handler(req(Buffer.from('x')), r);
    expect(out.status).toBe(403);
    expect(transport.callTransportBatch).not.toHaveBeenCalled();
  });

  it('fail-closes when the deploy is not wired for batch', async () => {
    // "Not set up here" and "the instance refused you" are different facts and
    // only one of them is actionable.
    transport.listBatchTargets.mockReturnValue([]);
    const { r, out } = res();
    await handler(req(Buffer.from('x')), r);
    expect(out.status).toBe(503);
    expect(out.body).toEqual({ error: 'transport_misconfigured' });
    expect(transport.callTransportBatch).not.toHaveBeenCalled();
  });

  it('refuses a non-multipart body before spending a relay', async () => {
    const { r, out } = res();
    await handler(
      req(Buffer.from('{}'), { headers: { 'content-type': 'application/json' } }),
      r,
    );
    expect(out.status).toBe(415);
    expect(out.body).toEqual({ error: 'not_multipart' });
    expect(transport.callTransportBatch).not.toHaveBeenCalled();
  });

  it('has the body parser DISABLED', async () => {
    // The whole route depends on this: Next's default parser would both reject
    // a 128 MiB body and destroy the multipart boundary.
    const mod = await import('../pages/api/batch/submit');
    expect(mod.config.api.bodyParser).toBe(false);
  });
});


describe('POST /api/batch/submit — target routing (#90)', () => {
  it('defaults to the HOME instance when no target is given', async () => {
    const { r } = res();
    await handler(req(Buffer.from('x')), r);
    expect(transport.callTransportBatch.mock.calls[0][1].target).toBe('Salem');
  });

  it('routes to the SELECTED instance', async () => {
    // The whole point of #90: VERA's batch must reach VERA. Routing it home
    // would file another instance's scans into this one's vault.
    const { r } = res();
    await handler(req(Buffer.from('x'), { query: { target: 'VERA' } }), r);
    expect(transport.callTransportBatch.mock.calls[0][1].target).toBe('VERA');
  });

  it('matches a target case-insensitively and relays the CONFIGURED spelling', async () => {
    // The env key is derived from the name, so relaying the user's casing
    // would turn "vera" into a lookup for ALFRED_WEB_BATCH_VERA_* on some
    // inputs and not others.
    const { r } = res();
    await handler(req(Buffer.from('x'), { query: { target: 'vera' } }), r);
    expect(transport.callTransportBatch.mock.calls[0][1].target).toBe('VERA');
  });

  it('refuses an unconfigured target and NAMES it', async () => {
    // "Unknown target" alone leaves the operator guessing whether they mistyped
    // it or it was never configured — the fix is the same env pair either way,
    // so the message says which instance it could not find.
    const { r, out } = res();
    await handler(req(Buffer.from('x'), { query: { target: 'HYPATIA' } }), r);
    expect(out.status).toBe(400);
    expect(out.body).toEqual({ error: 'unknown_target', target: 'HYPATIA' });
    expect(transport.callTransportBatch).not.toHaveBeenCalled();
  });

  it('an unconfigured target is refused BEFORE the body is relayed', async () => {
    const { r } = res();
    await handler(req(Buffer.from('x'.repeat(1000)), { query: { target: 'NOPE' } }), r);
    expect(transport.callTransportBatch).not.toHaveBeenCalled();
  });

  it('takes the first value of a repeated target param', async () => {
    // ?target=VERA&target=EVIL arrives as an array; picking the array itself
    // would stringify into a name that matches nothing, turning a probe into a
    // confusing 400 instead of a clean route.
    const { r } = res();
    await handler(req(Buffer.from('x'), { query: { target: ['VERA', 'EVIL'] } }), r);
    expect(transport.callTransportBatch.mock.calls[0][1].target).toBe('VERA');
  });

  it('503s when NO instance is wired for batch', async () => {
    transport.listBatchTargets.mockReturnValue([]);
    const { r, out } = res();
    await handler(req(Buffer.from('x')), r);
    expect(out.status).toBe(503);
    expect(out.body).toEqual({ error: 'transport_misconfigured' });
    expect(transport.callTransportBatch).not.toHaveBeenCalled();
  });
});
