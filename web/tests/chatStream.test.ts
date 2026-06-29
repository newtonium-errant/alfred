import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the BFF SSE relay: ALL validation (method/session/body/target) returns
// JSON BEFORE any stream byte; a 200 event-stream is passed through with no
// res.json(); an upstream JSON error relays as a clean status; cross-instance
// gates (owner/unknown-target) run before the relay.

const {
  mockResolveSessionToken,
  mockReadDisplayIdentity,
  mockCallTransportStream,
  mockCallChatStream,
  mockListCrossInstanceChatTargets,
} = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockReadDisplayIdentity: vi.fn(),
  mockCallTransportStream: vi.fn(),
  mockCallChatStream: vi.fn(),
  mockListCrossInstanceChatTargets: vi.fn(),
}));

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
  readDisplayIdentity: mockReadDisplayIdentity,
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransportStream: mockCallTransportStream,
  callChatStream: mockCallChatStream,
  listCrossInstanceChatTargets: mockListCrossInstanceChatTargets,
  TransportConfigError: class TransportConfigError extends Error {},
  TransportTimeoutError: class TransportTimeoutError extends Error {},
}));

import handler from '../pages/api/chat/stream';

function streamReq(body: unknown): NextApiRequest {
  const json = typeof body === 'string' ? body : JSON.stringify(body);
  return {
    method: 'POST',
    headers: {},
    async *[Symbol.asyncIterator]() {
      yield Buffer.from(json);
    },
    on: () => {},
  } as unknown as NextApiRequest;
}

function mockRes() {
  const writes: Buffer[] = [];
  let ended = false;
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  const res = {
    status,
    setHeader,
    json,
    write: vi.fn((c: Buffer) => {
      writes.push(c);
      return true;
    }),
    flushHeaders: vi.fn(),
    end: vi.fn(() => {
      ended = true;
    }),
    socket: { setTimeout: vi.fn() },
    get writableEnded() {
      return ended;
    },
  };
  return { res: res as unknown as NextApiResponse, status, json, setHeader, writes };
}

function sseUpstream(frames: string[]): Response {
  return {
    ok: true,
    status: 200,
    headers: { get: (k: string) => (k.toLowerCase() === 'content-type' ? 'text/event-stream' : null) },
    body: {
      async *[Symbol.asyncIterator]() {
        for (const f of frames) yield new TextEncoder().encode(f);
      },
    },
  } as unknown as Response;
}

function jsonUpstream(statusCode: number, body: unknown): Response {
  return {
    ok: statusCode >= 200 && statusCode < 300,
    status: statusCode,
    headers: { get: () => 'application/json' },
    json: async () => body,
  } as unknown as Response;
}

const DONE_FRAME =
  'event: done\ndata: {"reply":"hi","session_key":"k","ts":"t","user_ts":"u"}\n\n';

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockReadDisplayIdentity.mockReset();
  mockCallTransportStream.mockReset();
  mockCallChatStream.mockReset();
  mockListCrossInstanceChatTargets.mockReset();
  mockListCrossInstanceChatTargets.mockReturnValue([{ name: 'KALLE', label: 'KAL-LE' }]);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POST /api/chat/stream', () => {
  it('405 on a non-POST method', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
    expect(mockCallTransportStream).not.toHaveBeenCalled();
  });

  it('401 when there is no session (before reading the body)', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(streamReq({ session_key: 'k', message: 'hi' }), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransportStream).not.toHaveBeenCalled();
  });

  it('400 invalid_request on a malformed body', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await handler(streamReq({ session_key: 'k' }), res); // no message
    expect(status).toHaveBeenCalledWith(400);
    expect(json.mock.calls[0][0].error).toBe('invalid_request');
    expect(mockCallTransportStream).not.toHaveBeenCalled();
  });

  it('400 invalid_request on unparseable JSON', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await handler(streamReq('{not json'), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_request' });
  });

  it('HOME: passes the SSE stream through (no res.json) with SSE headers', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransportStream.mockResolvedValue(sseUpstream(['event: status\ndata: {"phase":"tool"}\n\n', DONE_FRAME]));
    const { res, json, setHeader, writes } = mockRes();
    await handler(streamReq({ session_key: 'k', message: 'hi' }), res);

    expect(mockCallTransportStream).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransportStream.mock.calls[0];
    expect(method).toBe('POST');
    expect(path).toBe('/chat/stream');
    expect(opts.sessionToken).toBe('tok');
    expect(opts.body.message).toBe('hi');
    // SSE passthrough — never res.json once streaming.
    expect(json).not.toHaveBeenCalled();
    expect(setHeader).toHaveBeenCalledWith('Content-Type', 'text/event-stream');
    expect(setHeader).toHaveBeenCalledWith('X-Accel-Buffering', 'no');
    const out = Buffer.concat(writes).toString();
    expect(out).toContain('event: done');
    expect(res.end).toHaveBeenCalled();
  });

  it('HOME: an upstream JSON error relays as a clean JSON status (no SSE)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransportStream.mockResolvedValue(jsonUpstream(404, { error: 'no_such_session' }));
    const { res, status, json, setHeader } = mockRes();
    await handler(streamReq({ session_key: 'k', message: 'hi' }), res);
    expect(status).toHaveBeenCalledWith(404);
    expect(json).toHaveBeenCalledWith({ error: 'no_such_session' });
    expect(setHeader).not.toHaveBeenCalledWith('Content-Type', 'text/event-stream');
  });

  it('HOME: a transport connect failure maps to a JSON 502 before any stream', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransportStream.mockRejectedValue(new Error('ECONNREFUSED'));
    const { res, status, json } = mockRes();
    await handler(streamReq({ session_key: 'k', message: 'hi' }), res);
    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'transport_unreachable' });
  });

  it('CROSS non-owner → 403, no relay', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'ben', role: 'ops' });
    const { res, status, json } = mockRes();
    await handler(streamReq({ session_key: 'k', message: 'hi', instance: 'KALLE' }), res);
    expect(status).toHaveBeenCalledWith(403);
    expect(json).toHaveBeenCalledWith({ error: 'forbidden' });
    expect(mockCallChatStream).not.toHaveBeenCalled();
  });

  it('CROSS owner + unknown target → 400 unknown_target', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
    mockListCrossInstanceChatTargets.mockReturnValue([]);
    const { res, status, json } = mockRes();
    await handler(streamReq({ session_key: 'k', message: 'hi', instance: 'KALLE' }), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'unknown_target' });
    expect(mockCallChatStream).not.toHaveBeenCalled();
  });

  it('CROSS owner + known target → relay stream with asserted user', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockReadDisplayIdentity.mockReturnValue({ name: 'andrew', role: 'owner' });
    mockCallChatStream.mockResolvedValue(sseUpstream([DONE_FRAME]));
    const { res, writes } = mockRes();
    await handler(streamReq({ session_key: 'k', message: 'hi', instance: 'KALLE' }), res);

    expect(mockCallTransportStream).not.toHaveBeenCalled();
    expect(mockCallChatStream).toHaveBeenCalledTimes(1);
    const [targetName, method, path, opts] = mockCallChatStream.mock.calls[0];
    expect(targetName).toBe('KALLE');
    expect(method).toBe('POST');
    expect(path).toBe('/chat/stream');
    expect(opts.userName).toBe('andrew');
    expect(opts.body.instance).toBeUndefined(); // BFF-only
    expect(Buffer.concat(writes).toString()).toContain('event: done');
  });
});
