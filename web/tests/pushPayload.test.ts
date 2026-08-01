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
      item({
        evidence: {
          sender: 'secret@person.com',
          body_preview: 'CONFIDENTIAL_TEXT',
          name: 'Jane Doe',
          // `body` is the prose digest field the peer_digest round introduced —
          // name it explicitly so this pin guards it (structural guarantee already
          // covers it; belt-and-suspenders).
          body: 'SECRET_DIGEST',
        },
      }),
    );
    const serialized = JSON.stringify(p);
    expect(serialized).not.toContain('CONFIDENTIAL_TEXT');
    expect(serialized).not.toContain('secret@person.com');
    expect(serialized).not.toContain('Jane Doe');
    expect(serialized).not.toContain('SECRET_DIGEST');
  });

  it('NEVER leaks an email_urgent card evidence — the #27 kind (title+kind+url only)', () => {
    // Slice 3's push rings for email_urgent; the field-named privacy pin extends
    // to its evidence shape. The producer-built TITLE legitimately carries
    // sender/subject (the design permits "title + sender/subject only, never the
    // body"), so this pins the EVIDENCE-only fields — the bounded body, the Gmail
    // deep-link, and the high_source discriminator — never reach the lock screen.
    const p = pushPayloadFor(
      item({
        id: 'email_urgent:note/Server outage.md',
        kind: 'email_urgent',
        title: 'Urgent email: ops@statuspage.io — Server outage',
        evidence: {
          sender: 'ops@statuspage.io',
          subject: 'Server outage',
          body: 'PRODUCTION_INCIDENT_DETAIL',
          gmail_url: 'https://mail.google.com/mail/u/0/#search/rfc822msgid:SECRET',
          high_source: 'override',
        },
      }),
    );
    expect(Object.keys(p).sort()).toEqual(['kind', 'title', 'url']);
    expect(p.kind).toBe('email_urgent');
    expect(p.url).toBe('/deck'); // needs-you → deck
    const serialized = JSON.stringify(p);
    expect(serialized).not.toContain('PRODUCTION_INCIDENT_DETAIL'); // bounded body
    expect(serialized).not.toContain('rfc822msgid'); // gmail deep-link
    expect(serialized).not.toContain('high_source'); // the slice-3 discriminator
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
