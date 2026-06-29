import { useCallback, useEffect, useRef, useState } from 'react';
import { chatApi } from './client';
import { ApiError } from './http';
import { HOME_INSTANCE_NAME } from './instance';
import { createSseParser } from './sse';
import type {
  ChatKind,
  ChatMessage,
  HistoryTurn,
  StreamDoneEvent,
  StreamErrorEvent,
  StreamStatusEvent,
} from './types';

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

// Errors where the turn MAY have completed server-side (the reply is persisted by
// append_turn before run_turn returns) — so we reconcile via /chat/history instead
// of dead-erroring. Distinct from definitive failures (engine_error / 4xx) where
// no reply was stamped. (CONTRACT S5/S8.)
const RECOVERABLE_CODES = new Set([
  'network_error',
  'transport_unreachable',
  'gateway_timeout',
  'timeout',
]);

function isRecoverable(e: unknown): boolean {
  return e instanceof ApiError && RECOVERABLE_CODES.has(e.code);
}

function safeJson<T>(s: string): T | null {
  try {
    return JSON.parse(s) as T;
  } catch {
    return null;
  }
}

// A live, human label for a stream `status` frame (tool activity on a long turn).
function workingLabelFor(s: StreamStatusEvent | null): string {
  if (s && s.phase === 'tool' && s.tool) {
    switch (s.tool) {
      case 'vault_search':
      case 'vault_list':
      case 'vault_read':
      case 'vault_context':
        return 'Searching the vault…';
      case 'vault_create':
      case 'vault_edit':
      case 'vault_move':
      case 'vault_delete':
        return 'Updating the vault…';
      default:
        return `Working… (${s.tool})`;
    }
  }
  return 'Working…';
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
  /** Live tool-activity label while a streamed turn is in flight (null otherwise). */
  working: string | null;
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
  const [working, setWorking] = useState<string | null>(null);
  const [unauthenticated, setUnauthenticated] = useState(false);
  const sessionKeyRef = useRef<string | null>(null);
  // Mirrors `messages` for synchronous reads inside async send flows (e.g. the
  // pre-send transcript length used by the history-reconcile "did it grow?" check).
  const messagesRef = useRef<ChatMessage[]>([]);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

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

  // Adopt the server transcript if it grew an assistant reply for our turn. The
  // reply is persisted (append_turn) BEFORE run_turn returns, so a turn that
  // completed but whose stream/response was lost is recoverable here — NEVER show
  // "Can't reach the assistant" when the turn actually finished (CONTRACT S5).
  const reconcileFromHistory = useCallback(
    async (key: string, priorLen: number): Promise<boolean> => {
      try {
        const { turns } = await chatApi.history(key, instance);
        const last = turns[turns.length - 1];
        if (turns.length > priorLen && last && last.role === 'assistant') {
          setMessages(turns.map(turnToMessage));
          setStatus('ready');
          setError(null);
          return true;
        }
      } catch {
        /* reconcile is best-effort — fall through to the caller's failure path */
      }
      return false;
    },
    [instance],
  );

  // Finalise the optimistic bubbles from the terminal `done` payload (patch the
  // user-turn ts + append the assistant reply) — identical to the buffered path.
  const finalizeReply = useCallback((userId: string, d: StreamDoneEvent) => {
    setMessages((prev) => [
      ...prev.map((m) => (m.id === userId ? { ...m, ts: d.user_ts || '' } : m)),
      { id: nextId(), role: 'assistant', text: d.reply, ts: d.ts || '' },
    ]);
    setStatus('ready');
  }, []);

  // Non-stream fallback (kept per CONTRACT §6) — used when the browser/runtime
  // can't read a streaming body. The shared idempotency key makes this safe.
  const bufferedTurn = useCallback(
    async (key: string, userId: string, text: string, kind: ChatKind, priorLen: number, idk: string) => {
      try {
        const d = await chatApi.turn(key, text, { kind, instance, idempotencyKey: idk });
        finalizeReply(userId, d);
      } catch (e) {
        if (isRecoverable(e) && (await reconcileFromHistory(key, priorLen))) return;
        fail(e);
      }
    },
    [instance, finalizeReply, reconcileFromHistory, fail],
  );

  const send = useCallback(
    async (raw: string, kind: ChatKind = 'text') => {
      const text = raw.trim();
      const key = sessionKeyRef.current;
      if (!text || !key) return;
      setError(null);
      setWorking(null);
      // Snapshot the transcript length BEFORE the optimistic bubble so the
      // reconcile "did the transcript grow?" check compares against persisted turns.
      const priorLen = messagesRef.current.length;
      // Mint an idempotency key per logical turn (resent on the buffered fallback)
      // so a retry of a turn that already ran returns the cached result instead of
      // double-acting (e.g. a vault write). (CONTRACT S6.)
      const idk = crypto.randomUUID();
      // Hoist the optimistic user bubble's id so we can patch its ts once the
      // backend returns the real user-turn stamp (keeps live == resume).
      const userId = nextId();
      setMessages((prev) => [...prev, { id: userId, role: 'user', text, ts: '' }]);
      setStatus('sending');

      let res: Response;
      try {
        res = await chatApi.stream(key, text, { kind, instance, idempotencyKey: idk });
      } catch (e) {
        // Network failure reaching our own BFF — the turn likely never ran, but it
        // MIGHT have; reconcile, then fall back to a definitive failure.
        const err = e instanceof ApiError ? e : new ApiError(0, 'network_error');
        if (await reconcileFromHistory(key, priorLen)) return;
        fail(err);
        return;
      }

      // The BFF returns a JSON error (401/400/403/502/504) BEFORE any stream byte.
      const usableStream = res.ok && res.body && typeof res.body.getReader === 'function';
      if (!usableStream) {
        if (!res.ok) {
          const body = (await res.json().catch(() => null)) as { error?: string; detail?: string } | null;
          const err = new ApiError(res.status, body?.error || 'request_failed', body?.detail);
          if (isRecoverable(err) && (await reconcileFromHistory(key, priorLen))) return;
          fail(err);
          return;
        }
        // 200 but no readable body (old runtime) → the non-stream fallback.
        await bufferedTurn(key, userId, text, kind, priorLen, idk);
        return;
      }

      // usableStream above already verified res.body + getReader.
      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      const parser = createSseParser();
      let finalized = false;
      try {
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          for (const ev of parser.push(decoder.decode(value, { stream: true }))) {
            if (ev.event === 'status') {
              setWorking(workingLabelFor(safeJson<StreamStatusEvent>(ev.data)));
            } else if (ev.event === 'done') {
              const d = safeJson<StreamDoneEvent>(ev.data);
              if (d) {
                finalizeReply(userId, d);
                finalized = true;
              }
            } else if (ev.event === 'error') {
              const er = safeJson<StreamErrorEvent>(ev.data);
              const err = new ApiError(502, er?.error || 'engine_error', er?.detail);
              finalized = true;
              if (isRecoverable(err) && (await reconcileFromHistory(key, priorLen))) {
                // adopted — nothing more to do
              } else {
                fail(err);
              }
            }
          }
        }
      } catch {
        /* reader dropped mid-stream → treated as an incomplete stream below */
      } finally {
        setWorking(null);
      }

      // Stream closed WITHOUT a terminal done/error → the turn may have completed
      // server-side; reconcile rather than dead-error (CONTRACT S5).
      if (!finalized) {
        if (!(await reconcileFromHistory(key, priorLen))) {
          setStatus('error');
          setError(friendlyError(new ApiError(0, 'network_error')));
        }
      }
    },
    [instance, fail, reconcileFromHistory, finalizeReply, bufferedTurn],
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
    working,
    unauthenticated,
    send,
    newChat,
  };
}
