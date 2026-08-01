import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import { useMediaSession } from '../components/player/useMediaSession';

describe('useMediaSession — the lock-screen / car gate', () => {
  let handlers: Record<string, (() => void) | null>;
  let ms: { metadata: unknown; playbackState: string; setActionHandler: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    handlers = {};
    ms = {
      metadata: null,
      playbackState: 'none',
      setActionHandler: vi.fn((action: string, handler: (() => void) | null) => {
        handlers[action] = handler;
      }),
    };
    Object.defineProperty(global.navigator, 'mediaSession', { value: ms, configurable: true, writable: true });
    (global as unknown as { MediaMetadata: unknown }).MediaMetadata = class {
      constructor(init: Record<string, unknown>) {
        Object.assign(this, init);
      }
    };
  });
  afterEach(() => {
    delete (global.navigator as unknown as { mediaSession?: unknown }).mediaSession;
    delete (global as unknown as { MediaMetadata?: unknown }).MediaMetadata;
    vi.restoreAllMocks();
  });

  it('wires play/pause/next/prev + metadata + playbackState; a lock-screen action drives the player (one seam)', () => {
    const cbs = { onPlay: vi.fn(), onPause: vi.fn(), onNext: vi.fn(), onPrev: vi.fn() };
    renderHook(() => useMediaSession({ title: 'Health', playing: true, ...cbs }));
    for (const a of ['play', 'pause', 'nexttrack', 'previoustrack']) {
      expect(ms.setActionHandler).toHaveBeenCalledWith(a, expect.any(Function));
    }
    expect(ms.playbackState).toBe('playing');
    expect((ms.metadata as { title?: string })?.title).toBe('Health');
    handlers['nexttrack']?.(); // a car "next" → the player's onNext, not a parallel path
    expect(cbs.onNext).toHaveBeenCalledTimes(1);
  });

  it('reflects the paused playback state', () => {
    renderHook(() => useMediaSession({ title: 'x', playing: false, onPlay: vi.fn(), onPause: vi.fn(), onNext: vi.fn(), onPrev: vi.fn() }));
    expect(ms.playbackState).toBe('paused');
  });

  it('is a no-op (never throws) when Media Session is unsupported', () => {
    delete (global.navigator as unknown as { mediaSession?: unknown }).mediaSession;
    expect(() =>
      renderHook(() => useMediaSession({ title: 'x', playing: false, onPlay: vi.fn(), onPause: vi.fn(), onNext: vi.fn(), onPrev: vi.fn() })),
    ).not.toThrow();
  });

  it('clears the handlers on unmount', () => {
    const { unmount } = renderHook(() =>
      useMediaSession({ title: 'x', playing: true, onPlay: vi.fn(), onPause: vi.fn(), onNext: vi.fn(), onPrev: vi.fn() }),
    );
    ms.setActionHandler.mockClear();
    unmount();
    expect(ms.setActionHandler).toHaveBeenCalledWith('play', null);
  });
});
