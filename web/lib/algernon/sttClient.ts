import { postBlob } from './http';
import type { SttTranscribeResponse } from './types';

// BROWSER-side STT client. Talks ONLY to the same-origin BFF
// (`/api/stt/transcribe`), never the transport directly — the BFF holds the peer
// token + relays the session token. Sends the raw audio blob with its own mime as
// Content-Type. Errors surface as `ApiError` (see ./http). A blob with no type
// (some MediaRecorder builds) falls back to audio/webm so the BFF mime guard has
// a value to validate.
export const sttClient = {
  transcribe: (blob: Blob): Promise<SttTranscribeResponse> =>
    postBlob<SttTranscribeResponse>(
      '/api/stt/transcribe',
      blob,
      blob.type || 'audio/webm',
    ),
};
