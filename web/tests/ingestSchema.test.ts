import { describe, expect, it } from 'vitest';
import {
  INGEST_RECORD_TYPES,
  MAX_INGEST_CHARS,
  ingestBodySchema,
  normaliseAudioMime,
  AUDIO_MIME_ALLOWLIST,
} from '../lib/algernon/schemas';

const valid = {
  target: 'SALEM',
  record_type: 'document' as const,
  title: 'A clear unique title',
  body: 'The verbatim body content.',
  source: 'paste',
};

describe('ingestBodySchema', () => {
  it('accepts a well-formed ingest body', () => {
    expect(ingestBodySchema.safeParse(valid).success).toBe(true);
  });

  it('accepts each of the universal record types', () => {
    for (const rt of INGEST_RECORD_TYPES) {
      expect(ingestBodySchema.safeParse({ ...valid, record_type: rt }).success).toBe(true);
    }
  });

  it('rejects an unknown record type', () => {
    expect(ingestBodySchema.safeParse({ ...valid, record_type: 'concept' }).success).toBe(false);
  });

  it('rejects an empty / whitespace-only title', () => {
    expect(ingestBodySchema.safeParse({ ...valid, title: '   ' }).success).toBe(false);
  });

  it('rejects an empty / whitespace-only body', () => {
    expect(ingestBodySchema.safeParse({ ...valid, body: '   ' }).success).toBe(false);
  });

  it('rejects an empty target', () => {
    expect(ingestBodySchema.safeParse({ ...valid, target: '' }).success).toBe(false);
  });

  it('rejects an empty source', () => {
    expect(ingestBodySchema.safeParse({ ...valid, source: '  ' }).success).toBe(false);
  });

  it('enforces the title length ceiling (300)', () => {
    expect(ingestBodySchema.safeParse({ ...valid, title: 'x'.repeat(300) }).success).toBe(true);
    expect(ingestBodySchema.safeParse({ ...valid, title: 'x'.repeat(301) }).success).toBe(false);
  });

  it('enforces the source length ceiling (500)', () => {
    expect(ingestBodySchema.safeParse({ ...valid, source: 'x'.repeat(500) }).success).toBe(true);
    expect(ingestBodySchema.safeParse({ ...valid, source: 'x'.repeat(501) }).success).toBe(false);
  });

  it('enforces the body length ceiling (MAX_INGEST_CHARS)', () => {
    expect(ingestBodySchema.safeParse({ ...valid, body: 'x'.repeat(MAX_INGEST_CHARS) }).success).toBe(true);
    expect(ingestBodySchema.safeParse({ ...valid, body: 'x'.repeat(MAX_INGEST_CHARS + 1) }).success).toBe(false);
  });

  it('trims title/body/source (the parsed value is trimmed)', () => {
    const r = ingestBodySchema.safeParse({ ...valid, title: '  Spaced  ' });
    expect(r.success).toBe(true);
    if (r.success) expect(r.data.title).toBe('Spaced');
  });
});

describe('normaliseAudioMime', () => {
  it('strips Content-Type params and lowercases', () => {
    expect(normaliseAudioMime('audio/webm;codecs=opus')).toBe('audio/webm');
    expect(normaliseAudioMime('AUDIO/WEBM')).toBe('audio/webm');
  });

  it('accepts every allowlisted mime', () => {
    for (const m of AUDIO_MIME_ALLOWLIST) {
      expect(normaliseAudioMime(m)).toBe(m);
    }
  });

  it('rejects a non-audio mime and empty/null input', () => {
    expect(normaliseAudioMime('application/json')).toBeNull();
    expect(normaliseAudioMime('')).toBeNull();
    expect(normaliseAudioMime(undefined)).toBeNull();
    expect(normaliseAudioMime(null)).toBeNull();
  });
});
