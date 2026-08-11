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

describe('#68 — the three codes that used to fall through to the generic message', () => {
  // All three are emitted by the REAL route (transport/routes_ingest.py:
  // invalid_json 400, title_too_long 400, wrong_peer 401) and the ingest BFF
  // relays upstream status + body verbatim, so each one reached the operator as
  // "Something went wrong." Verified against the route and the relay rather
  // than assumed from the task text.
  const CASES: Array<[string, RegExp]> = [
    ['invalid_json', /couldn't|could not|didn't reach|not something you did/i],
    ['title_too_long', /too long/i],
    ['wrong_peer', /refused/i],
  ];

  it.each(CASES)('%s gets its own words', (code, pattern) => {
    expect(refusal(code)).toMatch(pattern);
  });

  it('none of the three falls through to the generic message', () => {
    for (const [code] of CASES) {
      expect(refusal(code)).not.toBe('Something went wrong. Please try again.');
    }
  });

  it('title_too_long does not borrow the COLLISION wording', () => {
    // Two different title failures. "A record with a similar title exists" and
    // "your title is too long" need opposite actions — rename vs shorten — and
    // one message for both is advice that cannot work for one of them.
    expect(refusal('title_too_long')).not.toBe(refusal('title_collision'));
    expect(refusal('title_too_long').toLowerCase()).not.toContain('already exists');
  });

  it('title_too_long states no character limit', () => {
    // The transport sends the real limit as `max_chars`; ApiError carries only
    // status/code/detail, so any figure here would be a second copy of a
    // constant this layer cannot read — wrong, silently, the day the box
    // changes it. Pinned so a well-meant "300 characters" cannot be added.
    expect(refusal('title_too_long')).not.toMatch(/\d/);
  });

  it('invalid_json does not blame the operator for what they typed', () => {
    // The form serialises its own payload. Sending them back to re-read a
    // field they cannot have got wrong is the wrong instruction.
    const m = refusal('invalid_json').toLowerCase();
    expect(m).not.toMatch(/check the form|your title|your text/);
  });

  it('invalid_json is distinct from invalid_base64 — unreadable request vs damaged file', () => {
    expect(refusal('invalid_json')).not.toBe(refusal('invalid_base64'));
  });

  it('the three new messages are distinct from each other AND from every neighbour\n     they could be conflated with', () => {
    // Set-size rather than pairwise, so a mutation collapsing several codes
    // onto one string fails HERE even though each individual row above would
    // still match its own loose pattern.
    const codes = [
      'invalid_json', 'title_too_long', 'wrong_peer',
      'invalid_base64', 'title_collision', 'invalid_session',
      'invalid_request', 'forbidden',
    ];
    const messages = codes.map(refusal);
    expect(new Set(messages).size).toBe(codes.length);
  });

  it('wrong_peer does not borrow the expired-session wording', () => {
    // The nearest wrong neighbour: both are 401s, and only one of them is
    // about the operator's session.
    expect(refusal('wrong_peer')).not.toBe(refusal('invalid_session'));
  });

  describe('wrong_peer — refuses without teaching, and without wrong advice', () => {
    const message = refusal('wrong_peer');

    it('says the request was refused on identity grounds', () => {
      expect(message.toLowerCase()).toContain('refused');
      expect(message.toLowerCase()).toMatch(/identity|sign-in/);
    });

    it('does NOT tell the operator to sign in again — it cannot help', () => {
      // The browser never supplies the peer token: `web_ingest` is the BFF's
      // own server-side credential, so this can only be a server misconfig.
      // A "sign out and back in" instruction sends the operator through a
      // logout that cannot fix it — the same trap the brief routes avoid by
      // mapping wrong_peer to 502 instead of relaying a bare 401.
      //
      // Asserted as the ABSENCE of an instruction, plus the presence of the
      // correction, because the failure being pinned is well-meant advice
      // getting added back by someone reading "401" and reaching for the
      // session.
      expect(message).toMatch(/won't change it|not your sign-in/i);
      expect(message.toLowerCase()).not.toMatch(/please sign in|sign in again\.|log in again/);
    });

    it('names no credential, peer, token or header', () => {
      // An attacker reading this learns nothing about which check failed.
      const m = message.toLowerCase();
      for (const leak of ['token', 'peer', 'header', 'web_ingest', 'credential', 'bearer', 'auth']) {
        expect(m).not.toContain(leak);
      }
    });
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
