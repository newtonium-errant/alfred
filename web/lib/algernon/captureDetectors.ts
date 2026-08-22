/**
 * CRT capture detectors — the earned-tools model (queue slot 11, lane 1).
 *
 * The page this feeds opens as a blinking cursor and a timestamp and NOTHING
 * else. Every affordance after that is *earned* by what was actually written.
 * The sketch's bet, verbatim: "a capture box should start as a cursor and
 * nothing else. Every affordance after that has to be earned by what you
 * actually wrote."
 *
 * Five detectors, straight from the sketch's `analyze()`: note · source anchor ·
 * open question · thread split · commit bar. This module is the whole of that
 * analysis. It renders nothing, stores nothing, and fetches nothing.
 *
 * ── THE SEAM ────────────────────────────────────────────────────────────────
 * `analyzeCapture` takes A STRING AND NOTHING ELSE. It cannot learn where the
 * text came from, and that is the point rather than an accident of the
 * signature: dictation is in v1 (Q3), and a dictated capture must earn the
 * IDENTICAL affordance set as a typed capture of the same words. A parameter
 * naming the origin would make that guarantee unenforceable — the detectors
 * could drift apart per-origin and no test would be lying. With one string
 * argument the guarantee is structural: same characters, same answer.
 *
 * What that costs, and how it is paid: typed text arrives newline-delimited and
 * dictated text arrives as a space-joined run (`useComposerText`'s append rule).
 * A newline-only segmenter would therefore behave differently on the two — which
 * IS learning where the text came from, by the back door. So `segmentCapture`
 * splits on newlines AND on sentence-terminal punctuation. See its own note for
 * the boundary this does not reach (an unpunctuated transcript) and what lane 2
 * has to do about it.
 *
 * ── EVERY GUESS STATES ITS CONFIDENCE AND OFFERS A CORRECTION ───────────────
 * (2026-08-11 ratification, not a nicety.) The source anchor, the thread split
 * and the weaker question triggers are guesses, so each carries a confidence and
 * a named correction id. The word count is not a guess — a count asserts
 * nothing — and carries neither.
 *
 * Corrections are the ONE thing that moves a latch backwards without emptying
 * the capture (see `correctCapture`). Without that, "Not a source →" would be a
 * button that does nothing, because latching would immediately re-earn what it
 * dismissed.
 *
 * ── THRESHOLDS ARE NAMED ────────────────────────────────────────────────────
 * Every number below is an exported constant. The phase-2 learning loop tunes
 * these from the corrections log v1 writes; a literal buried in a conditional
 * can be neither tuned nor proposed on.
 *
 * Pure and DOM-free on purpose, per the convention `composerRouting.ts` sets
 * out — the page renders what these return, so the rules are testable without a
 * render.
 */

/* ══════════════════════════════════════════════════════════════════════════
 * Segmentation — the substrate all five detectors run on
 * ══════════════════════════════════════════════════════════════════════════ */

/**
 * A segment shorter than this is not a thought — it is a stray keystroke, a
 * bare "ok", or the tail of a line being deleted.
 *
 * The sketch spells this `l.trim().length > 3`; `> 3` and `>= 4` are the same
 * cut, written as the minimum that COUNTS rather than the maximum that does not.
 */
export const CAPTURE_SEGMENT_MIN_CHARS = 4;

/**
 * Words that end in a period WITHOUT ending a sentence.
 *
 * Without this, "I'm reading the NS Dept. of Ag guide" segments into two, which
 * inflates the segment count (earning `note` a beat early) and — worse — cuts a
 * source cue in half. Deliberately short and boring: an abbreviation list is a
 * heuristic that shows its work, and every entry here is one a capture in this
 * operator's vocabulary actually produces.
 *
 * Stored WITHOUT the trailing period; matched case-insensitively.
 */
export const CAPTURE_SENTENCE_ABBREVIATIONS: readonly string[] = [
  'approx', 'apt', 'assn', 'ave', 'co', 'corp', 'dept', 'dr', 'eg', 'est',
  'etc', 'fig', 'ft', 'govt', 'ie', 'inc', 'jr', 'lb', 'lbs', 'ltd', 'max',
  'min', 'mr', 'mrs', 'ms', 'mt', 'no', 'oz', 'pp', 'rd', 'sr', 'st', 'vs',
  'yd',
];

/** One analysable unit of a capture — a line, or a sentence within a line. */
export interface CaptureSegment {
  /** The segment's text, trimmed. */
  text: string;
  /**
   * Index into the capture's newline-delimited lines.
   *
   * The page renders LINES; the detectors analyse SEGMENTS, and one line can
   * hold several. A gutter marker has to land on the line the operator can see,
   * so every segment carries the line it came from.
   */
  lineIndex: number;
  /** Position among all segments, in reading order. */
  index: number;
}

const ABBREVIATION_SET = new Set(CAPTURE_SENTENCE_ABBREVIATIONS);

/** True when the `.` closing `chunk` is an abbreviation's period, not a stop. */
function endsInAbbreviation(chunk: string): boolean {
  const m = /([A-Za-z]+)\.$/.exec(chunk);
  if (!m) return false;
  // A single letter is an initial ("J. Smith"), never a sentence end.
  if (m[1].length === 1) return true;
  return ABBREVIATION_SET.has(m[1].toLowerCase());
}

/**
 * Split a capture into segments: newlines first, then sentence-terminal
 * punctuation within each line.
 *
 * WHY BOTH. Typed captures arrive newline-delimited. Dictated captures arrive
 * as one space-joined run, because that is what `useComposerText`'s append rule
 * produces. Splitting on newlines alone would mean the detectors fire on the
 * typed capture and go quiet on the dictated one — the page degrading to a text
 * box the moment you speak to it, which §0 of the design names as the failure
 * mode dictation-in-v1 has to be built against.
 *
 * KNOWN LIMIT, stated rather than discovered: this reaches a PUNCTUATED
 * transcript, not an unpunctuated one. A transcript with no sentence marks at
 * all is one segment however it is split, and no segmenter can recover the
 * boundaries the speaker did not utter. `tests/captureDetectors.test.ts` pins
 * that limit explicitly so it stays a known boundary. The remedy is lane 2's
 * and it is one line: append each transcript as ITS OWN LINE rather than
 * space-joining it onto the previous one — then every utterance is a segment
 * whether or not the STT punctuated it.
 *
 * A `.` is a stop only when whitespace or the end of the text follows it, so
 * "5.2" and "section 4.1" stay whole.
 */
export function segmentCapture(text: string): CaptureSegment[] {
  const out: CaptureSegment[] = [];
  const lines = text.split('\n');
  lines.forEach((line, lineIndex) => {
    // Cut after any run of . ? ! that is followed by whitespace or end-of-line.
    const pieces: string[] = [];
    let start = 0;
    for (let i = 0; i < line.length; i += 1) {
      if (!'.?!'.includes(line[i])) continue;
      // Consume the whole run ("...", "?!").
      let end = i;
      while (end + 1 < line.length && '.?!'.includes(line[end + 1])) end += 1;
      const followed = end + 1 >= line.length || /\s/.test(line[end + 1]);
      if (!followed) {
        i = end;
        continue;
      }
      const chunk = line.slice(start, end + 1);
      if (chunk.endsWith('.') && endsInAbbreviation(chunk)) {
        i = end;
        continue;
      }
      pieces.push(chunk);
      start = end + 1;
      i = end;
    }
    if (start < line.length) pieces.push(line.slice(start));
    pieces.forEach((piece) => {
      const trimmed = piece.trim();
      if (!trimmed) return;
      out.push({ text: trimmed, lineIndex, index: out.length });
    });
  });
  return out;
}

/** Whitespace-delimited word count, over the whole capture. */
export function countCaptureWords(text: string): number {
  const t = text.trim();
  return t ? t.split(/\s+/).length : 0;
}

/* ══════════════════════════════════════════════════════════════════════════
 * Detector 1 — Note
 * ══════════════════════════════════════════════════════════════════════════ */

/** Segments (sentence-ish units) at which a capture becomes a note. */
export const CAPTURE_EARN_NOTE_SEGMENTS = 2;

/** …or this many words, whichever lands first. */
export const CAPTURE_EARN_NOTE_WORDS = 14;

/* ══════════════════════════════════════════════════════════════════════════
 * Detector 2 — Source anchor
 * ══════════════════════════════════════════════════════════════════════════ */

/**
 * The phrases that anchor a source, each with what it is worth.
 *
 * The CUE SET is the sketch's, unchanged — `captureDetectors.test.ts` pins it
 * against the sketch's own `SRC_RE`, verbatim, as an independent oracle. What is
 * added here is the per-cue confidence, and it is added because the cues are
 * nothing like equally good: "I'm reading X" is a person telling you what they
 * are reading, while "from the" fires on "took it from the barn loft". Both
 * anchor; only one deserves to look certain.
 *
 * Ordered most-specific first — the first match wins, so "i'm reading" is
 * credited rather than the bare "reading" inside it.
 *
 * "Anchored, not asserted" (sketch copy): a cue never claims the source EXISTS,
 * only that the operator named one. Commit resolves that against the vault.
 */
export interface CaptureSourceCue {
  /** Human-readable, for the correction panel's "Detected from …" sentence. */
  readonly phrase: string;
  readonly pattern: RegExp;
  readonly confidence: number;
}

export const CAPTURE_SOURCE_CUES: readonly CaptureSourceCue[] = [
  { phrase: "i'm reading", pattern: /\b(i'?m reading)\b\s+(.{4,})/i, confidence: 0.9 },
  { phrase: 'per the', pattern: /\b(per the)\b\s+(.{4,})/i, confidence: 0.75 },
  { phrase: 'reading', pattern: /\b(reading)\b\s+(.{4,})/i, confidence: 0.6 },
  { phrase: 'from the', pattern: /\b(from the)\b\s+(.{4,})/i, confidence: 0.35 },
];

/**
 * The confidence below which a source cue is not offered at all.
 *
 * TODAY THIS SUPPRESSES NOTHING — every shipped cue clears it, and a premise pin
 * asserts exactly that, so v1 fires on precisely what the sketch fires on. It
 * exists because without it the phase-2 learning loop has nothing to move: if
 * corrections show "from the" is wrong four times in five, lowering its
 * confidence has to DO something, and this is the something. The knob and the
 * thing it turns ship together or the loop is decorative.
 */
export const CAPTURE_SOURCE_MIN_CONFIDENCE = 0.3;

/** A named source, as detected — never as asserted. */
export interface CaptureSourceAnchor {
  /** What was named: the cue's trailing capture, trimmed. */
  title: string;
  /** Which cue fired, for the correction panel's sentence. */
  cue: string;
  confidence: number;
  /** The segment the cue was found in. */
  text: string;
  lineIndex: number;
  index: number;
}

/**
 * The first source cue in the capture, or null.
 *
 * FIRST, not best — the sketch's `all.find(…)`. A capture names what it is
 * reading once, at the top; a later "from the" is far more likely to be prose.
 */
export function detectSource(
  segments: readonly CaptureSegment[],
  opts: { minConfidence?: number } = {},
): CaptureSourceAnchor | null {
  const floor = opts.minConfidence ?? CAPTURE_SOURCE_MIN_CONFIDENCE;
  for (const seg of segments) {
    for (const cue of CAPTURE_SOURCE_CUES) {
      const m = cue.pattern.exec(seg.text);
      if (!m) continue;
      if (cue.confidence < floor) continue;
      return {
        title: m[2].trim(),
        cue: cue.phrase,
        confidence: cue.confidence,
        text: seg.text,
        lineIndex: seg.lineIndex,
        index: seg.index,
      };
    }
  }
  return null;
}

/* ══════════════════════════════════════════════════════════════════════════
 * Detector 3 — Open questions
 * ══════════════════════════════════════════════════════════════════════════ */

/** A segment shorter than this cannot be a question. (Sketch: `length > 6`.) */
export const CAPTURE_QUESTION_MIN_CHARS = 7;

/**
 * The machine-legible flag an open question carries into the committed record.
 *
 * Q1 RULED (2026-08-21): a "?" line becomes an `## Open questions` entry
 * "carrying an explicit machine-legible flag, not prose that merely ends in a
 * question mark." The flag is what makes the stated upgrade path real: minting
 * actual `question` records later reads a FLAG, where prose would have to
 * re-derive intent from punctuation in a body someone may since have hand-edited.
 *
 * An HTML comment, because that is this repo's in-body machine-marker idiom
 * (`vault/attribution.py`'s `BEGIN_INFERRED`) and because it is invisible in
 * rendered Obsidian — the section reads as the operator's own words. The
 * `capture:` prefix names the producer.
 */
export const CAPTURE_QUESTION_FLAG = 'capture:open-question';

/**
 * The section heading questions land under.
 *
 * SAMPLED, not minted: this exact spelling is what `digest/writer.py` already
 * emits (`parts.append("## Open questions")`), so the two surfaces agree by
 * construction rather than by coincidence.
 */
export const CAPTURE_OPEN_QUESTIONS_HEADING = '## Open questions';

/** Why a segment was read as a question. */
export type CaptureQuestionTrigger =
  /** Ends in "?" — barely a guess. */
  | 'terminal-mark'
  /** Opens with "question" / "q" — the operator said so. */
  | 'explicit-lead'
  /** Opens with an interrogative and never closes — the real guess. */
  | 'interrogative-lead'
  /** The operator marked it themselves after the detector missed it. */
  | 'operator';

/**
 * What each trigger is worth.
 *
 * `interrogative-lead` is the one that is genuinely uncertain: "Do not forget
 * the mow" and "What a mess that was" both open with an interrogative and
 * neither is a question. It is kept because "should we amend before the mow"
 * (no mark) is exactly how this operator writes, and losing that is worse than
 * the occasional wrong amber gutter mark that one tap clears.
 */
export const CAPTURE_QUESTION_TRIGGER_CONFIDENCE: Readonly<
  Record<CaptureQuestionTrigger, number>
> = {
  'terminal-mark': 0.95,
  'explicit-lead': 0.9,
  'interrogative-lead': 0.6,
  operator: 1,
};

/** Interrogative openers, without a "?" to confirm them. */
export const CAPTURE_QUESTION_LEAD_WORDS: readonly string[] = [
  'do', 'should', 'can', 'is', 'are', 'why', 'how', 'what',
];

/** Openers where the operator has named the thing outright. */
export const CAPTURE_QUESTION_EXPLICIT_WORDS: readonly string[] = ['question', 'q'];

/** One open question, flagged. */
export interface CaptureQuestion {
  text: string;
  trigger: CaptureQuestionTrigger;
  confidence: number;
  /** Always `CAPTURE_QUESTION_FLAG` — carried per-item so a consumer reading
   *  one question in isolation still has it. */
  flag: string;
  lineIndex: number;
  index: number;
}

const EXPLICIT_LEAD_RE = new RegExp(
  `^\\s*(${CAPTURE_QUESTION_EXPLICIT_WORDS.join('|')})\\b`,
  'i',
);
const INTERROGATIVE_LEAD_RE = new RegExp(
  `^\\s*(${CAPTURE_QUESTION_LEAD_WORDS.join('|')})\\b`,
  'i',
);

/** Which trigger, if any, reads this segment as a question. */
export function questionTriggerFor(text: string): CaptureQuestionTrigger | null {
  if (text.trim().length < CAPTURE_QUESTION_MIN_CHARS) return null;
  if (/\?\s*$/.test(text)) return 'terminal-mark';
  if (EXPLICIT_LEAD_RE.test(text)) return 'explicit-lead';
  if (INTERROGATIVE_LEAD_RE.test(text)) return 'interrogative-lead';
  return null;
}

/** Every segment the question detector reads as open. */
export function detectQuestions(segments: readonly CaptureSegment[]): CaptureQuestion[] {
  const out: CaptureQuestion[] = [];
  for (const seg of segments) {
    const trigger = questionTriggerFor(seg.text);
    if (!trigger) continue;
    out.push({
      text: seg.text,
      trigger,
      confidence: CAPTURE_QUESTION_TRIGGER_CONFIDENCE[trigger],
      flag: CAPTURE_QUESTION_FLAG,
      lineIndex: seg.lineIndex,
      index: seg.index,
    });
  }
  return out;
}

/**
 * The `## Open questions` section, or null when there are none.
 *
 * Null rather than an empty heading: the section is appended to the OPERATOR'S
 * body, and a bare heading with nothing under it is noise in their note. This
 * is not an intentionally-left-blank violation — the DATA shape always carries
 * `questions: []` (see `CaptureAnalysis`), which is where "ran, found nothing"
 * has to be legible. This is prose.
 *
 * Lane 3 owns what the committed body looks like; this exists so the flag and
 * the heading have exactly one spelling between the lanes rather than two.
 */
export function formatOpenQuestionsSection(
  questions: readonly CaptureQuestion[],
): string | null {
  if (questions.length === 0) return null;
  const lines = questions.map((q) => `- [ ] ${q.text} <!-- ${q.flag} -->`);
  return [CAPTURE_OPEN_QUESTIONS_HEADING, '', ...lines].join('\n');
}

/* ══════════════════════════════════════════════════════════════════════════
 * Detector 4 — Thread split  (Q2: ships, deliberately the least certain guess)
 * ══════════════════════════════════════════════════════════════════════════ */

/**
 * REMOVAL RECIPE (the Q2 reversal, kept true rather than merely claimed).
 * Deleting this detector is: drop `detectThreadSplits` + `groupThreads` + the
 * five `CAPTURE_THREAD_*` constants, drop `threadSplits`/`threadCount` from
 * `CaptureAnalysis`, and drop the `threadCount > 1` clause from the commit
 * gate. Nothing else reads them. Every capture then has one thread and no other
 * behaviour changes.
 *
 * The sketch's `TOPICS` regexes (field/dispatch vocabulary) are demo
 * scaffolding, NOT contract — the design says so outright. They cannot ship:
 * they recognise this operator's two demo topics and nothing else, so any third
 * subject reads as topic-less. What ships is the explainable heuristic the
 * design specifies in their place — a sentence-initial discourse marker AND low
 * lexical overlap with what came before.
 *
 * BOTH conditions, not either. Overlap alone splits constantly: consecutive
 * sentences about one subject routinely share no content words ("soil ph came
 * back 5.2" → "that's down half a point from last august" share nothing), and a
 * detector that splits there would report a new thread per sentence. The marker
 * is what makes the guess about a topic SHIFT rather than about vocabulary
 * churn; the overlap test is what stops "also the north field needs another
 * sample" — a marker continuing the same subject — from splitting.
 */

/**
 * Sentence-initial markers that announce a change of subject.
 *
 * Longest-first so "on another note" is credited before "on".
 */
export const CAPTURE_THREAD_MARKERS: readonly string[] = [
  'on another note',
  'on a different note',
  'different note',
  'separate note',
  'side note',
  'new thread',
  'unrelated',
  'separately',
  'meanwhile',
  'switching',
  'anyway',
  'also',
  'oh and',
];

/**
 * The share of a segment's content words that may already appear in the running
 * text and still count as a topic shift.
 *
 * At or below this, the segment is talking about something else. Above it, a
 * leading "also" is a continuation — the operator adding to the subject they
 * were already on, which is not a thread.
 */
export const CAPTURE_THREAD_OVERLAP_CEILING = 0.2;

/** Below this many content words, overlap is noise and no split is offered. */
export const CAPTURE_THREAD_MIN_CONTENT_WORDS = 2;

/**
 * Confidence floor and span for a split.
 *
 * CALIBRATED TO THE SKETCH, which is the contract including its numbers: its
 * thread panel reads "Split guessed from a topic shift, confidence 0.68", and
 * its demo capture is a marker line with zero overlap. Zero overlap therefore
 * has to report 0.68 — BASE + SPAN — and it is pinned against the sketch's own
 * demo script. These are the two numbers phase 2 moves.
 */
export const CAPTURE_THREAD_CONFIDENCE_BASE = 0.5;
export const CAPTURE_THREAD_CONFIDENCE_SPAN = 0.18;

/**
 * Function words, excluded from the overlap test.
 *
 * Two sentences sharing "the" and "is" have nothing in common; counting them
 * would push every segment over the ceiling and silently disable the detector.
 */
export const CAPTURE_THREAD_STOPWORDS: readonly string[] = [
  'a', 'about', 'after', 'all', 'am', 'an', 'and', 'another', 'any', 'are',
  'as', 'at', 'back', 'be', 'been', 'before', 'being', 'both', 'but', 'by',
  'came', 'can', 'cant', 'could', 'did', 'do', 'does', 'doing', 'dont', 'down',
  'each', 'for', 'from', 'get', 'go', 'got', 'had', 'has', 'have', 'he', 'her',
  'here', 'hers', 'him', 'his', 'how', 'i', 'if', 'im', 'in', 'into', 'is',
  'it', 'its', 'just', 'me', 'more', 'most', 'my', 'no', 'not', 'now', 'of',
  'off', 'on', 'one', 'only', 'or', 'other', 'our', 'out', 'over', 'own',
  'same', 'she', 'should', 'so', 'some', 'still', 'such', 'than', 'that',
  'thats', 'the', 'their', 'them', 'then', 'there', 'these', 'they', 'this',
  'those', 'through', 'to', 'too', 'under', 'up', 'us', 'very', 'was', 'we',
  'were', 'what', 'when', 'where', 'which', 'while', 'who', 'why', 'will',
  'with', 'wont', 'would', 'you', 'your',
];

const STOPWORD_SET = new Set(CAPTURE_THREAD_STOPWORDS);
const THREAD_MARKERS_SORTED = [...CAPTURE_THREAD_MARKERS].sort(
  (a, b) => b.length - a.length,
);

/** The discourse marker this segment opens with, or null. */
export function threadMarkerFor(text: string): string | null {
  const lowered = text.trim().toLowerCase();
  for (const marker of THREAD_MARKERS_SORTED) {
    if (!lowered.startsWith(marker)) continue;
    // Must be a whole word/phrase: "also" opens "also kyle", not "alsatian".
    const next = lowered[marker.length];
    if (next === undefined || /[^a-z0-9']/.test(next)) return marker;
  }
  return null;
}

/** Content words of a segment: lowercased, de-punctuated, stopwords dropped. */
export function contentWords(text: string): string[] {
  const raw = text.toLowerCase().match(/[a-z0-9][a-z0-9'.]*/g) ?? [];
  const out: string[] = [];
  for (const token of raw) {
    const norm = token.replace(/['.]/g, '');
    if (norm.length < 2) continue;
    if (STOPWORD_SET.has(norm)) continue;
    out.push(norm);
  }
  return out;
}

/** One guessed topic shift. */
export interface CaptureThreadSplit {
  /** Segment index the new thread STARTS at. */
  atIndex: number;
  lineIndex: number;
  /** The marker that announced it. */
  marker: string;
  /** Share of content words already seen — the evidence for the guess. */
  overlap: number;
  confidence: number;
}

/**
 * Every guessed topic shift, in reading order. Empty means one thread.
 *
 * The first segment can never be a split — there is nothing before it to shift
 * away from.
 */
export function detectThreadSplits(
  segments: readonly CaptureSegment[],
): CaptureThreadSplit[] {
  const splits: CaptureThreadSplit[] = [];
  const seen = new Set<string>();
  segments.forEach((seg, i) => {
    const marker = threadMarkerFor(seg.text);
    // The running text is everything before this segment — accumulated even for
    // segments that are themselves too short to test, so a topic mentioned in
    // passing still counts as seen.
    const words = contentWords(marker ? seg.text.trim().slice(marker.length) : seg.text);
    if (i > 0 && marker && words.length >= CAPTURE_THREAD_MIN_CONTENT_WORDS) {
      const hits = words.filter((w) => seen.has(w)).length;
      const overlap = hits / words.length;
      if (overlap <= CAPTURE_THREAD_OVERLAP_CEILING) {
        const closeness = 1 - overlap / CAPTURE_THREAD_OVERLAP_CEILING;
        splits.push({
          atIndex: i,
          lineIndex: seg.lineIndex,
          marker,
          overlap,
          confidence:
            CAPTURE_THREAD_CONFIDENCE_BASE + closeness * CAPTURE_THREAD_CONFIDENCE_SPAN,
        });
      }
    }
    for (const w of contentWords(seg.text)) seen.add(w);
  });
  return splits;
}

/** One thread, with the tallies the sketch's thread dock shows. */
export interface CaptureThread {
  index: number;
  segments: CaptureSegment[];
  words: number;
  questionCount: number;
  hasSource: boolean;
  /** The guess that started this thread; null for the first, which was never
   *  guessed at. */
  split: CaptureThreadSplit | null;
}

/** Group segments into threads at the given splits. */
export function groupThreads(
  segments: readonly CaptureSegment[],
  splits: readonly CaptureThreadSplit[],
  source: CaptureSourceAnchor | null,
  questions: readonly CaptureQuestion[],
): CaptureThread[] {
  const startAt = new Map(splits.map((s) => [s.atIndex, s]));
  const threads: CaptureThread[] = [];
  segments.forEach((seg, i) => {
    const split = startAt.get(i) ?? null;
    if (threads.length === 0 || split) {
      threads.push({
        index: threads.length,
        segments: [],
        words: 0,
        questionCount: 0,
        hasSource: false,
        split,
      });
    }
    const t = threads[threads.length - 1];
    t.segments.push(seg);
    t.words += countCaptureWords(seg.text);
    if (questions.some((q) => q.index === seg.index)) t.questionCount += 1;
    if (source && source.index === seg.index) t.hasSource = true;
  });
  return threads;
}

/* ══════════════════════════════════════════════════════════════════════════
 * Detector 5 — Commit bar
 * ══════════════════════════════════════════════════════════════════════════ */

/**
 * Words below which a capture has not got enough shape to say where it lands.
 *
 * The gate is this AND at least one of (source ∨ question ∨ >1 thread): the
 * commit bar's whole job is to state the destination before the write, and a
 * fragment with no shape has no destination to state.
 */
export const CAPTURE_EARN_COMMIT_WORDS = 18;

/** Characters a derived title is cut to — the ingest door's own title bound. */
export const CAPTURE_TITLE_MAX_CHARS = 300;

/**
 * The title the commit bar states and the commit path writes.
 *
 * Here rather than in either consumer because there are two of them — the bar
 * says "Lands as note/<title>" and the submit sends it — and two spellings of
 * one derivation is how they drift into saying different things about the same
 * capture.
 *
 * From the FIRST segment (the design's "derived from the first line"), stripped
 * of trailing sentence punctuation so a title is not left wearing a question
 * mark, and cut to the transport's bound. Note this is deliberately NOT the
 * shortcut route's `deriveTitle`: that one appends a "— voice capture <stamp>"
 * suffix because a Shortcuts capture has no first line worth reading. This
 * surface does.
 */
export function deriveCaptureTitle(text: string): string {
  const first = segmentCapture(text)[0]?.text ?? '';
  const stripped = first.replace(/[.?!]+\s*$/, '').trim();
  return stripped.slice(0, CAPTURE_TITLE_MAX_CHARS);
}

/* ══════════════════════════════════════════════════════════════════════════
 * The analysis
 * ══════════════════════════════════════════════════════════════════════════ */

/**
 * Everything the five detectors see in one capture. Raw — no latch applied.
 *
 * Every field is ALWAYS PRESENT, carrying an empty value when there is nothing:
 * `source: null`, `questions: []`, `threadSplits: []`. A field that appeared
 * only when populated could not be told apart from a detector that never ran.
 */
export interface CaptureAnalysis {
  /** Nothing but whitespace — the only thing that returns the page to ground. */
  empty: boolean;
  words: number;
  segments: CaptureSegment[];
  /** Segments long enough to count as a thought (`CAPTURE_SEGMENT_MIN_CHARS`). */
  segmentCount: number;
  source: CaptureSourceAnchor | null;
  questions: CaptureQuestion[];
  threadSplits: CaptureThreadSplit[];
  /** Always ≥ 1. One thread is not a guess; it is the absence of one. */
  threadCount: number;
}

/**
 * Run all five detectors over a capture.
 *
 * ONE STRING ARGUMENT, DELIBERATELY — see the seam note at the top of the file.
 */
export function analyzeCapture(text: string): CaptureAnalysis {
  const segments = segmentCapture(text);
  const counted = segments.filter((s) => s.text.length >= CAPTURE_SEGMENT_MIN_CHARS);
  const source = detectSource(segments);
  const questions = detectQuestions(segments);
  const threadSplits = detectThreadSplits(segments);
  return {
    empty: text.trim().length === 0,
    words: countCaptureWords(text),
    segments,
    segmentCount: counted.length,
    source,
    questions,
    threadSplits,
    threadCount: threadSplits.length + 1,
  };
}

/* ══════════════════════════════════════════════════════════════════════════
 * The latch — once earned, it stays earned
 * ══════════════════════════════════════════════════════════════════════════ */

/**
 * The sketch says why, verbatim: "Once an affordance is earned it STAYS earned.
 * The interface must never flicker its own complexity away between keystrokes —
 * during the pause after a committed line the raw criteria momentarily stop
 * holding, and a naive re-test would blink the indicators off. Only a genuinely
 * empty capture returns the surface to ground state."
 *
 * So this is an OR-fold over the live analysis, never a re-test of it. It is a
 * plain serialisable value because the draft persists it: a reload must not
 * un-earn what the operator already earned.
 */
export interface CaptureEarned {
  note: boolean;
  source: boolean;
  question: boolean;
  threads: boolean;
  commit: boolean;
  /** Highest thread count reached — never falls except to ground or by
   *  correction. */
  threadCount: number;
  /** Operator answers that outrank the detector, in either direction. */
  overrides: CaptureOverrides;
  /** Questions the operator said were not questions, by normalised text. */
  dismissedQuestions: string[];
  /** Segments the operator said WERE questions, by normalised text. */
  assertedQuestions: string[];
}

export interface CaptureOverrides {
  source?: boolean;
  threads?: boolean;
}

/** The blank screen. A cursor, a timestamp, and nothing else. */
export const CAPTURE_GROUND_STATE: CaptureEarned = Object.freeze({
  note: false,
  source: false,
  question: false,
  threads: false,
  commit: false,
  threadCount: 1,
  overrides: Object.freeze({}) as CaptureOverrides,
  dismissedQuestions: Object.freeze([]) as unknown as string[],
  assertedQuestions: Object.freeze([]) as unknown as string[],
});

/** The affordances the page can earn, in the order the earned ledger lists them. */
export const CAPTURE_AFFORDANCES = ['note', 'source', 'question', 'threads', 'commit'] as const;
export type CaptureAffordance = (typeof CAPTURE_AFFORDANCES)[number];

/** Key a question by its text, so an override survives edits ELSEWHERE. */
export function normalizeQuestionKey(text: string): string {
  return text.trim().toLowerCase().replace(/\s+/g, ' ');
}

/**
 * The questions actually shown, after the operator's corrections.
 *
 * An asserted question whose segment no longer exists is DROPPED rather than
 * carried: the operator deleted the line, which un-asks it. That is the same
 * rule the detected questions follow, applied to the manual ones.
 */
export function effectiveQuestions(
  analysis: CaptureAnalysis,
  earned: CaptureEarned,
): CaptureQuestion[] {
  const dismissed = new Set(earned.dismissedQuestions);
  const kept = analysis.questions.filter(
    (q) => !dismissed.has(normalizeQuestionKey(q.text)),
  );
  const already = new Set(kept.map((q) => normalizeQuestionKey(q.text)));
  const asserted: CaptureQuestion[] = [];
  for (const key of earned.assertedQuestions) {
    if (already.has(key)) continue;
    const seg = analysis.segments.find((s) => normalizeQuestionKey(s.text) === key);
    if (!seg) continue;
    asserted.push({
      text: seg.text,
      trigger: 'operator',
      confidence: CAPTURE_QUESTION_TRIGGER_CONFIDENCE.operator,
      flag: CAPTURE_QUESTION_FLAG,
      lineIndex: seg.lineIndex,
      index: seg.index,
    });
  }
  return [...kept, ...asserted].sort((a, b) => a.index - b.index);
}

/** The thread count after the operator's corrections. */
export function effectiveThreadCount(
  analysis: CaptureAnalysis,
  earned: CaptureEarned,
): number {
  if (earned.overrides.threads === false) return 1;
  const raw = Math.max(analysis.threadCount, earned.threadCount);
  if (earned.overrides.threads === true) return Math.max(2, raw);
  return raw;
}

/**
 * Fold one analysis into the latch.
 *
 * An EMPTY capture returns the whole thing to ground state — including the
 * corrections, because there is no longer anything for them to be about.
 */
export function advanceEarned(
  prev: CaptureEarned,
  analysis: CaptureAnalysis,
): CaptureEarned {
  if (analysis.empty) return { ...CAPTURE_GROUND_STATE, overrides: {}, dismissedQuestions: [], assertedQuestions: [] };

  const questions = effectiveQuestions(analysis, prev);
  const threadCount = effectiveThreadCount(analysis, prev);

  const source =
    prev.overrides.source ?? (prev.source || analysis.source !== null);
  const threads = prev.overrides.threads ?? (prev.threads || threadCount > 1);
  const question = prev.question || questions.length > 0;
  const note =
    prev.note ||
    analysis.segmentCount >= CAPTURE_EARN_NOTE_SEGMENTS ||
    analysis.words >= CAPTURE_EARN_NOTE_WORDS;

  // The commit gate reads the EFFECTIVE signals — a source the operator
  // dismissed cannot newly earn a commit bar. It never un-earns one, though:
  // the bar is a readiness statement, not a guess, and blinking it away
  // mid-capture is exactly the flicker latching exists to prevent.
  const commit =
    prev.commit ||
    (analysis.words >= CAPTURE_EARN_COMMIT_WORDS &&
      (source || questions.length > 0 || threadCount > 1));

  return {
    note,
    source,
    question,
    threads,
    commit,
    threadCount,
    overrides: prev.overrides,
    dismissedQuestions: prev.dismissedQuestions,
    assertedQuestions: prev.assertedQuestions,
  };
}

/* ══════════════════════════════════════════════════════════════════════════
 * Corrections
 * ══════════════════════════════════════════════════════════════════════════ */

/**
 * The correction vocabulary, both directions.
 *
 * BOTH directions matter and only one of them is obvious. "Not a source" fixes
 * a guess that fired; "actually that IS a source" fixes one that never did —
 * and a learning loop fed only the first kind learns to fire less and never
 * learns to fire more.
 *
 * These are the ids the corrections log carries from day one (part 1 of the
 * self-correcting standard: capture the signal). Parts 2 and 3 — tune, then
 * propose at morning review — are phase 2.
 */
export const CAPTURE_CORRECTION_IDS = [
  'not_a_source',
  'is_a_source',
  'one_thought',
  'separate_threads',
  'not_a_question',
  'is_a_question',
] as const;
export type CaptureCorrectionId = (typeof CAPTURE_CORRECTION_IDS)[number];

/**
 * A correction the latch can apply.
 *
 * `is_a_source` is loggable but NOT here: naming a source the detector missed
 * needs the operator to say WHICH text is the title, and that picker is not in
 * v1. It is in the vocabulary above so the tap is recorded when v1's panel
 * offers it as feedback-only, and the type below is what stops it being passed
 * here as though it did something.
 */
export type CaptureCorrection =
  | { id: 'not_a_source' }
  | { id: 'one_thought' }
  | { id: 'separate_threads' }
  | { id: 'not_a_question'; text: string }
  | { id: 'is_a_question'; text: string };

/**
 * Apply the operator's answer. It outranks the detector AND the latch.
 *
 * This is the only way an affordance goes backwards without the capture being
 * emptied — and it has to be, because latching would otherwise re-earn the
 * dismissed guess on the very next keystroke and "Not a source →" would be a
 * button that visibly does nothing.
 */
export function correctCapture(
  prev: CaptureEarned,
  correction: CaptureCorrection,
): CaptureEarned {
  switch (correction.id) {
    case 'not_a_source':
      return { ...prev, source: false, overrides: { ...prev.overrides, source: false } };
    case 'one_thought':
      return {
        ...prev,
        threads: false,
        threadCount: 1,
        overrides: { ...prev.overrides, threads: false },
      };
    case 'separate_threads':
      return {
        ...prev,
        threads: true,
        threadCount: Math.max(2, prev.threadCount),
        overrides: { ...prev.overrides, threads: true },
      };
    case 'not_a_question': {
      const key = normalizeQuestionKey(correction.text);
      return {
        ...prev,
        dismissedQuestions: prev.dismissedQuestions.includes(key)
          ? prev.dismissedQuestions
          : [...prev.dismissedQuestions, key],
        assertedQuestions: prev.assertedQuestions.filter((k) => k !== key),
      };
    }
    case 'is_a_question': {
      const key = normalizeQuestionKey(correction.text);
      return {
        ...prev,
        question: true,
        assertedQuestions: prev.assertedQuestions.includes(key)
          ? prev.assertedQuestions
          : [...prev.assertedQuestions, key],
        dismissedQuestions: prev.dismissedQuestions.filter((k) => k !== key),
      };
    }
  }
}

/* ══════════════════════════════════════════════════════════════════════════
 * The one call the page makes
 * ══════════════════════════════════════════════════════════════════════════ */

/** Everything the page needs for one render. */
export interface CaptureState {
  analysis: CaptureAnalysis;
  earned: CaptureEarned;
  /** Detected, minus dismissed, plus asserted — what the gutter marks. */
  questions: CaptureQuestion[];
  /** After corrections. */
  threadCount: number;
  threads: CaptureThread[];
}

/**
 * Analyse, then fold into the latch. One call per keystroke.
 *
 * Pure: the latch is passed in and a new one is returned, so nothing here
 * remembers a previous capture. Two pages, or a test and a page, can run
 * interleaved without either seeing the other's state.
 */
export function stepCapture(prev: CaptureEarned, text: string): CaptureState {
  const analysis = analyzeCapture(text);
  const earned = advanceEarned(prev, analysis);
  const questions = effectiveQuestions(analysis, earned);
  const threadCount = effectiveThreadCount(analysis, earned);
  const splits =
    earned.overrides.threads === false ? [] : analysis.threadSplits;
  return {
    analysis,
    earned,
    questions,
    threadCount,
    threads: groupThreads(analysis.segments, splits, earned.source ? analysis.source : null, questions),
  };
}
