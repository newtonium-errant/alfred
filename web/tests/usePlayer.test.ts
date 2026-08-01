import { describe, expect, it } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { usePlayer } from '../components/player/usePlayer';
import type { PlayerSlide } from '../lib/algernon/player';

const slides: PlayerSlide[] = [
  { index: 0, sectionId: 'day_state', title: 'A', text: 'a', wordCount: 5 },
  { index: 1, sectionId: 'health', title: 'B', text: 'b', wordCount: 5 },
  { index: 2, sectionId: 'sign_off', title: 'C', text: 'c', wordCount: 5 },
];

describe('usePlayer — the interruptible state machine', () => {
  it('starts idle at {0,0} on the first slide', () => {
    const { result } = renderHook(() => usePlayer(slides));
    expect(result.current.state).toBe('idle');
    expect(result.current.position).toEqual({ slideIndex: 0, offsetSec: 0 });
    expect(result.current.currentSlide?.sectionId).toBe('day_state');
    expect(result.current.slideCount).toBe(3);
  });

  it('play → playing; pause holds the position', () => {
    const { result } = renderHook(() => usePlayer(slides));
    act(() => result.current.play());
    expect(result.current.state).toBe('playing');
    act(() => { result.current.next(); result.current.setOffset(20); });
    act(() => result.current.pause());
    expect(result.current.state).toBe('paused');
    expect(result.current.position).toEqual({ slideIndex: 1, offsetSec: 20 });
  });

  it('RESUME-AT-POSITION: pause then resume returns to the exact held instant (the centerpiece)', () => {
    const { result } = renderHook(() => usePlayer(slides));
    act(() => result.current.play());
    act(() => { result.current.seekToSlide(2); result.current.setOffset(12); });
    act(() => result.current.pause());
    act(() => result.current.resume());
    expect(result.current.state).toBe('playing');
    // ← reddens if resume resets the position instead of holding it.
    expect(result.current.position).toEqual({ slideIndex: 2, offsetSec: 12 });
  });

  it('ASK is FIRST-CLASS: a distinct state holding position; resume returns exactly there', () => {
    const { result } = renderHook(() => usePlayer(slides));
    act(() => result.current.play());
    act(() => { result.current.next(); result.current.setOffset(8); });
    act(() => result.current.ask());
    expect(result.current.state).toBe('asking'); // NOT 'paused' — the ask surface is open
    expect(result.current.position).toEqual({ slideIndex: 1, offsetSec: 8 }); // held
    act(() => result.current.resume());
    expect(result.current.state).toBe('playing');
    expect(result.current.position).toEqual({ slideIndex: 1, offsetSec: 8 }); // exact resume-at-position
  });

  it('ask works from paused too; ask is ignored from idle', () => {
    const { result } = renderHook(() => usePlayer(slides));
    act(() => result.current.ask()); // idle → ignored (nothing to interrupt)
    expect(result.current.state).toBe('idle');
    act(() => result.current.play());
    act(() => result.current.pause());
    act(() => result.current.ask());
    expect(result.current.state).toBe('asking');
  });

  it('ended mid-deck advances a slide (stays playing); ended on the last finishes → idle at the top', () => {
    const { result } = renderHook(() => usePlayer(slides));
    act(() => result.current.play());
    act(() => result.current.setOffset(30));
    act(() => result.current.ended()); // slide 0 finished → advance, offset reset
    expect(result.current.state).toBe('playing');
    expect(result.current.position).toEqual({ slideIndex: 1, offsetSec: 0 });
    act(() => result.current.seekToSlide(2)); // last
    act(() => result.current.ended()); // finish
    expect(result.current.state).toBe('idle');
    expect(result.current.position).toEqual({ slideIndex: 0, offsetSec: 0 });
  });

  it('next / prev clamp at the ends and reset the offset; atLastSlide tracks the tail', () => {
    const { result } = renderHook(() => usePlayer(slides));
    act(() => result.current.play());
    act(() => result.current.prev()); // clamp at 0
    expect(result.current.position.slideIndex).toBe(0);
    act(() => { result.current.setOffset(9); result.current.next(); }); // → 1, offset reset
    expect(result.current.position).toEqual({ slideIndex: 1, offsetSec: 0 });
    act(() => { result.current.next(); result.current.next(); }); // clamp at last (2)
    expect(result.current.position.slideIndex).toBe(2);
    expect(result.current.atLastSlide).toBe(true);
  });

  it('play on an empty deck stays idle (no crash, no current slide)', () => {
    const { result } = renderHook(() => usePlayer([]));
    act(() => result.current.play());
    expect(result.current.state).toBe('idle');
    expect(result.current.currentSlide).toBeNull();
    expect(result.current.atLastSlide).toBe(false);
  });
});
