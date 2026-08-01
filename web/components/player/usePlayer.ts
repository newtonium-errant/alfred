import { useCallback, useReducer } from 'react';
import type { PlayerSlide } from '../../lib/algernon/player';

// The interruptible briefing-player state machine (C3b centerpiece). DOM-free +
// reducer-driven so the transitions — and the load-bearing ruling, PAUSE → ASK →
// RESUME-AT-POSITION — are unit-pinned without audio or a render. Audio (C3c/inc-2)
// layers on: it drives `setOffset` while playing and calls `ended()` at a segment's
// end; text-along mode drives the same transitions with no audio. The machine holds
// POSITION (slide index + offset within the slide) across pause AND ask, so resume
// returns to the exact instant — interruptions are first-class, never a stop.

export type PlayerState = 'idle' | 'playing' | 'paused' | 'asking';

export interface PlayerPosition {
  /** Current slide (index into the omission-collapsed deck). */
  slideIndex: number;
  /** Audio offset within the current slide, seconds. 0 until audio is wired; carried
   *  now so resume-at-position is exact the moment audio lands. */
  offsetSec: number;
}

export interface UsePlayerResult {
  state: PlayerState;
  position: PlayerPosition;
  currentSlide: PlayerSlide | null;
  slideCount: number;
  /** On the last slide (for end-of-briefing affordances). */
  atLastSlide: boolean;
  play: () => void;
  pause: () => void;
  /** Interruption — a FIRST-CLASS state (distinct from paused): opens the ask surface
   *  and holds position, so resume returns exactly here. */
  ask: () => void;
  /** Resume-at-position from paused OR asking (the centerpiece). */
  resume: () => void;
  next: () => void;
  prev: () => void;
  seekToSlide: (i: number) => void;
  /** Audio position sync within the current slide (C3c). */
  setOffset: (sec: number) => void;
  /** The current slide's playback finished — advance, or finish on the last slide. */
  ended: () => void;
}

interface Machine {
  state: PlayerState;
  position: PlayerPosition;
}

type Action =
  | { type: 'play' }
  | { type: 'pause' }
  | { type: 'ask' }
  | { type: 'resume' }
  | { type: 'next' }
  | { type: 'prev' }
  | { type: 'seek'; index: number }
  | { type: 'offset'; sec: number }
  | { type: 'ended' };

const INITIAL: Machine = { state: 'idle', position: { slideIndex: 0, offsetSec: 0 } };

function reduce(m: Machine, a: Action, slideCount: number): Machine {
  const last = Math.max(0, slideCount - 1);
  const clamp = (i: number) => Math.max(0, Math.min(i, last));
  switch (a.type) {
    case 'play':
      // Start (from idle, position is {0,0}) OR resume (paused/asking hold position).
      return slideCount === 0 ? m : { ...m, state: 'playing' };
    case 'pause':
      return m.state === 'playing' ? { ...m, state: 'paused' } : m;
    case 'ask':
      // Interruption from playing OR paused — first-class, position untouched.
      return m.state === 'playing' || m.state === 'paused' ? { ...m, state: 'asking' } : m;
    case 'resume':
      // Resume-at-position — the held slideIndex + offsetSec are preserved.
      return m.state === 'paused' || m.state === 'asking' ? { ...m, state: 'playing' } : m;
    case 'next':
      return { ...m, position: { slideIndex: clamp(m.position.slideIndex + 1), offsetSec: 0 } };
    case 'prev':
      return { ...m, position: { slideIndex: clamp(m.position.slideIndex - 1), offsetSec: 0 } };
    case 'seek':
      return { ...m, position: { slideIndex: clamp(a.index), offsetSec: 0 } };
    case 'offset':
      return { ...m, position: { ...m.position, offsetSec: Math.max(0, a.sec) } };
    case 'ended':
      // Segment finished: advance a slide (stay playing) or, on the last, finish →
      // idle at the top (a fresh replay starts from slide 0).
      if (m.position.slideIndex < last) {
        return { ...m, position: { slideIndex: m.position.slideIndex + 1, offsetSec: 0 } };
      }
      return { state: 'idle', position: { slideIndex: 0, offsetSec: 0 } };
    default:
      return m;
  }
}

export function usePlayer(slides: PlayerSlide[]): UsePlayerResult {
  const slideCount = slides.length;
  // Inline reducer closes over the current slideCount (React uses the reducer from the
  // render in which dispatch fires, so a slide-count change is picked up).
  const [m, dispatch] = useReducer((st: Machine, a: Action) => reduce(st, a, slideCount), INITIAL);

  const play = useCallback(() => dispatch({ type: 'play' }), []);
  const pause = useCallback(() => dispatch({ type: 'pause' }), []);
  const ask = useCallback(() => dispatch({ type: 'ask' }), []);
  const resume = useCallback(() => dispatch({ type: 'resume' }), []);
  const next = useCallback(() => dispatch({ type: 'next' }), []);
  const prev = useCallback(() => dispatch({ type: 'prev' }), []);
  const seekToSlide = useCallback((i: number) => dispatch({ type: 'seek', index: i }), []);
  const setOffset = useCallback((sec: number) => dispatch({ type: 'offset', sec }), []);
  const ended = useCallback(() => dispatch({ type: 'ended' }), []);

  const currentSlide = m.position.slideIndex < slideCount ? slides[m.position.slideIndex] : null;

  return {
    state: m.state,
    position: m.position,
    currentSlide,
    slideCount,
    atLastSlide: slideCount > 0 && m.position.slideIndex === slideCount - 1,
    play,
    pause,
    ask,
    resume,
    next,
    prev,
    seekToSlide,
    setOffset,
    ended,
  };
}
