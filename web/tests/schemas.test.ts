import { describe, expect, it } from 'vitest';
import {
  MAX_DC_TEXT_CHARS,
  MAX_IMAGE_BYTES,
  MAX_MESSAGE_CHARS,
  MAX_SDP_CHARS,
  MAX_TRANSCRIPT_CHARS,
  base64DecodedBytes,
  chatTurnBodySchema,
  imageAttachmentSchema,
  playerPrimerSchema,
  voiceCancelFrame,
  voiceCloseBodySchema,
  voiceDcEventSchema,
  voiceHelloFrame,
  voiceOfferBodySchema,
} from '../lib/algernon/schemas';

// base64 of "hello" — a small valid attachment payload for the happy path.
const SMALL_B64 = 'aGVsbG8=';
// A base64 string whose DECODED size exceeds MAX_IMAGE_BYTES (5 MiB). All-'A'
// (valid alphabet, length % 4 === 0). 7 MiB chars → ~5.25 MiB decoded.
const OVERSIZED_B64 = 'A'.repeat(7 * 1024 * 1024);

describe('chatTurnBodySchema', () => {
  it('accepts a valid text turn', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'sess-123',
      message: 'hello there',
      kind: 'text',
    });
    expect(r.success).toBe(true);
  });

  it('accepts a turn with no kind (defaults handled downstream)', () => {
    const r = chatTurnBodySchema.safeParse({ session_key: 'k', message: 'hi' });
    expect(r.success).toBe(true);
  });

  it('rejects an empty message', () => {
    const r = chatTurnBodySchema.safeParse({ session_key: 'k', message: '   ' });
    expect(r.success).toBe(false);
  });

  it('rejects a missing session_key', () => {
    const r = chatTurnBodySchema.safeParse({ message: 'hi' });
    expect(r.success).toBe(false);
  });

  it('rejects an over-long message', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'x'.repeat(MAX_MESSAGE_CHARS + 1),
    });
    expect(r.success).toBe(false);
  });

  it('rejects an unknown kind', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'hi',
      kind: 'shout',
    });
    expect(r.success).toBe(false);
  });

  // --- image-carry (parity #29) ---
  it('accepts a valid images array', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'what is broken here?',
      images: [{ media_type: 'image/png', data: SMALL_B64 }],
    });
    expect(r.success).toBe(true);
  });

  it('rejects a bad image media_type', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'hi',
      images: [{ media_type: 'image/tiff', data: SMALL_B64 }],
    });
    expect(r.success).toBe(false);
  });

  it('rejects more than 4 images', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'hi',
      images: Array.from({ length: 5 }, () => ({ media_type: 'image/png', data: SMALL_B64 })),
    });
    expect(r.success).toBe(false);
  });

  it('rejects an oversized image (> 5 MiB decoded)', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'hi',
      images: [{ media_type: 'image/png', data: OVERSIZED_B64 }],
    });
    expect(r.success).toBe(false);
  });

  it('rejects image data carrying the data: URI prefix (must be bare base64)', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'hi',
      images: [{ media_type: 'image/png', data: `data:image/png;base64,${SMALL_B64}` }],
    });
    expect(r.success).toBe(false);
  });

  it('accepts a turn carrying a player primer (C3c)', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'why is that yellow?',
      primer: { brief_date: '2026-08-01', section_id: 'health' },
    });
    expect(r.success).toBe(true);
  });

  it('a turn with no primer is valid (byte-identical to the pre-feature path)', () => {
    const r = chatTurnBodySchema.safeParse({ session_key: 'k', message: 'hi' });
    expect(r.success).toBe(true);
  });

  // --- Learned-vocabulary capture (#54) -------------------------------------

  it('accepts a turn carrying the STT transcript', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'clean the chicken tractor',
      kind: 'voice',
      transcript: 'clean the chicken tracker',
    });
    expect(r.success).toBe(true);
    if (r.success) expect(r.data.transcript).toBe('clean the chicken tracker');
  });

  it('a turn with no transcript is valid, and the key stays ABSENT', () => {
    const r = chatTurnBodySchema.safeParse({ session_key: 'k', message: 'hi' });
    expect(r.success).toBe(true);
    // Not `undefined`-valued but PRESENT: the relay spreads on truthiness, and the
    // backend's "an older client sent nothing" branch keys on the field's absence.
    if (r.success) expect('transcript' in r.data).toBe(false);
  });

  it('rejects a transcript past MAX_TRANSCRIPT_CHARS (the trust-boundary bound)', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'short',
      transcript: 'x'.repeat(MAX_TRANSCRIPT_CHARS + 1),
    });
    expect(r.success).toBe(false);
  });

  it('accepts a transcript exactly AT the bound (the cap is inclusive)', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'short',
      transcript: 'x'.repeat(MAX_TRANSCRIPT_CHARS),
    });
    expect(r.success).toBe(true);
  });

  it('rejects a whitespace-only transcript (trimmed, then min(1) — same as message)', () => {
    const r = chatTurnBodySchema.safeParse({
      session_key: 'k',
      message: 'hi',
      transcript: '   \n  ',
    });
    expect(r.success).toBe(false);
  });

  it('the transcript bound tracks the MESSAGE cap — it is derived, not a second number', () => {
    // Stated as a pin because the bound's whole justification is that the
    // transcript is diffed against a message that cannot exceed this. If the
    // message cap moves and this does not, the comment stops being true.
    expect(MAX_TRANSCRIPT_CHARS).toBe(MAX_MESSAGE_CHARS);
  });
});

describe('playerPrimerSchema (C3c) — bound-only edge guard, backend is the validity authority', () => {
  it('accepts a well-formed primer', () => {
    const r = playerPrimerSchema.safeParse({ brief_date: '2026-08-01', section_id: 'day_state' });
    expect(r.success).toBe(true);
  });

  // The load-bearing fail-soft pin: the BFF must NOT reject an ISO-bad date or an unknown
  // section_id — the backend answers UN-GROUNDED for those (PlayerContextPrimer.valid). A
  // 400 here would turn the "answer un-grounded" contract into a hard failure.
  it('accepts an ISO-bad date and an unknown section_id (backend gates, not the BFF)', () => {
    const badDate = playerPrimerSchema.safeParse({ brief_date: 'not-a-date', section_id: 'health' });
    expect(badDate.success).toBe(true);
    const unknownSection = playerPrimerSchema.safeParse({ brief_date: '2026-08-01', section_id: 'made_up' });
    expect(unknownSection.success).toBe(true);
  });

  it('bounds the strings (DoS guard) — an over-long section_id / brief_date is rejected', () => {
    expect(playerPrimerSchema.safeParse({ brief_date: '2026-08-01', section_id: 'x'.repeat(65) }).success).toBe(false);
    expect(playerPrimerSchema.safeParse({ brief_date: 'x'.repeat(33), section_id: 'health' }).success).toBe(false);
  });
});

describe('imageAttachmentSchema + base64DecodedBytes', () => {
  it('base64DecodedBytes matches the true decoded length (padding-aware)', () => {
    expect(base64DecodedBytes(SMALL_B64)).toBe(5); // "hello"
    expect(base64DecodedBytes('')).toBe(0);
    expect(base64DecodedBytes('QQ==')).toBe(1); // "A"
    expect(base64DecodedBytes('QUI=')).toBe(2); // "AB"
    expect(base64DecodedBytes('QUJD')).toBe(3); // "ABC"
  });

  it('accepts a payload just under the 5 MiB decoded cap', () => {
    // 5242878 = 3 × 1747626 (multiple of 3 ⇒ unpadded base64 of length ×4/3).
    const underCap = 'A'.repeat((5242878 / 3) * 4);
    expect(base64DecodedBytes(underCap)).toBeLessThanOrEqual(MAX_IMAGE_BYTES);
    expect(imageAttachmentSchema.safeParse({ media_type: 'image/png', data: underCap }).success).toBe(true);
  });

  it('accepts each allowed media_type', () => {
    for (const mt of ['image/png', 'image/jpeg', 'image/gif', 'image/webp']) {
      expect(imageAttachmentSchema.safeParse({ media_type: mt, data: SMALL_B64 }).success).toBe(true);
    }
  });

  it('rejects empty data', () => {
    expect(imageAttachmentSchema.safeParse({ media_type: 'image/png', data: '' }).success).toBe(false);
  });
});

describe('voiceOfferBodySchema', () => {
  it('accepts a minimal offer', () => {
    const r = voiceOfferBodySchema.safeParse({ sdp: 'v=0...', type: 'offer' });
    expect(r.success).toBe(true);
  });

  it('accepts an optional session_key forward-hook', () => {
    const r = voiceOfferBodySchema.safeParse({
      sdp: 'v=0...',
      type: 'offer',
      session_key: 'sess-1',
    });
    expect(r.success).toBe(true);
  });

  it('strips (does NOT reject) unknown extra keys', () => {
    const r = voiceOfferBodySchema.safeParse({
      sdp: 'v=0...',
      type: 'offer',
      future_field: 'ignored',
    });
    expect(r.success).toBe(true);
    if (r.success) expect('future_field' in r.data).toBe(false);
  });

  it('rejects a missing sdp', () => {
    const r = voiceOfferBodySchema.safeParse({ type: 'offer' });
    expect(r.success).toBe(false);
  });

  it('rejects an empty sdp', () => {
    const r = voiceOfferBodySchema.safeParse({ sdp: '', type: 'offer' });
    expect(r.success).toBe(false);
  });

  it('rejects an sdp over MAX_SDP_CHARS', () => {
    const r = voiceOfferBodySchema.safeParse({
      sdp: 'x'.repeat(MAX_SDP_CHARS + 1),
      type: 'offer',
    });
    expect(r.success).toBe(false);
  });

  it('rejects a wrong type literal', () => {
    const r = voiceOfferBodySchema.safeParse({ sdp: 'v=0...', type: 'answer' });
    expect(r.success).toBe(false);
  });

  it('rejects an over-long session_key', () => {
    const r = voiceOfferBodySchema.safeParse({
      sdp: 'v=0...',
      type: 'offer',
      session_key: 'x'.repeat(129),
    });
    expect(r.success).toBe(false);
  });
});

describe('voiceCloseBodySchema', () => {
  it('accepts a voice_session_id', () => {
    const r = voiceCloseBodySchema.safeParse({ voice_session_id: 'a'.repeat(32) });
    expect(r.success).toBe(true);
  });

  it('rejects a missing voice_session_id', () => {
    const r = voiceCloseBodySchema.safeParse({});
    expect(r.success).toBe(false);
  });

  it('rejects an empty voice_session_id', () => {
    const r = voiceCloseBodySchema.safeParse({ voice_session_id: '' });
    expect(r.success).toBe(false);
  });
});

describe('voiceDcEventSchema (canonical D2 vocabulary)', () => {
  const ok = (obj: unknown) => voiceDcEventSchema.safeParse(obj).success;

  it('accepts every server event type with v:1', () => {
    expect(ok({ v: 1, type: 'state', state: 'ready', chat_session_key: 'k', voice_session_id: 'vs' })).toBe(true);
    expect(ok({ v: 1, type: 'state', state: 'superseded' })).toBe(true);
    expect(ok({ v: 1, type: 'state', state: 'turn_cancelled', turn_id: 't1' })).toBe(true);
    expect(ok({ v: 1, type: 'stt_partial', utterance_id: 'u1', text: 'hi', ts: 1 })).toBe(true);
    expect(ok({ v: 1, type: 'stt_final', utterance_id: 'u1', text: 'hello', ts: 'x' })).toBe(true);
    expect(ok({ v: 1, type: 'turn_started', turn_id: 't1' })).toBe(true);
    expect(ok({ v: 1, type: 'turn_text', turn_id: 't1', seq: 0, text: 'a' })).toBe(true);
    expect(ok({ v: 1, type: 'turn_tool', turn_id: 't1', tool: 'vault_search' })).toBe(true);
    expect(ok({ v: 1, type: 'turn_final', turn_id: 't1', reply: 'done', ts: 'a', user_ts: 'b', reply_chars: 4, truncated: false })).toBe(true);
    expect(ok({ v: 1, type: 'error', code: 'stt_unavailable', detail: 'down' })).toBe(true);
  });

  it('rejects an unknown state enum value', () => {
    expect(ok({ v: 1, type: 'state', state: 'exploded' })).toBe(false);
  });

  it('rejects a frame without v:1 (protocol version pinned)', () => {
    expect(ok({ type: 'turn_started', turn_id: 't1' })).toBe(false);
    expect(ok({ v: 2, type: 'turn_started', turn_id: 't1' })).toBe(false);
  });

  it('rejects an unknown event type (dropped by the caller via the lenient probe)', () => {
    expect(ok({ v: 1, type: 'tts_started', text: 'v2' })).toBe(false);
  });

  it('rejects an over-cap text chunk', () => {
    expect(ok({ v: 1, type: 'turn_text', turn_id: 't1', seq: 0, text: 'x'.repeat(MAX_DC_TEXT_CHARS + 1) })).toBe(false);
  });

  it('rejects turn_final with a non-string reply', () => {
    expect(ok({ v: 1, type: 'turn_final', turn_id: 't1', reply: 42 })).toBe(false);
  });

  it('accepts (and strips) unknown extra keys — non-strict', () => {
    const r = voiceDcEventSchema.safeParse({ v: 1, type: 'turn_started', turn_id: 't1', future: 'x' });
    expect(r.success).toBe(true);
    if (r.success) expect('future' in r.data).toBe(false);
  });

  // --- V2 additive TTS talk-back events ---
  it('accepts the three additive V2 events with v:1', () => {
    expect(ok({ v: 1, type: 'speaking_started', turn_id: 't1' })).toBe(true);
    expect(ok({ v: 1, type: 'speaking_done', turn_id: 't1' })).toBe(true); // reason optional
    expect(ok({ v: 1, type: 'speaking_done', turn_id: 't1', reason: 'drained' })).toBe(true);
    expect(ok({ v: 1, type: 'speaking_done', turn_id: 't1', reason: 'barged_in' })).toBe(true); // opaque, not enum
    expect(ok({ v: 1, type: 'utterance_discarded', utterance_id: 'u1' })).toBe(true);
  });

  it('rejects the V2 events without v:1', () => {
    expect(ok({ type: 'speaking_started', turn_id: 't1' })).toBe(false);
    expect(ok({ v: 1, type: 'speaking_started' })).toBe(false); // missing turn_id
    expect(ok({ v: 1, type: 'utterance_discarded' })).toBe(false); // missing utterance_id
  });

  it('rejects speaking_done with an over-cap reason (64-char bound pinned)', () => {
    // An over-cap reason drops the WHOLE frame → a stuck 'Speaking…' pill until
    // self-recovery. Trusted-server-only so low risk, but the bound gets its pin.
    expect(ok({ v: 1, type: 'speaking_done', turn_id: 't1', reason: 'x'.repeat(65) })).toBe(false);
    expect(ok({ v: 1, type: 'speaking_done', turn_id: 't1', reason: 'x'.repeat(64) })).toBe(true);
  });

  it('strips unknown keys on the V2 events (non-strict, forward-compat)', () => {
    const r = voiceDcEventSchema.safeParse({
      v: 1,
      type: 'speaking_started',
      turn_id: 't1',
      word_timings: [1, 2],
    });
    expect(r.success).toBe(true);
    if (r.success) expect('word_timings' in r.data).toBe(false);
  });
});

describe('client frame builders', () => {
  it('hello carries v:1', () => {
    expect(JSON.parse(voiceHelloFrame())).toEqual({ v: 1, type: 'hello' });
  });
  it('cancel carries v:1 + the turn_id when given', () => {
    expect(JSON.parse(voiceCancelFrame('t9'))).toEqual({ v: 1, type: 'cancel', turn_id: 't9' });
    expect(JSON.parse(voiceCancelFrame())).toEqual({ v: 1, type: 'cancel' });
  });
});
