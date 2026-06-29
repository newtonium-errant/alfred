import { afterEach, describe, expect, it, vi } from 'vitest';
import type { NextApiResponse } from 'next';
import { sendTransportError } from '../lib/algernon/bffError';
import { TransportConfigError, TransportTimeoutError } from '../lib/algernon/transport';

// Regression-pin the FE-2 reviewer security note: the BFF must NEVER return the
// internal cause (env-var names, raw fetch errors) to the client — only the
// generic code. The cause is logged server-side.

function mockRes() {
  const json = vi.fn();
  const status = vi.fn(() => ({ json }));
  return { res: { status } as unknown as NextApiResponse, status, json };
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('sendTransportError', () => {
  it('500 transport_misconfigured — generic code, NO detail, cause logged', () => {
    const { res, status, json } = mockRes();
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    sendTransportError(
      res,
      'chat/open',
      new TransportConfigError('ALFRED_WEB_PEER_TOKEN is not set'),
    );

    expect(status).toHaveBeenCalledWith(500);
    expect(json).toHaveBeenCalledWith({ error: 'transport_misconfigured' });
    expect(json.mock.calls[0][0]).not.toHaveProperty('detail');
    // the env-var name went to the server log, never the client body
    expect(errSpy).toHaveBeenCalled();
    expect(String(errSpy.mock.calls[0][0])).toContain('ALFRED_WEB_PEER_TOKEN');
  });

  it('502 transport_unreachable — generic code, NO detail', () => {
    const { res, status, json } = mockRes();
    vi.spyOn(console, 'error').mockImplementation(() => {});

    sendTransportError(res, 'chat/turn', new Error('ECONNREFUSED 127.0.0.1:8891'));

    expect(status).toHaveBeenCalledWith(502);
    expect(json).toHaveBeenCalledWith({ error: 'transport_unreachable' });
    expect(json.mock.calls[0][0]).not.toHaveProperty('detail');
  });

  it('504 gateway_timeout — distinct from 502, generic code, NO detail (S8)', () => {
    const { res, status, json } = mockRes();
    vi.spyOn(console, 'error').mockImplementation(() => {});

    sendTransportError(res, 'chat/turn', new TransportTimeoutError('timed out after 60000ms'));

    expect(status).toHaveBeenCalledWith(504);
    expect(json).toHaveBeenCalledWith({ error: 'gateway_timeout' });
    expect(json.mock.calls[0][0]).not.toHaveProperty('detail');
  });
});
