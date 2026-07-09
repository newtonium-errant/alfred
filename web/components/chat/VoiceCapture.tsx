import { ChangeEvent, useCallback, useRef, useState } from 'react';
import { Button } from '../ui/button';
import { Textarea } from '../ui/textarea';
import { sttClient } from '../../lib/algernon/sttClient';
import { useRecorder } from '../../lib/algernon/useRecorder';
import { ApiError } from '../../lib/algernon/http';
import type { SttTranscribeResponse } from '../../lib/algernon/types';

// REUSABLE voice capture: a mic record toggle + an audio-file upload that
// transcribe to text via the BFF, then show an EDITABLE transcript the operator
// confirms (Use) or drops (Discard) before it commits. Wired into BOTH the chat
// Composer and the ingest body (decision F). Per the self-correcting standard the
// editable field IS the human-in-the-loop correction surface; per
// intentionally-left-blank the low_confidence/empty/degraded signals surface as a
// NON-blocking notice (the field stays editable — a fallible transcript is never
// auto-committed). `onTranscript` fires only on an explicit Use.

type Phase = 'idle' | 'transcribing' | 'review';

function sttErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.code) {
      case 'unsupported_media_type':
        return 'That audio format isn’t supported. Try a different file.';
      case 'audio_too_large':
        return 'That recording is too large (max 25 MB).';
      case 'invalid_session':
        return 'Your session has ended — please sign in again.';
      case 'no_audio':
        return 'No audio was captured — try again.';
      case 'stt_failed':
      case 'transport_unreachable':
      case 'network_error':
        return 'Couldn’t transcribe that right now. Try again, or type it instead.';
      default:
        return 'Couldn’t transcribe that. Try again, or type it instead.';
    }
  }
  return 'Couldn’t transcribe that. Try again, or type it instead.';
}

function noticeFor(r: SttTranscribeResponse): string | null {
  if (r.empty) return 'Couldn’t make out any speech — type below or re-record.';
  if (r.degraded || r.low_confidence) {
    return 'Couldn’t make that out clearly — please review the transcript before using it.';
  }
  return null;
}

export function VoiceCapture({
  onTranscript,
  disabled = false,
  idPrefix = 'voice',
}: {
  onTranscript: (text: string) => void;
  disabled?: boolean;
  /** testid prefix so multiple instances (chat + ingest) don't collide. */
  idPrefix?: string;
}) {
  const recorder = useRecorder();
  const [phase, setPhase] = useState<Phase>('idle');
  const [transcript, setTranscript] = useState('');
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The last audio we tried to transcribe, RETAINED across a failure so the user
  // can RESEND the same recording instead of re-recording from scratch — the
  // lost-message fix (a dropped response on a successful server transcribe would
  // otherwise throw the audio away). Freed on success (no hoarding of mic data) and
  // on an explicit discard / re-record.
  const lastBlobRef = useRef<Blob | null>(null);
  const [retryable, setRetryable] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  const runTranscribe = useCallback(async (blob: Blob) => {
    lastBlobRef.current = blob; // retain BEFORE the request so a drop is recoverable
    setError(null);
    setNotice(null);
    setRetryable(false);
    setPhase('transcribing');
    try {
      const r = await sttClient.transcribe(blob);
      lastBlobRef.current = null; // success — free the audio (don't hoard mic data)
      setTranscript(r.transcript || '');
      setNotice(noticeFor(r));
      setPhase('review');
    } catch (e) {
      setError(sttErrorMessage(e));
      setRetryable(true); // the blob is retained → offer a RESEND, not a re-record
      setPhase('idle');
    }
  }, []);

  // Resend the SAME retained audio (a dropped/timed-out response on a transcribe
  // that likely SUCCEEDED server-side — retry recovers the message).
  const onRetry = useCallback(() => {
    const blob = lastBlobRef.current;
    if (!blob) return;
    void runTranscribe(blob);
  }, [runTranscribe]);

  const onStartStop = useCallback(async () => {
    if (recorder.recording) {
      const blob = await recorder.stop();
      if (blob) {
        await runTranscribe(blob);
      } else {
        setError('No audio was captured — try again.');
      }
      return;
    }
    // Starting a NEW recording = start over; drop any retained retry audio.
    lastBlobRef.current = null;
    setRetryable(false);
    setError(null);
    setNotice(null);
    recorder.reset();
    await recorder.start();
  }, [recorder, runTranscribe]);

  const onFile = useCallback(
    async (e: ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      // Reset the input so selecting the same file again re-fires change.
      e.target.value = '';
      if (file) await runTranscribe(file);
    },
    [runTranscribe],
  );

  const onUse = useCallback(() => {
    onTranscript(transcript);
    setTranscript('');
    setNotice(null);
    setPhase('idle');
  }, [onTranscript, transcript]);

  const onDiscard = useCallback(() => {
    lastBlobRef.current = null; // explicit give-up — free the retained audio
    setRetryable(false);
    setTranscript('');
    setNotice(null);
    setError(null);
    setPhase('idle');
  }, []);

  const busy = phase === 'transcribing';
  const controlsDisabled = disabled || busy;

  return (
    <div className="flex flex-col gap-2" data-testid={`${idPrefix}-capture`}>
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant={recorder.recording ? 'destructive' : 'outline'}
          size="sm"
          data-testid={`${idPrefix}-record`}
          disabled={controlsDisabled}
          aria-pressed={recorder.recording}
          onClick={() => void onStartStop()}
        >
          {recorder.recording ? '■ Stop' : retryable ? '🎤 Record again' : '🎤 Record'}
        </Button>

        <label
          className="inline-flex cursor-pointer items-center gap-2 rounded-xl border border-honeydew-300 bg-white px-3 py-1.5 text-sm font-semibold text-honeydew-700 hover:bg-honeydew-50"
          data-testid={`${idPrefix}-file-label`}
        >
          Upload audio
          <input
            ref={fileRef}
            type="file"
            accept="audio/*"
            className="hidden"
            data-testid={`${idPrefix}-file`}
            disabled={controlsDisabled}
            onChange={(e) => void onFile(e)}
          />
        </label>

        {busy && (
          // Intentionally-left-blank: an explicit working signal, not a dead UI.
          <span data-testid={`${idPrefix}-status`} className="text-sm text-honeydew-600">
            Transcribing…
          </span>
        )}
      </div>

      {recorder.error && (
        <p
          role="alert"
          data-testid={`${idPrefix}-recorder-error`}
          className="text-sm text-danger"
        >
          {recorder.error.message}
        </p>
      )}

      {error && (
        <div role="alert" data-testid={`${idPrefix}-error`} className="flex flex-col gap-1">
          <p className="text-sm text-danger">{error}</p>
          {retryable && (
            // Resend the SAME recording (the message is likely already transcribed
            // server-side; a retry recovers it). Distinct from "Record again", which
            // starts over — so a dropped response never forces a re-record.
            <div className="flex items-center gap-2">
              <Button
                type="button"
                size="sm"
                data-testid={`${idPrefix}-retry`}
                disabled={controlsDisabled}
                onClick={onRetry}
              >
                Try again
              </Button>
              <span className="text-xs text-honeydew-600">
                Resends the recording you just made.
              </span>
            </div>
          )}
        </div>
      )}

      {phase === 'review' && (
        <div className="flex flex-col gap-2">
          {notice && (
            <p data-testid={`${idPrefix}-notice`} className="text-sm text-honeydew-600">
              {notice}
            </p>
          )}
          <Textarea
            data-testid={`${idPrefix}-transcript`}
            aria-label="Transcript"
            rows={3}
            value={transcript}
            disabled={disabled}
            onChange={(e) => setTranscript(e.target.value)}
          />
          <div className="flex items-center gap-2">
            <Button
              type="button"
              size="sm"
              data-testid={`${idPrefix}-use`}
              disabled={disabled || transcript.trim().length === 0}
              onClick={onUse}
            >
              Use
            </Button>
            <Button
              type="button"
              variant="ghost"
              size="sm"
              data-testid={`${idPrefix}-discard`}
              onClick={onDiscard}
            >
              Discard
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
