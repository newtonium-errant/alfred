import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sttClient } from '../lib/algernon/sttClient';
import { STT_IDEMPOTENCY_HEADER } from '../lib/algernon/schemas';

// STT idempotency (lost-message #2): sttClient sends a CONTENT-ADDRESSED key — the
// SHA-256 hex of the audio bytes — so a retry of the SAME blob (VoiceCapture retains
// it across a dropped response) hashes to the SAME key ⇒ the backend returns the
// cached transcript (no re-transcribe / no double-charge). No client state minted.

// jsdom's Blob lacks arrayBuffer(); the real browser Blob has it. crypto.subtle is
// present in the vitest env (verified).
function audioBlob(text: string): Blob {
  const bytes = new TextEncoder().encode(text);
  return {
    type: 'audio/webm',
    size: bytes.byteLength,
    arrayBuffer: async () => bytes.buffer,
  } as unknown as Blob;
}

async function sha256hex(text: string): Promise<string> {
  const d = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(d))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => ({ transcript: 'ok' }) });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.restoreAllMocks();
});

function sentHeaders(call = 0): Record<string, string> {
  return fetchMock.mock.calls[call][1].headers as Record<string, string>;
}

describe('sttClient content-hash idempotency header', () => {
  it('sends X-Alfred-Stt-Idempotency-Key = SHA-256 hex of the audio bytes (and keeps Content-Type)', async () => {
    await sttClient.transcribe(audioBlob('hello'));
    const h = sentHeaders();
    expect(h[STT_IDEMPOTENCY_HEADER]).toBe(await sha256hex('hello'));
    expect(h['Content-Type']).toBe('audio/webm'); // header merge didn't drop the mime
  });

  it('is STABLE across a retry of the SAME blob (backend can dedup) and DIFFERS for different audio', async () => {
    const same = audioBlob('the-nearly-lost-note');
    await sttClient.transcribe(same);
    await sttClient.transcribe(same); // the retry resends the SAME blob → SAME key
    expect(sentHeaders(1)[STT_IDEMPOTENCY_HEADER]).toBe(sentHeaders(0)[STT_IDEMPOTENCY_HEADER]);

    await sttClient.transcribe(audioBlob('a-different-recording'));
    expect(sentHeaders(2)[STT_IDEMPOTENCY_HEADER]).not.toBe(sentHeaders(0)[STT_IDEMPOTENCY_HEADER]);
  });

  it('the key is a well-formed 64-char lowercase hex digest', async () => {
    await sttClient.transcribe(audioBlob('x'));
    expect(sentHeaders()[STT_IDEMPOTENCY_HEADER]).toMatch(/^[a-f0-9]{64}$/);
  });
});
