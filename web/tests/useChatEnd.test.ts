import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

// Locks the resilience FE behaviours (CONTRACT S6/S7): a bare "/end" is
// intercepted (never sent to the model) and ends the session with a confirmation;
// mid-prose "/end" is NOT a command; retry() resends the SAME idempotency key.

const { mockOpen, mockHistory, mockStream, mockTurn, mockTargets } = vi.hoisted(() => ({
  mockOpen: vi.fn(),
  mockHistory: vi.fn(),
  mockStream: vi.fn(),
  mockTurn: vi.fn(),
  mockTargets: vi.fn(),
}));

vi.mock('../lib/algernon/client', () => ({
  chatApi: {
    open: mockOpen,
    history: mockHistory,
    stream: mockStream,
    turn: mockTurn,
    targets: mockTargets,
  },
}));

import { useChat } from '../lib/algernon/useChat';

function streamResponse(frames: string[]): Response {
  let i = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          read: async () => {
            if (i < frames.length) {
              const value = new TextEncoder().encode(frames[i]);
              i += 1;
              return { value, done: false };
            }
            return { value: undefined, done: true };
          },
        };
      },
    },
  } as unknown as Response;
}

const doneFrame = (reply: string) =>
  `event: done\ndata: ${JSON.stringify({ reply, session_key: 'sess-1', ts: 'T', user_ts: 'U' })}\n\n`;

beforeEach(() => {
  mockOpen.mockReset();
  mockHistory.mockReset();
  mockStream.mockReset();
  mockTurn.mockReset();
  mockTargets.mockReset();
  mockOpen.mockResolvedValue({ session_key: 'sess-1' });
  try {
    localStorage.clear();
  } catch {
    /* ignore */
  }
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('useChat /end interception', () => {
  it('intercepts a bare "/end": ends the session (open), sets a notice, never sends to the model', async () => {
    const { result } = renderHook(() => useChat({ enabled: true }));
    await waitFor(() => expect(result.current.status).toBe('ready'));
    const openCallsAfterBoot = mockOpen.mock.calls.length;

    await act(async () => {
      await result.current.send('/end');
    });

    expect(mockStream).not.toHaveBeenCalled();
    expect(mockTurn).not.toHaveBeenCalled();
    expect(mockOpen.mock.calls.length).toBe(openCallsAfterBoot + 1); // close-then-open
    expect(result.current.notice).toBe('Saved your conversation — started a new chat.');
    expect(result.current.status).toBe('ready');
    expect(result.current.messages).toEqual([]);
  });

  it('intercepts "/end now" (start-of-message)', async () => {
    const { result } = renderHook(() => useChat({ enabled: true }));
    await waitFor(() => expect(result.current.status).toBe('ready'));
    await act(async () => {
      await result.current.send('/end now please');
    });
    expect(mockStream).not.toHaveBeenCalled();
    expect(result.current.notice).toBeTruthy();
  });

  it('does NOT intercept mid-prose "/end" — it is a normal turn', async () => {
    mockStream.mockResolvedValue(streamResponse([doneFrame('ok')]));
    const { result } = renderHook(() => useChat({ enabled: true }));
    await waitFor(() => expect(result.current.status).toBe('ready'));
    await act(async () => {
      await result.current.send('tell me about /end');
    });
    expect(mockStream).toHaveBeenCalledTimes(1);
    expect(result.current.notice).toBeNull();
  });
});

describe('useChat retry()', () => {
  it('after a recoverable failure, retry() resends the SAME idempotency key', async () => {
    // First attempt: network failure reaching the BFF; history shows no reply.
    mockStream.mockRejectedValueOnce(new Error('network down'));
    mockHistory.mockResolvedValue({ turns: [] });
    const { result } = renderHook(() => useChat({ enabled: true }));
    await waitFor(() => expect(result.current.status).toBe('ready'));

    await act(async () => {
      await result.current.send('hello');
    });
    expect(result.current.status).toBe('error');
    expect(result.current.retryable).toBe(true);
    const firstIdk = mockStream.mock.calls[0][2].idempotencyKey;

    // Retry: the turn now succeeds via a streamed done frame.
    mockStream.mockResolvedValueOnce(streamResponse([doneFrame('recovered')]));
    await act(async () => {
      await result.current.retry();
    });

    expect(mockStream).toHaveBeenCalledTimes(2);
    const retryIdk = mockStream.mock.calls[1][2].idempotencyKey;
    expect(retryIdk).toBe(firstIdk); // SAME key → backend dedups if it already ran
    expect(result.current.status).toBe('ready');
    expect(result.current.retryable).toBe(false);
    expect(result.current.messages[result.current.messages.length - 1].text).toBe('recovered');
    // The user's single bubble was not duplicated by the retry.
    expect(result.current.messages.filter((m) => m.role === 'user' && m.text === 'hello')).toHaveLength(1);
  });
});
