import { useCallback, useEffect, useRef, useState } from 'react';

// MediaRecorder hook for browser voice capture. getUserMedia → record → stop
// yields a Blob the caller POSTs to STT. Per the intentionally-left-blank
// standard, permission-denied / unsupported-browser / no-device are EXPLICIT
// error states (never a silent dead button). The mic is released (all tracks
// stopped) on stop so the browser's recording indicator clears.

// Preference order: Opus-in-WebM (Chrome/Firefox), then MP4/AAC (Safari), then
// bare container types. '' lets the browser pick its default if none match.
const MIME_CANDIDATES = [
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
  'audio/ogg;codecs=opus',
  'audio/ogg',
];

export type RecorderErrorCode =
  | 'unsupported'
  | 'permission-denied'
  | 'no-device'
  | 'mic-error';

export interface RecorderError {
  code: RecorderErrorCode;
  message: string;
}

export interface UseRecorder {
  recording: boolean;
  supported: boolean;
  error: RecorderError | null;
  start: () => Promise<void>;
  /** Stop + release the mic, resolving the recorded Blob (or null if not recording). */
  stop: () => Promise<Blob | null>;
  /** Clear any error so the control is re-armable. */
  reset: () => void;
}

function isSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices &&
    typeof navigator.mediaDevices.getUserMedia === 'function' &&
    typeof MediaRecorder !== 'undefined'
  );
}

function pickMimeType(): string {
  if (typeof MediaRecorder === 'undefined' || !MediaRecorder.isTypeSupported) return '';
  for (const candidate of MIME_CANDIDATES) {
    if (MediaRecorder.isTypeSupported(candidate)) return candidate;
  }
  return '';
}

function mapGetUserMediaError(e: unknown): RecorderError {
  const name = (e as { name?: string })?.name || '';
  if (name === 'NotAllowedError' || name === 'SecurityError' || name === 'PermissionDeniedError') {
    return { code: 'permission-denied', message: 'Microphone access was blocked.' };
  }
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return { code: 'no-device', message: 'No microphone was found.' };
  }
  return { code: 'mic-error', message: 'Could not start recording.' };
}

export function useRecorder(): UseRecorder {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<RecorderError | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const supported = isSupported();

  const releaseStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
  }, []);

  // Safety net: release the mic if the component unmounts mid-recording.
  useEffect(() => () => releaseStream(), [releaseStream]);

  const start = useCallback(async () => {
    setError(null);
    if (!supported) {
      setError({
        code: 'unsupported',
        message: 'Voice recording isn’t supported in this browser. Upload an audio file instead.',
      });
      return;
    }
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      setError(mapGetUserMediaError(e));
      return;
    }
    try {
      const mimeType = pickMimeType();
      const recorder = mimeType
        ? new MediaRecorder(stream, { mimeType })
        : new MediaRecorder(stream);
      chunksRef.current = [];
      recorder.ondataavailable = (ev: BlobEvent) => {
        if (ev.data && ev.data.size > 0) chunksRef.current.push(ev.data);
      };
      recorderRef.current = recorder;
      streamRef.current = stream;
      recorder.start();
      setRecording(true);
    } catch {
      // MediaRecorder construction / start failed — release the just-opened mic.
      stream.getTracks().forEach((t) => t.stop());
      setError({ code: 'mic-error', message: 'Could not start recording.' });
    }
  }, [supported]);

  const stop = useCallback((): Promise<Blob | null> => {
    const recorder = recorderRef.current;
    if (!recorder) return Promise.resolve(null);
    return new Promise<Blob | null>((resolve) => {
      recorder.onstop = () => {
        const type = recorder.mimeType || chunksRef.current[0]?.type || 'audio/webm';
        const blob = new Blob(chunksRef.current, { type });
        chunksRef.current = [];
        recorderRef.current = null;
        releaseStream();
        setRecording(false);
        resolve(blob.size > 0 ? blob : null);
      };
      try {
        recorder.stop();
      } catch {
        releaseStream();
        recorderRef.current = null;
        setRecording(false);
        resolve(null);
      }
    });
  }, [releaseStream]);

  const reset = useCallback(() => setError(null), []);

  return { recording, supported, error, start, stop, reset };
}
