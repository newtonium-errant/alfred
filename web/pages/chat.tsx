import { useCallback, useEffect, useRef, useState } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { NotificationList } from '../components/NotificationList';
import { ChatThread } from '../components/chat/ChatThread';
import { ChatTargetPicker } from '../components/chat/ChatTargetPicker';
import { Composer } from '../components/chat/Composer';
import { UnifiedComposer } from '../components/chat/UnifiedComposer';
import { VoicePanel } from '../components/chat/VoicePanel';
import { unifiedComposerEnabled } from '../lib/algernon/composerFlag';
import { Button } from '../components/ui/button';
import { authApi } from '../lib/algernon/authClient';
import { chatApi } from '../lib/algernon/client';
import { useChat } from '../lib/algernon/useChat';
import { useNotifications } from '../lib/algernon/useNotifications';
import { useSession } from '../lib/algernon/useSession';
import { HOME_INSTANCE_NAME } from '../lib/algernon/instance';
import { display, subtle } from '../lib/typography';
import type { ChatTarget } from '../lib/algernon/types';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// The chat surface (M1, non-streaming). Auth-gated: a signed-out visitor is sent
// to /login. Once signed in, resumes the active session, shows a typing indicator
// while a turn is in flight, and renders a warm empty state before the first
// message. Errors surface in a danger banner (danger-red is reserved for true
// system errors) but leave the composer usable for a retry.
export default function ChatPage() {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const authed = !sessionLoading && user !== null;

  // The configured chat instances + the active selection (defaults to home).
  const [targets, setTargets] = useState<ChatTarget[]>([]);
  const [instance, setInstance] = useState<string>(HOME_INSTANCE_NAME);

  // Chat bootstraps once signed in, scoped to the active instance.
  const {
    messages,
    status,
    error,
    sending,
    working,
    notice,
    unauthenticated,
    retryable,
    unsentText,
    clearUnsent,
    send,
    retry,
    newChat,
    endChat,
    sessionKey,
    refreshFromHistory,
  } = useChat({
    enabled: authed,
    instance,
  });
  const booting = status === 'booting';

  // Notification tray (parity #22, POLL slice). The hook fetches on bootstrap
  // + a focused-only ~60s poll; the effect below adds the after-each-turn
  // refresh (a completed turn may have triggered a peer notice).
  const {
    notifications,
    unread,
    ack: ackNotifications,
    dismiss: dismissNotifications,
    refresh: refreshNotifications,
  } = useNotifications({ enabled: authed });
  const wasSending = useRef(false);
  useEffect(() => {
    // Refresh on the sending true→false transition only (turn completed) —
    // the hook's own bootstrap fetch covers mount.
    if (wasSending.current && !sending) void refreshNotifications();
    wasSending.current = sending;
  }, [sending, refreshNotifications]);

  // Load the instance switcher's options once signed in (metadata only; the
  // picker hides itself when only the home instance is configured).
  useEffect(() => {
    if (!authed) return;
    let cancelled = false;
    void (async () => {
      try {
        const { targets: t } = await chatApi.targets();
        if (!cancelled) setTargets(t);
      } catch {
        /* non-fatal: the switcher just stays single-instance (home). */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authed]);

  const activeLabel = targets.find((t) => t.name === instance)?.label || INSTANCE_NAME;

  // Redirect signed-out visitors to login — either /api/auth/me said "no session"
  // or a /chat call returned 401 invalid_session.
  useEffect(() => {
    if ((!sessionLoading && !user) || unauthenticated) {
      router.replace(`/login?next=${encodeURIComponent('/chat')}`);
    }
  }, [sessionLoading, user, unauthenticated, router]);

  const handleSignOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      /* clearing is best-effort; redirect regardless */
    }
    router.replace('/login');
  }, [router]);

  // Pre-auth (resolving session, or about to redirect): an explicit loading
  // signal, never a flash of the chat UI or a blank pane.
  if (!authed) {
    return (
      <>
        <Head>
          <title>Chat · {INSTANCE_NAME}</title>
        </Head>
        <Layout showNav={false}>
          <p data-testid="auth-gate" className={subtle}>
            Loading…
          </p>
        </Layout>
      </>
    );
  }

  return (
    <>
      <Head>
        <title>Chat · {INSTANCE_NAME}</title>
      </Head>
      <Layout onSignOut={() => void handleSignOut()} unreadCount={unread}>
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h1 className={display}>Chat</h1>
            <p className={`mt-1 ${subtle}`}>
              Signed in as <span className="font-semibold text-honeydew-700">{user.name}</span>{' '}
              · a vault-grounded conversation with {activeLabel}.
            </p>
          </div>
          <div className="flex shrink-0 items-end gap-2">
            <ChatTargetPicker
              targets={targets}
              instance={instance}
              onInstanceChange={setInstance}
              disabled={booting || sending}
            />
            <Button
              variant="outline"
              size="sm"
              data-testid="new-chat"
              onClick={() => void newChat()}
              disabled={booting || sending}
            >
              New chat
            </Button>
            <Button
              variant="outline"
              size="sm"
              data-testid="end-chat"
              onClick={() => void endChat()}
              disabled={booting || sending}
            >
              End chat
            </Button>
          </div>
        </div>

        <div className="mt-6 flex min-h-[55vh] flex-col gap-4">
          <div className="flex-1">
            {booting ? (
              // Intentionally-left-blank: an explicit loading signal, not a blank pane.
              <p data-testid="chat-booting" className={subtle}>
                Loading the conversation…
              </p>
            ) : (
              <ChatThread messages={messages} sending={sending} workingLabel={working} />
            )}
          </div>

          {notice && (
            // A calm, non-error confirmation (e.g. after ending a chat) — never
            // danger-red, which is reserved for true system errors.
            <p
              role="status"
              data-testid="chat-notice"
              className="rounded-xl bg-honeydew-100 px-3 py-2 text-sm text-honeydew-700"
            >
              {notice}
            </p>
          )}

          {/* Notification tray (parity #22, POLL slice — Telegram remains the
              closed-app channel). Always rendered with an explicit "No
              notifications" empty state (ILB) so a blank tray is observably
              deliberate, not a broken section. */}
          <section data-testid="notifications-section" aria-label="Notifications">
            <h2 className={`mb-2 ${subtle}`}>
              Notifications{unread > 0 ? ` · ${unread} unread` : ''}
            </h2>
            <NotificationList
              notifications={notifications}
              onAck={(ids) => void ackNotifications(ids)}
              onDismiss={(ids) => void dismissNotifications(ids)}
            />
          </section>

          {error && (
            <div
              role="alert"
              data-testid="chat-error"
              className="flex items-center justify-between gap-3 rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
            >
              <span>{error}</span>
              {retryable && (
                <button
                  type="button"
                  data-testid="chat-retry"
                  onClick={() => void retry()}
                  disabled={sending}
                  className="shrink-0 rounded-lg border border-danger px-2 py-1 font-semibold hover:opacity-80 disabled:opacity-60"
                >
                  Try again
                </button>
              )}
            </div>
          )}

          {/* Voice panel. Renders nothing unless NEXT_PUBLIC_VOICE_ENABLED is on; a
              per-instance /voice/config probe then hides it for chat-only instances.
              `instance` drives the probe + the auto-hangup-on-switch; `sessionKey`
              binds the voice offer to this chat session and gates start; `onTurnFinal`
              adopts a completed voice turn into the thread. */}
          <VoicePanel
            instance={instance}
            sessionKey={sessionKey}
            onTurnFinal={refreshFromHistory}
          />

          {/* `transcript` (#54) is the raw STT text a voice-seeded send was built
              from — threaded through to the turn body so the backend can learn
              from what the operator corrected. A typed send passes nothing.

              #97: with NEXT_PUBLIC_UNIFIED_COMPOSER on, the universal composer
              mounts here instead — the same `onSend` contract, plus attachment
              routing to the ingest and batch doors. OFF (the default, and the
              shipped state until the operator has walked the chip defaults) this
              renders exactly what it always did. The two are alternatives, never
              layered, so "flag off" is the old component itself rather than the
              new one in a quiet mode. */}
          {unifiedComposerEnabled() ? (
            <UnifiedComposer
              onSend={(t, kind, images, transcript) => void send(t, kind, images, transcript)}
              disabled={booting || sending}
              seedText={unsentText}
              onSeedConsumed={clearUnsent}
              instance={instance}
              instanceLabel={activeLabel}
              onUnauthenticated={() => router.replace(`/login?next=${encodeURIComponent('/chat')}`)}
            />
          ) : (
            <Composer
              onSend={(t, kind, images, transcript) => void send(t, kind, images, transcript)}
              disabled={booting || sending}
              seedText={unsentText}
              onSeedConsumed={clearUnsent}
            />
          )}
        </div>
      </Layout>
    </>
  );
}
