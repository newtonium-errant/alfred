import { postBlob } from './http';
import { STT_IDEMPOTENCY_HEADER } from './schemas';
import type { SttTranscribeResponse } from './types';

// A long dictated note on flaky mobile is a slow upload + transcribe; give STT a
// far more generous ceiling than the default JSON budget so the FE never gives up
// on a legit long transcribe (the lost-message incident), while a genuinely dead
// connection still surfaces (bounded) so the retry affordance appears.
const STT_TIMEOUT_MS = 180000; // 3 min

// The SHA-256 hex of the audio bytes — a CONTENT-ADDRESSED idempotency key. Same
// audio ⇒ same key, so a retry of the SAME blob (VoiceCapture retains it across a
// dropped response) hits the backend's dedup cache and returns the cached
// transcript with no re-transcribe / no double-charge. No key to mint or hold —
// it's derived from the content on each attempt.
async function sttIdempotencyKey(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('');
}

// BROWSER-side STT client. Talks ONLY to the same-origin BFF
// (`/api/stt/transcribe`), never the transport directly — the BFF holds the peer
// token + relays the session token. Sends the raw audio blob with its own mime as
// Content-Type + the content-hash idempotency header. Errors surface as `ApiError`
// (see ./http). A blob with no type (some MediaRecorder builds) falls back to
// audio/webm so the BFF mime guard has a value to validate.
export const sttClient = {
  transcribe: async (blob: Blob): Promise<SttTranscribeResponse> =>
    postBlob<SttTranscribeResponse>('/api/stt/transcribe', blob, blob.type || 'audio/webm', {
      timeoutMs: STT_TIMEOUT_MS,
      headers: { [STT_IDEMPOTENCY_HEADER]: await sttIdempotencyKey(blob) },
    }),
};
