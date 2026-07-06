import { RefObject, useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from './http';
import { sendVoiceCloseBeacon, voiceApi } from './voiceClient';
import type { VoiceConfigResponse, VoiceOfferResponse } from './types';

// V0 voice engine: the WebRTC echo client as a state machine. The mic tap IS the
// user gesture (so autoplay of the echoed audio is allowed). Flow: config probe
// (pre-mic, fail-closed) → getUserMedia → RTCPeerConnection with the server's ICE
// servers → vanilla (non-trickle) offer/answer → live. Per intentionally-left-
// blank every failure is an EXPLICIT error state, never a silent dead control.
//
// audio-blocked is NOT a terminal error: it's an orthogonal flag (the pc stays
// live) surfaced as a "tap to enable audio" banner — a browser autoplay edge, not
// a call failure (CONTRACT §8).

export type VoiceState =
  | 'idle'
  | 'requesting-mic'
  | 'connecting'
  | 'live'
  | 'closing'
  | 'error';

export type VoiceErrorCode =
  | 'unsupported'
  | 'permission-denied'
  | 'no-device'
  | 'mic-error'
  | 'voice-disabled'
  | 'voice-busy'
  | 'session-expired'
  | 'signaling-failed'
  | 'connection-failed';

export interface VoiceError {
  code: VoiceErrorCode;
  message: string;
}

export interface UseVoice {
  state: VoiceState;
  muted: boolean;
  /** Remote audio playback is blocked (autoplay) — pc stays live; recover via retryAudio. */
  audioBlocked: boolean;
  error: VoiceError | null;
  voiceSessionId: string | null;
  /** The user gesture — request mic + negotiate. Only acts from idle. */
  start: () => Promise<void>;
  /** Flip the mic track (live only). No renegotiation. */
  toggleMute: () => void;
  /** Tear the call down (best-effort close beacon + local teardown). */
  hangup: () => void;
  /** Re-attempt audio.play() from a fresh user gesture (audio-blocked recovery). */
  retryAudio: () => void;
  /** Clear a terminal error → idle (re-armable). */
  reset: () => void;
}

const GATHER_TIMEOUT_MS = 3000; // vanilla-ICE gather-complete guard (host-only ⇒ sub-second)
const CONNECT_WATCHDOG_MS = 10000; // connecting must reach 'connected' within this
const DISCONNECT_GRACE_MS = 4000; // a transient 'disconnected' may recover within this

const ERROR_MESSAGES: Record<VoiceErrorCode, string> = {
  unsupported: 'Voice isn’t supported in this browser.',
  'permission-denied': 'Microphone access was blocked.',
  'no-device': 'No microphone was found.',
  'mic-error': 'Couldn’t start the microphone.',
  'voice-disabled': 'Voice isn’t available right now.',
  'voice-busy': 'Voice is busy right now — try again in a moment.',
  'session-expired': 'Your session has ended — please sign in again.',
  'signaling-failed': 'Couldn’t reach the voice service. Try again.',
  'connection-failed': 'The voice connection dropped. Try again.',
};

function isSupported(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices &&
    typeof navigator.mediaDevices.getUserMedia === 'function' &&
    typeof RTCPeerConnection !== 'undefined'
  );
}

// Reuse the useRecorder mic-error taxonomy so the two capture surfaces agree.
function mapGetUserMediaError(e: unknown): VoiceErrorCode {
  const name = (e as { name?: string })?.name || '';
  if (name === 'NotAllowedError' || name === 'SecurityError' || name === 'PermissionDeniedError') {
    return 'permission-denied';
  }
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return 'no-device';
  }
  return 'mic-error';
}

// Map a signalling ApiError to a terminal voice error code. Both the BFF-added
// codes and the relayed transport codes are covered; a bare 404 means the voice
// routes are unmounted server-side (feature off) ⇒ treat as disabled.
function mapSignalError(e: unknown): VoiceErrorCode {
  if (e instanceof ApiError) {
    if (e.status === 401 || e.code === 'invalid_session') return 'session-expired';
    if (e.status === 429 || e.code === 'too_many_sessions') return 'voice-busy';
    if (e.status === 404) return 'voice-disabled';
    if (e.status === 503 || e.code === 'voice_unavailable') return 'voice-disabled';
  }
  return 'signaling-failed';
}

export function useVoice(opts: { audioRef: RefObject<HTMLAudioElement>; enabled: boolean }): UseVoice {
  const { audioRef, enabled } = opts;

  const [state, setStateRaw] = useState<VoiceState>('idle');
  const [muted, setMuted] = useState(false);
  const [audioBlocked, setAudioBlocked] = useState(false);
  const [error, setError] = useState<VoiceError | null>(null);
  const [voiceSessionId, setVoiceSessionId] = useState<string | null>(null);

  // All live handles in refs so async callbacks never close over stale state.
  const stateRef = useRef<VoiceState>('idle');
  const pcRef = useRef<RTCPeerConnection | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const micTrackRef = useRef<MediaStreamTrack | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const closingRef = useRef(false); // suppress the 'closed' event we cause ourselves
  // A monotonic generation: any teardown / new start bumps it, and every awaited
  // step in start() re-checks it so an aborted attempt can't resurrect state.
  const genRef = useRef(0);
  // Timers.
  const watchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const disconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const setState = useCallback((s: VoiceState) => {
    stateRef.current = s;
    setStateRaw(s);
  }, []);

  const clearTimers = useCallback(() => {
    if (watchdogRef.current) {
      clearTimeout(watchdogRef.current);
      watchdogRef.current = null;
    }
    if (disconnectRef.current) {
      clearTimeout(disconnectRef.current);
      disconnectRef.current = null;
    }
  }, []);

  // Stop the mic, close the pc, detach playback, clear timers. NO network, NO
  // React state — safe to call from an unmount cleanup.
  const teardownLocal = useCallback(() => {
    clearTimers();
    closingRef.current = true;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    micTrackRef.current = null;
    const pc = pcRef.current;
    pcRef.current = null;
    if (pc) {
      pc.onconnectionstatechange = null;
      pc.ontrack = null;
      try {
        pc.close();
      } catch {
        /* already closed */
      }
    }
    if (audioRef.current) audioRef.current.srcObject = null;
  }, [audioRef, clearTimers]);

  const fail = useCallback(
    (code: VoiceErrorCode) => {
      genRef.current += 1; // invalidate any in-flight start()
      teardownLocal();
      sessionIdRef.current = null;
      setVoiceSessionId(null);
      setMuted(false);
      setAudioBlocked(false);
      setError({ code, message: ERROR_MESSAGES[code] });
      setState('error');
    },
    [setState, teardownLocal],
  );

  // Full teardown WITH the best-effort close beacon (voluntary hangup / lifecycle).
  const closeAndReset = useCallback(
    (nextState: VoiceState) => {
      genRef.current += 1;
      const id = sessionIdRef.current;
      if (id) sendVoiceCloseBeacon(id);
      teardownLocal();
      sessionIdRef.current = null;
      setVoiceSessionId(null);
      setMuted(false);
      setAudioBlocked(false);
      setError(null);
      setState(nextState);
    },
    [setState, teardownLocal],
  );

  const hangup = useCallback(() => {
    const s = stateRef.current;
    if (s === 'idle' || s === 'error' || s === 'closing') return;
    setState('closing');
    closeAndReset('idle');
  }, [closeAndReset, setState]);

  const toggleMute = useCallback(() => {
    if (stateRef.current !== 'live') return;
    const track = micTrackRef.current;
    if (!track) return;
    track.enabled = !track.enabled;
    setMuted(!track.enabled);
  }, []);

  const retryAudio = useCallback(() => {
    const el = audioRef.current;
    if (!el) return;
    void el
      .play()
      .then(() => setAudioBlocked(false))
      .catch(() => setAudioBlocked(true));
  }, [audioRef]);

  const reset = useCallback(() => {
    if (stateRef.current !== 'error') return;
    setError(null);
    setState('idle');
  }, [setState]);

  // Resolve once ICE gathering is complete (all candidates embedded — vanilla
  // ICE), or after a guard timeout (with host-only candidates completion is
  // sub-second; the guard just prevents a wedged 'connecting').
  const waitGatheringComplete = useCallback((pc: RTCPeerConnection): Promise<void> => {
    if (pc.iceGatheringState === 'complete') return Promise.resolve();
    return new Promise<void>((resolve) => {
      let done = false;
      const finish = () => {
        if (done) return;
        done = true;
        pc.removeEventListener('icegatheringstatechange', onChange);
        clearTimeout(timer);
        resolve();
      };
      const onChange = () => {
        if (pc.iceGatheringState === 'complete') finish();
      };
      const timer = setTimeout(finish, GATHER_TIMEOUT_MS);
      pc.addEventListener('icegatheringstatechange', onChange);
    });
  }, []);

  const start = useCallback(async () => {
    if (!enabled) return;
    if (stateRef.current !== 'idle') return;

    setError(null);
    setAudioBlocked(false);
    const gen = (genRef.current += 1);
    const stale = () => genRef.current !== gen;

    if (!isSupported()) {
      fail('unsupported');
      return;
    }

    setState('requesting-mic');

    // 1. Config probe FIRST — a disabled / aiortc-missing backend fails here,
    //    BEFORE the mic is ever requested (no spurious permission prompt).
    let config: VoiceConfigResponse;
    try {
      config = await voiceApi.config();
    } catch (e) {
      if (stale()) return;
      fail(mapSignalError(e));
      return;
    }
    if (stale()) return;
    if (!config.available) {
      fail('voice-disabled');
      return;
    }

    // 2. Mic — the tap that got us here is the gesture; constraints are hints
    //    (unsupported ones don't throw).
    let stream: MediaStream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: { ideal: 1 },
        },
      });
    } catch (e) {
      if (stale()) return;
      fail(mapGetUserMediaError(e));
      return;
    }
    if (stale()) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }

    // 3. Peer connection with the server-provided ICE servers (never hardcoded).
    const pc = new RTCPeerConnection({ iceServers: config.ice_servers ?? [] });
    pcRef.current = pc;
    streamRef.current = stream;
    closingRef.current = false;
    const micTrack = stream.getAudioTracks()[0] ?? null;
    micTrackRef.current = micTrack;
    if (micTrack) pc.addTrack(micTrack, stream);

    pc.ontrack = (ev: RTCTrackEvent) => {
      const el = audioRef.current;
      if (!el) return;
      el.srcObject = ev.streams[0] ?? new MediaStream([ev.track]);
      void el.play().catch(() => setAudioBlocked(true));
    };

    pc.onconnectionstatechange = () => {
      if (closingRef.current) return;
      const cs = pc.connectionState;
      if (cs === 'connected') {
        if (disconnectRef.current) {
          clearTimeout(disconnectRef.current);
          disconnectRef.current = null;
        }
        if (watchdogRef.current) {
          clearTimeout(watchdogRef.current);
          watchdogRef.current = null;
        }
        if (stateRef.current === 'connecting' || stateRef.current === 'live') {
          setState('live');
        }
      } else if (cs === 'failed') {
        fail('connection-failed');
      } else if (cs === 'closed') {
        // A close we didn't initiate (closingRef guards our own).
        fail('connection-failed');
      } else if (cs === 'disconnected') {
        if (!disconnectRef.current) {
          disconnectRef.current = setTimeout(() => {
            disconnectRef.current = null;
            if (!closingRef.current && pcRef.current === pc) fail('connection-failed');
          }, DISCONNECT_GRACE_MS);
        }
      }
    };

    setState('connecting');

    // Connecting watchdog — never let 'connecting' hang silently.
    watchdogRef.current = setTimeout(() => {
      watchdogRef.current = null;
      if (!closingRef.current && stateRef.current === 'connecting') fail('connection-failed');
    }, CONNECT_WATCHDOG_MS);

    // 4. Vanilla-ICE offer: create, set local, wait for gathering-complete.
    try {
      const offer = await pc.createOffer();
      if (stale()) return;
      await pc.setLocalDescription(offer);
    } catch {
      if (stale()) return;
      fail('signaling-failed');
      return;
    }
    if (stale()) return;
    await waitGatheringComplete(pc);
    if (stale()) return;

    // 5. Signal: POST the offer, apply the answer.
    const sdp = pc.localDescription?.sdp;
    if (!sdp) {
      fail('signaling-failed');
      return;
    }
    let answer: VoiceOfferResponse;
    try {
      answer = await voiceApi.offer(sdp);
    } catch (e) {
      if (stale()) return;
      fail(mapSignalError(e));
      return;
    }
    if (stale()) return;
    sessionIdRef.current = answer.voice_session_id;
    setVoiceSessionId(answer.voice_session_id);
    try {
      await pc.setRemoteDescription({ type: 'answer', sdp: answer.sdp });
    } catch {
      if (stale()) return;
      fail('signaling-failed');
      return;
    }
    // From here the pc drives itself: onconnectionstatechange → 'connected' → live.
  }, [audioRef, enabled, fail, setState, waitGatheringComplete]);

  // Auto-teardown when the capability is withdrawn (display flag off or an
  // instance switch away from home) — a live echo loop can't straddle it.
  useEffect(() => {
    if (!enabled && stateRef.current !== 'idle' && stateRef.current !== 'error') {
      closeAndReset('idle');
    }
  }, [enabled, closeAndReset]);

  // Best-effort teardown on unmount (fires the keepalive close beacon). Bump the
  // generation BEFORE teardown (as fail()/closeAndReset() do) so an in-flight
  // start() awaiting config/getUserMedia/offer sees stale() and bails — it must
  // NOT acquire a fresh mic or build a new pc after the component is gone.
  useEffect(() => {
    return () => {
      genRef.current += 1;
      if (sessionIdRef.current) sendVoiceCloseBeacon(sessionIdRef.current);
      teardownLocal();
    };
  }, [teardownLocal]);

  return {
    state,
    muted,
    audioBlocked,
    error,
    voiceSessionId,
    start,
    toggleMute,
    hangup,
    retryAudio,
    reset,
  };
}
