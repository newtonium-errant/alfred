import { afterEach, describe, expect, it, vi } from 'vitest';

// Pins the DOM-free feed foundation: swipe geometry, per-kind deck verbs, kind
// labels, defensive evidence flattening, and the browser feed client's request
// shaping. These are the contracts the deck + feed pages build on.

import {
  DECK_VERBS,
  HEAVY_KINDS,
  PARK_Y_THRESHOLD,
  SWIPE_X_THRESHOLD,
  deckVerbsFor,
  kindLabel,
  stampOpacity,
  verdictForDrag,
} from '../lib/algernon/feedConstants';
import { coerceEvidenceValue, evidenceLabel, evidenceRows } from '../lib/algernon/feedEvidence';

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
