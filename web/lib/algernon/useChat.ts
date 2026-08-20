import { useCallback, useEffect, useRef, useState } from 'react';
import { chatApi } from './client';
import { ApiError } from './http';
import { HOME_INSTANCE_NAME } from './instance';
import { createSseParser } from './sse';
import type { ImageAttachment } from './schemas';
import type {
  ChatHistoryResponse,
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

// EXPORTED so the suite pins the REAL predicate, not a copy of it (#94) —
// the same reason `friendlyError` below is exported. A mirrored decision table
// in the test file would pass against a build where this function was broken,
// which is precisely the failure mode being fixed here (a verdict computed in
// one place and ignored in another).
export function isRecoverable(e: unknown): boolean {
  if (!(e instanceof ApiError)) return false;
  // THE SERVER'S VERDICT WINS when it has one (#94). `telegram/api_errors.py`
  // already decides retryability — it is the layer holding the actual
  // exception — and ships the answer on the wire as `retryable`. The client
  // used to drop that field and re-decide from a local code list, which is how
  // an upstream Anthropic 500 got treated as definitive: the pending turn was
  // cleared, no retry button appeared, and the operator recovered by typing
  // "Try now" by hand.
  //
  // Deferring here rather than adding 'engine_unavailable' to the set above
  // keeps ONE source of truth: a future retryable condition is a classifier
  // change and needs no client edit. Same doctrine as the health-status
  // predicate — consume the decision, never re-localize the comparison.
  //
  // `undefined` means the server had no opinion (the classifier abstained), so
  // the local set below still answers for network/timeout failures, which the
  // server never sees at all.
  if (typeof e.retryable === 'boolean') return e.retryable;
  return RECOVERABLE_CODES.has(e.code);
}

function safeJson<T>(s: string): T | null {
  try {
    return JSON.parse(s) as T;
  } catch {
    return null;
  }
}

// A bare "/end" control (exact or start-of-message — mirroring bot.py's
// _detect_inline_command line-local convention, NOT mid-prose) ends the session.
// "/endNote" or "tell me about /end" are NOT commands. (CONTRACT S7.)
function isEndCommand(text: string): boolean {
  return /^\/end(\s|$)/.test(text.trim());
}

// A turn in flight, retained so an explicit retry resends the SAME idempotency
// key (the backend returns the cached result if the turn already ran). (S6.)
interface PendingTurn {
  key: string;
  userId: string;
  text: string;
  kind: ChatKind;
  priorLen: number;
  idk: string;
  // Carried screenshots (parity #29) — resent verbatim on retry so a retried
  // image turn dedups to the same result (the SAME idempotency key rides along).
  images?: ImageAttachment[];
  // The STT transcript as inserted (#54) — retained so a retry carries it too.
  // Safe to resend: the backend records the correction only on a REAL send, and
  // a retry rides the SAME idempotency key ⇒ a dedup hit, which is guarded. So
  // one operator correction can never be counted twice by a retry.
  transcript?: string;
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

// The extraction offer surfaced after a toggle-off (or re-surfaced on
// bootstrap for a closed-but-unextracted span): which span, how much is in it.
export interface ExtractionOffer {
  spanIndex: number;
  turns: number;
}

export interface UseChat {
  messages: ChatMessage[];
  status: ChatStatus;
  error: string | null;
  sending: boolean;
  /** Live tool-activity label while a streamed turn is in flight (null otherwise). */
  working: string | null;
  /** A transient, non-error confirmation (e.g. after ending a chat). */
  notice: string | null;
  /** True once any call reported 401 invalid_session — the page redirects to /login. */
  unauthenticated: boolean;
  /** True when the last turn failed recoverably and retry() will resend it. */
  retryable: boolean;
  /**
   * The operator's composed text, held when the send hit a dead session (#94c).
   * Survives the re-bootstrap that wipes the thread, so the page can offer it
   * back instead of the operator losing what they wrote.
   */
  unsentText: string | null;
  /** Retire the held text once it has been handed back. */
  clearUnsent: () => void;
  /**
   * `kind` tags the backend turn counter ('voice' for transcript-originated
   * sends). `images` carries optional screenshots (parity #29) — an image-only
   * turn passes a placeholder caption as `text` (the composer supplies it).
   * `transcript` carries the STT text as inserted (#54) so the backend can learn
   * from what the operator changed; only a voice-seeded send supplies it.
   */
  send: (
    text: string,
    kind?: ChatKind,
    images?: ImageAttachment[],
    transcript?: string,
  ) => Promise<void>;
  /** Resend the last failed turn with the SAME idempotency key (no double-act). */
  retry: () => Promise<void>;
  /** Start a fresh chat (archives the prior session). */
  newChat: () => Promise<void>;
  /** End the chat (archives+opens fresh) and show a confirmation — the real /end. */
  endChat: () => Promise<void>;
  /** The active session_key (null while booting) — voice binds it at offer time. */
  sessionKey: string | null;
  /**
   * Adopt the server transcript into the thread (used after a voice turn persists).
   * No-ops (false) while a TYPED turn is pending/retryable — adopting then would
   * drop the optimistic user bubble. Returns true iff the thread was updated.
   */
  refreshFromHistory: () => Promise<boolean>;
  // --- Capture toggle (R1 2026-08-20) ---------------------------------------
  /** SERVER-truth capture state — set from toggle responses, history
   *  bootstraps, and captured receipts; never from what the client asked. */
  captureActive: boolean;
  /** True while a toggle round-trip is in flight (the button disables). */
  captureBusy: boolean;
  /** Flip capture. On→off surfaces the extraction offer for the closed span. */
  toggleCapture: () => Promise<void>;
  /** The quiet extraction offer, or null (no chip). */
  extractionOffer: ExtractionOffer | null;
  /** True while an accepted extraction runs (the chip shows "Extracting…"). */
  extracting: boolean;
  /** Accept the offer — run extraction; outcome lands in `notice`. */
  acceptExtraction: () => Promise<void>;
  /** Put the offer away. Nothing is lost: an unextracted span is finalized
   *  by the backend when the conversation closes. */
  dismissExtraction: () => void;
}

// Floor copy for `image_too_large` when the response carried no detail. Kept
// next to the switch that uses it, and deliberately says the same thing the box
// classifier says — if these ever disagree, the box wins (it is what `detail`
// carries). Exported so usePlayerAsk shares the one literal.
export const IMAGE_TOO_LARGE_FALLBACK =
  'An image earlier in this conversation is too large for a chat with this ' +
  'many images. Retrying won’t help — start a new chat and re-attach ' +
  'the images you still need.';

// Exported for the #82 WARN-2 unit: the switch is the seam that decides what
// an operator reads, so it is pinned directly rather than only through the hook.
export function friendlyError(e: unknown): string {
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
      case 'engine_unavailable':
        // #94(b). The server's own words when it classified an upstream 5xx —
        // they name the cause and say the request was fine. The literal below
        // is only the floor for a response that lost its detail.
        return e.detail
          || 'The assistant’s engine was briefly unavailable. Nothing is wrong with your message — send it again.';
      case 'turn_in_flight':
        // #94(d). Previously fell through to "Something went wrong", which is
        // both wrong and alarming: nothing went wrong, the previous message is
        // still being answered. Says what is happening and what to do, and
        // does NOT invite a retry that would be refused the same way.
        return 'Your previous message is still being answered. Wait for that reply before sending the next one.';
      case 'image_too_large':
        // Deterministic 400, NOT retryable (#82): an oversized image already in
        // the conversation history fails the same way on every resend, so the
        // "try again in a moment" copy above is exactly wrong here. The box
        // classifier owns the wording and ships it in `detail` — one source of
        // truth across this switch, usePlayerAsk, and the Telegram reply. The
        // literal is only the floor for a response that lost its detail.
        return e.detail || IMAGE_TOO_LARGE_FALLBACK;
      case 'span_open':
        // Capture toggle (R1): extraction asked while capture is still on.
        return 'Capture is still on — turn it off before extracting.';
      case 'extraction_in_flight':
        return 'That capture is already being extracted — give it a moment.';
      case 'transport_unreachable':
      case 'network_error':
        return "Can't reach the assistant right now. Try again shortly.";
      case 'timeout':
      case 'gateway_timeout':
        // The turn may still be finishing server-side — recovery-flavored, not the
        // dead "can't reach" (CONTRACT S8). Reconcile runs first; this is the copy
        // shown only when reconcile found no reply yet.
        return 'That took longer than expected — checking if it went through. You can try again.';
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
  const [notice, setNotice] = useState<string | null>(null);
  const [retryable, setRetryable] = useState(false);
  // #94(c) — the operator's COMPOSED text, held when the session turned out to
  // be gone. Deliberately NOT cleared by bootstrap: the whole point is that it
  // outlives the re-open that wipes the thread, which is where it was being
  // lost (he recovered a 50-minute conversation's reply by screenshotting it).
  const [unsentText, setUnsentText] = useState<string | null>(null);
  const [unauthenticated, setUnauthenticated] = useState(false);
  // session_key held in a ref for synchronous reads inside async send flows, AND
  // mirrored to state so the page can plumb it to VoicePanel (voice binds it at
  // offer time). setKey keeps the two in lockstep.
  const [sessionKey, setSessionKey] = useState<string | null>(null);
  const sessionKeyRef = useRef<string | null>(null);
  const pendingRef = useRef<PendingTurn | null>(null);
  // Capture toggle (R1). State + a synchronous mirror for async send flows —
  // same pattern as sessionKeyRef. Server truth: every setter below writes
  // what the BACKEND said, never what the client intended.
  const [captureActive, setCaptureActiveState] = useState(false);
  const captureActiveRef = useRef(false);
  const setCaptureActive = useCallback((v: boolean) => {
    captureActiveRef.current = v;
    setCaptureActiveState(v);
  }, []);
  const [captureBusy, setCaptureBusy] = useState(false);
  const [extractionOffer, setExtractionOffer] =
    useState<ExtractionOffer | null>(null);
  const [extracting, setExtracting] = useState(false);
  // The in-flight streamed turn's AbortController — aborted on unmount or when a
  // re-bootstrap (instance switch) supersedes the turn, tearing down the
  // browser→BFF fetch (the backend detaches and keeps run_turn running, S4).
  const abortRef = useRef<AbortController | null>(null);
  // Mirrors `messages` for synchronous reads inside async send flows (e.g. the
  // pre-send transcript length used by the history-reconcile "did it grow?" check).
  const messagesRef = useRef<ChatMessage[]>([]);
  useEffect(() => {
    messagesRef.current = messages;
  }, [messages]);

  // Abort any in-flight stream when the hook unmounts.
  useEffect(() => () => abortRef.current?.abort(), []);

  const fail = useCallback((e: unknown) => {
    if (isUnauthenticated(e)) setUnauthenticated(true);
    setStatus('error');
    setError(friendlyError(e));
  }, []);

  const setKey = useCallback((k: string | null) => {
    sessionKeyRef.current = k;
    setSessionKey(k);
  }, []);

  // Adopt server-truth capture state off a history payload (R1). Every
  // bootstrap path ends on history, so this is how a refreshed client
  // resumes capturing — and how a closed-but-unextracted span gets its
  // offer BACK instead of silently losing it to a reload.
  const adoptCaptureState = useCallback(
    (h: ChatHistoryResponse) => {
      setCaptureActive(!!h.capture_active);
      const spans = h.capture_spans || [];
      const pendingSpan = [...spans]
        .reverse()
        .find((s) => s.end !== null && !s.extracted);
      setExtractionOffer(
        pendingSpan
          ? { spanIndex: pendingSpan.index, turns: pendingSpan.turns }
          : null,
      );
    },
    [setCaptureActive],
  );

  const openFresh = useCallback(async () => {
    const { session_key } = await chatApi.open(instance);
    setKey(session_key);
    writeStored(instance, session_key);
    setMessages([]);
    // A fresh session starts with capture off and no offer (server truth:
    // open_session writes no span state).
    setCaptureActive(false);
    setExtractionOffer(null);
  }, [instance, setKey, setCaptureActive]);

  const bootstrap = useCallback(async () => {
    // Supersede any in-flight turn from the previous instance/thread.
    abortRef.current?.abort();
    setStatus('booting');
    setError(null);
    setNotice(null);
    setRetryable(false);
    pendingRef.current = null;
    setKey(null);
    setMessages([]);
    try {
      const stored = readStored(instance);
      if (stored) {
        try {
          const h = await chatApi.history(stored, instance);
          setKey(stored);
          setMessages(h.turns.map(turnToMessage));
          adoptCaptureState(h);
          setStatus('ready');
          return;
        } catch (e) {
          // Only a 404 (session gone) falls through to opening a fresh one. Any
          // other failure is surfaced — opening a new session would likely fail
          // the same way and would silently lose the resume attempt.
          if (!(e instanceof ApiError && e.status === 404)) throw e;
        }
      }
      // OPEN-OR-RESUME (#94c). A stale key must not mean "open a new session":
      // /chat/open is close-prior-then-fresh, so doing that DESTROYS whatever
      // live session exists — which is how two devices tore one conversation
      // apart (A's 404 killed B's live session, B 404'd, killed A's; three
      // opens in 22 seconds in the log). Ask first, and adopt what is there.
      try {
        const { session_key: live } = await chatApi.active(instance);
        if (live) {
          const h = await chatApi.history(live, instance);
          setKey(live);
          writeStored(instance, live);
          setMessages(h.turns.map(turnToMessage));
          adoptCaptureState(h);
          setStatus('ready');
          return;
        }
      } catch {
        // Probing is an OPTIMISATION, never a gate: if it fails we fall
        // through to the old behaviour rather than leaving the operator with
        // no chat at all. Worst case is the pre-#94 open.
      }
      await openFresh();
      setStatus('ready');
    } catch (e) {
      fail(e);
    }
  }, [instance, openFresh, fail, setKey, adoptCaptureState]);

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

  // Public reconcile wrapper for a voice-driven turn: adopt the persisted exchange
  // into the visible thread. Guards against a pending TYPED turn (whose optimistic
  // bubble adoption would clobber) and a not-yet-booted session.
  const refreshFromHistory = useCallback(async (): Promise<boolean> => {
    if (pendingRef.current) return false;
    const key = sessionKeyRef.current;
    if (!key) return false;
    return reconcileFromHistory(key, messagesRef.current.length);
  }, [reconcileFromHistory]);

  // Finalise the optimistic bubbles from the terminal `done` payload (patch the
  // user-turn ts + append the assistant reply) — identical to the buffered path.
  // A CAPTURED receipt (R1) appends NO assistant bubble: the user bubble gets
  // its real ts + the received mark, and capture state resyncs to server truth
  // (a client that didn't know capture was on learns it from the receipt).
  const finalizeReply = useCallback(
    (userId: string, d: StreamDoneEvent) => {
      if (d.captured) {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === userId
              ? { ...m, ts: d.user_ts || '', captured: true }
              : m,
          ),
        );
        setCaptureActive(true);
        setStatus('ready');
        return;
      }
      setMessages((prev) => [
        ...prev.map((m) => (m.id === userId ? { ...m, ts: d.user_ts || '' } : m)),
        { id: nextId(), role: 'assistant', text: d.reply, ts: d.ts || '' },
      ]);
      setStatus('ready');
    },
    [setCaptureActive],
  );

  // Run (or re-run) one logical turn. Streaming primary, buffered fallback, with
  // the S5 reconcile so a completed-but-lost turn never dead-errors. Outcome
  // bookkeeping: a success clears the pending turn + retryable; a RECOVERABLE
  // failure (network/timeout) keeps the pending turn so retry() can resend the
  // SAME idempotency key (the backend dedups if it already ran).
  const runTurn = useCallback(
    async (ctx: PendingTurn) => {
      const { key, userId, text, kind, priorLen, idk, images, transcript } = ctx;
      setError(null);
      setWorking(null);
      setStatus('sending');

      // Per-turn AbortController — unmount / instance-switch aborts the fetch.
      const ac = new AbortController();
      abortRef.current = ac;

      const onSuccess = () => {
        pendingRef.current = null;
        setRetryable(false);
        // It went through — nothing left to hand back.
        setUnsentText(null);
      };
      const onDefinitive = (e: unknown) => {
        // A vanished session is definitive for THIS send — the key is dead, so
        // resending it cannot work — but the operator's words are not the
        // session's property. Hold them so the re-open can hand them back.
        if (e instanceof ApiError && e.code === 'no_such_session') {
          setUnsentText(text);
        }
        pendingRef.current = null;
        setRetryable(false);
        fail(e);
      };
      const onRecoverable = (e: unknown) => {
        setRetryable(true); // pendingRef kept → retry() resends the same key
        fail(e);
      };

      let res: Response;
      try {
        res = await chatApi.stream(key, text, { kind, instance, idempotencyKey: idk, images, transcript, signal: ac.signal });
      } catch (e) {
        // Aborted by unmount / instance switch → the turn is being abandoned; do
        // not reconcile or touch state (the component is gone or re-bootstrapping).
        if (ac.signal.aborted) return;
        const err = e instanceof ApiError ? e : new ApiError(0, 'network_error');
        if (await reconcileFromHistory(key, priorLen)) onSuccess();
        else onRecoverable(err);
        return;
      }

      // The BFF returns a JSON error (401/400/403/502/504) BEFORE any stream byte.
      const usableStream = res.ok && res.body && typeof res.body.getReader === 'function';
      if (!usableStream) {
        if (!res.ok) {
          const body = (await res.json().catch(() => null)) as
            { error?: string; detail?: string; retryable?: boolean } | null;
          // `retryable` threaded through (#94) — the field exists on the wire
          // (classification_payload) and was being dropped here, so the
          // server's verdict never reached isRecoverable.
          const err = new ApiError(
            res.status, body?.error || 'request_failed', body?.detail, body?.retryable,
          );
          if (isRecoverable(err)) {
            if (await reconcileFromHistory(key, priorLen)) onSuccess();
            else onRecoverable(err);
          } else {
            onDefinitive(err);
          }
          return;
        }
        // 200 but no readable body (old runtime) → the non-stream fallback. The
        // shared idempotency key makes this safe even after the stream attempt.
        try {
          const d = await chatApi.turn(key, text, { kind, instance, idempotencyKey: idk, images, transcript });
          finalizeReply(userId, d);
          onSuccess();
        } catch (e) {
          if (isRecoverable(e)) {
            if (await reconcileFromHistory(key, priorLen)) onSuccess();
            else onRecoverable(e);
          } else {
            onDefinitive(e);
          }
        }
        return;
      }

      const reader = res.body!.getReader();
      const decoder = new TextDecoder();
      const parser = createSseParser();
      let settled = false;
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
                onSuccess();
                settled = true;
              }
            } else if (ev.event === 'error') {
              const er = safeJson<StreamErrorEvent>(ev.data);
              const err = new ApiError(
                502, er?.error || 'engine_error', er?.detail, er?.retryable,
              );
              settled = true;
              if (isRecoverable(err)) {
                if (await reconcileFromHistory(key, priorLen)) onSuccess();
                else onRecoverable(err);
              } else {
                onDefinitive(err);
              }
            }
          }
        }
      } catch {
        /* reader dropped mid-stream → treated as an incomplete stream below */
      } finally {
        setWorking(null);
      }

      // Aborted mid-stream by unmount / instance switch → abandon silently.
      if (ac.signal.aborted) return;

      // Stream closed WITHOUT a terminal done/error → the turn may have completed
      // server-side; reconcile rather than dead-error (CONTRACT S5).
      if (!settled) {
        if (await reconcileFromHistory(key, priorLen)) onSuccess();
        else onRecoverable(new ApiError(0, 'network_error'));
      }
    },
    [instance, fail, reconcileFromHistory, finalizeReply],
  );

  // The real /end: archive+open a fresh session and show a transient confirmation
  // (the web path never sent "/end" to the model — that just made it role-play
  // "session closed" while the session stayed live). (CONTRACT S7.)
  const endChat = useCallback(async () => {
    setStatus('booting');
    setError(null);
    setNotice(null);
    setRetryable(false);
    pendingRef.current = null;
    try {
      await openFresh();
      setStatus('ready');
      setNotice('Saved your conversation — started a new chat.');
    } catch (e) {
      fail(e);
    }
  }, [openFresh, fail]);

  const send = useCallback(
    async (
      raw: string,
      kind: ChatKind = 'text',
      images?: ImageAttachment[],
      transcript?: string,
    ) => {
      const text = raw.trim();
      const key = sessionKeyRef.current;
      // The composer guarantees a non-empty caption even for an image-only turn
      // (a placeholder), so `!text` still gates a truly-empty send. `key` gates
      // a not-yet-booted session.
      if (!text || !key) return;
      // Intercept a bare "/end" control BEFORE it reaches the model (S7).
      if (isEndCommand(text)) {
        await endChat();
        return;
      }
      setError(null);
      setNotice(null);
      setWorking(null);
      // Snapshot the transcript length BEFORE the optimistic bubble so the
      // reconcile "did the transcript grow?" check compares against persisted turns.
      const priorLen = messagesRef.current.length;
      // Mint an idempotency key per logical turn so a retry of a turn that already
      // ran returns the cached result instead of double-acting (a vault write). (S6.)
      const idk = crypto.randomUUID();
      // Hoist the optimistic user bubble's id so we can patch its ts once the
      // backend returns the real user-turn stamp (keeps live == resume).
      const userId = nextId();
      setMessages((prev) => [...prev, { id: userId, role: 'user', text, ts: '' }]);
      const ctx: PendingTurn = {
        key,
        userId,
        text,
        kind,
        priorLen,
        idk,
        ...(images && images.length ? { images } : {}),
        // Absent (not null/empty) when there is no transcript, so a typed turn's
        // request body stays byte-identical to the pre-feature shape.
        ...(transcript ? { transcript } : {}),
      };
      pendingRef.current = ctx;
      await runTurn(ctx);
    },
    [endChat, runTurn],
  );

  // Resend the last failed turn with the SAME idempotency key. The optimistic
  // user bubble is already in the thread, so we do NOT re-append it.
  const retry = useCallback(async () => {
    const ctx = pendingRef.current;
    if (!ctx) return;
    setNotice(null);
    await runTurn(ctx);
  }, [runTurn]);

  const newChat = useCallback(async () => {
    setStatus('booting');
    setError(null);
    setNotice(null);
    setRetryable(false);
    pendingRef.current = null;
    try {
      await openFresh();
      setStatus('ready');
    } catch (e) {
      fail(e);
    }
  }, [openFresh, fail]);

  // --- Capture toggle (R1 2026-08-20) ---------------------------------------

  const toggleCapture = useCallback(async () => {
    const key = sessionKeyRef.current;
    if (!key) return;
    setCaptureBusy(true);
    setNotice(null);
    try {
      const res = await chatApi.capture(key, !captureActiveRef.current, instance);
      // Render what the SERVER said, not what we asked for.
      setCaptureActive(res.capture_active);
      if (!res.capture_active) {
        // The offer follows a real closed span; an empty span closed to
        // null and offers nothing (the backend discarded it, logged).
        setExtractionOffer(
          res.closed_span
            ? {
                spanIndex: res.closed_span.index,
                turns: res.closed_span.turns,
              }
            : null,
        );
      }
    } catch (e) {
      if (isUnauthenticated(e)) setUnauthenticated(true);
      // A refused toggle is a calm sentence, not a thread-level failure —
      // the conversation itself is fine (e.g. 409 while a turn runs).
      setNotice(friendlyError(e));
    } finally {
      setCaptureBusy(false);
    }
  }, [instance, setCaptureActive]);

  const acceptExtraction = useCallback(async () => {
    const key = sessionKeyRef.current;
    const offer = extractionOffer;
    if (!key || !offer || extracting) return;
    setExtracting(true);
    setNotice(null);
    try {
      const res = await chatApi.captureExtract(key, offer.spanIndex, instance);
      setExtractionOffer(null);
      const n = res.notes.length;
      // ILB copy: "ran, nothing worth keeping" is a real outcome and says
      // so — never a silent chip disappearance.
      setNotice(
        n > 0
          ? `Extracted ${n} record${n === 1 ? '' : 's'} from the capture.`
          : 'Extraction ran — nothing stood out enough to keep this time.',
      );
    } catch (e) {
      if (isUnauthenticated(e)) setUnauthenticated(true);
      if (e instanceof ApiError && e.code === 'already_extracted') {
        // Someone (a retry, the close backstop) beat us to it — the offer
        // is stale, the material is safe.
        setExtractionOffer(null);
        setNotice('That capture was already extracted.');
      } else {
        // Keep the offer so the operator can try again.
        setNotice(friendlyError(e));
      }
    } finally {
      setExtracting(false);
    }
  }, [instance, extractionOffer, extracting]);

  const dismissExtraction = useCallback(() => {
    // Client-side only, and SAFE by design: an unextracted span is
    // finalized by the backend when the conversation closes.
    setExtractionOffer(null);
  }, []);

  return {
    messages,
    status,
    error,
    sending: status === 'sending',
    working,
    notice,
    unauthenticated,
    retryable,
    unsentText,
    clearUnsent: () => setUnsentText(null),
    send,
    retry,
    newChat,
    endChat,
    sessionKey,
    refreshFromHistory,
    captureActive,
    captureBusy,
    toggleCapture,
    extractionOffer,
    extracting,
    acceptExtraction,
    dismissExtraction,
  };
}
