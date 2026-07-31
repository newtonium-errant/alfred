import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type { NextRouter } from 'next/router';
import { useComposerLog } from '../lib/algernon/composerLog';

// Pins the composer telemetry wiring: 'composed' once on mount, 'navigated_away'
// on the FIRST route change within the 10s window (and never after, never twice),
// and nothing while the mode is still resolving (null).

type EmitRouter = NextRouter & { emit: (url: string) => void };

function mockRouter(): EmitRouter {
  const handlers: Array<(url: string) => void> = [];
  return {
    events: {
      on: (_e: string, h: (url: string) => void) => handlers.push(h),
      off: (_e: string, h: (url: string) => void) => {
        const i = handlers.indexOf(h);
        if (i >= 0) handlers.splice(i, 1);
      },
    },
    emit: (url: string) => handlers.slice().forEach((h) => h(url)),
  } as unknown as EmitRouter;
}

let fetchMock: ReturnType<typeof vi.fn>;

function lastBody(): Record<string, unknown> {
  const call = fetchMock.mock.calls[fetchMock.mock.calls.length - 1];
  return JSON.parse((call[1] as { body: string }).body);
}

beforeEach(() => {
  vi.useFakeTimers();
  fetchMock = vi.fn().mockResolvedValue({});
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('useComposerLog', () => {
  it("posts 'composed' once on mount with the rule", () => {
    const router = mockRouter();
    renderHook(() => useComposerLog('feed', router));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/feed/composer-log');
    expect(lastBody()).toEqual({ rule: 'feed', event: 'composed' });
  });

  it('logs nothing while the mode is still null', () => {
    const router = mockRouter();
    renderHook(() => useComposerLog(null, router));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("posts 'navigated_away' with dwell + path on a route change inside 10s", () => {
    const router = mockRouter();
    renderHook(() => useComposerLog('checkin', router));
    fetchMock.mockClear();
    vi.advanceTimersByTime(3000);
    act(() => router.emit('/deck'));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const body = lastBody();
    expect(body.rule).toBe('checkin');
    expect(body.event).toBe('navigated_away');
    expect(body.path).toBe('/deck');
    expect(body.dwell_ms).toBe(3000);
  });

  it('does not post navigated_away after the 10s window', () => {
    const router = mockRouter();
    renderHook(() => useComposerLog('feed', router));
    fetchMock.mockClear();
    vi.advanceTimersByTime(11_000);
    act(() => router.emit('/deck'));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('fires navigated_away at most once', () => {
    const router = mockRouter();
    renderHook(() => useComposerLog('feed', router));
    fetchMock.mockClear();
    vi.advanceTimersByTime(1000);
    act(() => router.emit('/deck'));
    fetchMock.mockClear();
    act(() => router.emit('/feed'));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('truncates a very long destination path to 200 chars', () => {
    const router = mockRouter();
    renderHook(() => useComposerLog('feed', router));
    fetchMock.mockClear();
    vi.advanceTimersByTime(500);
    act(() => router.emit('/x'.repeat(300)));
    expect((lastBody().path as string).length).toBe(200);
  });
});
