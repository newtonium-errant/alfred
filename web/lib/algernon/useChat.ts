import { useCallback, useEffect, useRef, useState } from 'react';
import { ChatApiError, chatApi } from './client';
import type { ChatMessage, HistoryTurn } from './types';

// Client-side chat state + the session resume model (team-lead confirmed):
//   * persist the session_key in localStorage,
//   * on load, resume via GET /chat/history/{key},
//   * open a FRESH session ONLY when there's no stored key or history 404s.
// /chat/open archives+closes the prior session, so we never call it on every
// load — that would destroy continuity.

const SESSION_KEY_STORAGE = 'algernon:session_key';

let _idSeq = 0;
function nextId(): string {
  _idSeq += 1;
  return `m${Date.now()}-${_idSeq}`;
}

function turnToMessage(t: HistoryTurn): ChatMessage {
  return { id: nextId(), role: t.role, text: t.text, ts: t.ts };
}

function readStored(): string | null {
  try {
    return localStorage.getItem(SESSION_KEY_STORAGE);
  } catch {
    return null;
  }
}

function writeStored(key: string): void {
  try {
    localStorage.setItem(SESSION_KEY_STORAGE, key);
  } catch {
    /* private mode / storage disabled — session just won't persist across loads */
  }
}

export type ChatStatus = 'booting' | 'ready' | 'sending' | 'error';

export interface UseChat {
  messages: ChatMessage[];
  status: ChatStatus;
  error: string | null;
  sending: boolean;
  send: (text: string) => Promise<void>;
  newChat: () => Promise<void>;
}

function friendlyError(e: unknown): string {
  if (e instanceof ChatApiError) {
    switch (e.code) {
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

export function useChat(): UseChat {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [status, setStatus] = useState<ChatStatus>('booting');
  const [error, setError] = useState<string | null>(null);
  const sessionKeyRef = useRef<string | null>(null);

  const openFresh = useCallback(async () => {
    const { session_key } = await chatApi.open();
    sessionKeyRef.current = session_key;
    writeStored(session_key);
    setMessages([]);
  }, []);

  const bootstrap = useCallback(async () => {
    setStatus('booting');
    setError(null);
    try {
      const stored = readStored();
      if (stored) {
        try {
          const { turns } = await chatApi.history(stored);
          sessionKeyRef.current = stored;
          setMessages(turns.map(turnToMessage));
          setStatus('ready');
          return;
        } catch (e) {
          // Only a 404 (session gone) falls through to opening a fresh one. Any
          // other failure is surfaced — opening a new session would likely fail
          // the same way and would silently lose the resume attempt.
          if (!(e instanceof ChatApiError && e.status === 404)) throw e;
        }
      }
      await openFresh();
      setStatus('ready');
    } catch (e) {
      setStatus('error');
      setError(friendlyError(e));
    }
  }, [openFresh]);

  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  const send = useCallback(async (raw: string) => {
    const text = raw.trim();
    const key = sessionKeyRef.current;
    if (!text || !key) return;
    setError(null);
    setMessages((prev) => [...prev, { id: nextId(), role: 'user', text, ts: '' }]);
    setStatus('sending');
    try {
      const { reply } = await chatApi.turn(key, text);
      setMessages((prev) => [
        ...prev,
        { id: nextId(), role: 'assistant', text: reply, ts: '' },
      ]);
      setStatus('ready');
    } catch (e) {
      // The user's message stays in the thread; the error banner invites a retry.
      setStatus('error');
      setError(friendlyError(e));
    }
  }, []);

  const newChat = useCallback(async () => {
    setStatus('booting');
    setError(null);
    try {
      await openFresh();
      setStatus('ready');
    } catch (e) {
      setStatus('error');
      setError(friendlyError(e));
    }
  }, [openFresh]);

  return {
    messages,
    status,
    error,
    sending: status === 'sending',
    send,
    newChat,
  };
}
