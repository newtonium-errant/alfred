import { describe, expect, it } from 'vitest';
import { PUSH_TITLE_MAX_CHARS, pushDeepLink, pushPayloadFor } from '../lib/algernon/pushPayload';
import type { FeedItem } from '../lib/algernon/feed';

// Lock-screen privacy is the hard rule: the payload is title + kind + url ONLY,
// and NEVER anything from `evidence`.

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'email_tier:note/A.md',
    kind: 'email_tier',
    instance: 'salem',
    title: 'Email tier: a@b.com — Subject',
    mode: 'decide',
    attention: 'needs_you',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: '2026-07-30T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...overrides,
  };
}

describe('pushPayloadFor', () => {
  it('carries exactly title, kind, and url — no other keys', () => {
    const p = pushPayloadFor(item());
    expect(Object.keys(p).sort()).toEqual(['kind', 'title', 'url']);
  });

  it('NEVER leaks evidence content (lock-screen privacy)', () => {
    const p = pushPayloadFor(
      item({ evidence: { sender: 'secret@person.com', body_preview: 'CONFIDENTIAL_TEXT', name: 'Jane Doe' } }),
    );
    const serialized = JSON.stringify(p);
    expect(serialized).not.toContain('CONFIDENTIAL_TEXT');
    expect(serialized).not.toContain('secret@person.com');
    expect(serialized).not.toContain('Jane Doe');
  });

  it('deep-links decisions to /deck and glance items to /feed', () => {
    expect(pushDeepLink(item({ mode: 'decide', attention: 'needs_you' }))).toBe('/deck');
    expect(pushDeepLink(item({ mode: 'fyi', attention: 'fyi' }))).toBe('/feed');
  });

  it('caps the title length and falls back to the kind when the title is empty', () => {
    const long = pushPayloadFor(item({ title: 'x'.repeat(500) }));
    expect(long.title.length).toBe(PUSH_TITLE_MAX_CHARS);
    const noTitle = pushPayloadFor(item({ title: '' }));
    expect(noTitle.title).toBe('email_tier');
  });
});
