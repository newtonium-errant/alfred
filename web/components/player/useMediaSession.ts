import { useEffect } from 'react';

// Wires the Media Session API (lock-screen / car / headset controls) to the player —
// a §3 gate. A no-op when unsupported (SSR, or a browser without navigator.mediaSession)
// and never throws. Metadata + playbackState reflect the current slide/state; the action
// handlers drive the player's OWN transitions, so a car "next" advances a slide exactly
// like the on-screen button (one seam, no parallel logic).

export interface MediaSessionWiring {
  title: string;
  artist?: string;
  playing: boolean;
  onPlay: () => void;
  onPause: () => void;
  onNext: () => void;
  onPrev: () => void;
}

export function useMediaSession(w: MediaSessionWiring): void {
  const { title, artist, playing, onPlay, onPause, onNext, onPrev } = w;
  useEffect(() => {
    if (typeof navigator === 'undefined' || !('mediaSession' in navigator)) return;
    const ms = navigator.mediaSession;
    try {
      if (typeof MediaMetadata !== 'undefined') {
        ms.metadata = new MediaMetadata({ title, artist: artist ?? 'Algernon' });
      }
      ms.playbackState = playing ? 'playing' : 'paused';
    } catch {
      /* metadata is best-effort — never let it break playback */
    }
    const handlers: Array<[MediaSessionAction, MediaSessionActionHandler]> = [
      ['play', () => onPlay()],
      ['pause', () => onPause()],
      ['nexttrack', () => onNext()],
      ['previoustrack', () => onPrev()],
    ];
    for (const [action, handler] of handlers) {
      try {
        ms.setActionHandler(action, handler);
      } catch {
        /* the browser may not support this action — skip it, don't crash */
      }
    }
    return () => {
      for (const [action] of handlers) {
        try {
          ms.setActionHandler(action, null);
        } catch {
          /* ignore */
        }
      }
    };
  }, [title, artist, playing, onPlay, onPause, onNext, onPrev]);
}
