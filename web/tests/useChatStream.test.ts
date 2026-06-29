import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

// Locks the streaming consumer in useChat: a terminal `done` frame finalises the
// reply; a stream that closes WITHOUT a terminal frame reconciles via /chat/history
// (never a false "can't reach" when the turn actually completed — CONTRACT S5);
// each turn mints an idempotency key; the non-stream fallback reuses that key.

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

// A fake streaming Response whose body.getReader() yields the given SSE frames.
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

describe('useChat streaming', () => {
  it('finalises the assistant reply from a terminal done frame + mints an idempotency key', async () => {
    mockStream.mockResolvedValue(
      streamResponse([
        'event: status\ndata: {"phase":"tool","tool":"vault_search"}\n\n',
        doneFrame('Hi there'),
      ]),
    );
    const { result } = renderHook(() => useChat({ enabled: true }));
    await waitFor(() => expect(result.current.status).toBe('ready'));

    await act(async () => {
      await result.current.send('hello');
    });

    expect(result.current.messages.map((m) => [m.role, m.text])).toEqual([
      ['user', 'hello'],
      ['assistant', 'Hi there'],
    ]);
    expect(result.current.status).toBe('ready');
    expect(result.current.working).toBeNull();
    const idk = mockStream.mock.calls[0][2].idempotencyKey;
    expect(typeof idk).toBe('string');
    expect(idk.length).toBeGreaterThan(0);
  });

  it('reconciles via history when the stream closes WITHOUT a terminal frame', async () => {
    // Status frame then the stream just ends — no done/error.
    mockStream.mockResolvedValue(streamResponse(['event: status\ndata: {"phase":"tool"}\n\n']));
    mockHistory.mockResolvedValue({
      turns: [
        { role: 'user', text: 'hello', ts: 'U' },
        { role: 'assistant', text: 'Recovered reply', ts: 'T' },
      ],
    });
    const { result } = renderHook(() => useChat({ enabled: true }));
    await waitFor(() => expect(result.current.status).toBe('ready'));

    await act(async () => {
      await result.current.send('hello');
    });

    expect(mockHistory).toHaveBeenCalled();
    expect(result.current.messages[result.current.messages.length - 1].text).toBe('Recovered reply');
    expect(result.current.status).toBe('ready');
    expect(result.current.error).toBeNull();
  });

  it('soft-errors (not unauth) when an incomplete stream reconciles to no new reply', async () => {
    mockStream.mockResolvedValue(streamResponse(['event: status\ndata: {"phase":"tool"}\n\n']));
    mockHistory.mockResolvedValue({ turns: [] }); // no growth
    const { result } = renderHook(() => useChat({ enabled: true }));
    await waitFor(() => expect(result.current.status).toBe('ready'));

    await act(async () => {
      await result.current.send('hello');
    });

    expect(result.current.status).toBe('error');
    expect(result.current.unauthenticated).toBe(false);
    // The user's message stays in the thread for a retry.
    expect(result.current.messages.some((m) => m.role === 'user' && m.text === 'hello')).toBe(true);
  });

  it('falls back to the buffered turn (reusing the idempotency key) when there is no readable body', async () => {
    mockStream.mockResolvedValue({ ok: true, status: 200, body: null } as unknown as Response);
    mockTurn.mockResolvedValue({ reply: 'buffered', session_key: 'sess-1', ts: 'T', user_ts: 'U' });
    const { result } = renderHook(() => useChat({ enabled: true }));
    await waitFor(() => expect(result.current.status).toBe('ready'));

    await act(async () => {
      await result.current.send('hello');
    });

    expect(mockTurn).toHaveBeenCalledTimes(1);
    const streamIdk = mockStream.mock.calls[0][2].idempotencyKey;
    const turnIdk = mockTurn.mock.calls[0][2].idempotencyKey;
    expect(turnIdk).toBe(streamIdk); // same logical turn → same key
    expect(result.current.messages[result.current.messages.length - 1].text).toBe('buffered');
  });
});
