import { useCallback, useEffect, useState } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { ChatThread } from '../components/chat/ChatThread';
import { ChatTargetPicker } from '../components/chat/ChatTargetPicker';
import { Composer } from '../components/chat/Composer';
import { Button } from '../components/ui/button';
import { authApi } from '../lib/algernon/authClient';
import { chatApi } from '../lib/algernon/client';
import { useChat } from '../lib/algernon/useChat';
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
  const { messages, status, error, sending, working, unauthenticated, send, newChat } = useChat({
    enabled: authed,
    instance,
  });
  const booting = status === 'booting';

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
      router.replace(`/login?next=${encodeURIComponent('/')}`);
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
      <Layout onSignOut={() => void handleSignOut()}>
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

          {error && (
            <p
              role="alert"
              data-testid="chat-error"
              className="rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
            >
              {error}
            </p>
          )}

          <Composer onSend={(t, kind) => void send(t, kind)} disabled={booting || sending} />
        </div>
      </Layout>
    </>
  );
}
