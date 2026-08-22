import { describe, expect, it } from 'vitest';
import {
  CAPTURE_AFFORDANCES,
  CAPTURE_CORRECTION_IDS,
  CAPTURE_EARN_COMMIT_WORDS,
  CAPTURE_EARN_NOTE_SEGMENTS,
  CAPTURE_EARN_NOTE_WORDS,
  CAPTURE_GROUND_STATE,
  CAPTURE_OPEN_QUESTIONS_HEADING,
  CAPTURE_QUESTION_FLAG,
  CAPTURE_QUESTION_MIN_CHARS,
  CAPTURE_QUESTION_TRIGGER_CONFIDENCE,
  CAPTURE_SEGMENT_MIN_CHARS,
  CAPTURE_SENTENCE_ABBREVIATIONS,
  CAPTURE_SOURCE_CUES,
  CAPTURE_SOURCE_MIN_CONFIDENCE,
  CAPTURE_THREAD_CONFIDENCE_BASE,
  CAPTURE_THREAD_CONFIDENCE_SPAN,
  CAPTURE_THREAD_MIN_CONTENT_WORDS,
  CAPTURE_THREAD_OVERLAP_CEILING,
  CAPTURE_TITLE_MAX_CHARS,
  advanceEarned,
  analyzeCapture,
  contentWords,
  correctCapture,
  countCaptureWords,
  deriveCaptureTitle,
  detectQuestions,
  detectSource,
  detectThreadSplits,
  effectiveQuestions,
  effectiveSplits,
  formatOpenQuestionsSection,
  freshGroundState,
  groupThreads,
  normalizeSegmentKey,
  questionTriggerFor,
  segmentCapture,
  stepCapture,
  threadMarkerFor,
  type CaptureCorrection,
  type CaptureEarned,
} from '../lib/algernon/captureDetectors';

/**
 * The CRT capture detectors (queue slot 11, lane 1).
 *
 * ── WHERE THE EXPECTATIONS COME FROM ────────────────────────────────────────
 * Not from this file. Three independent sources, because a battery whose
 * fixtures were minted by the same mind as the code agrees with the code and
 * with nothing else:
 *
 *   1. `SKETCH_SRC_RE` / `SKETCH_Q_RE` are pasted VERBATIM out of
 *      `~/reports/reimagine-sketches/sketch-3-crt-capture.html` (lines 325-326)
 *      and used as oracles — the shipped detectors must fire on exactly what
 *      the contract's own regexes fire on.
 *   2. `SKETCH_DEMO_SCRIPT` is the sketch's `SCRIPT` array verbatim (lines
 *      317-323), and the numbers it is pinned to are read off the sketch's own
 *      rendered commit bar (line 226): "2 threads · 1 source · 1 open
 *      question". Those five lines are the contract's worked example, and this
 *      is the assertion that we reproduce it.
 *   3. The latch rule is the sketch's comment (lines 349-352), quoted where it
 *      is pinned.
 *
 * ── CONTROLS ────────────────────────────────────────────────────────────────
 * Every detector gets an input that MUST fire and an input that must NOT.
 * "Excluded input → nothing" on its own passes identically against a detector
 * that has been deleted, so each negative control sits beside the nearest
 * admissible neighbour that still fires.
 */

// ── Oracles, verbatim from the sketch ────────────────────────────────────────
// eslint-disable-next-line
const SKETCH_SRC_RE = /\b(i'?m reading|reading|from the|per the)\b\s+(.{4,})/i;
// eslint-disable-next-line
const SKETCH_Q_RE = /\?\s*$|^\s*(question|q)\b|^\s*(do|should|can|is|are|why|how|what)\b.*$/i;

const SKETCH_DEMO_SCRIPT = [
  'soil ph came back 5.2 on the north field',
  "that's down half a point from last august, same sample points",
  "i'm reading the ns dept of ag lowbush guide, section 4 on sulfur amendment rates",
  'question — do we amend before the fall mow or after?',
  "also kyle can't do wednesday so the yarmouth run moves to thursday",
];

const DEMO_TYPED = SKETCH_DEMO_SCRIPT.join('\n');

/** A line that anchors nothing, asks nothing and starts no thread. */
const UNRELATED_LINE = 'the gate latch needs a new pin';

/** Ground state, as a fresh mutable value (the export is frozen). */
const ground = (): CaptureEarned => freshGroundState();

/**
 * The page's own entry point, from a blank screen.
 *
 * Thread assertions go through THIS rather than through `analyzeCapture`,
 * because the number the operator sees is `CaptureState.threadCount` and the
 * defect this file exists to keep out was a count that disagreed with the dock
 * beside it. Asserting the intermediate cannot see that.
 */
const stateOf = (text: string) => stepCapture(ground(), text);

/** Type the text one character at a time, exactly as the page will. */
function typeOut(text: string): CaptureEarned {
  let earned = ground();
  for (let i = 0; i <= text.length; i += 1) {
    earned = stepCapture(earned, text.slice(0, i)).earned;
  }
  return earned;
}

const affordancesOf = (e: CaptureEarned) => CAPTURE_AFFORDANCES.filter((a) => e[a]);

/* ══════════════════════════════════════════════════════════════════════════
 * The contract's own worked example
 * ══════════════════════════════════════════════════════════════════════════ */

describe('the sketch demo capture', () => {
  it('reproduces the sketch commit bar: 2 threads · 1 source · 1 open question', () => {
    // Read off the sketch's rendered `.cwhere` copy, not computed here.
    const s = stateOf(DEMO_TYPED);
    expect(s.threadCount).toBe(2);
    expect(s.threads).toHaveLength(2);
    expect(s.analysis.source).not.toBeNull();
    expect(s.questions).toHaveLength(1);
  });

  it('pins the whole analysis, so a silent re-tuning shows up as a diff', () => {
    const a = analyzeCapture(DEMO_TYPED);
    // 9 + 11 + 15 + 11 + 12, whitespace-delimited exactly as the sketch counts
    // (the em dash on line 4 is a token, and so it is here).
    expect(a.words).toBe(58);
    expect(a.segments).toHaveLength(5);
    expect(a.segmentCount).toBe(5);
    expect(a.empty).toBe(false);
    expect(a.source?.cue).toBe("i'm reading");
    expect(a.source?.title).toBe(
      'the ns dept of ag lowbush guide, section 4 on sulfur amendment rates',
    );
    expect(a.questions[0].trigger).toBe('terminal-mark');
    expect(a.threadSplits).toHaveLength(1);
    expect(a.threadSplits[0].atIndex).toBe(4);
    expect(a.threadSplits[0].marker).toBe('also');
  });

  it('reports the confidence the sketch states — 0.68', () => {
    // The sketch's thread panel: "Split guessed from a topic shift,
    // confidence 0.68". Its demo is a marker line with zero overlap, so zero
    // overlap has to report 0.68. This is what calibrates BASE and SPAN.
    const a = analyzeCapture(DEMO_TYPED);
    expect(a.threadSplits[0].overlap).toBe(0);
    expect(a.threadSplits[0].confidence).toBeCloseTo(0.68, 10);
  });

  it('earns every affordance by the end, typed one character at a time', () => {
    expect(affordancesOf(typeOut(DEMO_TYPED))).toEqual([
      'note',
      'source',
      'question',
      'threads',
      'commit',
    ]);
  });

  it('earns `note` first, and `commit` before the ledger lists it', () => {
    // The earned ledger's order is a TEACHING order, not a temporal one, and
    // this is the assertion that says so rather than leaving the next reader to
    // find it out. `note` genuinely comes first. But `commit` needs 18 words
    // AND any one of (source ∨ question ∨ thread), so on this capture it is
    // earned the moment the source anchors — two stages before the ledger
    // shows it. That is correct: the bar states a destination, and by then
    // there is one.
    const firstSeen: string[] = [];
    let earned = ground();
    for (let i = 0; i <= DEMO_TYPED.length; i += 1) {
      earned = stepCapture(earned, DEMO_TYPED.slice(0, i)).earned;
      for (const a of CAPTURE_AFFORDANCES) {
        if (earned[a] && !firstSeen.includes(a)) firstSeen.push(a);
      }
    }
    expect(firstSeen[0]).toBe('note');
    expect(firstSeen.indexOf('commit')).toBeLessThan(firstSeen.indexOf('question'));
    expect(firstSeen.indexOf('source')).toBeLessThan(firstSeen.indexOf('commit'));
    expect([...firstSeen].sort()).toEqual([...CAPTURE_AFFORDANCES].sort());
  });

  it('groups into two threads with the tallies the thread dock shows', () => {
    const s = stepCapture(ground(), DEMO_TYPED);
    expect(s.threads).toHaveLength(2);
    expect(s.threads[0].segments).toHaveLength(4);
    expect(s.threads[0].hasSource).toBe(true);
    expect(s.threads[0].questionCount).toBe(1);
    expect(s.threads[0].split).toBeNull();
    expect(s.threads[1].segments).toHaveLength(1);
    expect(s.threads[1].hasSource).toBe(false);
    expect(s.threads[1].questionCount).toBe(0);
    expect(s.threads[1].split?.marker).toBe('also');
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * Segmentation
 * ══════════════════════════════════════════════════════════════════════════ */

describe('segmentCapture', () => {
  it('splits on newlines and carries each segment back to its line', () => {
    const segs = segmentCapture('first line\nsecond line\nthird line');
    expect(segs.map((s) => s.text)).toEqual(['first line', 'second line', 'third line']);
    expect(segs.map((s) => s.lineIndex)).toEqual([0, 1, 2]);
    expect(segs.map((s) => s.index)).toEqual([0, 1, 2]);
  });

  it('splits on sentence-terminal punctuation WITHIN a line', () => {
    const segs = segmentCapture('The field is wet. The mow can wait. Should we amend?');
    expect(segs.map((s) => s.text)).toEqual([
      'The field is wet.',
      'The mow can wait.',
      'Should we amend?',
    ]);
    // …and every one of them still points at the single line that was typed.
    expect(segs.map((s) => s.lineIndex)).toEqual([0, 0, 0]);
  });

  it('keeps decimals and section numbers whole', () => {
    // A `.` is a stop only when whitespace or end-of-text follows it.
    expect(segmentCapture('soil ph came back 5.2 on the north field')).toHaveLength(1);
    expect(segmentCapture('per the guide section 4.1 on sulfur rates')).toHaveLength(1);
  });

  it('keeps abbreviations whole — and still stops at a real sentence end', () => {
    // Positive control alongside: the guard must not disable stopping entirely.
    expect(
      segmentCapture("i'm reading the NS Dept. of Ag lowbush guide"),
    ).toHaveLength(1);
    const both = segmentCapture("i'm reading the NS Dept. of Ag guide. It says to amend.");
    expect(both.map((s) => s.text)).toEqual([
      "i'm reading the NS Dept. of Ag guide.",
      'It says to amend.',
    ]);
  });

  it('treats a single initial as an initial, not a sentence end', () => {
    expect(segmentCapture('J. Smith called about the trailer')).toHaveLength(1);
  });

  it('does NOT swallow a sentence boundary after a word like "no"', () => {
    // The abbreviation list's entry criterion, pinned as the PAIR that shows
    // why it matters. `no.` for *number* is far rarer than "no." the answer,
    // and the cost of getting it wrong is not a stray segment — it is silence:
    // the merged segment inherits the FIRST sentence's opening ("did the…"),
    // so the question after it stops being a question at all.
    const swallowed = 'did the amendment go on last week. no. should we redo it before the mow';
    const s = analyzeCapture(swallowed);
    expect(s.segments).toHaveLength(3);
    expect(s.questions).toHaveLength(1);
    expect(s.questions[0].text).toBe('should we redo it before the mow');

    // The control that proves this is about the LIST and not about the
    // sentence: the same capture with a word that was never in it.
    const control = analyzeCapture(
      'did the amendment go on last week. nope. should we redo it before the mow',
    );
    expect(control.segments).toHaveLength(3);
    expect(control.questions).toHaveLength(1);

    // …and the entries that DO belong still suppress their boundary.
    expect(segmentCapture("i'm reading the NS Dept. of Ag guide")).toHaveLength(1);
  });

  it('keeps the abbreviation list to words that do not end sentences', () => {
    // The criterion, asserted rather than left in a docstring. `no`/`min`/`max`
    // were removed for it; re-adding one reds here and has to be argued.
    for (const banned of ['no', 'min', 'max']) {
      expect([banned, CAPTURE_SENTENCE_ABBREVIATIONS.includes(banned)]).toEqual([
        banned,
        false,
      ]);
    }
    // Positive control: the list is not simply empty.
    expect(CAPTURE_SENTENCE_ABBREVIATIONS).toContain('dept');
    // `eg`/`ie` earn their place — CHECKED, not assumed. The dotted spelling is
    // already covered by the single-initial rule, but the undotted one is not.
    expect(segmentCapture('reading the guide eg. the field is wet')).toHaveLength(1);
  });

  it('treats a run of terminal marks as one stop', () => {
    const segs = segmentCapture('the trailer is gone?! call kyle now');
    expect(segs.map((s) => s.text)).toEqual(['the trailer is gone?!', 'call kyle now']);
  });

  it('returns nothing for an empty or whitespace-only capture', () => {
    expect(segmentCapture('')).toEqual([]);
    expect(segmentCapture('   \n  \n ')).toEqual([]);
    // Positive control: one real character and it segments.
    expect(segmentCapture('  ok  ')).toHaveLength(1);
  });

  it('drops empty segments but keeps the surviving line indices honest', () => {
    const segs = segmentCapture('first\n\n\nlast');
    expect(segs.map((s) => s.text)).toEqual(['first', 'last']);
    expect(segs.map((s) => s.lineIndex)).toEqual([0, 3]);
  });
});

describe('countCaptureWords', () => {
  it('counts whitespace-delimited words across lines', () => {
    expect(countCaptureWords('one two three')).toBe(3);
    expect(countCaptureWords('one\ntwo\nthree')).toBe(3);
    expect(countCaptureWords('  spaced   out  ')).toBe(2);
    expect(countCaptureWords('')).toBe(0);
    expect(countCaptureWords('   ')).toBe(0);
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * Detector 1 — Note
 * ══════════════════════════════════════════════════════════════════════════ */

describe('detector 1 — note', () => {
  it('earns at the segment threshold', () => {
    const under = analyzeCapture('short one');
    expect(under.segmentCount).toBe(1);
    expect(advanceEarned(ground(), under).note).toBe(false);

    const at = analyzeCapture('short one\nshort two');
    expect(at.segmentCount).toBe(CAPTURE_EARN_NOTE_SEGMENTS);
    expect(advanceEarned(ground(), at).note).toBe(true);
  });

  it('earns at the word threshold inside a single segment', () => {
    const words = (n: number) => Array.from({ length: n }, (_, i) => `w${i}`).join(' ');
    const under = analyzeCapture(words(CAPTURE_EARN_NOTE_WORDS - 1));
    expect(under.segmentCount).toBe(1);
    expect(advanceEarned(ground(), under).note).toBe(false);

    const at = analyzeCapture(words(CAPTURE_EARN_NOTE_WORDS));
    expect(at.segmentCount).toBe(1);
    expect(advanceEarned(ground(), at).note).toBe(true);
  });

  it('does not count a stray keystroke as a segment', () => {
    // `CAPTURE_SEGMENT_MIN_CHARS` — two stray characters are not two thoughts.
    const a = analyzeCapture('ok\nno');
    expect(a.segments).toHaveLength(2);
    expect(a.segmentCount).toBe(0);
    expect(advanceEarned(ground(), a).note).toBe(false);
    // Positive control: the same two lines, long enough to count.
    const b = analyzeCapture('okay then\nno chance');
    expect(b.segmentCount).toBe(2);
    expect(advanceEarned(ground(), b).note).toBe(true);
  });

  it('CAPTURE_SEGMENT_MIN_CHARS is the sketch cut, written as a minimum', () => {
    // The sketch spells it `l.trim().length > 3`.
    expect(CAPTURE_SEGMENT_MIN_CHARS).toBe(4);
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * Detector 2 — Source anchor
 * ══════════════════════════════════════════════════════════════════════════ */

describe('detector 2 — source anchor', () => {
  const SOURCE_CORPUS = [
    "i'm reading the ns dept of ag lowbush guide",
    'im reading the lowbush guide again',
    'reading the sulfur amendment tables tonight',
    'per the lowbush production guide, sulfur goes on in fall',
    'from the ns ag extension bulletin',
    // …and the ones that must not anchor:
    'soil ph came back 5.2 on the north field',
    "that's down half a point from last august",
    'kyle moved the yarmouth run to thursday',
    'reading', // cue with nothing after it
    'from the', // cue with nothing after it
  ];

  it('fires on exactly what the SKETCH regex fires on', () => {
    // The sketch's `SRC_RE`, verbatim, as an INDEPENDENT oracle: the cue set is
    // the contract's, and splitting it into per-cue confidences must not have
    // changed which strings anchor.
    for (const line of SOURCE_CORPUS) {
      const mine = detectSource(segmentCapture(line)) !== null;
      expect([line, mine]).toEqual([line, SKETCH_SRC_RE.test(line)]);
    }
  });

  it('anchors the named source and says which cue found it', () => {
    const a = analyzeCapture("i'm reading the ns dept of ag lowbush guide");
    expect(a.source?.cue).toBe("i'm reading");
    expect(a.source?.title).toBe('the ns dept of ag lowbush guide');
    expect(a.source?.confidence).toBe(0.9);
  });

  it('does NOT anchor ordinary prose — with the anchoring neighbour as control', () => {
    expect(analyzeCapture('soil ph came back 5.2 on the north field').source).toBeNull();
    expect(analyzeCapture('kyle moved the run to thursday').source).toBeNull();
    expect(analyzeCapture('per the lowbush guide, sulfur goes on in fall').source).not.toBeNull();
  });

  it('credits the most specific cue, not the one nested inside it', () => {
    // "reading" is a substring of "i'm reading"; the specific one has to win or
    // the correction panel names the wrong evidence.
    expect(analyzeCapture("i'm reading the lowbush guide").source?.cue).toBe("i'm reading");
    expect(analyzeCapture('reading the lowbush guide').source?.cue).toBe('reading');
  });

  it('takes the FIRST cue in the capture, per the sketch find()', () => {
    const a = analyzeCapture(
      "i'm reading the lowbush guide\nlater i took a sample from the north pump",
    );
    expect(a.source?.cue).toBe("i'm reading");
    expect(a.source?.index).toBe(0);
  });

  it('the weak cues carry weak confidence — the guess is honest about itself', () => {
    // "from the" fires on "took it from the barn loft". It ships because the
    // sketch ships it; it ships QUIET because it is usually wrong.
    const strong = analyzeCapture("i'm reading the lowbush guide").source;
    const weak = analyzeCapture('took it from the barn loft').source;
    expect(weak).not.toBeNull();
    expect(weak!.confidence).toBeLessThan(strong!.confidence);
  });

  it('every shipped cue clears the floor — so v1 suppresses nothing', () => {
    // PREMISE PIN. The floor exists for phase 2 to move; today it must be inert,
    // and "today it is inert" is a claim that needs asserting rather than
    // stating. Lower any cue below the floor and this reds.
    for (const cue of CAPTURE_SOURCE_CUES) {
      expect([cue.phrase, cue.confidence >= CAPTURE_SOURCE_MIN_CONFIDENCE]).toEqual([
        cue.phrase,
        true,
      ]);
    }
  });

  it('the floor DOES suppress when raised — driven, not merely present', () => {
    const segs = segmentCapture('took it from the barn loft');
    expect(detectSource(segs)).not.toBeNull();
    expect(detectSource(segs, { minConfidence: 0.5 })).toBeNull();
    // Positive control: the strong cue still anchors at the same raised floor,
    // so this proves a FLOOR and not a broken detector.
    expect(
      detectSource(segmentCapture("i'm reading the lowbush guide"), { minConfidence: 0.5 }),
    ).not.toBeNull();
  });

  it('cues are ordered most-specific first', () => {
    // The order IS the behaviour (first match wins), so it is pinned.
    expect(CAPTURE_SOURCE_CUES.map((c) => c.phrase)).toEqual([
      "i'm reading",
      'per the',
      'reading',
      'from the',
    ]);
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * Detector 3 — Open questions
 * ══════════════════════════════════════════════════════════════════════════ */

describe('detector 3 — open questions', () => {
  const QUESTION_CORPUS = [
    'do we amend before the fall mow or after?',
    'question — do we amend before the fall mow',
    'q: does sulfur go on before the mow',
    'should we amend before the mow',
    'is the north field still wet',
    'why did the ph drop half a point',
    'how much sulfur per acre',
    'what rate did we use last august',
    'can kyle take the thursday run',
    'are the sample points the same',
    // …and the ones that must not:
    'soil ph came back 5.2 on the north field',
    "also kyle can't do wednesday so the run moves",
    "i'm reading the lowbush guide",
    'the mow', // under the length floor
    'why?', // under the length floor even with a mark
  ];

  it('fires on exactly what the SKETCH regex fires on', () => {
    // The sketch's `Q_RE` + its `length > 6` floor, verbatim, as an INDEPENDENT
    // oracle.
    for (const line of QUESTION_CORPUS) {
      const oracle = SKETCH_Q_RE.test(line) && line.trim().length > 6;
      expect([line, questionTriggerFor(line) !== null]).toEqual([line, oracle]);
    }
  });

  it('names WHY each one was read as a question', () => {
    expect(questionTriggerFor('do we amend before the mow?')).toBe('terminal-mark');
    expect(questionTriggerFor('question — do we amend before the mow')).toBe('explicit-lead');
    expect(questionTriggerFor('should we amend before the mow')).toBe('interrogative-lead');
  });

  it('a terminal mark outranks a lead word', () => {
    // "do we amend?" satisfies both; it is barely a guess, and must be reported
    // as the near-certainty it is.
    const t = questionTriggerFor('do we amend before the mow?');
    expect(t).toBe('terminal-mark');
    expect(CAPTURE_QUESTION_TRIGGER_CONFIDENCE[t!]).toBeGreaterThan(
      CAPTURE_QUESTION_TRIGGER_CONFIDENCE['interrogative-lead'],
    );
  });

  it('the interrogative lead is the uncertain one, and says so', () => {
    expect(CAPTURE_QUESTION_TRIGGER_CONFIDENCE['interrogative-lead']).toBeLessThan(
      CAPTURE_QUESTION_TRIGGER_CONFIDENCE['explicit-lead'],
    );
    // It is a real guess with real false positives — kept, because it is how
    // this operator writes, and one tap clears it.
    expect(questionTriggerFor('do not forget the fall mow')).toBe('interrogative-lead');
  });

  it('does NOT fire on ordinary lines — with a question as the control', () => {
    const a = analyzeCapture(
      'soil ph came back 5.2 on the north field\nshould we amend before the mow',
    );
    expect(a.questions).toHaveLength(1);
    expect(a.questions[0].text).toBe('should we amend before the mow');
  });

  it('ignores anything under the length floor', () => {
    expect(questionTriggerFor('why?')).toBeNull();
    expect('why?'.length).toBeLessThan(CAPTURE_QUESTION_MIN_CHARS);
    // Positive control: one character over and it fires.
    expect(questionTriggerFor('why so?')).toBe('terminal-mark');
    expect('why so?'.length).toBe(CAPTURE_QUESTION_MIN_CHARS);
  });

  it('carries the machine-legible flag on EVERY question', () => {
    // Q1's ruling: an entry carrying an explicit flag, "not prose that merely
    // ends in a question mark". The flag is what the later upgrade to real
    // `question` records reads.
    const qs = detectQuestions(
      segmentCapture('should we amend before the mow\ndo we mow first?'),
    );
    expect(qs).toHaveLength(2);
    for (const q of qs) expect(q.flag).toBe(CAPTURE_QUESTION_FLAG);
  });

  it('marks each question on the LINE the page renders', () => {
    // Two questions inside one typed line still need two gutter marks on that
    // one line.
    const qs = detectQuestions(segmentCapture('should we amend? do we mow first?'));
    expect(qs).toHaveLength(2);
    expect(qs.map((q) => q.lineIndex)).toEqual([0, 0]);
    expect(qs.map((q) => q.index)).toEqual([0, 1]);
  });

  it('formats the Open questions section with the flag on every line', () => {
    const qs = detectQuestions(
      segmentCapture('should we amend before the mow\ndo we mow first?'),
    );
    expect(formatOpenQuestionsSection(qs)).toBe(
      `${CAPTURE_OPEN_QUESTIONS_HEADING}\n` +
        '\n' +
        `- [ ] should we amend before the mow <!-- ${CAPTURE_QUESTION_FLAG} -->\n` +
        `- [ ] do we mow first? <!-- ${CAPTURE_QUESTION_FLAG} -->`,
    );
  });

  it('emits no section when there are no questions', () => {
    expect(formatOpenQuestionsSection([])).toBeNull();
    // The DATA shape is where "ran, found nothing" has to be legible, and it is:
    expect(analyzeCapture('soil ph came back 5.2').questions).toEqual([]);
  });

  it('the heading matches the one the digest already writes', () => {
    // Sampled from `src/alfred/digest/writer.py`, not minted here.
    expect(CAPTURE_OPEN_QUESTIONS_HEADING).toBe('## Open questions');
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * Detector 4 — Thread split
 * ══════════════════════════════════════════════════════════════════════════ */

describe('detector 4 — thread split', () => {
  const FIELD_RUN = [
    'soil ph came back 5.2 on the north field',
    "that's down half a point from last august, same sample points",
  ].join('\n');

  it('splits on a discourse marker that changes the subject', () => {
    const a = analyzeCapture(
      `${FIELD_RUN}\nalso kyle can't do wednesday so the yarmouth run moves to thursday`,
    );
    expect(a.threadSplits).toHaveLength(1);
    expect(a.threadSplits[0].marker).toBe('also');
    expect(a.threadSplits[0].origin).toBe('detected');
  });

  it('does NOT split on a marker that CONTINUES the subject', () => {
    // The negative control the whole heuristic turns on. "also" is there, the
    // vocabulary is not new, so it is the operator adding to what they were
    // already saying — which is not a thread.
    const s = stateOf(`${FIELD_RUN}\nalso the north field ph needs another sample`);
    expect(s.analysis.threadSplits).toEqual([]);
    expect(s.threadCount).toBe(1);
    expect(s.threads).toHaveLength(1);
  });

  it('does NOT split on new vocabulary with no marker', () => {
    // Consecutive sentences about one subject routinely share no content words.
    // Overlap alone would report a new thread per sentence.
    const s = stateOf(`${FIELD_RUN}\nkyle can't do wednesday so the yarmouth run moves to thursday`);
    expect(s.analysis.threadSplits).toEqual([]);
    expect(s.threadCount).toBe(1);
    expect(s.threads).toHaveLength(1);
  });

  it('needs BOTH conditions — proven by flipping each one alone', () => {
    const marker = "also kyle can't do wednesday so the yarmouth run moves to thursday";
    const noMarker = "kyle can't do wednesday so the yarmouth run moves to thursday";
    const markerHighOverlap = 'also the north field ph needs another sample';
    expect(stateOf(`${FIELD_RUN}\n${marker}`).threadCount).toBe(2);
    expect(stateOf(`${FIELD_RUN}\n${noMarker}`).threadCount).toBe(1);
    expect(stateOf(`${FIELD_RUN}\n${markerHighOverlap}`).threadCount).toBe(1);
  });

  it('never splits at the first segment — there is nothing to shift away from', () => {
    const s = stateOf('also kyle moved the yarmouth run to thursday');
    expect(s.analysis.threadSplits).toEqual([]);
    expect(s.threadCount).toBe(1);
  });

  it('needs enough content words for the overlap to mean anything', () => {
    const a = analyzeCapture(`${FIELD_RUN}\nalso ok`);
    expect(contentWords('ok').length).toBeLessThan(CAPTURE_THREAD_MIN_CONTENT_WORDS);
    expect(a.threadSplits).toEqual([]);
    // Positive control: the same marker with enough new words does split.
    expect(
      stateOf(`${FIELD_RUN}\nalso kyle moved the yarmouth trailer`).threadCount,
    ).toBe(2);
  });

  it('counts a marker only at a word boundary', () => {
    expect(threadMarkerFor('also kyle moved the run')).toBe('also');
    expect(threadMarkerFor('alsop said the field is wet')).toBeNull();
  });

  it('credits the longest matching marker phrase', () => {
    expect(threadMarkerFor('on another note, the trailer needs tires')).toBe(
      'on another note',
    );
    expect(threadMarkerFor('on a different note, the trailer needs tires')).toBe(
      'on a different note',
    );
  });

  it('confidence falls as overlap rises, between the named bounds', () => {
    const zero = analyzeCapture(
      `${FIELD_RUN}\nalso kyle can't do wednesday so the yarmouth run moves to thursday`,
    ).threadSplits[0];
    expect(zero.overlap).toBe(0);
    expect(zero.confidence).toBeCloseTo(
      CAPTURE_THREAD_CONFIDENCE_BASE + CAPTURE_THREAD_CONFIDENCE_SPAN,
      10,
    );

    // A marker line sharing ONE content word of six — inside the ceiling, so it
    // still splits, but less certainly.
    const some = analyzeCapture(
      `${FIELD_RUN}\nalso the field trailer needs new tires thursday`,
    ).threadSplits[0];
    expect(some).toBeDefined();
    expect(some.overlap).toBeGreaterThan(0);
    expect(some.overlap).toBeLessThanOrEqual(CAPTURE_THREAD_OVERLAP_CEILING);
    expect(some.confidence).toBeLessThan(zero.confidence);
    expect(some.confidence).toBeGreaterThanOrEqual(CAPTURE_THREAD_CONFIDENCE_BASE);
  });

  it('function words are not shared subject matter', () => {
    // Counting "the" and "is" as overlap would push every segment over the
    // ceiling and silently disable the detector.
    expect(contentWords('the run is on the north field')).toEqual([
      'run',
      'north',
      'field',
    ]);
  });

  it('groups segments into the threads it guessed', () => {
    const text = `${FIELD_RUN}\nalso kyle moved the yarmouth run to thursday`;
    const a = analyzeCapture(text);
    const threads = groupThreads(a.segments, a.threadSplits, a.source, a.questions);
    expect(threads).toHaveLength(2);
    expect(threads[0].segments).toHaveLength(2);
    expect(threads[1].segments).toHaveLength(1);
    expect(threads[1].split?.atIndex).toBe(2);
    expect(threads[0].words).toBe(9 + 11);
  });

  it('one thread is the absence of a guess, not a guess of one', () => {
    const a = analyzeCapture('soil ph came back 5.2 on the north field');
    expect(a.threadSplits).toEqual([]);
    const threads = groupThreads(a.segments, a.threadSplits, a.source, a.questions);
    expect(threads[0].split).toBeNull();
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * Detector 5 — Commit bar
 * ══════════════════════════════════════════════════════════════════════════ */

describe('detector 5 — commit bar', () => {
  const words = (n: number) => Array.from({ length: n }, (_, i) => `word${i}`).join(' ');

  it('needs the word floor AND some shape', () => {
    const shapeless = analyzeCapture(words(CAPTURE_EARN_COMMIT_WORDS + 5));
    expect(shapeless.words).toBeGreaterThanOrEqual(CAPTURE_EARN_COMMIT_WORDS);
    expect(shapeless.source).toBeNull();
    expect(shapeless.questions).toEqual([]);
    expect(shapeless.threadSplits).toEqual([]);
    expect(advanceEarned(ground(), shapeless).commit).toBe(false);
  });

  it('earns once shape arrives — each of the three kinds on its own', () => {
    const pad = words(CAPTURE_EARN_COMMIT_WORDS);
    const withSource = analyzeCapture(`${pad}\ni'm reading the lowbush guide`);
    const withQuestion = analyzeCapture(`${pad}\nshould we amend before the mow`);
    expect(advanceEarned(ground(), withSource).commit).toBe(true);
    expect(advanceEarned(ground(), withQuestion).commit).toBe(true);
  });

  it('refuses a fragment that has shape but not enough of it', () => {
    const short = analyzeCapture("i'm reading the lowbush guide");
    expect(short.source).not.toBeNull();
    expect(short.words).toBeLessThan(CAPTURE_EARN_COMMIT_WORDS);
    expect(advanceEarned(ground(), short).commit).toBe(false);
    // Positive control: pad the SAME capture past the floor and it commits.
    const padded = analyzeCapture(`i'm reading the lowbush guide ${words(CAPTURE_EARN_COMMIT_WORDS)}`);
    expect(advanceEarned(ground(), padded).commit).toBe(true);
  });
});

describe('deriveCaptureTitle', () => {
  it('takes the first segment, without its sentence punctuation', () => {
    expect(deriveCaptureTitle(DEMO_TYPED)).toBe('soil ph came back 5.2 on the north field');
    expect(deriveCaptureTitle('should we amend before the mow?\nmore text')).toBe(
      'should we amend before the mow',
    );
  });

  it('cuts to the transport title bound', () => {
    const long = 'x'.repeat(CAPTURE_TITLE_MAX_CHARS + 50);
    expect(deriveCaptureTitle(long)).toHaveLength(CAPTURE_TITLE_MAX_CHARS);
  });

  it('is empty for an empty capture rather than inventing one', () => {
    expect(deriveCaptureTitle('')).toBe('');
    expect(deriveCaptureTitle('   ')).toBe('');
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * The latch
 * ══════════════════════════════════════════════════════════════════════════ */

describe('the latch — once earned it STAYS earned', () => {
  it('does not blink an affordance off when the raw criteria stop holding', () => {
    // The sketch's own words: "The interface must never flicker its own
    // complexity away between keystrokes." THIS is the pin the design asks for.
    let earned = advanceEarned(ground(), analyzeCapture("i'm reading the lowbush guide"));
    expect(earned.source).toBe(true);
    // Now the operator keeps typing and the cue is no longer the live text's
    // first line… the raw analysis of THIS text finds no cue at all.
    const after = analyzeCapture('the north field is wet');
    expect(after.source).toBeNull();
    earned = advanceEarned(earned, after);
    expect(earned.source).toBe(true);
  });

  it('holds every affordance across a keystroke that earns nothing', () => {
    const full = advanceEarned(ground(), analyzeCapture(DEMO_TYPED));
    expect(affordancesOf(full)).toHaveLength(5);
    const nothing = analyzeCapture('x');
    expect(analyzeCapture('x').source).toBeNull();
    expect(affordancesOf(advanceEarned(full, nothing))).toHaveLength(5);
  });

  it('latches the thread DOCK but lets the thread TALLY follow the text', () => {
    // FLIPPED, with the reversed premise recorded rather than the old
    // assertion deleted. This used to assert that the count never falls, and
    // that was the defect: the count latched monotonically while the dock was
    // rebuilt from the live text every pass, so deleting the line that started
    // the second thread reported "◆ 2 threads" over a dock holding one.
    //
    // What the sketch's rule actually protects is the AFFORDANCE — the dock
    // must not vanish. A tally that follows the operator's own deletion is not
    // the interface flickering its complexity away; it is the interface
    // telling the truth about what is on screen.
    const two = stepCapture(ground(), DEMO_TYPED);
    expect(two.threadCount).toBe(2);
    const cut = stepCapture(two.earned, SKETCH_DEMO_SCRIPT.slice(0, 4).join('\n'));
    expect(cut.earned.threads).toBe(true); // the dock stays
    expect(cut.threadCount).toBe(1); // …reporting what is actually in it
    expect(cut.threads).toHaveLength(1);
  });

  it('RETURNS TO GROUND STATE on a genuinely empty capture', () => {
    // "Only a genuinely empty capture returns the surface to ground state." The
    // one that will regress silently, so it is pinned in both directions.
    const full = advanceEarned(ground(), analyzeCapture(DEMO_TYPED));
    expect(affordancesOf(full)).toHaveLength(5);
    expect(advanceEarned(full, analyzeCapture(''))).toEqual(ground());
    expect(advanceEarned(full, analyzeCapture('   \n  '))).toEqual(ground());
  });

  it('does NOT return to ground on one surviving character', () => {
    // The boundary, from the other side: near-empty is not empty. Without this
    // the ground-state pin above passes just as well against a latch that
    // resets on ANY short capture.
    const full = advanceEarned(ground(), analyzeCapture(DEMO_TYPED));
    expect(analyzeCapture('a').empty).toBe(false);
    expect(affordancesOf(advanceEarned(full, analyzeCapture('a')))).toHaveLength(5);
  });

  it('an emptied capture forgets the operator corrections too', () => {
    // There is nothing left for them to be about, and a stale dismissal would
    // silently suppress the NEXT capture's source.
    const corrected = correctCapture(
      advanceEarned(ground(), analyzeCapture(DEMO_TYPED)),
      { id: 'not_a_source' },
      analyzeCapture(DEMO_TYPED),
    );
    expect(corrected.sourceDismissed).toBe(true);
    expect(advanceEarned(corrected, analyzeCapture(''))).toEqual(ground());
  });

  it('is a plain serialisable value, so a reload cannot un-earn it', () => {
    // The draft persists this. A Set or a class would survive JSON as `{}`.
    const full = advanceEarned(ground(), analyzeCapture(DEMO_TYPED));
    const roundTripped = JSON.parse(JSON.stringify(full));
    expect(roundTripped).toEqual(full);
    expect(advanceEarned(roundTripped, analyzeCapture('x'))).toEqual(
      advanceEarned(full, analyzeCapture('x')),
    );
  });

  it('holds through a character-by-character retype of the whole demo', () => {
    // Nothing may go off at ANY prefix once it has come on.
    //
    // THIS COMMENT USED TO SAY THIS PIN WAS WEAK — that it passed even with
    // the latch removed, because typing forward the raw criteria happened to
    // be monotone. That was measured and true of the ORIGINAL build, and it is
    // now false, so it is corrected here rather than only in a commit message
    // nobody reading this line will look up.
    //
    // What changed: the thread tally used to latch monotonically inside
    // `advanceEarned`, which hid the detector's own non-monotonicity from the
    // affordance flag. Removing that latch (it was the WARN-2 defect) exposed
    // a real mid-word flicker, and this pin now catches it. Measured, at
    // character 250 of this capture:
    //
    //     "…also kyle ca"   → content after the marker is [kyle, ca] = 2 → SPLIT
    //     "…also kyle can"  → "can" is a STOPWORD, content = [kyle] = 1 → none
    //
    // One keystroke, and the guess evaporates mid-word. That is the sketch's
    // "the raw criteria momentarily stop holding" happening for real rather
    // than quoted, and it is what the affordance latch absorbs.
    let earned = ground();
    let high = 0;
    for (let i = 0; i <= DEMO_TYPED.length; i += 1) {
      earned = stepCapture(earned, DEMO_TYPED.slice(0, i)).earned;
      const n = affordancesOf(earned).length;
      expect(n).toBeGreaterThanOrEqual(high);
      high = n;
    }
    expect(high).toBe(5);
  });

  it('holds while the capture is DELETED back down to one character', () => {
    // The flicker bug's real shape, and the sketch's own stated reason for the
    // latch: the raw criteria "momentarily stop holding" and a naive re-test
    // blinks the indicators off. Backspacing is where that happens for real —
    // every affordance's evidence disappears in turn — and everything must
    // stay lit until the screen is genuinely empty.
    let earned = advanceEarned(ground(), analyzeCapture(DEMO_TYPED));
    expect(affordancesOf(earned)).toHaveLength(5);
    for (let i = DEMO_TYPED.length; i >= 1; i -= 1) {
      const text = DEMO_TYPED.slice(0, i);
      earned = stepCapture(earned, text).earned;
      expect([text.length, affordancesOf(earned).length]).toEqual([text.length, 5]);
    }
    // …and only the empty screen resets it.
    expect(advanceEarned(earned, analyzeCapture(''))).toEqual(ground());
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * Corrections — the operator's answer outranks the detector AND the latch
 * ══════════════════════════════════════════════════════════════════════════ */

describe('corrections', () => {
  const sourceText = "i'm reading the lowbush guide\nsoil ph came back 5.2 on the field";
  const threadText = `soil ph came back 5.2 on the north field
that's down half a point from last august, same sample points
also kyle can't do wednesday so the yarmouth run moves to thursday`;

  it('“Not a source →” un-earns the anchor AND survives the next keystroke', () => {
    // The whole point. Latching would otherwise re-earn it immediately and the
    // button would visibly do nothing.
    let earned = advanceEarned(ground(), analyzeCapture(sourceText));
    expect(earned.source).toBe(true);
    earned = correctCapture(earned, { id: 'not_a_source' }, analyzeCapture(sourceText));
    expect(earned.source).toBe(false);
    earned = advanceEarned(earned, analyzeCapture(`${sourceText} and more`));
    expect(earned.source).toBe(false);
  });

  it('“It’s one thought →” collapses the split and survives re-analysis', () => {
    const before = stepCapture(ground(), threadText);
    expect(before.threadCount).toBe(2);
    const earned = correctCapture(before.earned, { id: 'one_thought' }, before.analysis);
    const after = stepCapture(earned, `${threadText}\n${UNRELATED_LINE}`);
    expect(after.threadCount).toBe(1);
    expect(after.threads).toHaveLength(1);
    expect(after.splits).toEqual([]);
    // The DOCK stays earned — the operator collapsed its contents, they did
    // not un-earn the affordance.
    expect(after.earned.threads).toBe(true);
  });

  it('“It’s one thought →” collapses only what is SHOWING, not threads forever', () => {
    // A blanket "no threads" latch would mean a capture that later moves to a
    // genuinely new subject could never offer a split again — the correction
    // silently becomes a permanent setting.
    const before = stepCapture(ground(), threadText);
    const earned = correctCapture(before.earned, { id: 'one_thought' }, before.analysis);
    expect(stepCapture(earned, threadText).threadCount).toBe(1);
    // …and a NEW topic shift, further down, is still offered.
    const grown = `${threadText}\nseparately the trailer needs new tires and an inspection`;
    const after = stepCapture(earned, grown);
    expect(after.threadCount).toBe(2);
    expect(after.threads).toHaveLength(2);
    expect(after.splits[0].marker).toBe('separately');
  });

  it('the MISSED-split inverse forces a split the detector did not offer', () => {
    // A loop fed only "you were wrong to fire" learns to fire less and never
    // learns to fire more.
    const quiet = 'soil ph came back 5.2\nkyle moved the yarmouth run to thursday';
    const before = stepCapture(ground(), quiet);
    expect(before.earned.threads).toBe(false);
    expect(before.threadCount).toBe(1);

    // IT CARRIES THE SEGMENT. "There is a split you missed" is not a complete
    // instruction — the page cannot place a boundary it was never told the
    // position of, and guessing one and then attributing the guess to the
    // operator is worse than not offering the correction at all. Same rule
    // that keeps `is_a_source` out of the applicable set.
    const earned = correctCapture(
      before.earned,
      { id: 'separate_threads', text: 'kyle moved the yarmouth run to thursday' },
      before.analysis,
    );
    const after = stepCapture(earned, quiet);
    expect(after.earned.threads).toBe(true);
    expect(after.threadCount).toBe(2);
    expect(after.threads).toHaveLength(2);
    expect(after.threads[1].segments[0].text).toBe('kyle moved the yarmouth run to thursday');
    expect(after.splits[0].origin).toBe('operator');
    // Not dressed up as a guess it never made.
    expect(after.splits[0].marker).toBeNull();
    // …and it survives typing elsewhere.
    expect(stepCapture(earned, `${quiet}\n${UNRELATED_LINE}`).threadCount).toBe(2);
  });

  it('an asserted split whose segment is deleted is dropped, not carried', () => {
    const quiet = 'soil ph came back 5.2\nkyle moved the yarmouth run to thursday';
    const before = stepCapture(ground(), quiet);
    const earned = correctCapture(
      before.earned,
      { id: 'separate_threads', text: 'kyle moved the yarmouth run to thursday' },
      before.analysis,
    );
    expect(stepCapture(earned, quiet).threadCount).toBe(2);
    const cut = stepCapture(earned, 'soil ph came back 5.2');
    expect(cut.threadCount).toBe(1);
    expect(cut.threads).toHaveLength(1);
  });

  it('a dismissed question stays dismissed while the rest still mark', () => {
    const text = 'do not forget the fall mow\nshould we amend before the mow';
    let earned = advanceEarned(ground(), analyzeCapture(text));
    expect(stepCapture(earned, text).questions).toHaveLength(2);
    earned = correctCapture(earned, { id: 'not_a_question', text: 'do not forget the fall mow' }, analyzeCapture(text));
    const after = stepCapture(earned, text);
    expect(after.questions).toHaveLength(1);
    expect(after.questions[0].text).toBe('should we amend before the mow');
  });

  it('the operator can mark a question the detector missed', () => {
    const text = 'soil ph came back 5.2 on the north field\nthe mow date is not settled';
    let earned = advanceEarned(ground(), analyzeCapture(text));
    expect(stepCapture(earned, text).questions).toEqual([]);
    earned = correctCapture(earned, { id: 'is_a_question', text: 'the mow date is not settled' }, analyzeCapture(text));
    const after = stepCapture(earned, text);
    expect(after.questions).toHaveLength(1);
    expect(after.questions[0].trigger).toBe('operator');
    expect(after.questions[0].confidence).toBe(1);
    expect(after.questions[0].flag).toBe(CAPTURE_QUESTION_FLAG);
    expect(after.questions[0].lineIndex).toBe(1);
    expect(after.earned.question).toBe(true);
  });

  it('an asserted question whose line is deleted is dropped, not carried', () => {
    const text = 'soil ph came back 5.2\nthe mow date is not settled';
    let earned = advanceEarned(ground(), analyzeCapture(text));
    earned = correctCapture(earned, { id: 'is_a_question', text: 'the mow date is not settled' }, analyzeCapture(text));
    expect(stepCapture(earned, text).questions).toHaveLength(1);
    // The operator deletes that line — which un-asks it, exactly as deleting a
    // detected question does.
    expect(stepCapture(earned, 'soil ph came back 5.2').questions).toEqual([]);
  });

  it('the two question corrections cancel each other rather than stacking', () => {
    const text = 'should we amend before the mow';
    let earned = advanceEarned(ground(), analyzeCapture(text));
    earned = correctCapture(earned, { id: 'not_a_question', text }, analyzeCapture(text));
    expect(stepCapture(earned, text).questions).toEqual([]);
    earned = correctCapture(earned, { id: 'is_a_question', text }, analyzeCapture(text));
    expect(stepCapture(earned, text).questions).toHaveLength(1);
  });

  it('keys a question by its text, so edits ELSEWHERE do not revive it', () => {
    const dismissed = 'do not forget the fall mow';
    let earned = advanceEarned(ground(), analyzeCapture(dismissed));
    earned = correctCapture(earned, { id: 'not_a_question', text: dismissed }, analyzeCapture(dismissed));
    const grown = `${dismissed}\nsoil ph came back 5.2 on the north field`;
    expect(stepCapture(earned, grown).questions).toEqual([]);
    expect(normalizeSegmentKey('  Do Not   Forget the Fall Mow ')).toBe(dismissed);
  });

  it('a correction never un-earns the commit bar', () => {
    // The bar is a readiness statement, not a guess. Blinking it away
    // mid-capture is the flicker the latch exists to prevent.
    const text = `i'm reading the lowbush guide ${Array.from({ length: 20 }, (_, i) => `w${i}`).join(' ')}`;
    let earned = advanceEarned(ground(), analyzeCapture(text));
    expect(earned.commit).toBe(true);
    earned = correctCapture(earned, { id: 'not_a_source' }, analyzeCapture(text));
    expect(advanceEarned(earned, analyzeCapture(text)).commit).toBe(true);
  });

  it('a dismissed source cannot NEWLY earn a commit bar', () => {
    // The other direction of the same rule: what was corrected away is not
    // evidence any more.
    const short = "i'm reading the lowbush guide";
    let earned = advanceEarned(ground(), analyzeCapture(short));
    expect(earned.commit).toBe(false);
    earned = correctCapture(earned, { id: 'not_a_source' }, analyzeCapture(short));
    const padded = `${short} ${Array.from({ length: 20 }, (_, i) => `w${i}`).join(' ')}`;
    expect(advanceEarned(earned, analyzeCapture(padded)).commit).toBe(false);
    // Positive control: the SAME padded text without the correction commits.
    expect(advanceEarned(ground(), analyzeCapture(padded)).commit).toBe(true);
  });

  it('dismissing the last question keeps the INDICATOR but empties the gutter', () => {
    // `earned.question` latches ON and never falls — deliberate, so the
    // indicator does not blink away mid-capture. The consequence lane 2 has to
    // know: that boolean is the indicator's RIGHT TO EXIST, never its count.
    // Read it as the count and this capture reports "? 1 open" over an empty
    // gutter.
    const text = 'should we amend before the mow';
    const before = stepCapture(ground(), text);
    expect(before.questions).toHaveLength(1);
    expect(before.earned.question).toBe(true);

    const earned = correctCapture(
      before.earned,
      { id: 'not_a_question', text },
      before.analysis,
    );
    const after = stepCapture(earned, text);
    expect(after.questions).toEqual([]); // ← what lane 2 must count
    expect(after.earned.question).toBe(true); // ← what it must NOT count
  });

  it('the correction vocabulary covers both directions of every guess', () => {
    expect([...CAPTURE_CORRECTION_IDS].sort()).toEqual([
      'is_a_question',
      'is_a_source',
      'not_a_question',
      'not_a_source',
      'one_thought',
      'separate_threads',
    ]);
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * Correction pairs — asserted at BOTH ends, from one table
 * ══════════════════════════════════════════════════════════════════════════ */

const SPLIT_TEXT = `soil ph came back 5.2 on the north field
that's down half a point from last august, same sample points
also kyle can't do wednesday so the yarmouth run moves to thursday`;
const QUIET_TEXT = 'soil ph came back 5.2\nkyle moved the yarmouth run to thursday';
const QUESTION_TEXT = 'should we amend before the mow';
const STATEMENT_TEXT = 'soil ph came back 5.2\nthe mow date is not settled';

/**
 * WHY THIS IS A TABLE AND NOT FOUR HAND-WRITTEN TESTS.
 *
 * The defect this guards was a relation asserted at the direction where it
 * holds and left unasserted at the direction where it breaks: `one_thought`
 * had its collection pinned, `separate_threads` had only its tally, and the
 * disagreement lived in the half nobody wrote an assertion for. Hand-writing
 * each half is precisely what lets the halves differ — so both ends of both
 * pairs come out of one row, and a new pair cannot be added half-covered.
 *
 * The SOURCE pair is deliberately absent, with its own test below. Membership
 * here would prove wiring, not fitness: `is_a_source` has no applicable
 * direction, so its row would have to be special-cased into meaninglessness.
 */
const CORRECTION_PAIRS = [
  {
    name: 'threads',
    read: (s: ReturnType<typeof stateOf>) => s.threadCount,
    deny: { text: SPLIT_TEXT, before: 2, after: 1, correction: { id: 'one_thought' } as CaptureCorrection },
    affirm: {
      text: QUIET_TEXT,
      before: 1,
      after: 2,
      correction: {
        id: 'separate_threads',
        text: 'kyle moved the yarmouth run to thursday',
      } as CaptureCorrection,
    },
  },
  {
    name: 'questions',
    read: (s: ReturnType<typeof stateOf>) => s.questions.length,
    deny: {
      text: QUESTION_TEXT,
      before: 1,
      after: 0,
      correction: { id: 'not_a_question', text: QUESTION_TEXT } as CaptureCorrection,
    },
    affirm: {
      text: STATEMENT_TEXT,
      before: 0,
      after: 1,
      correction: {
        id: 'is_a_question',
        text: 'the mow date is not settled',
      } as CaptureCorrection,
    },
  },
];

describe.each(CORRECTION_PAIRS)('the $name correction pair', (pair) => {
  it.each([
    ['deny', pair.deny],
    ['affirm', pair.affirm],
  ])('moves the reported value in the %s direction', (_dir, leg) => {
    const before = stepCapture(ground(), leg.text);
    expect(pair.read(before)).toBe(leg.before);
    const earned = correctCapture(before.earned, leg.correction, before.analysis);
    const after = stepCapture(earned, leg.text);
    expect(pair.read(after)).toBe(leg.after);
    // …and survives typing ELSEWHERE rather than being re-earned away. The
    // suffix is a new line, deliberately: appending to the corrected segment
    // itself RELEASES the correction (see the self-cleaning test below), so
    // asserting survival there would assert the opposite of the contract.
    expect(pair.read(stepCapture(earned, `${leg.text}\n${UNRELATED_LINE}`))).toBe(leg.after);
  });

  it('is RELEASED when the corrected segment itself is rewritten', () => {
    // The other half of keying by text, and the half that looks like a bug
    // until it is stated: a correction is about the words it was made about.
    // Rewrite them and it is a different sentence, so the detector gets to
    // guess again. Deleting the segment drops it entirely.
    const leg = pair.deny;
    const before = stepCapture(ground(), leg.text);
    const earned = correctCapture(before.earned, leg.correction, before.analysis);
    expect(pair.read(stepCapture(earned, leg.text))).toBe(leg.after);
    expect(pair.read(stepCapture(earned, `${leg.text} and monday`))).toBe(leg.before);
  });
});

describe('reported-shape invariant', () => {
  const EVERY_LATCHABLE: CaptureCorrection[] = [
    { id: 'not_a_source' },
    { id: 'one_thought' },
    { id: 'separate_threads', text: "also kyle can't do wednesday so the yarmouth run moves to thursday" },
    { id: 'not_a_question', text: 'question — do we amend before the fall mow or after?' },
    { id: 'is_a_question', text: 'soil ph came back 5.2 on the north field' },
  ];

  it.each(EVERY_LATCHABLE.map((c) => [c.id, c] as const))(
    'after %s, the tally still equals the collection',
    (_id, correction) => {
      // THE ONE ASSERTION THAT WOULD HAVE CAUGHT THE ORIGINAL DEFECT, and it
      // is cheap enough to make on every correction rather than on the one
      // somebody remembered. "◆ N threads" over a dock holding M is a lie
      // whichever correction produced it.
      const before = stepCapture(ground(), DEMO_TYPED);
      expect(before.threadCount).toBe(before.threads.length);
      const earned = correctCapture(before.earned, correction, before.analysis);
      const after = stepCapture(earned, DEMO_TYPED);
      expect(after.threadCount).toBe(after.threads.length);
      expect(after.splits).toHaveLength(Math.max(0, after.threads.length - 1));
    },
  );

  it('holds while the demo is typed one character at a time', () => {
    let earned = ground();
    for (let i = 0; i <= DEMO_TYPED.length; i += 1) {
      const s = stepCapture(earned, DEMO_TYPED.slice(0, i));
      expect([i, s.threadCount]).toEqual([i, s.threads.length]);
      earned = s.earned;
    }
  });

  it('the SOURCE pair is one-directional on purpose', () => {
    // The declared exception, with its rationale AT the assertion so the next
    // reader meets the decision instead of rediscovering the argument.
    // `is_a_source` is loggable but not applicable: a source TITLE is a
    // SUBSTRING of a segment, not a segment, so naming one needs a picker that
    // is not in v1. Every applicable correction can be completed by naming a
    // segment — that is the rule, and this is the case it excludes.
    expect(CAPTURE_CORRECTION_IDS).toContain('is_a_source');
    const applicable = EVERY_LATCHABLE.map((c) => c.id);
    expect(applicable).not.toContain('is_a_source');
    // Positive control: its INVERSE is applicable, so this is a statement about
    // that one direction and not about the source detector being unreachable.
    expect(applicable).toContain('not_a_source');
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * The seam — the detectors cannot learn where the text came from
 * ══════════════════════════════════════════════════════════════════════════ */

describe('the origin seam', () => {
  it('TAKES A STRING AND NOTHING ELSE — the claim itself, asserted', () => {
    // The whole by-construction argument rests on this signature, and nothing
    // was asserting it. `detectSource` already takes a second `opts`
    // parameter, so a future editor adding one here would not look out of
    // place — and the moment `analyzeCapture` can see where text came from,
    // dictated and typed captures can drift apart with every test still green.
    expect(analyzeCapture).toHaveLength(1);

    // Arity alone is not enough: an OPTIONAL second parameter keeps
    // `.length === 1`. So also prove that a second argument changes nothing.
    const sneak = analyzeCapture as (t: string, o?: unknown) => ReturnType<typeof analyzeCapture>;
    expect(sneak(DEMO_TYPED, { origin: 'dictation' })).toEqual(analyzeCapture(DEMO_TYPED));
    expect(sneak(DEMO_TYPED, { origin: 'typed' })).toEqual(sneak(DEMO_TYPED, { origin: 'dictation' }));
  });

  it('is pure — the same string always analyses the same way', () => {
    expect(analyzeCapture(DEMO_TYPED)).toEqual(analyzeCapture(DEMO_TYPED));
  });

  it('remembers nothing between captures', () => {
    // Interleaved, so a module-level accumulator would show up as a difference.
    const a1 = analyzeCapture(DEMO_TYPED);
    analyzeCapture('a completely different capture about the trailer');
    analyzeCapture('');
    const a2 = analyzeCapture(DEMO_TYPED);
    expect(a2).toEqual(a1);
  });

  it('the analysis names no origin — there is nothing for a consumer to branch on', () => {
    const keys = Object.keys(analyzeCapture(DEMO_TYPED));
    for (const k of keys) {
      expect([k, /origin|dictat|voice|typed|stt|spoken/i.test(k)]).toEqual([k, false]);
    }
  });

  it('PARITY: a dictated capture earns exactly what the typed one earns', () => {
    // §0's positive control, at this lane's altitude. The dictated shape is the
    // same words as ONE space-joined run — what `useComposerText`'s append rule
    // produces — carrying the sentence punctuation an STT emits.
    const dictated =
      'Soil pH came back 5.2 on the north field. ' +
      "That's down half a point from last August, same sample points. " +
      "I'm reading the NS Dept of Ag lowbush guide, section 4 on sulfur amendment rates. " +
      'Question — do we amend before the fall mow or after? ' +
      "Also Kyle can't do Wednesday so the Yarmouth run moves to Thursday.";
    expect(dictated).not.toContain('\n');

    const typed = analyzeCapture(DEMO_TYPED);
    const spoken = analyzeCapture(dictated);
    expect(spoken.segments).toHaveLength(typed.segments.length);
    expect(spoken.words).toBe(typed.words);
    expect(spoken.questions).toHaveLength(typed.questions.length);
    expect(spoken.source).not.toBeNull();
    expect(stateOf(dictated).threadCount).toBe(stateOf(DEMO_TYPED).threadCount);
    expect(affordancesOf(advanceEarned(ground(), spoken))).toEqual(
      affordancesOf(advanceEarned(ground(), typed)),
    );
  });

  it('KNOWN LIMIT: an UNPUNCTUATED transcript does not reach parity', () => {
    // Stated rather than discovered. No segmenter can recover boundaries the
    // speaker never uttered, so the fix is not here: lane 2 must append each
    // transcript as its OWN LINE instead of space-joining it onto the previous
    // one. This pin is what makes that a known boundary rather than a surprise
    // in the field — if a later change DOES reach it, this test reds and the
    // limit gets deleted deliberately.
    const unpunctuated = SKETCH_DEMO_SCRIPT.join(' ').replace(/\?/g, '');
    const spoken = analyzeCapture(unpunctuated);
    const typed = analyzeCapture(DEMO_TYPED);
    expect(spoken.words).toBe(typed.words);
    expect(spoken.segments).toHaveLength(1);
    expect(stateOf(unpunctuated).threadCount).toBe(1);
    expect(spoken.questions).toEqual([]);

    // …and the remedy, proven: the same unpunctuated utterances, one per line.
    const perLine = analyzeCapture(SKETCH_DEMO_SCRIPT.join('\n').replace(/\?/g, ''));
    expect(perLine.segments).toHaveLength(5);
    expect(stateOf(SKETCH_DEMO_SCRIPT.join('\n').replace(/\?/g, '')).threadCount).toBe(2);
    expect(perLine.questions).toHaveLength(1); // the "question —" lead still fires
  });
});

/* ══════════════════════════════════════════════════════════════════════════
 * Ground state
 * ══════════════════════════════════════════════════════════════════════════ */

describe('ground state', () => {
  it('is a cursor and nothing else', () => {
    const s = stepCapture(ground(), '');
    expect(s.analysis.empty).toBe(true);
    expect(affordancesOf(s.earned)).toEqual([]);
    expect(s.questions).toEqual([]);
    expect(s.threads).toEqual([]);
    // ZERO, not one. `threadCount` is `threads.length` and an empty screen has
    // no threads — the old "always ≥ 1" was the second number that let a tally
    // disagree with its dock. Nothing renders it here: the "◆ N threads"
    // indicator is gated on `earned.threads`, which is false at ground.
    expect(s.threadCount).toBe(0);
    expect(s.earned.threads).toBe(false);
  });

  it('the exported ground state is frozen, so no page can edit the blank screen', () => {
    expect(Object.isFrozen(CAPTURE_GROUND_STATE)).toBe(true);
  });

  it('whitespace alone is still ground', () => {
    expect(analyzeCapture('  \n\t\n  ').empty).toBe(true);
    // Positive control: one non-space character and it is not.
    expect(analyzeCapture('  x  ').empty).toBe(false);
  });
});
