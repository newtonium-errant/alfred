import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { HOME_INSTANCE_NAME } from '../lib/algernon/instance';

// V1 useChat extensions: the exposed sessionKey state + refreshFromHistory() —
// the reconcile wrapper a completed VOICE turn uses to land in the visible thread.

const { mockHistory, mockOpen, mockStream, mockTurn } = vi.hoisted(() => ({
  mockHistory: vi.fn(),
  mockOpen: vi.fn(),
  mockStream: vi.fn(),
  mockTurn: vi.fn(),
}));

vi.mock('../lib/algernon/client', () => ({
  chatApi: {
    history: mockHistory,
    open: mockOpen,
    stream: mockStream,
    turn: mockTurn,
    targets: vi.fn(),
  },
}));

import { useChat } from '../lib/algernon/useChat';

const STORAGE_KEY = `algernon:session_key:${HOME_INSTANCE_NAME}`;

function turns(pairs: Array<['user' | 'assistant', string]>) {
  return { turns: pairs.map(([role, text], i) => ({ role, text, ts: `t${i}` })) };
}

beforeEach(() => {
  mockHistory.mockReset();
  mockOpen.mockReset();
  mockStream.mockReset();
  mockTurn.mockReset();
  localStorage.clear();
  localStorage.setItem(STORAGE_KEY, 'sess-1');
  // Default bootstrap transcript: one prior exchange.
  mockHistory.mockResolvedValue(turns([['user', 'hello'], ['assistant', 'hi there']]));
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function bootReady() {
  const rendered = renderHook(() => useChat({ enabled: true }));
  await waitFor(() => expect(rendered.result.current.status).toBe('ready'));
  return rendered;
}

describe('useChat V1 extensions', () => {
  it('exposes the resumed sessionKey', async () => {
    const { result } = await bootReady();
    expect(result.current.sessionKey).toBe('sess-1');
    expect(result.current.messages).toHaveLength(2);
  });

  it('refreshFromHistory adopts a grown transcript with a trailing assistant turn', async () => {
    const { result } = await bootReady();
    mockHistory.mockResolvedValue(
      turns([
        ['user', 'hello'],
        ['assistant', 'hi there'],
        ['user', 'what is on my calendar'],
        ['assistant', 'Two meetings.'],
      ]),
    );
    let adopted: boolean | undefined;
    await act(async () => {
      adopted = await result.current.refreshFromHistory();
    });
    expect(adopted).toBe(true);
    expect(result.current.messages).toHaveLength(4);
    expect(result.current.messages[3].text).toBe('Two meetings.');
  });

  it('refreshFromHistory returns false (no adoption) when the transcript did not grow', async () => {
    const { result } = await bootReady();
    const before = result.current.messages.length;
    let adopted: boolean | undefined;
    await act(async () => {
      adopted = await result.current.refreshFromHistory();
    });
    expect(adopted).toBe(false);
    expect(result.current.messages).toHaveLength(before);
  });

  it('refreshFromHistory yields (false) while a typed turn is pending', async () => {
    const { result } = await bootReady();
    // A typed send whose stream never resolves → pendingRef stays set.
    mockStream.mockReturnValue(new Promise<Response>(() => {}));
    act(() => {
      void result.current.send('a typed question');
    });
    // The grown transcript is available, but the pending typed turn must block
    // adoption (it would drop the optimistic user bubble).
    mockHistory.mockResolvedValue(
      turns([['user', 'hello'], ['assistant', 'hi there'], ['user', 'x'], ['assistant', 'y']]),
    );
    let adopted: boolean | undefined;
    await act(async () => {
      adopted = await result.current.refreshFromHistory();
    });
    expect(adopted).toBe(false);
  });
});
