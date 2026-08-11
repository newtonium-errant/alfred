import { useCallback, useRef, useState } from 'react';
import { chatApi } from '../../lib/algernon/client';
import { ApiError } from '../../lib/algernon/http';
import { HOME_INSTANCE_NAME } from '../../lib/algernon/instance';
import { IMAGE_TOO_LARGE_FALLBACK } from '../../lib/algernon/useChat';
import type { PlayerPrimer } from '../../lib/algernon/player';

// The player's ASK flow (C3c) — a single-shot contextual question to the assistant,
// carrying the on-screen primer, answered as a TEXT card in-player. It REUSES the
// shared chat client (chatApi → the same /api/chat/* BFF the /chat page uses); it is
// NOT a parallel chat client. The answer is a text card v1 (answer-TTS is boarded, not
// built — an arbitrary-text synth surface needs its own credit-guard thinking).
//
// SESSION: the ask rides the operator's EXISTING home-instance chat session — a player
// question and its answer land in the same thread /chat resumes (continuity). We read
// the SAME stored key useChat writes and NEVER call /chat/open on a live session (that
// archives it — the clobber useChat's own comment warns against). A fresh session opens
// ONLY when none is stored, or the stored one was archived server-side
// (no_such_session), and the retry rides the SAME idempotency key (no double-run).

// Mirrors useChat's localStorage convention (algernon:session_key:<instance>). NOT
// imported from useChat to avoid coupling the player to that heavily-pinned hook; the
// convention is stable, and reading the same key is what shares the session.
const SESSION_KEY_PREFIX = 'algernon:session_key';
function homeSessionStorageKey(): string {
  return `${SESSION_KEY_PREFIX}:${HOME_INSTANCE_NAME}`;
}
function readStoredSession(): string | null {
  try {
    return localStorage.getItem(homeSessionStorageKey());
  } catch {
    return null;
  }
}
function writeStoredSession(key: string): void {
  try {
    localStorage.setItem(homeSessionStorageKey(), key);
  } catch {
    /* private mode — the ask still works this session, it just won't share the key */
  }
}

async function openFreshSession(): Promise<string> {
  const { session_key } = await chatApi.open(); // home instance — no live session to clobber here
  writeStoredSession(session_key);
  return session_key;
}

async function resolveSessionKey(): Promise<string> {
  const stored = readStoredSession();
  if (stored) return stored;
  return openFreshSession();
}

export type AskStatus = 'idle' | 'sending' | 'answered' | 'error';

export interface UsePlayerAskOptions {
  /** Called when a turn reports 401 — the page redirects to /login (mirrors load). */
  onAuthExpired?: () => void;
}

export interface UsePlayerAskResult {
  status: AskStatus;
  sending: boolean;
  /** The assistant's reply (present when status === 'answered'). */
  answer: string | null;
  /** A friendly error (present when status === 'error'); the keyboard stays live to retry. */
  error: string | null;
  ask: (question: string, primer: PlayerPrimer) => Promise<void>;
  /** Clear back to idle AND supersede any in-flight ask (called on resume / new ask). */
  reset: () => void;
}

// Generic per-instance-safe copy (never an instance literal — mirrors useChat's
// friendlyError, which says "the assistant", not a name). Per feedback_hardcoding.
// Exported for the #82 WARN-2 unit — see useChat.friendlyError.
export function askErrorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.code) {
      case 'invalid_session':
        return 'Your session has ended — please sign in again.';
      case 'unknown_user':
        return "This account isn't on the allowlist for this instance.";
      case 'engine_error':
        return 'The assistant hit a snag answering that. Try again in a moment.';
      case 'image_too_large':
        // Mirrors useChat's case (#82) — deterministic 400, retrying can't
        // clear it. Both switches must carry every code the box can emit, or
        // the new one silently renders the generic fallback below.
        return e.detail || IMAGE_TOO_LARGE_FALLBACK;
      case 'transport_unreachable':
      case 'network_error':
        return "Can't reach the assistant right now. Try again shortly.";
      case 'timeout':
      case 'gateway_timeout':
        return 'That took longer than expected. Try again.';
      default:
        return 'Something went wrong asking that. Try again.';
    }
  }
  return 'Something went wrong asking that. Try again.';
}

export function usePlayerAsk(options: UsePlayerAskOptions = {}): UsePlayerAskResult {
  const { onAuthExpired } = options;
  const [status, setStatus] = useState<AskStatus>('idle');
  const [answer, setAnswer] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Supersedes an in-flight ask: reset() (resume, or a new ask) bumps this so a late
  // resolution can't set state onto a surface the operator already left. No abort of
  // the underlying fetch (the turn may have run server-side — it just isn't shown).
  const seqRef = useRef(0);

  const ask = useCallback(
    async (question: string, primer: PlayerPrimer) => {
      const q = question.trim();
      if (!q) return;
      const seq = ++seqRef.current;
      const isCurrent = () => seqRef.current === seq;
      setStatus('sending');
      setError(null);
      setAnswer(null);
      // One idempotency key for this logical ask — reused on the no_such_session retry
      // so a turn that already ran returns its cached result (no double-run). (S6.)
      const idk = crypto.randomUUID();
      const runOn = (key: string) => chatApi.turn(key, q, { kind: 'text', idempotencyKey: idk, primer });
      try {
        let key = await resolveSessionKey();
        let res;
        try {
          res = await runOn(key);
        } catch (e) {
          // Stored session archived server-side → open a fresh one (nothing live to
          // clobber) and retry ONCE with the same idempotency key.
          if (e instanceof ApiError && e.code === 'no_such_session') {
            key = await openFreshSession();
            res = await runOn(key);
          } else {
            throw e;
          }
        }
        if (!isCurrent()) return; // superseded by resume / a newer ask
        setAnswer(res.reply || '');
        setStatus('answered');
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) onAuthExpired?.();
        if (!isCurrent()) return;
        setError(askErrorMessage(e));
        setStatus('error');
      }
    },
    [onAuthExpired],
  );

  const reset = useCallback(() => {
    seqRef.current += 1; // supersede any in-flight ask
    setStatus('idle');
    setAnswer(null);
    setError(null);
  }, []);

  return { status, sending: status === 'sending', answer, error, ask, reset };
}
