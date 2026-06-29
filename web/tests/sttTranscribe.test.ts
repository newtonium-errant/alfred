import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the binary STT BFF edge guards: method (405) → session (401) → mime
// allowlist (415) → empty (400) → relay. The 25 MiB cap is exercised by the
// streaming logic (not size-tested here — too large for a unit fixture).

const { mockResolveSessionToken, mockCallTransportBinary } = vi.hoisted(() => ({
  mockResolveSessionToken: vi.fn(),
  mockCallTransportBinary: vi.fn(),
}));

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransportBinary: mockCallTransportBinary,
  TransportConfigError: class TransportConfigError extends Error {},
}));

import handler from '../pages/api/stt/transcribe';

function mockRes() {
  const json = vi.fn();
  const setHeader = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status, setHeader, json } as unknown as NextApiResponse, status, json, setHeader };
}

// A POST request that is async-iterable (mimics the raw Node request stream the
// route consumes with `for await (const chunk of req)`).
function audioReq(contentType: string | undefined, chunks: Buffer[]): NextApiRequest {
  return {
    method: 'POST',
    headers: { 'content-type': contentType },
    async *[Symbol.asyncIterator]() {
      for (const c of chunks) yield c;
    },
  } as unknown as NextApiRequest;
}

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockCallTransportBinary.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POST /api/stt/transcribe', () => {
  it('405 on a non-POST method', async () => {
    const { res, status, json } = mockRes();
    await handler({ method: 'GET' } as unknown as NextApiRequest, res);
    expect(status).toHaveBeenCalledWith(405);
    expect(json).toHaveBeenCalledWith({ error: 'method_not_allowed' });
  });

  it('401 when there is no session (before reading the body)', async () => {
    mockResolveSessionToken.mockReturnValue(null);
    const { res, status, json } = mockRes();
    await handler(audioReq('audio/webm', [Buffer.from('x')]), res);
    expect(status).toHaveBeenCalledWith(401);
    expect(json).toHaveBeenCalledWith({ error: 'invalid_session' });
    expect(mockCallTransportBinary).not.toHaveBeenCalled();
  });

  it('415 for a non-allowlisted mime', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await handler(audioReq('application/json', [Buffer.from('x')]), res);
    expect(status).toHaveBeenCalledWith(415);
    expect(json).toHaveBeenCalledWith({ error: 'unsupported_media_type' });
    expect(mockCallTransportBinary).not.toHaveBeenCalled();
  });

  it('415 when the Content-Type header is missing', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status } = mockRes();
    await handler(audioReq(undefined, [Buffer.from('x')]), res);
    expect(status).toHaveBeenCalledWith(415);
  });

  it('400 no_audio when the body is empty', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status, json } = mockRes();
    await handler(audioReq('audio/webm', []), res);
    expect(status).toHaveBeenCalledWith(400);
    expect(json).toHaveBeenCalledWith({ error: 'no_audio' });
    expect(mockCallTransportBinary).not.toHaveBeenCalled();
  });

  it('relays the audio buffer + normalised mime + session, returns the transcript', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransportBinary.mockResolvedValue({
      status: 200,
      body: { transcript: 'hello there', low_confidence: false },
    });
    const { res, status, json } = mockRes();
    await handler(audioReq('audio/webm;codecs=opus', [Buffer.from('aud'), Buffer.from('io')]), res);

    expect(mockCallTransportBinary).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransportBinary.mock.calls[0];
    expect(method).toBe('POST');
    expect(path).toBe('/stt/transcribe');
    expect(Buffer.isBuffer(opts.body)).toBe(true);
    expect(opts.body.toString()).toBe('audio'); // 'aud' + 'io' concatenated
    expect(opts.contentType).toBe('audio/webm'); // params stripped
    expect(opts.sessionToken).toBe('tok');

    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalledWith({ transcript: 'hello there', low_confidence: false });
  });
});
