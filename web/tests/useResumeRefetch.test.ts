import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useResumeRefetch, RESUME_REFETCH_MIN_INTERVAL_MS } from '../lib/algernon/useResumeRefetch';

// #62 defect (3). iOS SUSPENDS a backgrounded PWA rather than unloading it, so
// reopening restores the JS heap intact — same tree, same state, same rendered
// rows, no mount and no fetch. At 11:43 the operator was looking at a deck
// rendered at 23:43 the night before.

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
}

beforeEach(() => { setVisibility('visible'); vi.useFakeTimers(); });
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); });

describe('useResumeRefetch brings a resumed PWA back to current', () => {
  it('refetches when the page becomes visible again', () => {
    const refetch = vi.fn();
    renderHook(() => useResumeRefetch(refetch));
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it('also refetches on pageshow — Safari bfcache can skip visibilitychange', () => {
    // Both listeners are needed: a bfcache restore fires pageshow and may never
    // fire visibilitychange, so a visibilitychange-only hook misses exactly the
    // resume path this exists for.
    const refetch = vi.fn();
    renderHook(() => useResumeRefetch(refetch));
    act(() => { window.dispatchEvent(new Event('pageshow')); });
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it('does NOT refetch when the page went hidden', () => {
    const refetch = vi.fn();
    renderHook(() => useResumeRefetch(refetch));
    setVisibility('hidden');
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(refetch).not.toHaveBeenCalled();
  });

  it('debounces a burst of app-switching into one refetch', () => {
    const refetch = vi.fn();
    renderHook(() => useResumeRefetch(refetch));
    act(() => {
      for (let i = 0; i < 5; i += 1) document.dispatchEvent(new Event('visibilitychange'));
    });
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it('allows the next refetch once the interval has passed', () => {
    const refetch = vi.fn();
    renderHook(() => useResumeRefetch(refetch));
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    vi.advanceTimersByTime(RESUME_REFETCH_MIN_INTERVAL_MS + 1);
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(refetch).toHaveBeenCalledTimes(2);
  });

  it('calls the LATEST refetch, not the one captured at mount', () => {
    // Callers pass inline closures. Holding the first one would refetch with a
    // stale filter or a stale token after any re-render.
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = renderHook(({ fn }) => useResumeRefetch(fn), {
      initialProps: { fn: first },
    });
    rerender({ fn: second });
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
  });

  it('detaches its listeners on unmount', () => {
    const refetch = vi.fn();
    const { unmount } = renderHook(() => useResumeRefetch(refetch));
    unmount();
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(refetch).not.toHaveBeenCalled();
  });

  it('does nothing when disabled', () => {
    const refetch = vi.fn();
    renderHook(() => useResumeRefetch(refetch, { enabled: false }));
    act(() => { document.dispatchEvent(new Event('visibilitychange')); });
    expect(refetch).not.toHaveBeenCalled();
  });
});
