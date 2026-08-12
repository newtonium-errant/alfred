import { describe, expect, it } from 'vitest';
import { narrationSlides, slideAtFraction, slideDeepLink, SEGMENT_ORDER, type BriefNarration, type NarrationSegment } from '../lib/algernon/player';

function seg(section_id: string, over: Partial<NarrationSegment> = {}): NarrationSegment {
  return { section_id, title: over.title ?? section_id, text: over.text ?? `${section_id} text`, word_count: over.word_count ?? 10 };
}
function narration(segments: NarrationSegment[], over: Partial<BriefNarration> = {}): BriefNarration {
  return { brief_date: '2026-08-01', segments, total_words: segments.reduce((n, s) => n + s.word_count, 0), empty: false, ...over };
}

describe('narrationSlides', () => {
  it('returns [] for null / empty:true / empty segments (the ILB "nothing to play" cases)', () => {
    expect(narrationSlides(null)).toEqual([]);
    expect(narrationSlides(undefined)).toEqual([]);
    expect(narrationSlides(narration([], { empty: true }))).toEqual([]);
    expect(narrationSlides(narration([]))).toEqual([]);
  });

  it('one slide per segment, in SEGMENT_ORDER, with a 0-based rendered index', () => {
    // Built FROM SEGMENT_ORDER rather than from a hand-listed copy of it: the
    // hand-listed version silently stopped covering the full deck the moment a
    // segment was added (Phase C's `waiting`), because a missing id sorts last
    // instead of failing. Derive it, and the pin grows with the contract.
    const slides = narrationSlides(narration(SEGMENT_ORDER.map((id) => seg(id))));
    expect(slides.map((s) => s.sectionId)).toEqual([...SEGMENT_ORDER]);
    expect(slides.map((s) => s.index)).toEqual(SEGMENT_ORDER.map((_id, i) => i));
  });

  it('OMITS an absent segment (calm weather → no weather slide) and collapses the index — no gap', () => {
    const slides = narrationSlides(
      narration([seg('day_state'), seg('health'), seg('day_plan'), seg('events'), seg('sign_off')]), // no weather
    );
    expect(slides.map((s) => s.sectionId)).toEqual(['day_state', 'health', 'day_plan', 'events', 'sign_off']);
    expect(slides.map((s) => s.index)).toEqual([0, 1, 2, 3, 4]); // ← reddens if omission leaves a blank/gap
  });

  it('re-orders out-of-order segments to SEGMENT_ORDER (single-seam ordering)', () => {
    const slides = narrationSlides(narration([seg('sign_off'), seg('day_state'), seg('weather')]));
    expect(slides.map((s) => s.sectionId)).toEqual(['day_state', 'weather', 'sign_off']);
  });

  it('an unknown section_id sorts LAST, never dropped (defensive)', () => {
    const slides = narrationSlides(narration([seg('mystery'), seg('day_state')]));
    expect(slides.map((s) => s.sectionId)).toEqual(['day_state', 'mystery']);
  });

  it('carries title / text / word_count through to the slide', () => {
    const slides = narrationSlides(narration([seg('health', { title: 'Health', text: 'You slept 7h.', word_count: 4 })]));
    expect(slides[0]).toMatchObject({ sectionId: 'health', title: 'Health', text: 'You slept 7h.', wordCount: 4 });
  });
});

describe('slideDeepLink', () => {
  it('day_state → /feed, day_plan → /deck, everything else (incl. unknown) → /brief — never a dead link', () => {
    expect(slideDeepLink('day_state')).toBe('/feed');
    expect(slideDeepLink('day_plan')).toBe('/deck');
    expect(slideDeepLink('health')).toBe('/brief');
    expect(slideDeepLink('weather')).toBe('/brief');
    expect(slideDeepLink('sign_off')).toBe('/brief');
    expect(slideDeepLink('mystery')).toBe('/brief');
  });
});

describe('slideAtFraction — word_count position sync', () => {
  const slides = narrationSlides(
    narration([
      seg('day_state', { word_count: 10 }), // 0.00 – 0.25
      seg('health', { word_count: 10 }), //    0.25 – 0.50
      seg('day_plan', { word_count: 20 }), //   0.50 – 1.00
    ]),
  );
  it('clamps ≤0 → first slide, ≥1 → last slide', () => {
    expect(slideAtFraction(slides, -0.2)).toBe(0);
    expect(slideAtFraction(slides, 0)).toBe(0);
    expect(slideAtFraction(slides, 1)).toBe(2);
    expect(slideAtFraction(slides, 1.5)).toBe(2);
  });
  it('lands in the segment whose word_count share contains the fraction', () => {
    expect(slideAtFraction(slides, 0.1)).toBe(0);
    expect(slideAtFraction(slides, 0.3)).toBe(1);
    expect(slideAtFraction(slides, 0.75)).toBe(2);
  });
  it('empty slides → 0 (no crash)', () => {
    expect(slideAtFraction([], 0.5)).toBe(0);
  });
});
