import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { UseVoice, VoiceState, VoiceTurnState } from '../lib/algernon/useVoice';
import { HOME_INSTANCE_NAME } from '../lib/algernon/instance';

// Component tests with useVoice MOCKED: the display flag (renders nothing when
// off), the Salem-only cross-instance guard, start-gating on sessionKey, and the
// V1 dictation surface (sub-state pill, transcript / reply / tool / turn-error
// regions). The state-machine itself is covered in useVoice(.Dictation).test.ts.

const { mockUseVoice } = vi.hoisted(() => ({ mockUseVoice: vi.fn() }));

vi.mock('../lib/algernon/useVoice', () => ({ useVoice: mockUseVoice }));

import { VoicePanel } from '../components/chat/VoicePanel';

const actions = {
  start: vi.fn(),
  toggleMute: vi.fn(),
  toggleSpeakerMute: vi.fn(),
  cancelTurn: vi.fn(),
  hangup: vi.fn(),
  retryAudio: vi.fn(),
  reset: vi.fn(),
};

function setVoice(overrides: Partial<UseVoice> = {}) {
  mockUseVoice.mockReturnValue({
    state: 'idle' as VoiceState,
    muted: false,
    audioBlocked: false,
    error: null,
    voiceSessionId: null,
    voiceTurnState: 'listening' as VoiceTurnState,
    partialTranscript: '',
    replyText: '',
    turnError: null,
    toolName: null,
    dictationUnavailable: false,
    speakerMuted: false,
    ttsUnavailable: false,
    discardNotice: false,
    canCancel: false,
    ...actions,
    ...overrides,
  });
}

beforeEach(() => {
  Object.values(actions).forEach((f) => f.mockReset());
  mockUseVoice.mockReset();
  setVoice();
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

describe('VoicePanel', () => {
  it('renders NOTHING when the display flag is absent', () => {
    render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
    expect(screen.queryByTestId('voice-panel')).toBeNull();
  });

  it('renders NOTHING when the display flag is a non-truthy value', () => {
    vi.stubEnv('NEXT_PUBLIC_VOICE_ENABLED', '0');
    render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
    expect(screen.queryByTestId('voice-panel')).toBeNull();
  });

  describe('with the display flag on', () => {
    beforeEach(() => vi.stubEnv('NEXT_PUBLIC_VOICE_ENABLED', '1'));

    it('disables voice + shows a Salem-only hint on a cross-instance selection', () => {
      render(<VoicePanel instance="KALLE" sessionKey="s1" />);
      expect(screen.getByTestId('voice-panel')).not.toBeNull();
      const start = screen.getByTestId('voice-start') as HTMLButtonElement;
      expect(start.disabled).toBe(true);
      expect(screen.getByTestId('voice-cross-instance-hint').textContent).toContain(
        HOME_INSTANCE_NAME,
      );
    });

    it('shows an enabled Voice button in idle once a sessionKey exists and wires start()', () => {
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      const start = screen.getByTestId('voice-start') as HTMLButtonElement;
      expect(start.disabled).toBe(false);
      expect(screen.getByTestId('voice-audio')).not.toBeNull(); // hidden audio always mounted
      fireEvent.click(start);
      expect(actions.start).toHaveBeenCalledTimes(1);
    });

    it('disables start (with a loading hint) while the chat session is still booting', () => {
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey={null} />);
      const start = screen.getByTestId('voice-start') as HTMLButtonElement;
      expect(start.disabled).toBe(true);
      expect(screen.getByTestId('voice-loading-hint')).not.toBeNull();
      // Distinct from the cross-instance case (no Salem-only hint here).
      expect(screen.queryByTestId('voice-cross-instance-hint')).toBeNull();
    });

    it('live + listening shows the Listening pill + Mute/Hang up (no Stop)', () => {
      setVoice({ state: 'live', voiceTurnState: 'listening', voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.getByTestId('voice-status').textContent).toContain('Listening');
      expect(screen.queryByTestId('voice-cancel')).toBeNull();
      fireEvent.click(screen.getByTestId('voice-mute'));
      expect(actions.toggleMute).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByTestId('voice-hangup'));
      expect(actions.hangup).toHaveBeenCalledTimes(1);
    });

    it('thinking shows the Thinking pill + a Stop (cancel) control', () => {
      setVoice({ state: 'live', voiceTurnState: 'thinking', voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.getByTestId('voice-status').textContent).toContain('Thinking');
      fireEvent.click(screen.getByTestId('voice-cancel'));
      expect(actions.cancelTurn).toHaveBeenCalledTimes(1);
    });

    it('replying shows the Replying pill', () => {
      setVoice({ state: 'live', voiceTurnState: 'replying', voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.getByTestId('voice-status').textContent).toContain('Replying');
    });

    it('muted pill copy takes precedence over the sub-state', () => {
      setVoice({ state: 'live', muted: true, voiceTurnState: 'replying', voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.getByTestId('voice-status').textContent).toContain('Muted');
      const mute = screen.getByTestId('voice-mute');
      expect(mute.textContent).toContain('Unmute');
      expect(mute.getAttribute('aria-pressed')).toBe('true');
    });

    // --- V2 talk-back ---
    it('speaking shows the Speaking pill and outranks mic-muted', () => {
      setVoice({ state: 'live', muted: true, voiceTurnState: 'speaking', voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      // 'Speaking…' reports the audible activity even while the mic is muted;
      // mic-mute stays visible via the Mute button's own label + aria-pressed.
      expect(screen.getByTestId('voice-status').textContent).toContain('Speaking');
      expect(screen.getByTestId('voice-mute').getAttribute('aria-pressed')).toBe('true');
    });

    it('Stop shows during speaking ONLY while the turn is still cancellable', () => {
      setVoice({ state: 'live', voiceTurnState: 'speaking', canCancel: true, voiceSessionId: 'vs-1' });
      const { rerender } = render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.getByTestId('voice-cancel')).not.toBeNull();
      // Post-final playout (canCancel false): the Stop control hides (cancel would no-op).
      setVoice({ state: 'live', voiceTurnState: 'speaking', canCancel: false, voiceSessionId: 'vs-1' });
      rerender(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.queryByTestId('voice-cancel')).toBeNull();
    });

    it('wires the speaker-mute control (aria-pressed reflects speakerMuted)', () => {
      setVoice({ state: 'live', voiceTurnState: 'speaking', speakerMuted: true, voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      const sp = screen.getByTestId('voice-speaker-mute');
      expect(sp.textContent).toContain('Unmute speaker');
      expect(sp.getAttribute('aria-pressed')).toBe('true');
      fireEvent.click(sp);
      expect(actions.toggleSpeakerMute).toHaveBeenCalledTimes(1);
    });

    it('renders the tts-unavailable + utterance-discarded notices when set (live-only)', () => {
      setVoice({
        state: 'live',
        voiceTurnState: 'speaking',
        ttsUnavailable: true,
        discardNotice: true,
        voiceSessionId: 'vs-1',
      });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.getByTestId('voice-tts-unavailable').textContent).toContain('as text');
      expect(screen.getByTestId('voice-discard-notice').textContent).toContain('Heard you');
    });

    it('omits the V2 notices when unset', () => {
      setVoice({ state: 'live', voiceTurnState: 'listening', voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.queryByTestId('voice-tts-unavailable')).toBeNull();
      expect(screen.queryByTestId('voice-discard-notice')).toBeNull();
    });

    it('renders the live transcript, streaming reply, tool line, and turn-error regions', () => {
      setVoice({
        state: 'live',
        voiceTurnState: 'replying',
        partialTranscript: 'what is on my calendar',
        replyText: 'You have two meetings.',
        toolName: 'vault_search',
        turnError: 'That didn’t go through — try again.',
        voiceSessionId: 'vs-1',
      });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.getByTestId('voice-transcript').textContent).toContain('calendar');
      expect(screen.getByTestId('voice-reply').textContent).toContain('two meetings');
      expect(screen.getByTestId('voice-tool').textContent).toContain('vault_search');
      expect(screen.getByTestId('voice-turn-error').textContent).toContain('didn’t go through');
    });

    it('omits the dictation regions when they are empty', () => {
      setVoice({ state: 'live', voiceTurnState: 'listening', voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.queryByTestId('voice-transcript')).toBeNull();
      expect(screen.queryByTestId('voice-reply')).toBeNull();
      expect(screen.queryByTestId('voice-tool')).toBeNull();
      expect(screen.queryByTestId('voice-turn-error')).toBeNull();
    });

    it('shows the dictation-unavailable notice when the server never confirmed', () => {
      setVoice({ state: 'live', dictationUnavailable: true, voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.getByTestId('voice-dictation-unavailable')).not.toBeNull();
    });

    it('shows a connecting status', () => {
      setVoice({ state: 'connecting' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.getByTestId('voice-status').textContent).toContain('Connecting');
    });

    it('renders an error banner (covers the new codes) + a reset affordance', () => {
      setVoice({
        state: 'error',
        error: { code: 'channel-failed', message: 'The voice data link dropped. Try again.' },
      });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      const banner = screen.getByTestId('voice-error');
      expect(banner.getAttribute('role')).toBe('alert');
      expect(banner.textContent).toContain('data link');
      fireEvent.click(screen.getByTestId('voice-retry'));
      expect(actions.reset).toHaveBeenCalledTimes(1);
    });

    it('shows the audio-blocked recovery banner and wires retryAudio()', () => {
      setVoice({ state: 'live', audioBlocked: true, voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} sessionKey="s1" />);
      expect(screen.getByTestId('voice-audio-blocked')).not.toBeNull();
      fireEvent.click(screen.getByTestId('voice-audio-unblock'));
      expect(actions.retryAudio).toHaveBeenCalledTimes(1);
    });
  });
});
