import { describe, expect, it } from 'vitest';
import { MAX_MESSAGE_CHARS, chatTurnBodySchema } from '../lib/algernon/schemas';

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
