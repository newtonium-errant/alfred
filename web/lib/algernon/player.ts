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

// The C3c player-ask context primer — the on-screen grounding the ask carries to the
// chat turn. Mirrors the backend carry-shape (src/alfred/brief/player_primer.py
// PlayerContextPrimer.to_dict): two stable keys, so Salem resolves deictic references
// ("that", "it", "this") against the slide the operator PAUSED on, grounded in that
// day's brief (not a re-derived "today"). The backend validity-gates it (bad date /
// unknown section_id ⟹ answer un-grounded, never fabricate a slide context).
export interface PlayerPrimer {
  /** The brief being played (ISO YYYY-MM-DD). */
  brief_date: string;
  /** The current slide's narration section id (a SEGMENT_ORDER value). */
  section_id: string;
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

// Where a slide's deep-link (tap the slide element) lands — the REAL surface for that
// section, per the design. day_state (goal rings) → the feed; day_plan (slots) → the
// deck (accept/complete); everything else (health/events/weather/sign_off) → the brief
// page. Unknown sections default to the brief page (never a dead link).
const SECTION_DEEP_LINK: Record<string, string> = {
  day_state: '/feed',
  day_plan: '/deck',
};
export function slideDeepLink(sectionId: string): string {
  return SECTION_DEEP_LINK[sectionId] ?? '/brief';
}

/**
 * The slide index a playback FRACTION (0..1 of the whole briefing) falls in — the v1
 * audio→slide sync (one mp3; segment N spans its word_count share of total_words).
 * Pure so the boundary math is unit-pinned without an <audio> element. Clamps: ≤0 → 0,
 * ≥1 → last; zero-total (no words) → 0. A later refinement is precise per-segment audio.
 */
export function slideAtFraction(slides: PlayerSlide[], fraction: number): number {
  if (slides.length === 0) return 0;
  const total = slides.reduce((n, s) => n + Math.max(0, s.wordCount), 0);
  if (total <= 0 || fraction <= 0) return 0;
  if (fraction >= 1) return slides.length - 1;
  let acc = 0;
  for (let i = 0; i < slides.length; i++) {
    acc += Math.max(0, slides[i].wordCount) / total;
    if (fraction < acc) return i;
  }
  return slides.length - 1;
}
