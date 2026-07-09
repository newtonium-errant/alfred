import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { VoiceCapture } from '../components/chat/VoiceCapture';

// The audio-file-upload path exercises transcribe → editable review → Use/Discard
// WITHOUT MediaRecorder (jsdom lacks it). The editable transcript is the
// human-in-the-loop surface; low_confidence/empty surface a non-blocking notice
// but keep the field editable (never auto-commit a fallible transcript).

const { mockTranscribe } = vi.hoisted(() => ({ mockTranscribe: vi.fn() }));

vi.mock('../lib/algernon/sttClient', () => ({
  sttClient: { transcribe: mockTranscribe },
}));

beforeEach(() => {
  mockTranscribe.mockReset();
});

afterEach(() => {
  vi.restoreAllMocks();
});

function uploadAudio() {
  const input = screen.getByTestId('voice-file') as HTMLInputElement;
  const file = new File(['dummy-audio'], 'note.webm', { type: 'audio/webm' });
  fireEvent.change(input, { target: { files: [file] } });
}

describe('VoiceCapture (file upload)', () => {
  it('transcribes an uploaded file, shows the editable transcript, and Use emits it', async () => {
    mockTranscribe.mockResolvedValue({ transcript: 'hello world', low_confidence: false });
    const onTranscript = vi.fn();
    render(<VoiceCapture onTranscript={onTranscript} />);

    uploadAudio();

    const textarea = (await screen.findByTestId('voice-transcript')) as HTMLTextAreaElement;
    expect(textarea.value).toBe('hello world');
    expect(mockTranscribe).toHaveBeenCalledTimes(1);

    await userEvent.click(screen.getByTestId('voice-use'));
    expect(onTranscript).toHaveBeenCalledWith('hello world');
    // After Use, the review surface collapses.
    expect(screen.queryByTestId('voice-transcript')).toBeNull();
  });

  it('lets the operator edit the transcript before Use', async () => {
    mockTranscribe.mockResolvedValue({ transcript: 'helo wrld', low_confidence: true });
    const onTranscript = vi.fn();
    render(<VoiceCapture onTranscript={onTranscript} />);

    uploadAudio();
    const textarea = (await screen.findByTestId('voice-transcript')) as HTMLTextAreaElement;
    await userEvent.clear(textarea);
    await userEvent.type(textarea, 'hello world');

    await userEvent.click(screen.getByTestId('voice-use'));
    expect(onTranscript).toHaveBeenCalledWith('hello world');
  });

  it('surfaces a non-blocking notice on empty/low-confidence but keeps editing open', async () => {
    mockTranscribe.mockResolvedValue({ transcript: '', empty: true, low_confidence: true });
    render(<VoiceCapture onTranscript={vi.fn()} />);

    uploadAudio();
    expect(await screen.findByTestId('voice-notice')).not.toBeNull();
    // Field is still present + editable (not auto-committed).
    expect(screen.getByTestId('voice-transcript')).not.toBeNull();
  });

  it('shows an error and no transcript when transcription fails', async () => {
    mockTranscribe.mockRejectedValue(new Error('boom'));
    render(<VoiceCapture onTranscript={vi.fn()} />);

    uploadAudio();
    expect(await screen.findByTestId('voice-error')).not.toBeNull();
    expect(screen.queryByTestId('voice-transcript')).toBeNull();
  });

  it('Discard clears the review surface without emitting', async () => {
    mockTranscribe.mockResolvedValue({ transcript: 'throwaway' });
    const onTranscript = vi.fn();
    render(<VoiceCapture onTranscript={onTranscript} />);

    uploadAudio();
    await screen.findByTestId('voice-transcript');
    await userEvent.click(screen.getByTestId('voice-discard'));

    expect(onTranscript).not.toHaveBeenCalled();
    expect(screen.queryByTestId('voice-transcript')).toBeNull();
  });

  // --- the lost-message fix: blob retention + resend ---

  it('RETAINS the audio on a failed transcribe and "Try again" RESENDS the SAME blob (not a re-record)', async () => {
    // The incident: the server transcribed fine but the RESPONSE dropped on flaky
    // LTE. A retry of the SAME audio recovers the message.
    mockTranscribe
      .mockRejectedValueOnce(new Error('response dropped'))
      .mockResolvedValueOnce({ transcript: 'the long note that was nearly lost' });
    const onTranscript = vi.fn();
    render(<VoiceCapture onTranscript={onTranscript} />);

    uploadAudio();
    // Failure surfaces the error + a distinct "Try again" resend affordance.
    expect(await screen.findByTestId('voice-error')).not.toBeNull();
    const retry = await screen.findByTestId('voice-retry');
    expect(screen.queryByTestId('voice-transcript')).toBeNull();

    await userEvent.click(retry);

    // The retry re-ran transcribe with the SAME blob object — a resend, NOT a new recording.
    expect(mockTranscribe).toHaveBeenCalledTimes(2);
    expect(mockTranscribe.mock.calls[1][0]).toBe(mockTranscribe.mock.calls[0][0]);
    // The recovered transcript is now editable + committable.
    const textarea = (await screen.findByTestId('voice-transcript')) as HTMLTextAreaElement;
    expect(textarea.value).toBe('the long note that was nearly lost');
    await userEvent.click(screen.getByTestId('voice-use'));
    expect(onTranscript).toHaveBeenCalledWith('the long note that was nearly lost');
  });

  it('labels the record button "Record again" after a failure (distinct from the "Try again" resend)', async () => {
    mockTranscribe.mockRejectedValue(new Error('boom'));
    render(<VoiceCapture onTranscript={vi.fn()} />);
    uploadAudio();
    await screen.findByTestId('voice-retry');
    expect(screen.getByTestId('voice-record').textContent).toContain('Record again');
  });

  it('a SUCCESSFUL transcribe leaves no retry affordance (the audio is freed, not hoarded)', async () => {
    mockTranscribe.mockResolvedValue({ transcript: 'clean' });
    render(<VoiceCapture onTranscript={vi.fn()} />);
    uploadAudio();
    await screen.findByTestId('voice-transcript');
    expect(screen.queryByTestId('voice-retry')).toBeNull();
    expect(screen.queryByTestId('voice-error')).toBeNull();
  });

  it('a recovered retry that SUCCEEDS clears the retry affordance (freed after resend)', async () => {
    mockTranscribe
      .mockRejectedValueOnce(new Error('dropped'))
      .mockResolvedValueOnce({ transcript: 'recovered' });
    render(<VoiceCapture onTranscript={vi.fn()} />);
    uploadAudio();
    await userEvent.click(await screen.findByTestId('voice-retry'));
    await screen.findByTestId('voice-transcript');
    expect(screen.queryByTestId('voice-retry')).toBeNull(); // freed on the successful resend
  });
});
