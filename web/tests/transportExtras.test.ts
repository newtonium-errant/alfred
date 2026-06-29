import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  TransportConfigError,
  callTransportBinary,
  callTransportTo,
  listIngestTargets,
  resolveIngestTarget,
} from '../lib/algernon/transport';

// Locks the cross-instance ingest target resolution (per-target env, fail-closed)
// + the binary STT relay (peer token injected server-side, raw audio body).

const ORIG_ENV = { ...process.env };

function clearIngestEnv() {
  for (const key of Object.keys(process.env)) {
    if (key.startsWith('ALFRED_WEB_INGEST_')) delete process.env[key];
  }
}

beforeEach(() => {
  clearIngestEnv();
  process.env.ALFRED_WEB_TRANSPORT_URL = 'http://transport.test:8891/';
  process.env.ALFRED_WEB_PEER_TOKEN = 'chat-peer-token';
});

afterEach(() => {
  process.env = { ...ORIG_ENV };
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('listIngestTargets', () => {
  it('returns a target only when BOTH url and token are set (fail-closed)', () => {
    process.env.ALFRED_WEB_INGEST_SALEM_URL = 'http://127.0.0.1:8891';
    process.env.ALFRED_WEB_INGEST_SALEM_TOKEN = 'salem-ingest';
    // KALLE has a URL but no token → must be skipped.
    process.env.ALFRED_WEB_INGEST_KALLE_URL = 'http://127.0.0.1:8892';

    const targets = listIngestTargets();
    expect(targets.map((t) => t.name)).toEqual(['SALEM']);
    expect(targets[0].label).toBe('SALEM'); // defaults to the name
    expect(targets[0].recordTypes).toEqual(['document', 'note', 'source']);
  });

  it('uses the optional label and sorts by label; never leaks url/token', () => {
    process.env.ALFRED_WEB_INGEST_SALEM_URL = 'http://127.0.0.1:8891';
    process.env.ALFRED_WEB_INGEST_SALEM_TOKEN = 's';
    process.env.ALFRED_WEB_INGEST_SALEM_LABEL = 'Salem';
    process.env.ALFRED_WEB_INGEST_KALLE_URL = 'http://127.0.0.1:8892';
    process.env.ALFRED_WEB_INGEST_KALLE_TOKEN = 'k';
    process.env.ALFRED_WEB_INGEST_KALLE_LABEL = 'KAL-LE';

    const targets = listIngestTargets();
    expect(targets.map((t) => t.label)).toEqual(['KAL-LE', 'Salem']); // sorted
    const json = JSON.stringify(targets);
    expect(json).not.toContain('127.0.0.1');
    expect(json).not.toContain('http');
  });

  it('returns an empty array when nothing is configured', () => {
    expect(listIngestTargets()).toEqual([]);
  });
});

describe('resolveIngestTarget', () => {
  it('resolves a configured target (trims trailing slash, carries the client)', () => {
    process.env.ALFRED_WEB_INGEST_SALEM_URL = 'http://127.0.0.1:8891/';
    process.env.ALFRED_WEB_INGEST_SALEM_TOKEN = 'salem-ingest';
    const t = resolveIngestTarget('SALEM');
    expect(t.baseUrl).toBe('http://127.0.0.1:8891');
    expect(t.token).toBe('salem-ingest');
    expect(t.client).toBe('web');
  });

  it('throws when the env pair is missing', () => {
    expect(() => resolveIngestTarget('SALEM')).toThrow(TransportConfigError);
  });

  it('throws on a malformed target name (no arbitrary env probing)', () => {
    expect(() => resolveIngestTarget('a b')).toThrow(TransportConfigError);
    expect(() => resolveIngestTarget('../secret')).toThrow(TransportConfigError);
  });
});

describe('callTransportTo', () => {
  beforeEach(() => {
    process.env.ALFRED_WEB_INGEST_SALEM_URL = 'http://127.0.0.1:8891';
    process.env.ALFRED_WEB_INGEST_SALEM_TOKEN = 'salem-ingest';
  });

  it('uses the TARGET token + client + extra headers + JSON body', async () => {
    const fetchMock = vi.fn(async () => ({ status: 201, json: async () => ({ status: 'created' }) }));
    vi.stubGlobal('fetch', fetchMock);

    const res = await callTransportTo('SALEM', 'POST', '/vault/ingest', {
      body: { title: 'x' },
      headers: { 'X-Alfred-Ingest-User': 'andrew' },
    });

    expect(res.status).toBe(201);
    const [url, init] = fetchMock.mock.calls[0] as any[];
    expect(url).toBe('http://127.0.0.1:8891/vault/ingest');
    expect(init.headers.Authorization).toBe('Bearer salem-ingest'); // target token, not chat token
    expect(init.headers['X-Alfred-Client']).toBe('web');
    expect(init.headers['X-Alfred-Ingest-User']).toBe('andrew');
    expect(init.headers['Content-Type']).toBe('application/json');
    expect(init.body).toBe(JSON.stringify({ title: 'x' }));
  });
});

describe('callTransportBinary', () => {
  it('injects the chat peer token, sends the audio mime + raw body + session', async () => {
    const fetchMock = vi.fn(async () => ({ status: 200, json: async () => ({ transcript: 'hi' }) }));
    vi.stubGlobal('fetch', fetchMock);

    const audio = Buffer.from('audio-bytes');
    const res = await callTransportBinary('POST', '/stt/transcribe', {
      body: audio,
      contentType: 'audio/webm',
      sessionToken: 'sess',
    });

    expect(res.status).toBe(200);
    const [url, init] = fetchMock.mock.calls[0] as any[];
    expect(url).toBe('http://transport.test:8891/stt/transcribe');
    expect(init.headers.Authorization).toBe('Bearer chat-peer-token');
    expect(init.headers['X-Alfred-Client']).toBe('web');
    expect(init.headers['X-Alfred-Session']).toBe('sess');
    expect(init.headers['Content-Type']).toBe('audio/webm');
    expect(init.body).toBeInstanceOf(Uint8Array);
    expect(Buffer.from(init.body).toString()).toBe('audio-bytes');
  });

  it('throws TransportConfigError when the peer token is missing', async () => {
    delete process.env.ALFRED_WEB_PEER_TOKEN;
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    await expect(
      callTransportBinary('POST', '/stt/transcribe', {
        body: Buffer.from('x'),
        contentType: 'audio/webm',
        sessionToken: 'sess',
      }),
    ).rejects.toBeInstanceOf(TransportConfigError);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
