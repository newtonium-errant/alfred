import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import type { UseVoice, VoiceState } from '../lib/algernon/useVoice';
import { HOME_INSTANCE_NAME } from '../lib/algernon/instance';

// Component tests with useVoice MOCKED: the display flag (renders nothing when
// off), the Salem-only cross-instance guard, and per-state honeydew rendering +
// control wiring. The state-machine itself is covered in useVoice.test.ts.

const { mockUseVoice } = vi.hoisted(() => ({ mockUseVoice: vi.fn() }));

vi.mock('../lib/algernon/useVoice', () => ({ useVoice: mockUseVoice }));

import { VoicePanel } from '../components/chat/VoicePanel';

const actions = {
  start: vi.fn(),
  toggleMute: vi.fn(),
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
    render(<VoicePanel instance={HOME_INSTANCE_NAME} />);
    expect(screen.queryByTestId('voice-panel')).toBeNull();
  });

  it('renders NOTHING when the display flag is a non-truthy value', () => {
    vi.stubEnv('NEXT_PUBLIC_VOICE_ENABLED', '0');
    render(<VoicePanel instance={HOME_INSTANCE_NAME} />);
    expect(screen.queryByTestId('voice-panel')).toBeNull();
  });

  describe('with the display flag on', () => {
    beforeEach(() => vi.stubEnv('NEXT_PUBLIC_VOICE_ENABLED', '1'));

    it('disables voice + shows a Salem-only hint on a cross-instance selection', () => {
      render(<VoicePanel instance="KALLE" />);
      expect(screen.getByTestId('voice-panel')).not.toBeNull();
      const start = screen.getByTestId('voice-start') as HTMLButtonElement;
      expect(start.disabled).toBe(true);
      const hint = screen.getByTestId('voice-cross-instance-hint');
      expect(hint.textContent).toContain(HOME_INSTANCE_NAME);
    });

    it('shows an enabled Voice button in idle and wires start()', () => {
      render(<VoicePanel instance={HOME_INSTANCE_NAME} />);
      const start = screen.getByTestId('voice-start') as HTMLButtonElement;
      expect(start.disabled).toBe(false);
      expect(screen.getByTestId('voice-audio')).not.toBeNull(); // hidden audio always mounted
      fireEvent.click(start);
      expect(actions.start).toHaveBeenCalledTimes(1);
    });

    it('renders the Live pill + Mute/Hang up and wires them', () => {
      setVoice({ state: 'live', voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} />);
      expect(screen.getByTestId('voice-status').textContent).toContain('Live');
      fireEvent.click(screen.getByTestId('voice-mute'));
      expect(actions.toggleMute).toHaveBeenCalledTimes(1);
      fireEvent.click(screen.getByTestId('voice-hangup'));
      expect(actions.hangup).toHaveBeenCalledTimes(1);
    });

    it('shows the muted (amber) pill + Unmute label when muted', () => {
      setVoice({ state: 'live', muted: true, voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} />);
      expect(screen.getByTestId('voice-status').textContent).toContain('Muted');
      const mute = screen.getByTestId('voice-mute');
      expect(mute.textContent).toContain('Unmute');
      expect(mute.getAttribute('aria-pressed')).toBe('true');
    });

    it('shows a connecting status', () => {
      setVoice({ state: 'connecting' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} />);
      expect(screen.getByTestId('voice-status').textContent).toContain('Connecting');
    });

    it('renders an error banner with per-code copy + a reset affordance', () => {
      setVoice({
        state: 'error',
        error: { code: 'permission-denied', message: 'Microphone access was blocked.' },
      });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} />);
      const banner = screen.getByTestId('voice-error');
      expect(banner.getAttribute('role')).toBe('alert');
      expect(banner.textContent).toContain('Microphone access was blocked.');
      fireEvent.click(screen.getByTestId('voice-retry'));
      expect(actions.reset).toHaveBeenCalledTimes(1);
    });

    it('shows the audio-blocked recovery banner and wires retryAudio()', () => {
      setVoice({ state: 'live', audioBlocked: true, voiceSessionId: 'vs-1' });
      render(<VoicePanel instance={HOME_INSTANCE_NAME} />);
      expect(screen.getByTestId('voice-audio-blocked')).not.toBeNull();
      fireEvent.click(screen.getByTestId('voice-audio-unblock'));
      expect(actions.retryAudio).toHaveBeenCalledTimes(1);
    });
  });
});
