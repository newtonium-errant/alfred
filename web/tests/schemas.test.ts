import { describe, expect, it } from 'vitest';
import {
  MAX_DC_TEXT_CHARS,
  MAX_MESSAGE_CHARS,
  MAX_SDP_CHARS,
  chatTurnBodySchema,
  voiceCancelFrame,
  voiceCloseBodySchema,
  voiceDcEventSchema,
  voiceHelloFrame,
  voiceOfferBodySchema,
} from '../lib/algernon/schemas';

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
