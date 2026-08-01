import { describe, expect, it } from 'vitest';
import { narrationSlides, SEGMENT_ORDER, type BriefNarration, type NarrationSegment } from '../lib/algernon/player';

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
    const slides = narrationSlides(
      narration([seg('day_state'), seg('health'), seg('day_plan'), seg('events'), seg('weather'), seg('sign_off')]),
    );
    expect(slides.map((s) => s.sectionId)).toEqual([...SEGMENT_ORDER]);
    expect(slides.map((s) => s.index)).toEqual([0, 1, 2, 3, 4, 5]);
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
