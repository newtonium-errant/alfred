import { RefObject, useCallback, useEffect, useRef, useState } from 'react';
import { ApiError } from './http';
import { sendVoiceCloseBeacon, voiceApi } from './voiceClient';
import {
  voiceCancelFrame,
  voiceDcEventSchema,
  voiceHelloFrame,
  type VoiceDcEvent,
} from './schemas';
import type { VoiceConfigResponse, VoiceOfferResponse } from './types';

// Voice engine hook. V0 spine: the WebRTC session as a 6-state machine (the mic
// tap IS the user gesture; config probe → getUserMedia → RTCPeerConnection →
// vanilla-ICE offer/answer → live). V1 adds the dictation datachannel: a `voice`
// channel created BEFORE the offer carries streaming STT partials/finals and the
// streamed text reply; `live` grows a sub-state machine (listening → thinking →
// replying) rendered by VoicePanel, and a completed turn is adopted into the chat
// thread via onTurnFinal → refreshFromHistory. Per intentionally-left-blank every
// failure is an EXPLICIT state, never a silent dead control.
//
// audio-blocked stays an orthogonal flag (pc stays live). The dictation datachannel
// is treated as the product in V1: once the server confirms dictation is live
// (state:ready), a channel death while the pc lives is a FULL teardown — a hot mic
// streaming to cloud STT with no visible transcript is a privacy hazard, not a
// degraded-but-ok state. Before ready (or under the echo pipeline, which never
// sends ready) the channel is dormant and its closure is benign.

export type VoiceState =
  | 'idle'
  | 'requesting-mic'
  | 'connecting'
  | 'live'
  | 'closing'
  | 'error';

// The dictation lifecycle WITHIN 'live' (always 'listening' when not live). V2
// adds 'speaking': the assistant's reply is being spoken aloud (streaming TTS).
export type VoiceTurnState = 'listening' | 'thinking' | 'replying' | 'speaking';

export type VoiceErrorCode =
  | 'unsupported'
  | 'permission-denied'
  | 'no-device'
  | 'mic-error'
  | 'voice-disabled'
  | 'voice-busy'
  | 'session-expired'
  | 'signaling-failed'
  | 'connection-failed'
  | 'channel-failed'
  | 'stt-failed'
  | 'reconnect-timeout';

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
  // --- V1 dictation surface (meaningful while state==='live') ---
  /** listening | thinking | replying — the turn sub-state. */
  voiceTurnState: VoiceTurnState;
  /** The live/pinned transcript of the current utterance ('' when none). */
  partialTranscript: string;
  /** The streaming reply accumulation ('' when none / after thread adoption). */
  replyText: string;
  /** A non-fatal per-turn failure notice (the call stays live). */
  turnError: string | null;
  /** The active tool's name during a tool turn (null otherwise). */
  toolName: string | null;
  /** Live but the server never confirmed dictation (echo pipeline / dead dictation). */
  dictationUnavailable: boolean;
  // --- V2 talk-back surface ---
  /** Local speaker mute (audio element `.muted`). Client-local: the server keeps synthesizing. */
  speakerMuted: boolean;
  /** TTS is degraded for this session (non-fatal) — replies still arrive as text. */
  ttsUnavailable: boolean;
  /** A brief "heard you — hold on" notice: an utterance was discarded while speaking (half-duplex). */
  discardNotice: boolean;
  /** True while a turn is in flight (a cancel frame would act). Mirrors the internal turn id. */
  canCancel: boolean;
  /** An auto-reconnect (one-shot, after a transient live drop) is in flight. */
  reconnecting: boolean;
  /** The user gesture — request mic + negotiate. Only acts from idle. */
  start: () => Promise<void>;
  /** Flip the mic track (live only). No renegotiation. Muting doubles as "over". */
  toggleMute: () => void;
  /** Flip remote-audio playback muting (live only). Client-local — does NOT stop synthesis. */
  toggleSpeakerMute: () => void;
  /** Cancel the in-flight turn (sends a cancel frame; the call stays live). */
  cancelTurn: () => void;
  /** Tear the call down (best-effort close beacon + local teardown). */
  hangup: () => void;
  /** Re-attempt audio.play() from a fresh user gesture (audio-blocked recovery). */
  retryAudio: () => void;
  /** Clear a terminal error → idle (re-armable). */
  reset: () => void;
}

const GATHER_TIMEOUT_MS = 3000; // vanilla-ICE gather-complete guard (host-only ⇒ sub-second)
const CONNECT_WATCHDOG_MS = 15000; // connecting must reach 'connected' within this
const DISCONNECT_GRACE_MS = 4000; // a transient 'disconnected' may recover within this
const DC_READY_TIMEOUT_MS = 5000; // after connect, dictation should confirm within this
// Pre-live (requesting-mic) hard cap: config-fetch + getUserMedia must complete
// within this. Without it, a stall after a network transition wedges the panel at
// 'requesting-mic' holding a live mic (the reconnect bug). Its two awaits also get
// their own bounded timeouts so a never-resolving promise can't outlive the cap.
const REQUESTING_MIC_WATCHDOG_MS = 10000;
const CONFIG_TIMEOUT_MS = 8000; // voiceApi.config() bound (the BFF's own budget is 70s)
const MIC_TIMEOUT_MS = 8000; // getUserMedia bound (native call has NO timeout)
const AUTO_RETRY_DELAY_MS = 1500; // wait before the one-shot auto-reconnect

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
  'channel-failed': 'The voice data link dropped. Try again.',
  'stt-failed': 'Speech recognition failed. Try again.',
  'reconnect-timeout': 'Couldn’t reconnect. Try again.',
};

// Reject `p` after `ms` (TimeoutError) so a stalled promise can't hang the machine.
function withTimeout<T>(p: Promise<T>, ms: number, label: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new DOMException(`${label} timed out`, 'TimeoutError')), ms);
  });
  return Promise.race([p, timeout]).finally(() => clearTimeout(timer));
}

// getUserMedia with a timeout AND a late-grant guard: if the timeout wins but the
// real getUserMedia resolves afterwards, its tracks are stopped so a late mic grant
// can never leak (the reconnect hot-mic hazard). Native getUserMedia has no timeout.
function getUserMediaWithTimeout(constraints: MediaStreamConstraints, ms: number): Promise<MediaStream> {
  const gum = navigator.mediaDevices.getUserMedia(constraints);
  return new Promise<MediaStream>((resolve, reject) => {
    let done = false;
    const timer = setTimeout(() => {
      if (done) return;
      done = true;
      gum.then((s) => s.getTracks().forEach((t) => t.stop())).catch(() => {});
      reject(new DOMException('microphone timed out', 'TimeoutError'));
    }, ms);
    gum.then(
      (s) => {
        if (done) {
          s.getTracks().forEach((t) => t.stop());
          return;
        }
        done = true;
        clearTimeout(timer);
        resolve(s);
      },
      (e) => {
        if (done) return;
        done = true;
        clearTimeout(timer);
        reject(e);
      },
    );
  });
}

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

export function useVoice(opts: {
  audioRef: RefObject<HTMLAudioElement>;
  enabled: boolean;
  /** Bound into voiceApi.offer at offer time (the V0 session_key forward-hook). */
  sessionKey?: string | null;
  /** Thread adoption after a completed turn; true ⇒ clear the in-panel reply. */
  onTurnFinal?: () => Promise<boolean>;
}): UseVoice {
  const { audioRef, enabled } = opts;

  const [state, setStateRaw] = useState<VoiceState>('idle');
  const [muted, setMuted] = useState(false);
  const [audioBlocked, setAudioBlocked] = useState(false);
  const [error, setError] = useState<VoiceError | null>(null);
  const [voiceSessionId, setVoiceSessionId] = useState<string | null>(null);
  const [voiceTurnState, setVoiceTurnState] = useState<VoiceTurnState>('listening');
  const [partialTranscript, setPartialTranscript] = useState('');
  const [replyText, setReplyText] = useState('');
  const [turnError, setTurnError] = useState<string | null>(null);
  const [toolName, setToolName] = useState<string | null>(null);
  const [dictationUnavailable, setDictationUnavailable] = useState(false);
  const [speakerMuted, setSpeakerMuted] = useState(false);
  const [ttsUnavailable, setTtsUnavailable] = useState(false);
  const [discardNotice, setDiscardNotice] = useState(false);
  const [canCancel, setCanCancel] = useState(false);
  const [reconnecting, setReconnecting] = useState(false);

  // All live handles in refs so async callbacks never close over stale state.
  const stateRef = useRef<VoiceState>('idle');
  const pcRef = useRef<RTCPeerConnection | null>(null);
  const dcRef = useRef<RTCDataChannel | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const micTrackRef = useRef<MediaStreamTrack | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const closingRef = useRef(false); // suppress the 'closed' event we cause ourselves
  const dictationActiveRef = useRef(false); // true once state:ready seen
  const assistantPipelineRef = useRef(false); // config.pipeline === 'assistant'
  const currentTurnIdRef = useRef<string | null>(null);
  const currentUtteranceIdRef = useRef<string | null>(null);
  // The turn whose reply is being SPOKEN. Deliberately separate from
  // currentTurnIdRef — speaking outlives turn_final (which clears the turn id).
  const speakingTurnIdRef = useRef<string | null>(null);
  // Latest opts mirrored to refs so async closures read the current value.
  const sessionKeyRef = useRef<string | null>(opts.sessionKey ?? null);
  const onTurnFinalRef = useRef(opts.onTurnFinal);
  const enabledRef = useRef(enabled);
  sessionKeyRef.current = opts.sessionKey ?? null;
  onTurnFinalRef.current = opts.onTurnFinal;
  enabledRef.current = enabled;
  // Auto-reconnect (one-shot per live session): a budget flag + the delay timer +
  // a ref to the latest start() so a connection handler can re-drive it.
  const retriedRef = useRef(false);
  const retryTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const startRef = useRef<() => Promise<void>>(async () => {});
  // A monotonic generation: any teardown / new start bumps it, and every awaited
  // step in start() (and every dc callback) re-checks it so an aborted attempt
  // cannot resurrect state.
  const genRef = useRef(0);
  // Timers.
  const watchdogRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const disconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const dcReadyRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const preliveRef = useRef<ReturnType<typeof setTimeout> | null>(null); // requesting-mic cap

  const setState = useCallback((s: VoiceState) => {
    stateRef.current = s;
    setStateRaw(s);
  }, []);

  const clearTimers = useCallback(() => {
    for (const ref of [watchdogRef, disconnectRef, dcReadyRef, preliveRef, retryTimerRef]) {
      if (ref.current) {
        clearTimeout(ref.current);
        ref.current = null;
      }
    }
  }, []);

  // Reset the dictation + talk-back surface to its empty baseline.
  const resetDictation = useCallback(() => {
    dictationActiveRef.current = false;
    assistantPipelineRef.current = false;
    currentTurnIdRef.current = null;
    currentUtteranceIdRef.current = null;
    speakingTurnIdRef.current = null;
    setVoiceTurnState('listening');
    setPartialTranscript('');
    setReplyText('');
    setTurnError(null);
    setToolName(null);
    setDictationUnavailable(false);
    setTtsUnavailable(false);
    setDiscardNotice(false);
    setCanCancel(false);
    // Symmetric with setMuted(false): drop any speaker mute + unmute the element.
    setSpeakerMuted(false);
    if (audioRef.current) audioRef.current.muted = false;
  }, [audioRef]);

  // Stop the mic, close the dc + pc, detach playback, clear timers. NO network, NO
  // React state — safe to call from an unmount cleanup.
  const teardownLocal = useCallback(() => {
    clearTimers();
    closingRef.current = true;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    micTrackRef.current = null;
    const dc = dcRef.current;
    dcRef.current = null;
    if (dc) {
      dc.onopen = null;
      dc.onmessage = null;
      dc.onclose = null;
      dc.onerror = null;
      try {
        dc.close();
      } catch {
        /* already closed */
      }
    }
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
      genRef.current += 1; // invalidate any in-flight start() + dc callbacks
      teardownLocal();
      sessionIdRef.current = null;
      setVoiceSessionId(null);
      setMuted(false);
      setAudioBlocked(false);
      setReconnecting(false);
      resetDictation();
      setError({ code, message: ERROR_MESSAGES[code] });
      setState('error');
    },
    [resetDictation, setState, teardownLocal],
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
      setReconnecting(false);
      retriedRef.current = false; // a clean end resets the auto-retry budget
      resetDictation();
      setError(null);
      setState(nextState);
    },
    [resetDictation, setState, teardownLocal],
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

  // Speaker mute: silence remote-audio PLAYBACK locally (audio element .muted).
  // Deliberately client-local — the server keeps synthesizing (ElevenLabs egress
  // continues); a server-notified mute is deferred to V3 where interrupt subsumes it.
  const toggleSpeakerMute = useCallback(() => {
    if (stateRef.current !== 'live') return;
    const el = audioRef.current;
    if (!el) return;
    el.muted = !el.muted;
    setSpeakerMuted(el.muted);
  }, [audioRef]);

  const cancelTurn = useCallback(() => {
    if (stateRef.current !== 'live') return;
    const dc = dcRef.current;
    const turnId = currentTurnIdRef.current;
    if (!dc || dc.readyState !== 'open' || !turnId) return;
    try {
      dc.send(voiceCancelFrame(turnId));
    } catch {
      /* best-effort; the server also supersedes on the next utterance */
    }
  }, []);

  const retryAudio = useCallback(() => {
    const el = audioRef.current;
    if (!el) return;
    void el
      .play()
      .then(() => setAudioBlocked(false))
      .catch(() => setAudioBlocked(true));
  }, [audioRef]);

  // CLEAN RECONNECT: a full teardown before returning to idle, so a fresh start()
  // runs from a genuinely clean slate — never the dead session's remnants. Bumping
  // the generation invalidates any lingering async continuation or dc callback;
  // teardownLocal stops the mic, closes+nulls pc+dc+handlers, clears every timer,
  // and detaches the audio element. The requesting-mic wedge cannot inherit stale
  // gen/refs after this.
  const reset = useCallback(() => {
    if (stateRef.current !== 'error') return;
    genRef.current += 1;
    retriedRef.current = false; // a manual attempt gets its own one-shot auto-retry budget
    teardownLocal();
    sessionIdRef.current = null;
    setVoiceSessionId(null);
    setMuted(false);
    setAudioBlocked(false);
    setReconnecting(false);
    resetDictation();
    setError(null);
    setState('idle');
  }, [resetDictation, setState, teardownLocal]);

  // Apply one validated datachannel event to the dictation sub-state. `gen` is the
  // generation captured when the channel was wired — the async onTurnFinal
  // re-checks it so a completed reconcile after teardown can't resurrect state.
  const dispatchDcEvent = useCallback((evt: VoiceDcEvent, gen: number) => {
    switch (evt.type) {
      case 'state':
        if (evt.state === 'ready') {
          dictationActiveRef.current = true;
          setDictationUnavailable(false);
          if (dcReadyRef.current) {
            clearTimeout(dcReadyRef.current);
            dcReadyRef.current = null;
          }
        } else if (evt.state === 'superseded') {
          // A newer utterance replaced the in-flight turn — drop the abandoned
          // reply and show we're processing the newer one (partials keep rendering).
          // The server flushes any prior playout, so clear the speaking ref.
          setReplyText('');
          setTurnError(null);
          setToolName(null);
          speakingTurnIdRef.current = null;
          setDiscardNotice(false);
          setVoiceTurnState('thinking');
        } else if (evt.state === 'turn_cancelled') {
          setReplyText('');
          setToolName(null);
          currentTurnIdRef.current = null;
          speakingTurnIdRef.current = null;
          setCanCancel(false);
          setDiscardNotice(false);
          setVoiceTurnState('listening');
        }
        return;
      case 'stt_partial':
        if (evt.utterance_id !== currentUtteranceIdRef.current) {
          currentUtteranceIdRef.current = evt.utterance_id;
          // A fresh utterance with no active turn → clear the prior exchange.
          if (currentTurnIdRef.current === null) {
            setReplyText('');
            setTurnError(null);
            setToolName(null);
          }
        }
        setPartialTranscript(evt.text);
        return;
      case 'stt_final':
        currentUtteranceIdRef.current = evt.utterance_id;
        setPartialTranscript(evt.text); // pinned final; EOU marker
        setVoiceTurnState('thinking');
        return;
      case 'turn_started':
        currentTurnIdRef.current = evt.turn_id;
        setCanCancel(true);
        // A new turn ⇒ the server flushed prior playout; drop the speaking ref.
        speakingTurnIdRef.current = null;
        setDiscardNotice(false);
        setReplyText('');
        setToolName(null);
        setVoiceTurnState('thinking');
        return;
      case 'turn_text':
        setReplyText((r) => r + evt.text); // server owns spacing
        setToolName(null);
        // Don't ping-pong the pill: while the reply is being SPOKEN, keep
        // 'speaking' (text still streams into the reply region; the pill reports
        // the audible activity, which outranks the silent text stream).
        if (speakingTurnIdRef.current === null) setVoiceTurnState('replying');
        return;
      case 'turn_tool':
        setToolName(evt.tool ?? null);
        return;
      case 'turn_final': {
        currentTurnIdRef.current = null;
        setCanCancel(false);
        setToolName(null);
        // If the reply is already being spoken, stay 'speaking' until speaking_done;
        // otherwise the turn is done → 'listening'.
        setVoiceTurnState(speakingTurnIdRef.current !== null ? 'speaking' : 'listening');
        setReplyText(evt.reply); // full persisted reply (in case chunks dropped)
        const onFinal = onTurnFinalRef.current;
        if (onFinal) {
          void onFinal()
            .then((adopted) => {
              if (genRef.current !== gen) return; // torn down mid-reconcile
              if (adopted) {
                // The exchange now lives in the chat thread — clear the panel copy.
                setReplyText('');
                setPartialTranscript('');
                setTurnError(null);
                currentUtteranceIdRef.current = null;
              }
              // !adopted ⇒ keep replyText readable in-panel (graceful).
            })
            .catch(() => {
              /* reconcile failed — keep the reply visible in-panel */
            });
        }
        return;
      }
      case 'speaking_started':
        // May arrive before OR after turn_final (short replies finish text first).
        // Must NOT read currentTurnIdRef — a post-final arrival is legal.
        speakingTurnIdRef.current = evt.turn_id;
        setTtsUnavailable(false); // TTS is clearly working — self-heal the notice
        setDiscardNotice(false);
        setVoiceTurnState('speaking');
        return;
      case 'speaking_done':
        // Idempotent: a stale/dup done for a turn we're not speaking is a no-op.
        if (speakingTurnIdRef.current === null) return;
        speakingTurnIdRef.current = null;
        setDiscardNotice(false);
        // If text is still streaming (audio finished first) go back to 'replying';
        // otherwise the turn is fully done → 'listening'.
        setVoiceTurnState(currentTurnIdRef.current !== null ? 'replying' : 'listening');
        return;
      case 'utterance_discarded':
        // Half-duplex: the user spoke while the assistant was speaking; the final
        // was dropped server-side. Surface the honest notice (clears when speaking ends).
        setDiscardNotice(true);
        return;
      case 'error':
        if (evt.code === 'stt_unavailable') {
          fail('stt-failed');
        } else if (evt.code === 'tts_unavailable') {
          // Non-fatal TTS degrade — its OWN branch (CONTRACT §1.2). Must NOT touch
          // replyText / turnError / voiceTurnState / the turn id: the generic branch
          // below would wrongly clear the streaming reply mid-turn.
          setTtsUnavailable(true);
        } else {
          // A per-turn failure (e.g. no_such_session) — non-fatal, call stays live.
          setTurnError(evt.detail || 'That didn’t go through — try saying it again.');
          setReplyText('');
          setToolName(null);
          setVoiceTurnState('listening');
          currentTurnIdRef.current = null;
          setCanCancel(false);
        }
        return;
    }
  }, [fail]);

  // Parse + validate one raw datachannel message, then dispatch. A bad frame is
  // ALWAYS dropped (console.debug), never a state change or error — the unknown
  // type/version is surfaced via the lenient probe for telemetry.
  const handleDcMessage = useCallback(
    (data: unknown, gen: number) => {
      if (typeof data !== 'string') {
        console.debug('voice dc: ignored non-string frame');
        return;
      }
      let raw: unknown;
      try {
        raw = JSON.parse(data);
      } catch {
        console.debug('voice dc: ignored malformed JSON frame');
        return;
      }
      const parsed = voiceDcEventSchema.safeParse(raw);
      if (!parsed.success) {
        console.debug('voice dc: ignored unrecognized frame', dcTypeProbe(raw));
        return;
      }
      dispatchDcEvent(parsed.data, gen);
    },
    [dispatchDcEvent],
  );

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
    resetDictation();
    const gen = (genRef.current += 1);
    const stale = () => genRef.current !== gen;
    // A fresh attempt is NOT closing — reset here (not at pc creation) so the
    // requesting-mic watchdog + handlers aren't suppressed by a prior teardown's
    // leftover closingRef=true.
    closingRef.current = false;

    if (!isSupported()) {
      fail('unsupported');
      return;
    }

    setState('requesting-mic');

    // PRE-LIVE WATCHDOG: config-fetch + getUserMedia must complete within the cap.
    // After a network transition either can stall; without this the panel wedges
    // forever at 'requesting-mic' holding a live mic (the reconnect bug). On fire →
    // clean error + mic released (fail → teardownLocal; a late mic grant is stopped
    // by getUserMediaWithTimeout's own guard).
    preliveRef.current = setTimeout(() => {
      preliveRef.current = null;
      if (!closingRef.current && stateRef.current === 'requesting-mic') fail('reconnect-timeout');
    }, REQUESTING_MIC_WATCHDOG_MS);

    // 1. Config probe FIRST — a disabled / aiortc-missing backend fails here,
    //    BEFORE the mic is ever requested (no spurious permission prompt). Bounded
    //    so a stalled fetch can't outlive the watchdog.
    let config: VoiceConfigResponse;
    try {
      config = await withTimeout(voiceApi.config(), CONFIG_TIMEOUT_MS, 'config');
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
    // Only the assistant pipeline streams to cloud STT + emits dictation `ready`,
    // so only then is a missing/dead dictation channel fatal (CONTRACT §17b).
    // Echo / absent field ⇒ the benign dictation-unavailable path.
    assistantPipelineRef.current = config.pipeline === 'assistant';

    // 2. Mic — the tap that got us here is the gesture; constraints are hints
    //    (unsupported ones don't throw). Bounded (native getUserMedia has no timeout)
    //    with a late-grant guard so a mic acquired after we bail is never leaked.
    let stream: MediaStream;
    try {
      stream = await getUserMediaWithTimeout(
        {
          audio: {
            echoCancellation: true,
            noiseSuppression: true,
            autoGainControl: true,
            channelCount: { ideal: 1 },
          },
        },
        MIC_TIMEOUT_MS,
      );
    } catch (e) {
      if (stale()) return;
      fail(mapGetUserMediaError(e));
      return;
    }
    if (stale()) {
      stream.getTracks().forEach((t) => t.stop());
      return;
    }

    // 3. Peer connection + datachannel. The synchronous build is EXCEPTION-GUARDED:
    //    a transient RTCPeerConnection/addTrack/createDataChannel throw (seen after a
    //    network transition) must release the just-acquired mic + fail cleanly, NOT
    //    wedge at 'requesting-mic' with a hot mic (the `void start()` caller swallows
    //    the rejection, so an unguarded throw is invisible).
    let pc: RTCPeerConnection;
    let dc: RTCDataChannel;
    try {
      pc = new RTCPeerConnection({ iceServers: config.ice_servers ?? [] });
      pcRef.current = pc;
      streamRef.current = stream;
      const micTrack = stream.getAudioTracks()[0] ?? null;
      micTrackRef.current = micTrack;
      if (micTrack) pc.addTrack(micTrack, stream);
      // 3a. The dictation datachannel — created BEFORE createOffer so its SCTP
      //     m-section rides the initial vanilla-ICE offer (no renegotiation).
      dc = pc.createDataChannel('voice', { ordered: true });
      dcRef.current = dc;
    } catch {
      if (stale()) {
        stream.getTracks().forEach((t) => t.stop());
        return;
      }
      stream.getTracks().forEach((t) => t.stop()); // release the just-acquired mic
      fail('connection-failed');
      return;
    }
    dc.onopen = () => {
      if (genRef.current !== gen) return;
      // Client speaks first (aiortc #212 hello-gate: the server stays silent until
      // it receives a valid frame).
      try {
        dc.send(voiceHelloFrame());
      } catch {
        /* channel raced closed — the close/error handler covers it */
      }
    };
    dc.onmessage = (ev: MessageEvent) => {
      if (genRef.current !== gen) return;
      handleDcMessage(ev.data, gen);
    };
    const onDcDead = () => {
      if (genRef.current !== gen || closingRef.current) return;
      // Fatal ONLY once dictation was confirmed live (hot-mic hazard is real then).
      // Before ready — or under the echo pipeline that never readies — a dc close
      // is benign and the audio session continues.
      if (dictationActiveRef.current) fail('channel-failed');
    };
    dc.onclose = onDcDead;
    dc.onerror = onDcDead;

    pc.ontrack = (ev: RTCTrackEvent) => {
      const el = audioRef.current;
      if (!el) return;
      el.srcObject = ev.streams[0] ?? new MediaStream([ev.track]);
      void el.play().catch(() => setAudioBlocked(true));
    };

    // A LIVE session dropped unexpectedly (pc failed / disconnected past grace, NOT a
    // user hangup — closingRef guards that). AUTO-RETRY ONCE: clean-teardown the dead
    // session, then re-drive start() after a short delay. Exactly one attempt per live
    // session (retriedRef); a second failure surfaces the error + manual Reconnect.
    const dropWhileLive = () => {
      if (retriedRef.current) {
        fail('connection-failed');
        return;
      }
      retriedRef.current = true;
      setReconnecting(true);
      genRef.current += 1; // this handler's gen goes stale; nothing here can resurrect
      teardownLocal();
      sessionIdRef.current = null;
      setVoiceSessionId(null);
      setMuted(false);
      setAudioBlocked(false);
      resetDictation();
      setError(null);
      setState('idle'); // transient — start() re-drives; the panel shows "Reconnecting…"
      retryTimerRef.current = setTimeout(() => {
        retryTimerRef.current = null;
        if (!enabledRef.current || stateRef.current !== 'idle') {
          setReconnecting(false);
          return;
        }
        void startRef.current();
      }, AUTO_RETRY_DELAY_MS);
    };

    pc.onconnectionstatechange = () => {
      if (closingRef.current) return;
      const cs = pc.connectionState;
      if (cs === 'connected') {
        clearTimers(); // clear the pre-live + connect watchdogs (disconnect grace too)
        setReconnecting(false);
        retriedRef.current = false; // a healthy live session re-arms the auto-retry budget
        if (stateRef.current === 'connecting' || stateRef.current === 'live') {
          setState('live');
        }
        // Dictation-ready watchdog. If the server never confirms dictation:
        //  - assistant pipeline ⇒ FATAL 'channel-failed' (a hot mic is streaming to
        //    cloud STT with no visible transcript — the privacy hazard, CONTRACT §17b);
        //  - echo / absent pipeline ⇒ a NON-fatal notice (the audio session stays up).
        if (!dcReadyRef.current && !dictationActiveRef.current) {
          dcReadyRef.current = setTimeout(() => {
            dcReadyRef.current = null;
            if (genRef.current !== gen || closingRef.current) return;
            if (dictationActiveRef.current) return;
            if (assistantPipelineRef.current) fail('channel-failed');
            else setDictationUnavailable(true);
          }, DC_READY_TIMEOUT_MS);
        }
      } else if (cs === 'failed') {
        // A LIVE drop auto-retries once; a failure while still connecting is a plain
        // connect failure (surfaced immediately).
        if (stateRef.current === 'live') dropWhileLive();
        else fail('connection-failed');
      } else if (cs === 'closed') {
        // A close we didn't initiate (closingRef guards our own).
        fail('connection-failed');
      } else if (cs === 'disconnected') {
        if (!disconnectRef.current) {
          disconnectRef.current = setTimeout(() => {
            disconnectRef.current = null;
            if (closingRef.current || pcRef.current !== pc) return;
            if (stateRef.current === 'live') dropWhileLive();
            else fail('connection-failed');
          }, DISCONNECT_GRACE_MS);
        }
      }
    };

    // Entering connecting — clear the pre-live watchdog, arm the connect watchdog.
    if (preliveRef.current) {
      clearTimeout(preliveRef.current);
      preliveRef.current = null;
    }
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

    // 5. Signal: POST the offer (binding the chat session_key), apply the answer.
    const sdp = pc.localDescription?.sdp;
    if (!sdp) {
      fail('signaling-failed');
      return;
    }
    let answer: VoiceOfferResponse;
    try {
      answer = await voiceApi.offer(sdp, sessionKeyRef.current ?? undefined);
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
    // From here the pc drives itself: onconnectionstatechange → 'connected' → live;
    // the datachannel drives dictation → the sub-state machine.
  }, [
    audioRef,
    clearTimers,
    enabled,
    fail,
    handleDcMessage,
    resetDictation,
    setState,
    teardownLocal,
    waitGatheringComplete,
  ]);
  // The auto-retry connection handler re-drives start() via this ref (breaks the
  // start ↔ handler definition cycle).
  startRef.current = start;

  // Auto-teardown when the capability is withdrawn (display flag off or an
  // instance switch away from home) — a live session can't straddle it.
  useEffect(() => {
    if (!enabled && stateRef.current !== 'idle' && stateRef.current !== 'error') {
      closeAndReset('idle');
    }
  }, [enabled, closeAndReset]);

  // Wake Lock: hold the screen awake while live so an auto-lock can't kill the
  // session mid-turn. Feature-detected silently (typed via a local shape so it
  // works regardless of lib.dom's Wake Lock coverage); released on leaving live /
  // unmount; re-acquired on tab re-show (the OS releases it when hidden).
  useEffect(() => {
    if (state !== 'live' || typeof navigator === 'undefined') return;
    const nav = navigator as unknown as { wakeLock?: WakeLockLike };
    const wakeLock = nav.wakeLock;
    if (!wakeLock) return;
    let cancelled = false;
    let sentinel: WakeLockSentinelLike | null = null;
    const acquire = async () => {
      try {
        const s = await wakeLock.request('screen');
        if (cancelled) {
          void s.release().catch(() => {});
          return;
        }
        sentinel = s;
      } catch {
        /* denied / not-visible — best-effort */
      }
    };
    void acquire();
    const onVisible = () => {
      if (document.visibilityState === 'visible' && !sentinel) void acquire();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      cancelled = true;
      document.removeEventListener('visibilitychange', onVisible);
      void sentinel?.release().catch(() => {});
      sentinel = null;
    };
  }, [state]);

  // Best-effort teardown on unmount (fires the keepalive close beacon). Bump the
  // generation BEFORE teardown (as fail()/closeAndReset() do) so an in-flight
  // start() awaiting config/getUserMedia/offer — or a pending dc callback — sees
  // stale() and bails: it must NOT acquire a fresh mic or build a new pc after the
  // component is gone (the V0 hot-mic pin).
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
    voiceTurnState,
    partialTranscript,
    replyText,
    turnError,
    toolName,
    dictationUnavailable,
    speakerMuted,
    ttsUnavailable,
    discardNotice,
    canCancel,
    reconnecting,
    start,
    toggleMute,
    toggleSpeakerMute,
    cancelTurn,
    hangup,
    retryAudio,
    reset,
  };
}

// A lenient probe to surface the `type`/`v` of a frame that failed the strict
// union (telemetry only — the frame is dropped regardless).
function dcTypeProbe(raw: unknown): { type?: unknown; v?: unknown } {
  if (raw && typeof raw === 'object') {
    const o = raw as Record<string, unknown>;
    return { type: o.type, v: o.v };
  }
  return {};
}

// Minimal Wake Lock shape — avoids depending on lib.dom's (version-varying) types.
interface WakeLockSentinelLike {
  release: () => Promise<void>;
}
interface WakeLockLike {
  request: (type: 'screen') => Promise<WakeLockSentinelLike>;
}
