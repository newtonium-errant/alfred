import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { NextApiRequest, NextApiResponse } from 'next';

// Locks the BFF buffered-turn relay for image-carry (#29): a valid images array
// on the parsed body is forwarded VERBATIM to the transport (home path), and the
// 32 MiB bodyParser sizeLimit is declared (mirrors backend MAX_TURN_BODY_BYTES).

const { mockResolveSessionToken, mockCallTransport, mockCallChatTo, mockIsHome, mockGate } =
  vi.hoisted(() => ({
    mockResolveSessionToken: vi.fn(),
    mockCallTransport: vi.fn(),
    mockCallChatTo: vi.fn(),
    mockIsHome: vi.fn(),
    mockGate: vi.fn(),
  }));

vi.mock('../lib/algernon/identity', () => ({
  resolveSessionToken: mockResolveSessionToken,
}));

vi.mock('../lib/algernon/transport', () => ({
  callTransport: mockCallTransport,
  callChatTo: mockCallChatTo,
}));

vi.mock('../lib/algernon/chatRouting', () => ({
  isHomeInstance: mockIsHome,
  gateCrossInstance: mockGate,
}));

import handler, { config } from '../pages/api/chat/turn';

function turnReq(body: unknown): NextApiRequest {
  return { method: 'POST', headers: {}, body } as unknown as NextApiRequest;
}

function mockRes() {
  const json = vi.fn();
  const status = vi.fn(() => ({ json }));
  const setHeader = vi.fn();
  const res = { status, json, setHeader } as unknown as NextApiResponse;
  return { res, status, json, setHeader };
}

const SMALL_B64 = 'aGVsbG8='; // "hello"

beforeEach(() => {
  mockResolveSessionToken.mockReset();
  mockCallTransport.mockReset();
  mockCallChatTo.mockReset();
  mockIsHome.mockReset();
  mockGate.mockReset();
  mockIsHome.mockReturnValue(true);
});

afterEach(() => vi.restoreAllMocks());

describe('POST /api/chat/turn (image-carry #29)', () => {
  it('declares the 32 MiB bodyParser sizeLimit (LOCKSTEP cap)', () => {
    expect((config as { api: { bodyParser: { sizeLimit: string } } }).api.bodyParser.sizeLimit).toBe('32mb');
  });

  it('forwards a valid images array to the transport (home path)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: { reply: 'ok', session_key: 'k', ts: '', user_ts: '' } });
    const { res, status, json } = mockRes();
    await handler(
      turnReq({ session_key: 'k', message: 'what is broken?', images: [{ media_type: 'image/png', data: SMALL_B64 }] }),
      res,
    );
    expect(mockCallTransport).toHaveBeenCalledTimes(1);
    const [method, path, opts] = mockCallTransport.mock.calls[0];
    expect(method).toBe('POST');
    expect(path).toBe('/chat/turn');
    expect(opts.body.images).toHaveLength(1);
    expect(opts.body.images[0].media_type).toBe('image/png');
    expect(status).toHaveBeenCalledWith(200);
    expect(json).toHaveBeenCalled();
  });

  it('a text-only turn carries NO images field (byte-identical to pre-feature)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: {} });
    const { res } = mockRes();
    await handler(turnReq({ session_key: 'k', message: 'hi' }), res);
    const opts = mockCallTransport.mock.calls[0][2];
    expect('images' in opts.body).toBe(false);
  });

  it('400s an invalid image (bad media_type) before any relay', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status } = mockRes();
    await handler(
      turnReq({ session_key: 'k', message: 'hi', images: [{ media_type: 'image/tiff', data: SMALL_B64 }] }),
      res,
    );
    expect(status).toHaveBeenCalledWith(400);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });
});

describe('POST /api/chat/turn (player primer #C3c)', () => {
  it('forwards a valid primer to the transport VERBATIM (home path)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: { reply: 'ok', session_key: 'k', ts: '', user_ts: '' } });
    const { res, status } = mockRes();
    await handler(
      turnReq({ session_key: 'k', message: 'why is that yellow?', primer: { brief_date: '2026-08-01', section_id: 'health' } }),
      res,
    );
    expect(mockCallTransport).toHaveBeenCalledTimes(1);
    const opts = mockCallTransport.mock.calls[0][2];
    expect(opts.body.primer).toEqual({ brief_date: '2026-08-01', section_id: 'health' });
    expect(status).toHaveBeenCalledWith(200);
  });

  it('a turn with NO primer carries no primer field (byte-identical to a normal chat turn)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: {} });
    const { res } = mockRes();
    await handler(turnReq({ session_key: 'k', message: 'hi' }), res);
    expect('primer' in mockCallTransport.mock.calls[0][2].body).toBe(false);
  });

  it('an ISO-bad date / unknown section still RELAYS (fail-soft — backend gates, no 400)', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    mockCallTransport.mockResolvedValue({ status: 200, body: { reply: 'ungrounded', session_key: 'k', ts: '', user_ts: '' } });
    const { res, status } = mockRes();
    await handler(
      turnReq({ session_key: 'k', message: 'hmm', primer: { brief_date: 'not-a-date', section_id: 'made_up' } }),
      res,
    );
    expect(mockCallTransport).toHaveBeenCalledTimes(1);
    expect(mockCallTransport.mock.calls[0][2].body.primer).toEqual({ brief_date: 'not-a-date', section_id: 'made_up' });
    expect(status).not.toHaveBeenCalledWith(400);
  });

  it('400s an over-long section_id (DoS bound) before any relay', async () => {
    mockResolveSessionToken.mockReturnValue('tok');
    const { res, status } = mockRes();
    await handler(
      turnReq({ session_key: 'k', message: 'hi', primer: { brief_date: '2026-08-01', section_id: 'x'.repeat(65) } }),
      res,
    );
    expect(status).toHaveBeenCalledWith(400);
    expect(mockCallTransport).not.toHaveBeenCalled();
  });
});
