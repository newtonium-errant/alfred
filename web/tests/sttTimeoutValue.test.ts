import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// VALUE guard for the generous STT upload ceiling. The mechanism is tested in
// sttTimeout.test.ts (at the postBlob layer, since crypto.subtle doesn't advance
// under fake timers) — but that test hardcodes its own timeoutMs, so it would NOT
// catch a revert of STT_TIMEOUT_MS to the 70s default. This pins that sttClient
// actually PASSES 180000 to postBlob: mock postBlob, assert the arg. A revert of the
// constant fails this test.

const { mockPostBlob } = vi.hoisted(() => ({ mockPostBlob: vi.fn() }));

vi.mock('../lib/algernon/http', () => ({
  postBlob: mockPostBlob,
  ApiError: class ApiError extends Error {},
}));

import { sttClient } from '../lib/algernon/sttClient';

// jsdom's Blob lacks arrayBuffer(); sttClient content-hashes the audio (real timers
// here, so crypto.subtle resolves normally).
function audioBlob(text = 'audio'): Blob {
  const bytes = new TextEncoder().encode(text);
  return { type: 'audio/webm', arrayBuffer: async () => bytes.buffer } as unknown as Blob;
}

beforeEach(() => {
  mockPostBlob.mockReset().mockResolvedValue({ transcript: 'ok' });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('sttClient STT upload timeout value', () => {
  it('passes the generous STT ceiling (180000ms) to postBlob — a revert to the 70s default fails this', async () => {
    await sttClient.transcribe(audioBlob());
    const opts = mockPostBlob.mock.calls[0][3];
    expect(opts.timeoutMs).toBe(180000);
  });
});
