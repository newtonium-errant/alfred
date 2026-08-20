import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';
import { HOME_INSTANCE_NAME } from '../lib/algernon/instance';

// useChat capture pins (R1): server-truth adoption on bootstrap, the toggle
// round-trip rendering what the SERVER said, the captured receipt appending
// NO assistant bubble, and the extraction offer lifecycle.

const { mockHistory, mockOpen, mockStream, mockTurn, mockTargets, mockCapture, mockCaptureExtract } =
  vi.hoisted(() => ({
    mockHistory: vi.fn(),
    mockOpen: vi.fn(),
    mockStream: vi.fn(),
    mockTurn: vi.fn(),
    mockTargets: vi.fn(),
    mockCapture: vi.fn(),
    mockCaptureExtract: vi.fn(),
  }));

vi.mock('../lib/algernon/client', () => ({
  chatApi: {
    history: mockHistory,
    open: mockOpen,
    stream: mockStream,
    turn: mockTurn,
    targets: mockTargets,
    capture: mockCapture,
    captureExtract: mockCaptureExtract,
  },
}));

import { ApiError } from '../lib/algernon/http';
import { useChat } from '../lib/algernon/useChat';

const STORAGE_KEY = `algernon:session_key:${HOME_INSTANCE_NAME}`;

function history(
  pairs: Array<['user' | 'assistant', string]>,
  capture: { active?: boolean; spans?: unknown[] } = {},
) {
  return {
    turns: pairs.map(([role, text], i) => ({ role, text, ts: `t${i}` })),
    capture_active: capture.active ?? false,
    capture_spans: capture.spans ?? [],
  };
}

beforeEach(() => {
  for (const m of [mockHistory, mockOpen, mockStream, mockTurn, mockTargets, mockCapture, mockCaptureExtract]) {
    m.mockReset();
  }
  localStorage.clear();
  localStorage.setItem(STORAGE_KEY, 'sess-1');
  mockHistory.mockResolvedValue(history([['user', 'hi'], ['assistant', 'hello']]));
});

afterEach(() => {
  vi.restoreAllMocks();
});

async function bootReady() {
  const rendered = renderHook(() => useChat({ enabled: true }));
  await waitFor(() => expect(rendered.result.current.status).toBe('ready'));
  return rendered;
}

describe('useChat capture (R1)', () => {
  it('bootstrap adopts capture-ON from history — the refresh-resume pin', async () => {
    mockHistory.mockResolvedValue(
      history([['user', 'dictated']], {
        active: true,
        spans: [{ index: 0, start: 0, end: null, turns: 1, extracted: false }],
      }),
    );
    const { result } = await bootReady();
    expect(result.current.captureActive).toBe(true);
    // An OPEN span is not an offer.
    expect(result.current.extractionOffer).toBeNull();
  });

  it('bootstrap re-surfaces the offer for a closed UNEXTRACTED span — and not for an extracted one', async () => {
    mockHistory.mockResolvedValue(
      history([['user', 'a'], ['user', 'b'], ['user', 'c']], {
        active: false,
        spans: [
          { index: 0, start: 0, end: 1, turns: 1, extracted: true },
          { index: 1, start: 1, end: 3, turns: 2, extracted: false },
        ],
      }),
    );
    const { result } = await bootReady();
    expect(result.current.captureActive).toBe(false);
    expect(result.current.extractionOffer).toEqual({ spanIndex: 1, turns: 2 });

    // POSITIVE-NEGATIVE pair: every span extracted → no offer.
    mockHistory.mockResolvedValue(
      history([['user', 'a']], {
        active: false,
        spans: [{ index: 0, start: 0, end: 1, turns: 1, extracted: true }],
      }),
    );
    localStorage.setItem(STORAGE_KEY, 'sess-1');
    const second = await bootReady();
    expect(second.result.current.extractionOffer).toBeNull();
  });

  it('toggleCapture renders what the server said, not what it asked', async () => {
    const { result } = await bootReady();
    // Server refuses to turn on (e.g. race) — active must STAY false.
    mockCapture.mockResolvedValueOnce({
      session_key: 'sess-1',
      capture_active: false,
      spans: [],
      closed_span: null,
    });
    await act(async () => {
      await result.current.toggleCapture();
    });
    expect(mockCapture).toHaveBeenCalledWith('sess-1', true, HOME_INSTANCE_NAME);
    expect(result.current.captureActive).toBe(false);

    // Server confirms ON.
    mockCapture.mockResolvedValueOnce({
      session_key: 'sess-1',
      capture_active: true,
      spans: [{ index: 0, start: 2, end: null, turns: 0, extracted: false }],
      closed_span: null,
    });
    await act(async () => {
      await result.current.toggleCapture();
    });
    expect(result.current.captureActive).toBe(true);
  });

  it('toggle OFF with a closed span surfaces the offer; empty span (null) offers nothing', async () => {
    const { result } = await bootReady();
    mockCapture.mockResolvedValueOnce({
      session_key: 'sess-1',
      capture_active: true,
      spans: [{ index: 0, start: 2, end: null, turns: 0, extracted: false }],
      closed_span: null,
    });
    await act(async () => {
      await result.current.toggleCapture();
    });
    mockCapture.mockResolvedValueOnce({
      session_key: 'sess-1',
      capture_active: false,
      spans: [{ index: 0, start: 2, end: 4, turns: 2, extracted: false }],
      closed_span: { index: 0, turns: 2 },
    });
    await act(async () => {
      await result.current.toggleCapture();
    });
    expect(result.current.captureActive).toBe(false);
    expect(result.current.extractionOffer).toEqual({ spanIndex: 0, turns: 2 });

    // Empty-span close: closed_span null → the offer is cleared, not held.
    mockCapture.mockResolvedValueOnce({
      session_key: 'sess-1',
      capture_active: false,
      spans: [],
      closed_span: null,
    });
    await act(async () => {
      await result.current.toggleCapture();
    });
    expect(result.current.extractionOffer).toBeNull();
  });

  it('a refused toggle lands as a calm notice, never a thread error', async () => {
    const { result } = await bootReady();
    mockCapture.mockRejectedValueOnce(new ApiError(409, 'turn_in_flight'));
    await act(async () => {
      await result.current.toggleCapture();
    });
    expect(result.current.captureActive).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.notice).toContain('still being answered');
  });

  it('a captured receipt appends NO assistant bubble and marks the user bubble', async () => {
    mockHistory.mockResolvedValue(
      history([], { active: true, spans: [{ index: 0, start: 0, end: null, turns: 0, extracted: false }] }),
    );
    const { result } = await bootReady();
    expect(result.current.captureActive).toBe(true);
    // Non-streamable 200 → the buffered fallback path → chatApi.turn.
    mockStream.mockResolvedValue({ ok: true, status: 200, body: null } as unknown as Response);
    mockTurn.mockResolvedValue({
      reply: '',
      captured: true,
      session_key: 'sess-1',
      ts: '',
      user_ts: 'U1',
      deduped: false,
    });
    await act(async () => {
      await result.current.send('dictated capture line');
    });
    expect(result.current.messages).toHaveLength(1);
    expect(result.current.messages[0]).toMatchObject({
      role: 'user',
      text: 'dictated capture line',
      ts: 'U1',
      captured: true,
    });
    expect(result.current.status).toBe('ready');

    // POSITIVE CONTROL on the same path: a NORMAL turn appends the reply.
    mockTurn.mockResolvedValue({
      reply: 'a real answer',
      captured: false,
      session_key: 'sess-1',
      ts: 'T2',
      user_ts: 'U2',
      deduped: false,
    });
    await act(async () => {
      await result.current.send('and now answer me');
    });
    expect(result.current.messages).toHaveLength(3);
    expect(result.current.messages[2]).toMatchObject({
      role: 'assistant',
      text: 'a real answer',
    });
  });

  it('acceptExtraction: success clears the offer and reports the count', async () => {
    mockHistory.mockResolvedValue(
      history([['user', 'a']], {
        active: false,
        spans: [{ index: 0, start: 0, end: 1, turns: 1, extracted: false }],
      }),
    );
    const { result } = await bootReady();
    expect(result.current.extractionOffer).toEqual({ spanIndex: 0, turns: 1 });
    mockCaptureExtract.mockResolvedValue({
      ok: true,
      session_key: 'sess-1',
      span_index: 0,
      record: 'session/capture-x.md',
      notes: ['note/A.md', 'note/B.md'],
      skipped_reason: '',
    });
    await act(async () => {
      await result.current.acceptExtraction();
    });
    expect(mockCaptureExtract).toHaveBeenCalledWith('sess-1', 0, HOME_INSTANCE_NAME);
    expect(result.current.extractionOffer).toBeNull();
    expect(result.current.notice).toBe('Extracted 2 records from the capture.');
  });

  it('acceptExtraction: zero notes is said out loud (ILB), not silence', async () => {
    mockHistory.mockResolvedValue(
      history([['user', 'a']], {
        active: false,
        spans: [{ index: 0, start: 0, end: 1, turns: 1, extracted: false }],
      }),
    );
    const { result } = await bootReady();
    mockCaptureExtract.mockResolvedValue({
      ok: true,
      session_key: 'sess-1',
      span_index: 0,
      record: 'session/capture-x.md',
      notes: [],
      skipped_reason: 'no_notes_emitted',
    });
    await act(async () => {
      await result.current.acceptExtraction();
    });
    expect(result.current.notice).toContain('nothing stood out');
  });

  it('acceptExtraction failures: already_extracted retires the offer; other errors keep it', async () => {
    mockHistory.mockResolvedValue(
      history([['user', 'a']], {
        active: false,
        spans: [{ index: 0, start: 0, end: 1, turns: 1, extracted: false }],
      }),
    );
    const { result } = await bootReady();
    mockCaptureExtract.mockRejectedValueOnce(
      new ApiError(502, 'engine_error'),
    );
    await act(async () => {
      await result.current.acceptExtraction();
    });
    // Recoverable-ish failure: the offer SURVIVES for another try.
    expect(result.current.extractionOffer).toEqual({ spanIndex: 0, turns: 1 });

    mockCaptureExtract.mockRejectedValueOnce(
      new ApiError(409, 'already_extracted'),
    );
    await act(async () => {
      await result.current.acceptExtraction();
    });
    expect(result.current.extractionOffer).toBeNull();
    expect(result.current.notice).toContain('already extracted');
  });

  it('a fresh chat resets capture state and the offer', async () => {
    mockHistory.mockResolvedValue(
      history([['user', 'a']], {
        active: true,
        spans: [{ index: 0, start: 0, end: null, turns: 1, extracted: false }],
      }),
    );
    const { result } = await bootReady();
    expect(result.current.captureActive).toBe(true);
    mockOpen.mockResolvedValue({ session_key: 'sess-2' });
    await act(async () => {
      await result.current.newChat();
    });
    expect(result.current.captureActive).toBe(false);
    expect(result.current.extractionOffer).toBeNull();
  });
});
