import { describe, expect, it } from 'vitest';
import {
  MAX_MESSAGE_CHARS,
  MAX_SDP_CHARS,
  chatTurnBodySchema,
  voiceCloseBodySchema,
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
