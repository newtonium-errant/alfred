import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError, postBlob } from '../lib/algernon/http';

// The lost-message incident was a dropped/timed-out response on a LONG upload over
// flaky LTE. The STT upload must NOT give up before a legit long transcribe
// completes (far above the 70s default JSON budget) — but a genuinely dead
// connection must still surface (bounded) so the retry affordance appears. The
// timeout MECHANISM lives in postBlob; sttClient just passes the generous ceiling.
// (Tested at the postBlob layer so fake timers don't have to race crypto.subtle,
// which resolves off the libuv threadpool and isn't advanced by fake timers.)

describe('postBlob timeout (the STT upload bound)', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('does NOT abort a still-in-flight upload at the 70s default budget, but DOES past the generous bound', async () => {
    // A fetch that respects the abort signal but otherwise never resolves.
    vi.stubGlobal(
      'fetch',
      vi.fn(
        (_url: string, init: RequestInit) =>
          new Promise((_resolve, reject) => {
            init.signal?.addEventListener('abort', () =>
              reject(Object.assign(new Error('aborted'), { name: 'AbortError' })),
            );
          }),
      ),
    );
    const blob = new Blob(['audio'], { type: 'audio/webm' });
    const p = postBlob('/api/stt/transcribe', blob, 'audio/webm', { timeoutMs: 180000 });
    const settled = vi.fn();
    p.then(settled, settled);

    await vi.advanceTimersByTimeAsync(70000); // the default JSON budget
    expect(settled).not.toHaveBeenCalled(); // still waiting — a long transcribe is allowed

    await vi.advanceTimersByTimeAsync(120000); // total 190s > the 180s ceiling
    await expect(p).rejects.toBeInstanceOf(ApiError); // eventually surfaces (bounded) → retry
  });
});
