import { useRef } from 'react';
import { Button } from '../ui/button';
import { useVoice } from '../../lib/algernon/useVoice';
import { HOME_INSTANCE_NAME, isHomeInstance } from '../../lib/algernon/instance';
import { subtle } from '../../lib/typography';

// V0 voice affordance, embedded in the chat surface (above the Composer). Two
// fail-closed gates: a DISPLAY flag (NEXT_PUBLIC_VOICE_ENABLED — absent ⇒ renders
// NOTHING, capability default-off) and a Salem-only instance guard (cross-instance
// selection ⇒ a disabled control + an explicit hint, never a silent dead button).
// The authoritative gate is the backend (web.voice.enabled → GET /voice/config);
// the display flag only decides whether to show the affordance at all.
//
// Reads process.env per-render (Next inlines NEXT_PUBLIC_* to a literal at build)
// so the flag is testable via stubbed env without a module reset.
function voiceDisplayEnabled(): boolean {
  const v = process.env.NEXT_PUBLIC_VOICE_ENABLED;
  return v === '1' || v === 'true';
}

export function VoicePanel({ instance }: { instance: string }) {
  const audioRef = useRef<HTMLAudioElement>(null);
  const homeOk = isHomeInstance(instance);
  // Hooks run unconditionally (rules-of-hooks); the display gate returns below.
  const voice = useVoice({ audioRef, enabled: voiceDisplayEnabled() && homeOk });

  if (!voiceDisplayEnabled()) return null;

  const { state, muted, audioBlocked, error } = voice;

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
              <Button
                variant="outline"
                size="sm"
                data-testid="voice-start"
                onClick={() => void voice.start()}
              >
                🎙 Voice
              </Button>
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
                <span
                  data-testid="voice-status"
                  className={
                    muted
                      ? 'inline-flex items-center gap-1 rounded-full bg-status-progress px-2.5 py-1 text-sm font-semibold text-status-progress-fg'
                      : 'inline-flex items-center gap-1 rounded-full bg-status-done px-2.5 py-1 text-sm font-semibold text-status-done-fg'
                  }
                >
                  {muted ? 'Muted' : '● Live — echo test'}
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

          {/* Autoplay edge (iOS/Safari): the pc is live but playback was blocked.
              A distinct NON-red notice with a fresh-gesture recovery button. */}
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
