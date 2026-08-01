import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

// Pins the /player render: the ILB states (no_brief / narration_unavailable / empty),
// slides + text-along degradation, and the play → ask (first-class) → resume controls.
// briefPlayer's fetches are mocked; narrationSlides / usePlayer / useMediaSession are real
// (Media Session no-ops without navigator.mediaSession in jsdom).

const { mockFetchNarration, mockFetchAudio, mockTurn, mockOpen } = vi.hoisted(() => ({
  mockFetchNarration: vi.fn(),
  mockFetchAudio: vi.fn(),
  mockTurn: vi.fn(),
  mockOpen: vi.fn(),
}));
vi.mock('../lib/algernon/briefPlayer', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/algernon/briefPlayer')>();
  return { ...actual, fetchNarration: mockFetchNarration, fetchAudio: mockFetchAudio };
});
vi.mock('../lib/algernon/useSession', () => ({ useSession: () => ({ user: { name: 'andrew', role: 'owner' }, loading: false }) }));
vi.mock('next/router', () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn(), query: {} }) }));
vi.mock('../lib/algernon/authClient', () => ({ authApi: { logout: vi.fn() } }));
// The player-ask send path reuses the shared chat client — mock it (not a parallel client).
vi.mock('../lib/algernon/client', () => ({ chatApi: { turn: mockTurn, open: mockOpen } }));
// VoiceCapture has its own test; stub it here to isolate the page's ask logic from
// MediaRecorder (absent in jsdom). The stub's "Use" fires onTranscript, mirroring the
// real component's confirm step (which prefills the always-live keyboard input).
vi.mock('../components/chat/VoiceCapture', () => ({
  VoiceCapture: ({ onTranscript, idPrefix }: { onTranscript: (t: string) => void; idPrefix: string }) => (
    <button type="button" data-testid={`${idPrefix}-mock-use`} onClick={() => onTranscript('why is that yellow?')}>
      mock-stt-use
    </button>
  ),
}));

import PlayerPage from '../pages/player';
import { ApiError } from '../lib/algernon/http';
import type { BriefNarration } from '../lib/algernon/player';

const seg = (section_id: string) => ({ section_id, title: `${section_id} title`, text: `${section_id} text`, word_count: 10 });
const narration = (segments: BriefNarration['segments']): BriefNarration => ({
  brief_date: '2026-08-01',
  segments,
  total_words: segments.reduce((n, s) => n + s.word_count, 0),
  empty: segments.length === 0,
});

beforeEach(() => {
  mockFetchNarration.mockReset();
  mockFetchAudio.mockReset();
  mockTurn.mockReset();
  mockOpen.mockReset();
  // A stored home session so the ask reuses it (no /chat/open clobber — see usePlayerAsk).
  localStorage.clear();
  localStorage.setItem('algernon:session_key:Algernon', 'stored-sess');
  // jsdom gaps: HTMLMediaElement play/pause + URL object-URL helpers aren't implemented
  // (they emit jsdomErrors, which the page's try/catch can't intercept — nothing throws).
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue(undefined);
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
  (URL as unknown as { createObjectURL: unknown }).createObjectURL = vi.fn(() => 'blob:test');
  (URL as unknown as { revokeObjectURL: unknown }).revokeObjectURL = vi.fn();
});
afterEach(() => vi.restoreAllMocks());

describe('PlayerPage — ILB states', () => {
  it('no_brief → offer the deck, not the player', async () => {
    mockFetchNarration.mockResolvedValue({ state: 'no_brief' });
    mockFetchAudio.mockResolvedValue({ kind: 'no_brief' });
    render(<PlayerPage />);
    await waitFor(() => expect(screen.queryByTestId('player-no-brief')).not.toBeNull());
    expect(screen.getByTestId('player-no-brief-link').getAttribute('href')).toBe('/deck');
    expect(screen.queryByTestId('player')).toBeNull();
  });

  it('narration_unavailable → "brief exists, audio unavailable" + brief-page link', async () => {
    mockFetchNarration.mockResolvedValue({ state: 'narration_unavailable' });
    mockFetchAudio.mockResolvedValue({ kind: 'narration_unavailable' });
    render(<PlayerPage />);
    await waitFor(() => expect(screen.queryByTestId('player-narration-unavailable')).not.toBeNull());
    expect(screen.getByTestId('player-narration-link').getAttribute('href')).toBe('/brief');
  });

  it('an empty narration dict → the "nothing to play" state', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([]) });
    mockFetchAudio.mockResolvedValue({ kind: 'unavailable' });
    render(<PlayerPage />);
    await waitFor(() => expect(screen.queryByTestId('player-empty')).not.toBeNull());
  });
});

describe('PlayerPage — slides + degradation', () => {
  it('slides + audio → the player renders the first slide + the <audio> element (no text-along banner)', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([seg('day_state'), seg('health'), seg('sign_off')]) });
    mockFetchAudio.mockResolvedValue({ kind: 'audio', url: 'blob:brief' });
    render(<PlayerPage />);
    await waitFor(() => expect(screen.queryByTestId('player')).not.toBeNull());
    expect(screen.getByTestId('player-slide').getAttribute('data-section')).toBe('day_state');
    expect(screen.queryByTestId('player-audio')).not.toBeNull();
    expect(screen.queryByTestId('player-textalong')).toBeNull();
  });

  it('slides + tts_not_configured → text-along (slides, NO audio element, honest banner)', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([seg('day_state')]) });
    mockFetchAudio.mockResolvedValue({ kind: 'tts_not_configured' });
    render(<PlayerPage />);
    await waitFor(() => expect(screen.queryByTestId('player')).not.toBeNull());
    expect(screen.queryByTestId('player-textalong')).not.toBeNull();
    expect(screen.queryByTestId('player-audio')).toBeNull();
  });

  it('slides + unavailable → text-along too (never a broken play button)', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([seg('day_state')]) });
    mockFetchAudio.mockResolvedValue({ kind: 'unavailable' });
    render(<PlayerPage />);
    await waitFor(() => expect(screen.queryByTestId('player-textalong')).not.toBeNull());
    expect(screen.queryByTestId('player-audio')).toBeNull();
  });
});

describe('PlayerPage — controls + first-class interruption (C3c ask-wiring)', () => {
  it('play → pause; ask opens the first-class ask surface with a live keyboard; resume returns to playing', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([seg('day_state'), seg('health')]) });
    mockFetchAudio.mockResolvedValue({ kind: 'audio', url: 'blob:x' });
    render(<PlayerPage />);
    await waitFor(() => expect(screen.queryByTestId('player')).not.toBeNull());

    expect(screen.queryByTestId('player-play')).not.toBeNull(); // idle → play
    act(() => fireEvent.click(screen.getByTestId('player-play')));
    expect(screen.queryByTestId('player-pause')).not.toBeNull(); // playing

    act(() => fireEvent.click(screen.getByTestId('player-ask-open')));
    expect(screen.queryByTestId('player-ask')).not.toBeNull(); // first-class ask surface
    expect(screen.queryByTestId('player-ask-input')).not.toBeNull(); // keyboard ALWAYS live (real, no stub)

    act(() => fireEvent.click(screen.getByTestId('player-resume')));
    expect(screen.queryByTestId('player-ask')).toBeNull();
    expect(screen.queryByTestId('player-pause')).not.toBeNull(); // resumed → playing
  });

  // THE C3c gate: ask → real chat roundtrip (mocked) → resume returns to the SAME slide,
  // AND the primer carries the CURRENT (paused) section — not slide 0.
  it('ask carries the paused slide as the primer, answers as a card, resumes at position', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([seg('day_state'), seg('health')]) });
    mockFetchAudio.mockResolvedValue({ kind: 'audio', url: 'blob:x' });
    mockTurn.mockResolvedValue({ reply: 'A driver is out, so it needs your eyes.', session_key: 'stored-sess', ts: '', user_ts: '' });
    render(<PlayerPage />);
    await waitFor(() => expect(screen.queryByTestId('player')).not.toBeNull());

    act(() => fireEvent.click(screen.getByTestId('player-play')));
    // Advance to slide 1 (health) — the paused slide the primer must reflect.
    act(() => fireEvent.click(screen.getByTestId('player-next')));
    expect(screen.getByTestId('player-slide').getAttribute('data-section')).toBe('health');

    act(() => fireEvent.click(screen.getByTestId('player-ask-open')));
    act(() => fireEvent.change(screen.getByTestId('player-ask-input'), { target: { value: 'why is that yellow?' } }));
    await act(async () => { fireEvent.click(screen.getByTestId('player-ask-send')); });

    await waitFor(() => expect(screen.queryByTestId('player-ask-answer')).not.toBeNull());
    expect(screen.getByTestId('player-ask-answer').textContent).toContain('A driver is out');
    // Primer-content correctness: brief_date + the CURRENT section (health), not slide 0.
    const [, message, opts] = mockTurn.mock.calls[0];
    expect(message).toBe('why is that yellow?');
    expect(opts.primer).toEqual({ brief_date: '2026-08-01', section_id: 'health' });

    // Resume returns to the held slide (health), not the top.
    act(() => fireEvent.click(screen.getByTestId('player-resume')));
    expect(screen.queryByTestId('player-ask')).toBeNull();
    expect(screen.getByTestId('player-slide').getAttribute('data-section')).toBe('health');
  });

  it('the STT "Use" prefills the keyboard input (mic path → confirm → send)', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([seg('day_state')]) });
    mockFetchAudio.mockResolvedValue({ kind: 'audio', url: 'blob:x' });
    mockTurn.mockResolvedValue({ reply: 'ok', session_key: 'stored-sess', ts: '', user_ts: '' });
    render(<PlayerPage />);
    await waitFor(() => expect(screen.queryByTestId('player')).not.toBeNull());

    act(() => fireEvent.click(screen.getByTestId('player-play')));
    act(() => fireEvent.click(screen.getByTestId('player-ask-open')));
    // STT confirms a transcript → it prefills the input (operator reviews before sending).
    act(() => fireEvent.click(screen.getByTestId('player-ask-voice-mock-use')));
    expect((screen.getByTestId('player-ask-input') as HTMLTextAreaElement).value).toBe('why is that yellow?');

    await act(async () => { fireEvent.click(screen.getByTestId('player-ask-send')); });
    await waitFor(() => expect(mockTurn).toHaveBeenCalledTimes(1));
    expect(mockTurn.mock.calls[0][1]).toBe('why is that yellow?');
  });

  it('an ask failure shows an honest error and the keyboard stays live (no dead mic)', async () => {
    mockFetchNarration.mockResolvedValue({ narration: narration([seg('day_state')]) });
    mockFetchAudio.mockResolvedValue({ kind: 'audio', url: 'blob:x' });
    mockTurn.mockRejectedValue(new ApiError(502, 'engine_error'));
    render(<PlayerPage />);
    await waitFor(() => expect(screen.queryByTestId('player')).not.toBeNull());

    act(() => fireEvent.click(screen.getByTestId('player-play')));
    act(() => fireEvent.click(screen.getByTestId('player-ask-open')));
    act(() => fireEvent.change(screen.getByTestId('player-ask-input'), { target: { value: 'hmm' } }));
    await act(async () => { fireEvent.click(screen.getByTestId('player-ask-send')); });

    await waitFor(() => expect(screen.queryByTestId('player-ask-error')).not.toBeNull());
    // The keyboard input is still present and usable (the STT-fail / send-fail contract).
    expect(screen.queryByTestId('player-ask-input')).not.toBeNull();
    expect(screen.queryByTestId('player-ask-answer')).toBeNull();
  });
});
