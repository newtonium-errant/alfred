import { getJson, postJson } from './http';
import type {
  VoiceCloseResponse,
  VoiceConfigResponse,
  VoiceOfferResponse,
} from './types';

// BROWSER-side voice client. Talks ONLY to the same-origin BFF (`/api/voice/*`),
// never the transport directly — the BFF holds the peer token + relays the session
// token. Errors surface as `ApiError` (see ./http). Mirrors sttClient / chatApi:
// thin typed wrappers over the shared fetch helpers.
export const voiceApi = {
  // Pre-flight capability/ICE probe. Called FIRST in the connect flow so a
  // disabled/aiortc-missing backend fails BEFORE the mic is ever requested.
  config: (): Promise<VoiceConfigResponse> =>
    getJson<VoiceConfigResponse>('/api/voice/config'),
  // Vanilla-ICE signalling: the local offer (all candidates embedded) → the
  // server's answer. `sessionKey` is a V0 forward-hook (unused today; V1 binds the
  // call to a chat session) — relayed only when present.
  offer: (sdp: string, sessionKey?: string): Promise<VoiceOfferResponse> =>
    postJson<VoiceOfferResponse>('/api/voice/offer', {
      sdp,
      type: 'offer',
      ...(sessionKey ? { session_key: sessionKey } : {}),
    }),
  // Idempotent, owner-bound teardown. Used for reconciliation / explicit-await
  // closes; the hook's live teardown uses `sendVoiceCloseBeacon` below so it also
  // fires during pagehide/unmount (postJson can't set `keepalive`).
  close: (voiceSessionId: string): Promise<VoiceCloseResponse> =>
    postJson<VoiceCloseResponse>('/api/voice/close', {
      voice_session_id: voiceSessionId,
    }),
};

// Best-effort teardown beacon for hangup / pagehide / unmount. A `keepalive` fetch
// survives the document being torn down, unlike a normal fetch that the browser
// cancels. SECURITY W7: the body MUST be a Blob typed `application/json` — a plain
// JSON string body defaults the Content-Type to text/plain, which the transport's
// zod boundary rejects with a 400 (so the session would never actually close). We
// deliberately do NOT await or read the response (the document may be gone).
export function sendVoiceCloseBeacon(voiceSessionId: string): void {
  try {
    const blob = new Blob([JSON.stringify({ voice_session_id: voiceSessionId })], {
      type: 'application/json',
    });
    void fetch('/api/voice/close', { method: 'POST', body: blob, keepalive: true });
  } catch {
    /* best-effort: the reaper closes the session on its idle/absolute deadline. */
  }
}
