import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { sttClient } from '../lib/algernon/sttClient';
import { ApiError } from '../lib/algernon/http';

// The lost-message incident was a dropped/timed-out response on a LONG upload over
// flaky LTE. The STT upload must NOT give up before a legit long transcribe
// completes (far above the 70s default JSON budget) — but a genuinely dead
// connection must still surface (bounded) so the retry affordance appears.

describe('sttClient.transcribe timeout', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it('does NOT abort a still-in-flight transcribe at the 70s default budget (generous for long audio)', async () => {
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
    const p = sttClient.transcribe(blob);
    const settled = vi.fn();
    p.then(settled, settled);

    await vi.advanceTimersByTimeAsync(70000); // the default budget
    expect(settled).not.toHaveBeenCalled(); // still waiting — a long transcribe is allowed

    await vi.advanceTimersByTimeAsync(120000); // past the 180s STT ceiling
    await expect(p).rejects.toBeInstanceOf(ApiError); // eventually surfaces (bounded) → retry
  });
});
