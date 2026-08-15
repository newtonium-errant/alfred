import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';

// THE DOCUMENT RENDER'S NEW CONDITION (operator ruling, 2026-08-15).
//
// "I found white space in the brief weather, and most of the brief text is
// unreadable in that colour. That said, if we're just retiring the old written
// brief and keeping the player then no need to fix it before removing it."
//
// So the Morning Brief's raw-markdown block is no longer rendered below a
// WORKING player — the slides and the audio are the brief now. It survives as a
// DEGRADATION render, firing exactly when there is nothing to play, which is
// what keeps both "Full text below ↓" links pointing at something real.
//
// The Daily Sync is NOT part of that ruling and renders unconditionally: it has
// no narration, no slides and no audio (`/api/brief/narration` takes no `kind`),
// so this block is its only reachable form in the PWA. Removing it would undo
// the reachability move `pages/brief.tsx` was retired to make.
//
// These pins exist because NOTHING covered the always-on render before: the page
// suite never mocked `/api/brief/latest`, so both BriefViews were absent from
// every render it exercised, and `briefViewFrontmatter` unit-renders the
// component directly. The behaviour being changed here was unpinned.

const { mockFetchNarration, mockFetchAudio } = vi.hoisted(() => ({
  mockFetchNarration: vi.fn(),
  mockFetchAudio: vi.fn(),
}));
vi.mock('../lib/algernon/briefPlayer', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/algernon/briefPlayer')>();
  return { ...actual, fetchNarration: mockFetchNarration, fetchAudio: mockFetchAudio };
});
vi.mock('../lib/algernon/useSession', () => ({
  useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }),
}));
vi.mock('next/router', () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {} }),
}));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn(), me: vi.fn() } }));
vi.mock('../lib/algernon/client', () => ({ chatApi: { turn: vi.fn(), open: vi.fn() } }));
vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: () => <button type="button">mock-stt</button>,
}));

import PlayerPage from '../pages/player';
import type { BriefNarration } from '../lib/algernon/player';

const BRIEF_BODY = '# Morning Brief\n\nThe weather is fine.';
const SYNC_BODY = '# Daily Sync\n\nTwo things moved.';

const seg = (section_id: string) => ({
  section_id,
  title: `${section_id} title`,
  text: `${section_id} text`,
  word_count: 10,
});
const narration = (segments: BriefNarration['segments']): BriefNarration => ({
  brief_date: '2026-08-15',
  segments,
  total_words: segments.reduce((n, s) => n + s.word_count, 0),
  empty: segments.length === 0,
});

/** Both artifacts spooled — so an ABSENT block is a decision, never missing data. */
function serveBothArtifacts() {
  (globalThis as unknown as { fetch: unknown }).fetch = vi.fn(async (url: string) => {
    const u = String(url);
    if (u.includes('kind=daily_sync')) {
      return { ok: true, status: 200, json: async () => ({ kind: 'daily_sync', date: '2026-08-15', markdown: SYNC_BODY }) };
    }
    if (u.includes('kind=brief')) {
      return { ok: true, status: 200, json: async () => ({ kind: 'brief', date: '2026-08-15', markdown: BRIEF_BODY }) };
    }
    return { ok: true, status: 200, json: async () => ({}) };
  });
}

beforeEach(() => {
  mockFetchNarration.mockReset();
  mockFetchAudio.mockReset();
  localStorage.clear();
  serveBothArtifacts();
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
  (URL as unknown as { createObjectURL: unknown }).createObjectURL = vi.fn(() => 'blob:test');
  (URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn();
});
afterEach(() => vi.restoreAllMocks());

describe('a WORKING player does not render the brief document', () => {
  it('slides play; the Morning Brief block is gone and the Daily Sync stays', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([seg('health'), seg('weather')]) });
    mockFetchAudio.mockResolvedValue({ kind: 'audio', url: 'blob:test' });
    render(<PlayerPage />);

    // POSITIVE CONTROL FIRST. Without it, "no brief-view" passes identically on a
    // page that crashed, rendered nothing, or never loaded its artifacts at all.
    await waitFor(() => expect(screen.queryByTestId('player-slide')).not.toBeNull());
    // And the Daily Sync proves the artifact fetch WORKED — so the brief's
    // absence is the gate firing, not an empty response.
    await waitFor(() => expect(screen.queryByTestId('daily-sync-view')).not.toBeNull());

    expect(screen.queryByTestId('brief-view')).toBeNull();
  });

  it('an unmapped section offers no link; a mapped one still does', async () => {
    // The fallback anchor is gone: it pointed at the brief text, which is not
    // rendered while slides exist, so every unmapped slide would have landed on
    // the Daily Sync.
    mockFetchNarration.mockResolvedValue({ narration: narration([seg('weather')]) });
    mockFetchAudio.mockResolvedValue({ kind: 'audio', url: 'blob:test' });
    render(<PlayerPage />);

    await waitFor(() => expect(screen.queryByTestId('player-slide')).not.toBeNull());
    expect(screen.queryByTestId('player-slide-link')).toBeNull();
  });

  it('a MAPPED section keeps its destination — the control', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([seg('day_plan')]) });
    mockFetchAudio.mockResolvedValue({ kind: 'audio', url: 'blob:test' });
    render(<PlayerPage />);

    await waitFor(() => expect(screen.queryByTestId('player-slide')).not.toBeNull());
    expect(screen.getByTestId('player-slide-link').getAttribute('href')).toBe('/deck');
  });
});

describe('when the player CANNOT play, the document comes back', () => {
  // Each of these is a state whose ILB card offers "Full text below ↓" or the
  // deck; the render below is what makes that offer true.

  it('narration_unavailable — the brief exists and is readable', async () => {
    mockFetchNarration.mockResolvedValue({ state: 'narration_unavailable' });
    mockFetchAudio.mockResolvedValue({ kind: 'narration_unavailable' });
    render(<PlayerPage />);

    await waitFor(() => expect(screen.queryByTestId('player-narration-unavailable')).not.toBeNull());
    await waitFor(() => expect(screen.queryByTestId('brief-view')).not.toBeNull());
    // The link that promises it, and the anchor it lands on.
    expect(screen.getByTestId('player-narration-link').getAttribute('href')).toBe('#brief-text');
    expect(screen.getByTestId('brief-view').textContent).toContain('The weather is fine.');
  });

  it('an empty narration — nothing to play, so there is something to read', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([]) });
    mockFetchAudio.mockResolvedValue({ kind: 'unavailable' });
    render(<PlayerPage />);

    await waitFor(() => expect(screen.queryByTestId('player-empty')).not.toBeNull());
    await waitFor(() => expect(screen.queryByTestId('brief-view')).not.toBeNull());
  });
});

describe('the document is legible on the register it renders on', () => {
  it('spends the register ink, not a dark literal', async () => {
    // THE DEFECT THE OPERATOR PHOTOGRAPHED: `text-neutral-800` on
    // `bg-console-panel` (#131d21) — dark ink on a dark panel, which is why
    // "most of the brief text is unreadable in that colour". The panel had
    // adopted the register and the text it holds never did.
    //
    // Asserted on the DOM through the page rather than by reading the source, and
    // asserted in BOTH directions: the literal must be gone AND the token
    // present, because deleting the class entirely would also remove the literal
    // while leaving the text to inherit whatever it lands on.
    mockFetchNarration.mockResolvedValue({ state: 'narration_unavailable' });
    mockFetchAudio.mockResolvedValue({ kind: 'narration_unavailable' });
    render(<PlayerPage />);

    await waitFor(() => expect(screen.queryByTestId('brief-view-content')).not.toBeNull());
    const cls = screen.getByTestId('brief-view-content').className;
    expect(cls).toContain('text-console-ink');
    expect(cls).not.toContain('text-neutral-800');
  });
});

describe('the Daily Sync is unconditional', () => {
  it.each([
    ['a working player', { narration: narration([seg('health')]) }, { kind: 'audio', url: 'blob:test' }],
    ['a failed narration', { state: 'narration_unavailable' }, { kind: 'narration_unavailable' }],
  ])('renders under %s', async (_label, narrationResult, audioResult) => {
    // It has no player of its own to fall back FROM, so its render is not
    // conditional on anything. This is the pin that would catch a future tidy-up
    // sweeping it out alongside the brief.
    mockFetchNarration.mockResolvedValue(narrationResult);
    mockFetchAudio.mockResolvedValue(audioResult);
    render(<PlayerPage />);

    await waitFor(() => expect(screen.queryByTestId('daily-sync-view')).not.toBeNull());
    expect(screen.getByTestId('daily-sync-view').textContent).toContain('Two things moved.');
  });
});
