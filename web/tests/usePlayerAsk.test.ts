import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook, waitFor } from '@testing-library/react';

// The C3c player-ask hook: session resolution (reuse the stored home key; open fresh only
// when none / archived), the turn carrying the primer, the no_such_session retry on the
// SAME idempotency key, error mapping + the 401 → onAuthExpired hook, and the seq-guard
// that supersedes an in-flight ask on reset (resume). DOM-free, mocked chatApi.

const { mockTurn, mockOpen } = vi.hoisted(() => ({ mockTurn: vi.fn(), mockOpen: vi.fn() }));
vi.mock('../lib/algernon/client', () => ({ chatApi: { turn: mockTurn, open: mockOpen } }));

import { usePlayerAsk } from '../components/player/usePlayerAsk';
import { ApiError } from '../lib/algernon/http';

// Mirrors useChat's convention; HOME_INSTANCE_NAME defaults to 'Algernon' in tests.
const HOME_KEY = 'algernon:session_key:Algernon';
const PRIMER = { brief_date: '2026-08-01', section_id: 'health' };
const replyOk = { reply: 'Because a driver is out.', session_key: 'sess-1', ts: '', user_ts: '' };

beforeEach(() => {
  mockTurn.mockReset();
  mockOpen.mockReset();
  localStorage.clear();
});
afterEach(() => vi.restoreAllMocks());

describe('usePlayerAsk — session + send', () => {
  it('reuses the stored home session key and sends the turn WITH the primer', async () => {
    localStorage.setItem(HOME_KEY, 'stored-sess');
    mockTurn.mockResolvedValue(replyOk);
    const { result } = renderHook(() => usePlayerAsk());

    await act(async () => {
      await result.current.ask('why is that yellow?', PRIMER);
    });

    expect(mockOpen).not.toHaveBeenCalled(); // stored key ⇒ never clobber a live session
    expect(mockTurn).toHaveBeenCalledTimes(1);
    const [key, message, opts] = mockTurn.mock.calls[0];
    expect(key).toBe('stored-sess');
    expect(message).toBe('why is that yellow?');
    expect(opts.kind).toBe('text');
    expect(typeof opts.idempotencyKey).toBe('string');
    expect(opts.primer).toEqual(PRIMER);
    expect(result.current.status).toBe('answered');
    expect(result.current.answer).toBe('Because a driver is out.');
  });

  it('opens a fresh session (and stores it) when none is stored', async () => {
    mockOpen.mockResolvedValue({ session_key: 'fresh-sess' });
    mockTurn.mockResolvedValue(replyOk);
    const { result } = renderHook(() => usePlayerAsk());

    await act(async () => {
      await result.current.ask('what is next?', PRIMER);
    });

    expect(mockOpen).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem(HOME_KEY)).toBe('fresh-sess');
    expect(mockTurn.mock.calls[0][0]).toBe('fresh-sess');
    expect(result.current.status).toBe('answered');
  });

  it('on no_such_session, opens fresh and retries on the SAME idempotency key', async () => {
    localStorage.setItem(HOME_KEY, 'dead-sess');
    mockOpen.mockResolvedValue({ session_key: 'reopened' });
    mockTurn
      .mockRejectedValueOnce(new ApiError(400, 'no_such_session'))
      .mockResolvedValueOnce(replyOk);
    const { result } = renderHook(() => usePlayerAsk());

    await act(async () => {
      await result.current.ask('again please', PRIMER);
    });

    expect(mockOpen).toHaveBeenCalledTimes(1);
    expect(mockTurn).toHaveBeenCalledTimes(2);
    // The archived key first, the reopened key second — with the SAME idempotency key.
    expect(mockTurn.mock.calls[0][0]).toBe('dead-sess');
    expect(mockTurn.mock.calls[1][0]).toBe('reopened');
    expect(mockTurn.mock.calls[0][2].idempotencyKey).toBe(mockTurn.mock.calls[1][2].idempotencyKey);
    expect(result.current.status).toBe('answered');
  });

  it('an empty / whitespace question is a no-op (no turn)', async () => {
    localStorage.setItem(HOME_KEY, 'stored-sess');
    const { result } = renderHook(() => usePlayerAsk());
    await act(async () => {
      await result.current.ask('   ', PRIMER);
    });
    expect(mockTurn).not.toHaveBeenCalled();
    expect(result.current.status).toBe('idle');
  });
});

describe('usePlayerAsk — errors', () => {
  it('maps an engine_error to an honest message and status error (keyboard stays live)', async () => {
    localStorage.setItem(HOME_KEY, 'stored-sess');
    mockTurn.mockRejectedValue(new ApiError(502, 'engine_error'));
    const { result } = renderHook(() => usePlayerAsk());
    await act(async () => {
      await result.current.ask('hmm', PRIMER);
    });
    expect(result.current.status).toBe('error');
    expect(result.current.error).toMatch(/snag/i);
    expect(result.current.answer).toBeNull();
  });

  it('calls onAuthExpired on a 401', async () => {
    localStorage.setItem(HOME_KEY, 'stored-sess');
    mockTurn.mockRejectedValue(new ApiError(401, 'invalid_session'));
    const onAuthExpired = vi.fn();
    const { result } = renderHook(() => usePlayerAsk({ onAuthExpired }));
    await act(async () => {
      await result.current.ask('hmm', PRIMER);
    });
    expect(onAuthExpired).toHaveBeenCalledTimes(1);
    expect(result.current.status).toBe('error');
  });
});

describe('usePlayerAsk — reset supersedes an in-flight ask', () => {
  it('a resume (reset) before the turn resolves discards the late answer', async () => {
    localStorage.setItem(HOME_KEY, 'stored-sess');
    let resolveTurn: (v: unknown) => void = () => undefined;
    mockTurn.mockImplementation(() => new Promise((r) => { resolveTurn = r; }));
    const { result } = renderHook(() => usePlayerAsk());

    let pending: Promise<void> = Promise.resolve();
    // Let the microtasks advance so the ask reaches the (deferred) turn call — only then
    // is `resolveTurn` the real resolver and the status 'sending'.
    await act(async () => {
      pending = result.current.ask('slow one', PRIMER);
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(result.current.status).toBe('sending');

    act(() => result.current.reset()); // operator resumes mid-flight
    expect(result.current.status).toBe('idle');

    await act(async () => {
      resolveTurn(replyOk); // the late turn resolves AFTER reset
      await pending;
    });
    // The superseded resolution must NOT flip the surface back to answered.
    expect(result.current.status).toBe('idle');
    expect(result.current.answer).toBeNull();
  });

  it('reset clears a prior answer/error back to idle', async () => {
    localStorage.setItem(HOME_KEY, 'stored-sess');
    mockTurn.mockResolvedValue(replyOk);
    const { result } = renderHook(() => usePlayerAsk());
    await act(async () => { await result.current.ask('q', PRIMER); });
    expect(result.current.status).toBe('answered');
    act(() => result.current.reset());
    expect(result.current.status).toBe('idle');
    expect(result.current.answer).toBeNull();
    expect(result.current.error).toBeNull();
  });
});
