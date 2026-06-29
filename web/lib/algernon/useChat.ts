import { useCallback, useEffect, useRef, useState } from 'react';
import { chatApi } from './client';
import { ApiError } from './http';
import { HOME_INSTANCE_NAME } from './instance';
import type { ChatKind, ChatMessage, HistoryTurn } from './types';

// Client-side chat state + the session resume model (team-lead confirmed):
//   * persist the session_key in localStorage,
//   * on load, resume via GET /chat/history/{key},
//   * open a FRESH session ONLY when there's no stored key or history 404s.
// /chat/open archives+closes the prior session, so we never call it on every
// load — that would destroy continuity.
//
// `enabled` gates the initial bootstrap so the page can wait until the user is
// known to be signed in (avoids a guaranteed-401 /chat call when signed out).
//
// MULTI-INSTANCE: `instance` selects which assistant this thread talks to. The
// session_key is scoped PER-INSTANCE in localStorage (algernon:session_key:<inst>)
// so switching instances shows that instance's own thread; changing `instance`
// re-bootstraps. The home instance keeps the existing same-instance path.

const SESSION_KEY_PREFIX = 'algernon:session_key';
// The pre-multi-instance key (no instance suffix). Migrated once to the home key.
const LEGACY_SESSION_KEY = 'algernon:session_key';

function storageKeyFor(instance: string): string {
  return `${SESSION_KEY_PREFIX}:${instance}`;
}

let _idSeq = 0;
function nextId(): string {
  _idSeq += 1;
  return `m${Date.now()}-${_idSeq}`;
}

function turnToMessage(t: HistoryTurn): ChatMessage {
  return { id: nextId(), role: t.role, text: t.text, ts: t.ts };
}

function readStored(instance: string): string | null {
  try {
    const k = storageKeyFor(instance);
    const v = localStorage.getItem(k);
    if (v) return v;
    // One-time migration of the legacy unsuffixed key → the home instance key.
    if (instance === HOME_INSTANCE_NAME) {
      const legacy = localStorage.getItem(LEGACY_SESSION_KEY);
      if (legacy && legacy !== k) {
        try {
          localStorage.setItem(k, legacy);
        } catch {
          /* ignore */
        }
        return legacy;
      }
    }
    return null;
  } catch {
    return null;
  }
}

function writeStored(instance: string, key: string): void {
  try {
    localStorage.setItem(storageKeyFor(instance), key);
  } catch {
    /* private mode / storage disabled — session just won't persist across loads */
  }
}

function isUnauthenticated(e: unknown): boolean {
  return e instanceof ApiError && e.status === 401 && e.code === 'invalid_session';
}

export type ChatStatus = 'booting' | 'ready' | 'sending' | 'error';

export interface UseChatOptions {
  /** Bootstrap only once the user is known signed in. Defaults true. */
  enabled?: boolean;
  /** Which assistant this thread talks to. Defaults to the home instance. */
  instance?: string;
}

export interface UseChat {
  messages: ChatMessage[];
  status: ChatStatus;
  error: string | null;
  sending: boolean;
  /** True once any call reported 401 invalid_session — the page redirects to /login. */
  unauthenticated: boolean;
  /** `kind` tags the backend turn counter ('voice' for transcript-originated sends). */
  send: (text: string, kind?: ChatKind) => Promise<void>;
  newChat: () => Promise<void>;
}

function friendlyError(e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.code) {
      case 'invalid_session':
        return 'Your session has ended — please sign in again.';
      case 'unknown_user':
        return "This account isn't on the allowlist for this instance.";
      case 'no_such_session':
        return 'That conversation has ended. Start a new chat to continue.';
      case 'engine_error':
        return 'The assistant hit a snag answering that. Try again in a moment.';
      case 'transport_unreachable':
      case 'network_error':
        return "Can't reach the assistant right now. Try again shortly.";
      case 'transport_misconfigured':
        return 'The chat backend isn’t configured yet.';
      default:
        return 'Something went wrong. Please try again.';
    }
  }
  return 'Something went wrong. Please try again.';
}

export function useChat(options: UseChatOptions = {}): UseChat {
  const enabled = options.enabled ?? true;
  const instance = options.instance || HOME_INSTANCE_NAME;
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<ChatStatus>('booting');
  const [error, setError] = useState<string | null>(null);
  const [unauthenticated, setUnauthenticated] = useState(false);
  const sessionKeyRef = useRef<string | null>(null);

  const fail = useCallback((e: unknown) => {
    if (isUnauthenticated(e)) setUnauthenticated(true);
    setStatus('error');
    setError(friendlyError(e));
  }, []);

  const openFresh = useCallback(async () => {
    const { session_key } = await chatApi.open(instance);
    sessionKeyRef.current = session_key;
    writeStored(instance, session_key);
    setMessages([]);
  }, [instance]);

  const bootstrap = useCallback(async () => {
    setStatus('booting');
    setError(null);
    sessionKeyRef.current = null;
    setMessages([]);
    try {
      const stored = readStored(instance);
      if (stored) {
        try {
          const { turns } = await chatApi.history(stored, instance);
          sessionKeyRef.current = stored;
          setMessages(turns.map(turnToMessage));
          setStatus('ready');
          return;
        } catch (e) {
          // Only a 404 (session gone) falls through to opening a fresh one. Any
          // other failure is surfaced — opening a new session would likely fail
          // the same way and would silently lose the resume attempt.
          if (!(e instanceof ApiError && e.status === 404)) throw e;
        }
      }
      await openFresh();
      setStatus('ready');
    } catch (e) {
      fail(e);
    }
  }, [instance, openFresh, fail]);

  // Re-bootstrap whenever the active instance changes — each instance has its own
  // independent thread (per-instance session key).
  useEffect(() => {
    if (enabled) void bootstrap();
  }, [enabled, bootstrap]);

  const send = useCallback(
    async (raw: string, kind: ChatKind = 'text') => {
      const text = raw.trim();
      const key = sessionKeyRef.current;
      if (!text || !key) return;
      setError(null);
      // Hoist the optimistic user bubble's id so we can patch its ts once the
      // backend returns the real user-turn stamp (keeps live == resume — the same
      // message wouldn't visibly shift its time on the next history reload).
      const userId = nextId();
      setMessages((prev) => [...prev, { id: userId, role: 'user', text, ts: '' }]);
      setStatus('sending');
      try {
        const { reply, ts, user_ts } = await chatApi.turn(key, text, { kind, instance });
        setMessages((prev) => [
          ...prev.map((m) =>
            m.id === userId ? { ...m, ts: user_ts || '' } : m,
          ),
          { id: nextId(), role: 'assistant', text: reply, ts: ts || '' },
        ]);
        setStatus('ready');
      } catch (e) {
        // The user's message stays in the thread; the error banner invites a retry.
        fail(e);
      }
    },
    [instance, fail],
  );

  const newChat = useCallback(async () => {
    setStatus('booting');
    setError(null);
    try {
      await openFresh();
      setStatus('ready');
    } catch (e) {
      fail(e);
    }
  }, [openFresh, fail]);

  return {
    messages,
    status,
    error,
    sending: status === 'sending',
    unauthenticated,
    send,
    newChat,
  };
}
