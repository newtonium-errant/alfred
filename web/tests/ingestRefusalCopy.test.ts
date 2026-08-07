import { describe, expect, it } from 'vitest';
import { friendlyError } from '../lib/algernon/useIngest';
import { ApiError } from '../lib/algernon/http';

// #57 gate, WARN-1. These pin the WORDS the operator reads for each ingest
// refusal — the layer that was previously guarded by a comment and nothing else.
//
// Why this file exists at all: the #57 build shipped a pin named
// `test_a_scanned_pdf_does_not_promise_a_future_capability`, and it does pass —
// but it asserts the BOX-SIDE extractor message, not this web-facing copy. Two
// different strings, and the one the operator actually sees had zero coverage.
// A name that matches the claim is not the same as an assertion that supports
// it, which is precisely how the gap survived review-by-author.
//
// The load-bearing pin is the scanned-PDF one: operator-ruled 2026-08-07 as a
// PLAIN refusal, with the "point at the vision work" copy option considered and
// explicitly NOT taken. That ruling now fails a test if it is ever edited away.

function refusal(code: string): string {
  return friendlyError(new ApiError(422, code));
}

// Language that would turn a refusal into a promise. Deliberately includes the
// bare capability nouns: the ruling was not "don't say coming soon", it was
// "don't point at the vision work at all", so naming OCR or vision is itself
// the violation regardless of the tense it is wrapped in.
const FUTURE_PROMISE = [
  'vision',
  'ocr',
  'coming soon',
  'not yet',
  'will be able',
  'in future',
  'for now',
  'later',
  'soon',
];

describe('the scanned-PDF refusal is PLAIN — the operator ruling, pinned', () => {
  const message = refusal('pdf_no_text_layer');

  it('says what happened, in the ruled wording', () => {
    expect(message).toContain('no selectable text');
    expect(message.toLowerCase()).toContain('scan');
  });

  it('offers something the operator can actually do', () => {
    // A refusal that only says no leaves the operator stuck. Both routes out
    // are things they can do TODAY with no new capability.
    expect(message.toLowerCase()).toMatch(/saved as text|paste/);
  });

  it.each(FUTURE_PROMISE)('does not dangle %s', (phrase) => {
    expect(message.toLowerCase()).not.toContain(phrase);
  });

  it('does not point at the deferred vision work', () => {
    // #67 is deferred and unruled. Advertising it here would promise the
    // operator a capability that does not exist and has not been approved.
    expect(message).not.toMatch(/#\s?\d+/);
    expect(message.toLowerCase()).not.toContain('image');
  });
});

describe('every PDF refusal names its OWN cause', () => {
  // Six failures rendering as one "something went wrong" is the operator-facing
  // form of silence — and none of these is fixable without knowing which
  // happened. The mapping is what makes the box's distinct reasons worth having.
  const CASES: Array<[string, RegExp]> = [
    ['file_too_large', /10 MB|limit/i],
    ['extracted_text_too_large', /text is too long|too long/i],
    ['pdf_no_text_layer', /no selectable text/i],
    ['pdf_encrypted', /password-protected/i],
    ['pdf_unreadable', /could not be read/i],
    ['pdf_support_unavailable', /can't read PDFs|paste the text/i],
    ['invalid_base64', /didn't arrive intact/i],
    ['empty_file', /empty/i],
  ];

  it.each(CASES)('%s gets its own words', (code, pattern) => {
    expect(refusal(code)).toMatch(pattern);
  });

  it('none of them falls through to the generic message', () => {
    const generic = 'Something went wrong. Please try again.';
    for (const [code] of CASES) {
      expect(refusal(code)).not.toBe(generic);
    }
  });

  it('all eight messages are DISTINCT from each other', () => {
    // Guards the table above from a mutation that maps every code to one
    // string, which would leave each individual row green.
    const messages = CASES.map(([code]) => refusal(code));
    expect(new Set(messages).size).toBe(CASES.length);
  });

  it('the byte and character limits are never confused for each other', () => {
    // Telling someone to shrink a file whose SIZE was never the problem is
    // advice that cannot work — the whole reason these are two codes.
    expect(refusal('file_too_large').toLowerCase()).toContain('mb');
    expect(refusal('extracted_text_too_large').toLowerCase()).not.toContain('mb');
  });
});

describe('the pre-#57 mappings still hold', () => {
  it.each([
    ['title_collision', /similar title/i],
    ['body_too_large', /too large/i],
    ['invalid_type', /record type/i],
    ['forbidden', /owner-only/i],
    ['invalid_session', /sign in again/i],
  ])('%s is unchanged', (code, pattern) => {
    expect(refusal(code)).toMatch(pattern);
  });

  it('an unknown code still degrades to the generic message, never to silence', () => {
    expect(refusal('some_code_that_does_not_exist')).toBe(
      'Something went wrong. Please try again.',
    );
    expect(friendlyError(new Error('not an ApiError'))).toBe(
      'Something went wrong. Please try again.',
    );
  });
});
