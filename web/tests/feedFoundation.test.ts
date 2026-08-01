import { afterEach, describe, expect, it, vi } from 'vitest';

// Pins the DOM-free feed foundation: swipe geometry, per-kind deck verbs, kind
// labels, defensive evidence flattening, and the browser feed client's request
// shaping. These are the contracts the deck + feed pages build on.

import {
  DECK_VERBS,
  HEAVY_KINDS,
  PARK_Y_THRESHOLD,
  SWIPE_X_THRESHOLD,
  affirmLabelFor,
  deckVerbsFor,
  emailPriority,
  isDeckDealt,
  kindLabel,
  stampOpacity,
  verdictForDrag,
} from '../lib/algernon/feedConstants';
import { coerceEvidenceValue, evidenceBody, evidenceExternalLink, evidenceLabel, evidenceRows, isEmailEvidence } from '../lib/algernon/feedEvidence';
import type { FeedItem } from '../lib/algernon/feed';

function feedItem(kind: string, evidence: Record<string, unknown>): FeedItem {
  return {
    id: `${kind}:x`,
    kind,
    instance: 'salem',
    title: 't',
    mode: 'decide',
    attention: 'needs_you',
    evidence,
    actions: [],
    state: 'open',
    created_at: '2026-07-31T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
  };
}

describe('emailPriority — the on-face tier', () => {
  it('reads classifier_priority (the real key), case/space-insensitive', () => {
    expect(emailPriority(feedItem('email_tier', { classifier_priority: 'HIGH' }))).toBe('high');
    expect(emailPriority(feedItem('email_tier', { classifier_priority: ' low ' }))).toBe('low');
    expect(emailPriority(feedItem('email_tier', { classifier_priority: 'medium' }))).toBe('medium');
  });
  it('surfaces spam too (face honesty — operator ruling)', () => {
    expect(emailPriority(feedItem('email_tier', { classifier_priority: 'spam' }))).toBe('spam');
  });
  it('is null for empty / missing / wrong-key / non-email (no badge, plain verb)', () => {
    expect(emailPriority(feedItem('email_tier', { classifier_priority: '' }))).toBeNull();
    expect(emailPriority(feedItem('email_tier', { classifier_priority: 'garbage' }))).toBeNull();
    expect(emailPriority(feedItem('email_tier', {}))).toBeNull();
    expect(emailPriority(feedItem('email_tier', { priority: 'high' }))).toBeNull(); // wrong key ignored
    expect(emailPriority(feedItem('attribution', { classifier_priority: 'high' }))).toBeNull();
  });
});

describe('evidenceBody — prose body extraction', () => {
  it('extracts a non-empty body + the truncated flag', () => {
    expect(evidenceBody({ body: 'line one\nline two', truncated: false })).toEqual({
      text: 'line one\nline two',
      truncated: false,
    });
    expect(evidenceBody({ body: 'digest…[truncated]', truncated: true })?.truncated).toBe(true);
  });
  it('honours the email_tier body_truncated flag as truncation too (#26)', () => {
    expect(evidenceBody({ body: 'bounded email preview', body_truncated: true })?.truncated).toBe(true);
    expect(evidenceBody({ body: 'short email', body_truncated: false })?.truncated).toBe(false);
  });
  it('is null for absent / empty / non-string / non-object', () => {
    expect(evidenceBody({})).toBeNull();
    expect(evidenceBody({ body: '   ' })).toBeNull();
    expect(evidenceBody({ body: 42 })).toBeNull();
    expect(evidenceBody(null)).toBeNull();
    expect(evidenceBody('x')).toBeNull();
  });
  it('caps a pathologically long body', () => {
    expect(evidenceBody({ body: 'x'.repeat(9000) })?.text.length).toBe(4000);
  });
});

describe('evidenceRows — body/truncated are prose, never key:value rows', () => {
  it('drops body and truncated from the rows', () => {
    const rows = evidenceRows({ peer: 'kalle', body: 'the digest', truncated: true });
    const keys = rows.map((r) => r.key);
    expect(keys).toContain('peer');
    expect(keys).not.toContain('body');
    expect(keys).not.toContain('truncated');
  });
  it('hides the email_tier plumbing keys (#26) from the rows', () => {
    const rows = evidenceRows({
      sender: 'a@b.com',
      body: 'preview',
      body_truncated: true,
      message_id: '<abc@mail>',
      gmail_url: 'https://mail.google.com/mail/u/0/#search/rfc822msgid:abc',
    });
    const keys = rows.map((r) => r.key);
    expect(keys).toContain('sender'); // the one real row
    expect(keys).not.toContain('body_truncated');
    expect(keys).not.toContain('message_id');
    expect(keys).not.toContain('gmail_url'); // never a raw URL row — it's the anchor
  });
});

describe('evidenceExternalLink — the scheme-gated "Open in Gmail" link (#26)', () => {
  it('accepts a server-built https://mail.google.com URL and returns it VERBATIM', () => {
    const url = 'https://mail.google.com/mail/u/0/#search/rfc822msgid:a%40b';
    expect(evidenceExternalLink({ gmail_url: url })).toEqual({ href: url, label: 'Open in Gmail' });
  });
  it('is null for a non-Gmail host / non-https scheme / hostile URL (never a clickable href)', () => {
    expect(evidenceExternalLink({ gmail_url: 'javascript:alert(1)' })).toBeNull();
    expect(evidenceExternalLink({ gmail_url: 'https://evil.example.com/mail.google.com' })).toBeNull();
    expect(evidenceExternalLink({ gmail_url: 'http://mail.google.com/x' })).toBeNull(); // not https
    expect(evidenceExternalLink({ gmail_url: 'data:text/html,x' })).toBeNull();
  });
  it('is null for absent / empty / non-string / non-object', () => {
    expect(evidenceExternalLink({})).toBeNull();
    expect(evidenceExternalLink({ gmail_url: '' })).toBeNull();
    expect(evidenceExternalLink({ gmail_url: 42 })).toBeNull();
    expect(evidenceExternalLink(null)).toBeNull();
    expect(evidenceExternalLink('x')).toBeNull();
  });
});

describe('isEmailEvidence — email_tier discriminator for honest truncation copy (#26)', () => {
  it('is true when the email signature (sender + subject + classifier_priority) is present', () => {
    // Present even with empty values / a blank gmail_url — key-presence, not value.
    expect(isEmailEvidence({ sender: 'a@b.com', subject: '', classifier_priority: 'high', gmail_url: '' })).toBe(true);
  });
  it('is false for peer_digest (prose body, none of the email keys)', () => {
    expect(isEmailEvidence({ body: 'the digest', truncated: true })).toBe(false);
  });
  it('is false when the signature is incomplete (missing any of the three)', () => {
    expect(isEmailEvidence({ sender: 'a@b.com', subject: 'hi' })).toBe(false); // no classifier_priority
    expect(isEmailEvidence({ sender: 'a@b.com', classifier_priority: 'low' })).toBe(false); // no subject
  });
  it('is false for non-object', () => {
    expect(isEmailEvidence(null)).toBe(false);
    expect(isEmailEvidence('x')).toBe(false);
    expect(isEmailEvidence([1, 2])).toBe(false);
  });
});

describe('affirmLabelFor — dynamic affirm verb', () => {
  it('appends the tier for an email with a priority', () => {
    expect(affirmLabelFor(feedItem('email_tier', { classifier_priority: 'high' }))).toBe('Confirm HIGH');
  });
  it('appends SPAM too (face honesty — the spam tier is confirmable)', () => {
    expect(affirmLabelFor(feedItem('email_tier', { classifier_priority: 'spam' }))).toBe('Confirm SPAM');
  });
  it('stays the plain static label for email without a recognised priority', () => {
    expect(affirmLabelFor(feedItem('email_tier', {}))).toBe('Confirm');
    expect(affirmLabelFor(feedItem('email_tier', { classifier_priority: 'garbage' }))).toBe('Confirm');
  });
  it('leaves other kinds unchanged, and is null when there is no affirm action', () => {
    expect(affirmLabelFor(feedItem('routine_match', {}))).toBe("That's it");
    expect(affirmLabelFor(feedItem('attribution', { classifier_priority: 'high' }))).toBe('Confirm');
    expect(affirmLabelFor(feedItem('weather', {}))).toBeNull(); // unmapped kind
  });
});

describe('verdictForDrag — swipe thresholds', () => {
  it('affirms past the right x-threshold', () => {
    expect(verdictForDrag(SWIPE_X_THRESHOLD + 1, 0)).toBe('affirm');
  });
  it('rejects past the left x-threshold', () => {
    expect(verdictForDrag(-(SWIPE_X_THRESHOLD + 1), 0)).toBe('reject');
  });
  it('parks on a mostly-vertical upward flick', () => {
    expect(verdictForDrag(10, -(PARK_Y_THRESHOLD + 1))).toBe('park');
  });
  it('springs back (null) when nothing crosses a threshold', () => {
    expect(verdictForDrag(20, -10)).toBeNull();
    expect(verdictForDrag(SWIPE_X_THRESHOLD, 0)).toBeNull(); // exactly at → not past
  });
  it('a big horizontal flick with upward drift is affirm/reject, not park', () => {
    // dx dominates → park guard (|dx|<70) fails, so horizontal wins.
    expect(verdictForDrag(120, -120)).toBe('affirm');
    expect(verdictForDrag(-120, -120)).toBe('reject');
  });
});

describe('stampOpacity', () => {
  it('is 0 below the fade-start and ramps to a 1 ceiling', () => {
    expect(stampOpacity(0)).toBe(0);
    expect(stampOpacity(40)).toBe(0);
    expect(stampOpacity(70)).toBeCloseTo(0.5, 5);
    expect(stampOpacity(1000)).toBe(1);
  });
});

describe('deck verbs', () => {
  it('maps each decide family to action_ids within its B1 capability', () => {
    expect(DECK_VERBS.email_tier.affirm).toBe('confirm');
    expect(DECK_VERBS.email_tier.reject).toBe('spam');
    expect(DECK_VERBS.pending.reject).toBeNull(); // pending has no reject
    expect(DECK_VERBS.pending.affirm).toBe('noted');
  });
  it('flags proposal + recurrence as heavy (two-step confirm)', () => {
    expect(HEAVY_KINDS.has('proposal')).toBe(true);
    expect(HEAVY_KINDS.has('recurrence')).toBe(true);
    expect(DECK_VERBS.proposal.heavy).toBe(true);
    expect(DECK_VERBS.email_tier.heavy).toBe(false);
  });
  it('returns null for an unmapped kind (park-only)', () => {
    expect(deckVerbsFor('weather')).toBeNull();
    expect(deckVerbsFor('email_tier')).not.toBeNull();
  });
});

describe('email_urgent — the interrupt kind deals + acks (#27)', () => {
  const urgent = (over: Record<string, unknown> = {}) =>
    feedItem('email_urgent', { sender: 'a@b.com', subject: 'Re: prod down', classifier_priority: 'high', high_source: 'override', ...over });

  it('deals into the deck (isDeckDealt true — the C2-era generic path carries it)', () => {
    expect(isDeckDealt(urgent())).toBe(true);
  });
  it('is ACK-only: affirm "ack", no reject (re-tier stays on the calibration card)', () => {
    const v = deckVerbsFor('email_urgent');
    expect(v?.affirm).toBe('ack');
    expect(v?.reject).toBeNull();
    expect(v?.heavy).toBe(false);
  });
  it('affirmLabelFor is the static "Got it" (no tier append — not a calibration card)', () => {
    expect(affirmLabelFor(urgent())).toBe('Got it');
    expect(emailPriority(urgent())).toBeNull(); // the priority badge is email_tier-only
  });
  it('reuses the email honest-copy path: isEmailEvidence fires (sender+subject+classifier_priority)', () => {
    expect(isEmailEvidence(urgent().evidence)).toBe(true);
  });
  it('hides high_source from the raw rows (it renders as the on-face provenance chip)', () => {
    const keys = evidenceRows(urgent().evidence).map((r) => r.key);
    expect(keys).not.toContain('high_source');
    expect(keys).toContain('sender'); // ordinary evidence still shows
  });
});

describe('kindLabel', () => {
  it('uses the friendly label, else a humanised fallback', () => {
    expect(kindLabel('email_tier')).toBe('Email tier');
    expect(kindLabel('some_new_kind')).toBe('SOME NEW KIND');
  });
});

describe('evidence flattening (defensive, untrusted display data)', () => {
  it('coerces any value to a safe display string', () => {
    expect(coerceEvidenceValue('hi')).toBe('hi');
    expect(coerceEvidenceValue(3)).toBe('3');
    expect(coerceEvidenceValue(true)).toBe('true');
    expect(coerceEvidenceValue(null)).toBe('');
    expect(coerceEvidenceValue({ a: 1 })).toBe('{"a":1}');
    expect(coerceEvidenceValue([1, 2])).toBe('[1,2]');
  });
  it('drops empty values + hidden plumbing keys, bounds the row count', () => {
    const rows = evidenceRows({ sender: 'a@b.com', subject: '', item_number: 3, note: 'hi' });
    const keys = rows.map((r) => r.key);
    expect(keys).toContain('sender');
    expect(keys).toContain('note');
    expect(keys).not.toContain('subject'); // empty
    expect(keys).not.toContain('item_number'); // hidden
  });
  it('never treats a non-object as evidence', () => {
    expect(evidenceRows(null)).toEqual([]);
    expect(evidenceRows('nope')).toEqual([]);
    expect(evidenceRows([1, 2])).toEqual([]);
  });
  it('does NOT emit markup — a script-ish value stays a plain string (React escapes it)', () => {
    const rows = evidenceRows({ subject: '<img src=x onerror=alert(1)>' });
    // The helper returns the raw string verbatim; the CARD renders it as a React
    // text child (auto-escaped). It must never be wrapped in markup here.
    expect(rows[0].value).toBe('<img src=x onerror=alert(1)>');
  });
  it('humanises a key for its label', () => {
    expect(evidenceLabel('record_path')).toBe('Record Path');
  });
});

describe('feed client request shaping', () => {
  const { mockGetJson, mockPostJson } = vi.hoisted(() => ({
    mockGetJson: vi.fn(),
    mockPostJson: vi.fn(),
  }));
  vi.mock('../lib/algernon/http', () => ({
    getJson: mockGetJson,
    postJson: mockPostJson,
  }));

  afterEach(() => {
    mockGetJson.mockReset();
    mockPostJson.mockReset();
  });

  it('list builds an allowlisted query string, omitting empties', async () => {
    const { feedApi } = await import('../lib/algernon/feed');
    mockGetJson.mockResolvedValue({ items: [], count: 0 });
    await feedApi.list({ state: 'open', mode: 'decide' });
    expect(mockGetJson).toHaveBeenCalledWith('/api/feed/list?state=open&mode=decide');
    await feedApi.list();
    expect(mockGetJson).toHaveBeenLastCalledWith('/api/feed/list');
  });

  it('act posts {id, action_id}', async () => {
    const { feedApi } = await import('../lib/algernon/feed');
    mockPostJson.mockResolvedValue({ ok: true, status: 'acted' });
    await feedApi.act('email_tier:note/A.md', 'confirm');
    expect(mockPostJson).toHaveBeenCalledWith('/api/feed/act', {
      id: 'email_tier:note/A.md',
      action_id: 'confirm',
    });
  });
});
