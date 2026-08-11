import { describe, expect, it } from 'vitest';
import {
  AUDIO_EXTENSIONS,
  BATCH_DEFAULT_THRESHOLD,
  DOC_EXTENSIONS,
  UNIVERSAL_ACCEPT,
  availableIntents,
  classifyAttachment,
  defaultDocIntent,
  defaultImageIntent,
  intentBlockedReason,
  intentDescription,
  intentLabel,
  matchTarget,
  missingTargetMessage,
  stripExtension,
  unsupportedAttachmentMessage,
} from '../lib/algernon/composerRouting';
import { MAX_IMAGES_PER_TURN } from '../lib/algernon/schemas';

// The routing table (#97), pinned as a table.
//
// These defaults decide where an operator's documents go. They are ratified —
// 1–4 images discuss, 5+ batch, documents and recordings file — so they are
// pinned as VALUES rather than as "whatever the function returns", which is the
// only form that catches a silent re-tuning.

describe('classifyAttachment', () => {
  it('routes each accepted type to its kind', () => {
    expect(classifyAttachment({ name: 'shot.png', type: 'image/png' })).toBe('image');
    expect(classifyAttachment({ name: 'scan.jpg', type: 'image/jpeg' })).toBe('image');
    expect(classifyAttachment({ name: 'notes.md', type: 'text/markdown' })).toBe('doc');
    expect(classifyAttachment({ name: 'rows.csv', type: 'text/csv' })).toBe('doc');
    expect(classifyAttachment({ name: 'statement.pdf', type: 'application/pdf' })).toBe('doc');
    expect(classifyAttachment({ name: 'memo.m4a', type: 'audio/mp4' })).toBe('audio');
  });

  it('falls back to the EXTENSION when the browser names the type badly', () => {
    // Windows commonly reports application/vnd.ms-excel for .csv via the
    // registry, and some Android providers send an empty type. Refusing those
    // would refuse the real inputs this door exists for.
    expect(classifyAttachment({ name: 'rows.csv', type: 'application/vnd.ms-excel' })).toBe('doc');
    expect(classifyAttachment({ name: 'notes.md', type: '' })).toBe('doc');
    expect(classifyAttachment({ name: 'memo.m4a', type: '' })).toBe('audio');
  });

  it('refuses what no route accepts — WITH the accepted set as the control', () => {
    // The positive control is the point: an "unsupported is refused" assertion
    // on its own passes identically against a classifier that refuses
    // EVERYTHING, which would silently break the whole composer.
    expect(classifyAttachment({ name: 'archive.zip', type: 'application/zip' })).toBeNull();
    expect(classifyAttachment({ name: 'clip.mov', type: 'video/quicktime' })).toBeNull();
    expect(classifyAttachment({ name: 'sheet.xlsx', type: 'application/vnd.ms-excel' })).toBeNull();
    // …and the nearest admissible neighbours still classify.
    expect(classifyAttachment({ name: 'clip.mp3', type: 'audio/mpeg' })).toBe('audio');
    expect(classifyAttachment({ name: 'sheet.csv', type: 'text/csv' })).toBe('doc');
    expect(classifyAttachment({ name: 'frame.png', type: 'image/png' })).toBe('image');
  });

  it('an octet-stream is audio only when the extension agrees', () => {
    // `application/octet-stream` is in the STT allowlist as a last-resort audio
    // fallback, but it is also what a browser calls an unknown binary. Taking it
    // at face value would send a .bin to be transcribed.
    expect(classifyAttachment({ name: 'memo.m4a', type: 'application/octet-stream' })).toBe('audio');
    expect(classifyAttachment({ name: 'blob.bin', type: 'application/octet-stream' })).toBeNull();
  });

  it('the refusal names the file and what IS accepted', () => {
    const msg = unsupportedAttachmentMessage('archive.zip');
    expect(msg).toContain('archive.zip');
    expect(msg).toContain('.pdf');
    expect(msg.toLowerCase()).toContain('audio');
  });
});

describe('the universal picker accepts everything the three pages did', () => {
  it('lists the image mimes, the document extensions and audio', () => {
    for (const ext of DOC_EXTENSIONS) expect(UNIVERSAL_ACCEPT).toContain(ext);
    expect(UNIVERSAL_ACCEPT).toContain('image/png');
    expect(UNIVERSAL_ACCEPT).toContain('image/webp');
    expect(UNIVERSAL_ACCEPT).toContain('audio/*');
  });

  it('every audio extension it claims to take actually classifies as audio', () => {
    for (const ext of AUDIO_EXTENSIONS) {
      expect(classifyAttachment({ name: `rec${ext}`, type: '' })).toBe('audio');
    }
  });
});

describe('the ratified default intents', () => {
  it('1 to 4 images DISCUSS; 5 and up BATCH', () => {
    expect(defaultImageIntent(1)).toBe('discuss');
    expect(defaultImageIntent(4)).toBe('discuss');
    expect(defaultImageIntent(5)).toBe('batch');
    expect(defaultImageIntent(30)).toBe('batch');
  });

  it('the threshold is DERIVED from the per-turn cap, not written as 5', () => {
    // Written as a literal, the two drift into a gap (a set too big to discuss
    // and too small to default to batch) the day either moves.
    expect(BATCH_DEFAULT_THRESHOLD).toBe(MAX_IMAGES_PER_TURN + 1);
    expect(defaultImageIntent(MAX_IMAGES_PER_TURN)).toBe('discuss');
    expect(defaultImageIntent(MAX_IMAGES_PER_TURN + 1)).toBe('batch');
  });

  it('documents and recordings default to FILING', () => {
    expect(defaultDocIntent()).toBe('file');
  });
});

describe('which intents are on offer', () => {
  it('a discussable image set offers both, default first', () => {
    expect(availableIntents('image', { count: 3 })).toEqual(['discuss', 'batch']);
  });

  it('an over-cap image set offers BATCH ONLY — and says why on the blocked flip', () => {
    expect(availableIntents('image', { count: 9 })).toEqual(['batch']);
    const why = intentBlockedReason('image', 'discuss', { count: 9 });
    expect(why).toContain(String(MAX_IMAGES_PER_TURN));
    expect(why).toContain('9');
    // The remedy is stated, and it is the ACTIONABLE one: remove 5 of them.
    expect(why).toContain(String(9 - MAX_IMAGES_PER_TURN));
  });

  it('a readable document offers both; a PDF offers filing only', () => {
    expect(availableIntents('doc', { filename: 'notes.md' })).toEqual(['file', 'discuss']);
    expect(availableIntents('doc', { filename: 'rows.csv' })).toEqual(['file', 'discuss']);
    expect(availableIntents('doc', { filename: 'statement.pdf' })).toEqual(['file']);
    // A recording's transcript is text, so it can go either way.
    expect(availableIntents('audio', { filename: 'memo.m4a' })).toEqual(['file', 'discuss']);
  });

  it("the PDF refusal explains WHERE extraction happens, and offers the real path", () => {
    const why = intentBlockedReason('doc', 'discuss', { filename: 'statement.pdf' });
    expect(why).toBeTruthy();
    expect(why!.toLowerCase()).toContain('browser');
    // It must not dead-end: filing then reading from the vault IS the way to
    // discuss a PDF here.
    expect(why!.toLowerCase()).toContain('file it');
  });

  it('an AVAILABLE intent has no blocked reason (the control for the two above)', () => {
    expect(intentBlockedReason('image', 'discuss', { count: 2 })).toBeNull();
    expect(intentBlockedReason('image', 'batch', { count: 2 })).toBeNull();
    expect(intentBlockedReason('doc', 'file', { filename: 'statement.pdf' })).toBeNull();
    expect(intentBlockedReason('doc', 'discuss', { filename: 'notes.md' })).toBeNull();
  });
});

describe('the chip says what pressing Send will do', () => {
  it('labels are the operator-facing words', () => {
    expect(intentLabel('discuss')).toBe('Discuss');
    expect(intentLabel('batch')).toBe('Batch');
    expect(intentLabel('file')).toBe('File to vault');
  });

  it('each description NAMES the instance, and batch says it is not the conversation', () => {
    expect(intentDescription('discuss', 'Salem')).toContain('Salem');
    expect(intentDescription('file', 'Salem')).toContain('Salem');
    const batch = intentDescription('batch', 'Salem');
    expect(batch).toContain('Salem');
    // The 12-block history cap is why a bulk set must not enter context; the
    // operator is told that outright rather than discovering it.
    expect(batch).toContain('not into this conversation');
  });
});

describe('matching the selected chat instance to another route’s target', () => {
  const ingest = [
    { name: 'SALEM', label: 'Salem' },
    { name: 'VERA', label: 'VERA' },
  ];

  it('matches case-insensitively — chat says "Salem", ingest env says "SALEM"', () => {
    // The families genuinely spell targets differently. A case-sensitive match
    // would make a perfectly configured deploy look unconfigured.
    expect(matchTarget('Salem', ingest)?.name).toBe('SALEM');
    expect(matchTarget('salem', ingest)?.name).toBe('SALEM');
    expect(matchTarget(' VERA ', ingest)?.name).toBe('VERA');
  });

  it('returns null for an instance with NO target — and a configured one still matches', () => {
    // Exclusion pin + its positive control: "unconfigured → null" alone would
    // pass against a matcher that returns null for everything, which would
    // refuse every filing on a fully configured box.
    expect(matchTarget('Hypatia', ingest)).toBeNull();
    expect(matchTarget('', ingest)).toBeNull();
    expect(matchTarget('Salem', ingest)).not.toBeNull();
  });

  it('the missing-target refusal NAMES the instance and says nothing was sent', () => {
    const filed = missingTargetMessage('file', 'Hypatia');
    expect(filed).toContain('Hypatia');
    expect(filed).toContain('nothing was filed');
    const batched = missingTargetMessage('batch', 'Hypatia');
    expect(batched).toContain('Hypatia');
    expect(batched).toContain('nothing was sent');
    // Two routes, two sentences: "bulk scans" and "documents" are different
    // facts about what this instance is missing.
    expect(filed).not.toBe(batched);
  });
});

describe('filename-derived defaults', () => {
  it('strips only the final extension', () => {
    expect(stripExtension('bank statement.pdf')).toBe('bank statement');
    expect(stripExtension('notes.2026.08.md')).toBe('notes.2026.08');
    expect(stripExtension('README')).toBe('README');
    // A dotfile has no extension to strip — the name IS the name.
    expect(stripExtension('.gitignore')).toBe('.gitignore');
  });
});
