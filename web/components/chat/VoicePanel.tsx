import { useRef } from 'react';
import { Button } from '../ui/button';
import { useVoice } from '../../lib/algernon/useVoice';
import { HOME_INSTANCE_NAME, isHomeInstance } from '../../lib/algernon/instance';
import { subtle } from '../../lib/typography';

// Voice affordance embedded in the chat surface (above the Composer). Two
// fail-closed gates: a DISPLAY flag (NEXT_PUBLIC_VOICE_ENABLED — absent ⇒ renders
// NOTHING) and a Salem-only instance guard (cross-instance ⇒ disabled control +
// explicit hint). The authoritative gate is the backend (web.voice.enabled → GET
// /voice/config). V1 adds the dictation surface: the live pill reflects the turn
// sub-state (listening/thinking/replying), the transcript + streaming reply render
// live, and the completed exchange is adopted into the chat thread via onTurnFinal.
//
// Reads process.env per-render (Next inlines NEXT_PUBLIC_* to a literal at build)
// so the flag is testable via stubbed env without a module reset.
function voiceDisplayEnabled(): boolean {
  const v = process.env.NEXT_PUBLIC_VOICE_ENABLED;
  return v === '1' || v === 'true';
}

const DONE_PILL =
  'inline-flex items-center gap-1 rounded-full bg-status-done px-2.5 py-1 text-sm font-semibold text-status-done-fg';
const PROGRESS_PILL =
  'inline-flex items-center gap-1 rounded-full bg-status-progress px-2.5 py-1 text-sm font-semibold text-status-progress-fg';

export function VoicePanel({
  instance,
  sessionKey,
  onTurnFinal,
}: {
  instance: string;
  sessionKey?: string | null;
  onTurnFinal?: () => Promise<boolean>;
}) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const homeOk = isHomeInstance(instance);
  // Hooks run unconditionally (rules-of-hooks); the display gate returns below.
  const voice = useVoice({
    audioRef,
    enabled: voiceDisplayEnabled() && homeOk,
    sessionKey,
    onTurnFinal,
  });

  if (!voiceDisplayEnabled()) return null;

  const {
    state,
    muted,
    audioBlocked,
    error,
    voiceTurnState,
    partialTranscript,
    replyText,
    turnError,
    toolName,
    dictationUnavailable,
  } = voice;

  // A voice turn needs the chat session_key (bound at offer time); gate start on it.
  const notReady = sessionKey == null;
  const inTurn = voiceTurnState === 'thinking' || voiceTurnState === 'replying';

  const pill = muted
    ? { cls: PROGRESS_PILL, label: 'Muted', pulse: false }
    : voiceTurnState === 'thinking'
      ? { cls: PROGRESS_PILL, label: 'Thinking…', pulse: true }
      : voiceTurnState === 'replying'
        ? { cls: DONE_PILL, label: 'Replying…', pulse: false }
        : { cls: DONE_PILL, label: '● Listening', pulse: false };

  return (
    <div
      data-testid="voice-panel"
      className="flex flex-col gap-2 rounded-xl border border-honeydew-300 bg-honeydew-50 px-3 py-2"
    >
      {/* Mounted whenever the panel renders so it exists BEFORE ontrack fires. */}
      <audio ref={audioRef} autoPlay playsInline className="hidden" data-testid="voice-audio" />

      {!homeOk ? (
        <div className="flex flex-col gap-1">
          <Button variant="outline" size="sm" data-testid="voice-start" disabled>
            🎙 Voice
          </Button>
          <p className={subtle} data-testid="voice-cross-instance-hint">
            Voice is available with {HOME_INSTANCE_NAME} only (for now).
          </p>
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          <div className="flex flex-wrap items-center gap-2">
            {state === 'idle' && (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  data-testid="voice-start"
                  disabled={notReady}
                  onClick={() => void voice.start()}
                >
                  🎙 Voice
                </Button>
                {notReady && (
                  <span data-testid="voice-loading-hint" className={subtle}>
                    Chat is still loading…
                  </span>
                )}
              </>
            )}

            {(state === 'requesting-mic' || state === 'connecting' || state === 'closing') && (
              <span
                data-testid="voice-status"
                className="inline-flex items-center gap-2 text-sm text-honeydew-600"
              >
                <span
                  aria-hidden
                  className="h-2 w-2 rounded-full bg-honeydew-500 motion-safe:animate-pulse"
                />
                {state === 'requesting-mic' && 'Requesting microphone…'}
                {state === 'connecting' && 'Connecting…'}
                {state === 'closing' && 'Ending…'}
              </span>
            )}

            {state === 'live' && (
              <>
                <span data-testid="voice-status" className={pill.cls}>
                  {pill.pulse && (
                    <span
                      aria-hidden
                      className="h-2 w-2 rounded-full bg-current motion-safe:animate-pulse"
                    />
                  )}
                  {pill.label}
                </span>
                <Button
                  variant="outline"
                  size="sm"
                  data-testid="voice-mute"
                  aria-pressed={muted}
                  onClick={() => voice.toggleMute()}
                >
                  {muted ? 'Unmute' : 'Mute'}
                </Button>
                {inTurn && (
                  <Button
                    variant="outline"
                    size="sm"
                    data-testid="voice-cancel"
                    onClick={() => voice.cancelTurn()}
                  >
                    Stop
                  </Button>
                )}
                <Button
                  variant="destructive"
                  size="sm"
                  data-testid="voice-hangup"
                  onClick={() => voice.hangup()}
                >
                  ■ Hang up
                </Button>
              </>
            )}
          </div>

          {/* Live transcript of the current utterance. */}
          {state === 'live' && partialTranscript && (
            <p
              data-testid="voice-transcript"
              aria-live="polite"
              className={subtle}
            >
              {partialTranscript}
            </p>
          )}

          {/* The active tool during a tool turn — honest UX for 10–23s turns. */}
          {state === 'live' && toolName && (
            <p
              data-testid="voice-tool"
              aria-live="polite"
              className="text-sm text-honeydew-600"
            >
              Using {toolName}…
            </p>
          )}

          {/* Streaming reply — clears once the exchange is adopted into the thread. */}
          {state === 'live' && replyText && (
            <div
              data-testid="voice-reply"
              aria-live="polite"
              className="max-h-40 overflow-y-auto rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-800"
            >
              {replyText}
            </div>
          )}

          {/* Non-fatal per-turn failure notice (call stays live) — honeydew, not red. */}
          {turnError && (
            <p
              role="status"
              data-testid="voice-turn-error"
              className="rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-700"
            >
              {turnError}
            </p>
          )}

          {/* Live but dictation never confirmed (echo pipeline / dead dictation). */}
          {state === 'live' && dictationUnavailable && (
            <p data-testid="voice-dictation-unavailable" className={subtle}>
              Dictation isn’t active for this session.
            </p>
          )}

          {/* Autoplay edge (iOS/Safari): the pc is live but playback was blocked. */}
          {audioBlocked && (state === 'live' || state === 'connecting') && (
            <div
              data-testid="voice-audio-blocked"
              className="flex items-center justify-between gap-3 rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-700"
            >
              <span>Audio is muted by the browser.</span>
              <button
                type="button"
                data-testid="voice-audio-unblock"
                onClick={() => voice.retryAudio()}
                className="shrink-0 rounded-lg border border-honeydew-300 px-2 py-1 font-semibold hover:bg-honeydew-50"
              >
                Tap to enable audio
              </button>
            </div>
          )}

          {state === 'error' && error && (
            <div
              role="alert"
              data-testid="voice-error"
              className="flex items-center justify-between gap-3 rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
            >
              <span>{error.message}</span>
              <button
                type="button"
                data-testid="voice-retry"
                onClick={() => voice.reset()}
                className="shrink-0 rounded-lg border border-danger px-2 py-1 font-semibold hover:opacity-80"
              >
                Try again
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
