import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { mockGetJson } = vi.hoisted(() => ({ mockGetJson: vi.fn() }));
vi.mock('../lib/algernon/http', () => ({ getJson: mockGetJson }));

import { fetchNarration, playerAudioState } from '../lib/algernon/briefPlayer';
import type { BriefNarration } from '../lib/algernon/player';

describe('playerAudioState — the forward-compat degradation seam', () => {
  it('audio/mpeg + a blob url → the playable audio state', () => {
    expect(playerAudioState({ ok: true, status: 200, contentType: 'audio/mpeg', blobUrl: 'blob:x' })).toEqual({
      kind: 'audio',
      url: 'blob:x',
    });
  });

  it('200 JSON {state:no_brief} → no_brief (offer the deck)', () => {
    expect(playerAudioState({ ok: true, status: 200, contentType: 'application/json', state: 'no_brief' })).toEqual({ kind: 'no_brief' });
  });

  it('200 JSON {state:tts_not_configured} → tts_not_configured (text-along)', () => {
    expect(playerAudioState({ ok: true, status: 200, contentType: 'application/json', state: 'tts_not_configured' })).toEqual({
      kind: 'tts_not_configured',
    });
  });

  it('an UNKNOWN state string → unavailable (FORWARD-COMPAT: the pending third state renders safely under any name)', () => {
    // ← reddens if an unrecognized state ever crashes or falls through to a broken play button.
    expect(playerAudioState({ ok: true, status: 200, contentType: 'application/json', state: 'brief_exists_narration_missing' })).toEqual({
      kind: 'unavailable',
    });
    expect(playerAudioState({ ok: true, status: 200, contentType: 'application/json', state: 'some_future_state' })).toEqual({
      kind: 'unavailable',
    });
  });

  it('no state / 502 synth-fail / network / non-audio / audio-without-blob → unavailable (text-along, never a crash)', () => {
    expect(playerAudioState({ ok: true, status: 200, contentType: 'application/json' })).toEqual({ kind: 'unavailable' });
    expect(playerAudioState({ ok: false, status: 502, contentType: 'application/json' })).toEqual({ kind: 'unavailable' });
    expect(playerAudioState({ ok: false, status: 0, contentType: '' })).toEqual({ kind: 'unavailable' });
    // defensive: an audio content-type with no blob url (shouldn't happen) → unavailable, not a throw.
    expect(playerAudioState({ ok: true, status: 200, contentType: 'audio/mpeg' })).toEqual({ kind: 'unavailable' });
  });
});

describe('fetchNarration', () => {
  beforeEach(() => mockGetJson.mockReset());
  afterEach(() => vi.restoreAllMocks());

  const dict: BriefNarration = {
    brief_date: '2026-08-01',
    segments: [{ section_id: 'day_state', title: 'S', text: 't', word_count: 3 }],
    total_words: 3,
    empty: false,
  };

  it('a narration dict → {narration}', async () => {
    mockGetJson.mockResolvedValue(dict);
    await expect(fetchNarration()).resolves.toEqual({ narration: dict });
  });

  it('200 {state:no_brief} → {noBrief:true}', async () => {
    mockGetJson.mockResolvedValue({ state: 'no_brief' });
    await expect(fetchNarration()).resolves.toEqual({ noBrief: true });
  });

  it('an empty:true dict is a dict (not no_brief) — narrationSlides renders the "nothing to play" ILB', async () => {
    const empty: BriefNarration = { brief_date: '2026-08-01', segments: [], total_words: 0, empty: true };
    mockGetJson.mockResolvedValue(empty);
    await expect(fetchNarration()).resolves.toEqual({ narration: empty });
  });
});
