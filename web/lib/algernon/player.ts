// C3b briefing-player data model. Pure narration→slides derivation, kept out of the
// component so slide ordering / omission is unit-testable without a DOM. The narration
// JSON is c2's STABLE contract (BriefNarration.to_dict); audio + the C3c interrupt
// chat loop layer on top later — this module knows only slides.

// One speakable segment (c2's BriefNarration segment, verbatim shape).
export interface NarrationSegment {
  section_id: string;
  title: string;
  text: string;
  word_count: number;
}

// The narration JSON (c2's BriefNarration.to_dict). `empty` = no speakable content for
// the day (a genuinely empty brief); segments is then [] and the player shows its ILB
// "nothing to play" state (distinct from "no brief" / "tts not configured").
export interface BriefNarration {
  brief_date: string;
  segments: NarrationSegment[];
  total_words: number;
  empty: boolean;
}

// Canonical slide order (c2's SEGMENT_ORDER). A segment ABSENT from the narration is
// an OMITTED slide (calm weather → no weather slide — the operator's demote ruling);
// the deck collapses the gap (no blank slide). Ordering is applied here as the single
// seam so a producer reorder can't desync the deck.
export const SEGMENT_ORDER = ['day_state', 'health', 'day_plan', 'events', 'weather', 'sign_off'] as const;
export type SectionId = (typeof SEGMENT_ORDER)[number];

export interface PlayerSlide {
  /** 0-based position in the RENDERED (omission-collapsed) deck. */
  index: number;
  /** The narration section_id — the deep-link key + the C3c primer's section id. */
  sectionId: string;
  title: string;
  text: string;
  wordCount: number;
}

function orderRank(id: string): number {
  const i = (SEGMENT_ORDER as readonly string[]).indexOf(id);
  // Unknown section_ids sort LAST (defensive — never silently dropped; the producer
  // emits in SEGMENT_ORDER, but a new/unmapped id still renders rather than vanishing).
  return i === -1 ? SEGMENT_ORDER.length : i;
}

/**
 * The player's slides for a narration — one slide per PRESENT segment, in
 * SEGMENT_ORDER, omission-collapsed (absent segment = no slide). `empty` narration or
 * a missing/blank segments array → [] (the caller renders the ILB "nothing to play").
 */
export function narrationSlides(n: BriefNarration | null | undefined): PlayerSlide[] {
  if (!n || n.empty || !Array.isArray(n.segments) || n.segments.length === 0) return [];
  const sorted = [...n.segments].sort((a, b) => orderRank(a.section_id) - orderRank(b.section_id));
  return sorted.map((s, i) => ({
    index: i,
    sectionId: s.section_id,
    title: s.title,
    text: s.text,
    wordCount: s.word_count,
  }));
}
