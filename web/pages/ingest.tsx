import { useCallback, useEffect } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { EmptyState } from '../components/EmptyState';
import { IngestForm } from '../components/ingest/IngestForm';
import { authApi } from '../lib/algernon/authClient';
import { useSession } from '../lib/algernon/useSession';
import { display, subtle } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// Cross-instance ingest surface. Auth-gated (same shell as the chat page): a
// signed-out visitor is sent to /login. Ingest is OWNER-ONLY (decision C) —
// enforced at the BFF; the page also shows a clear notice for non-owner sessions
// rather than a form that would 403. The operator picks a target instance + type,
// pastes/uploads/dictates a body, reviews the auto-provenance, and submits a
// verbatim document write.
export default function IngestPage() {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const authed = !sessionLoading && user !== null;

  useEffect(() => {
    if (!sessionLoading && !user) {
      router.replace(`/login?next=${encodeURIComponent('/ingest')}`);
    }
  }, [sessionLoading, user, router]);

  const handleSignOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      /* clearing is best-effort; redirect regardless */
    }
    router.replace('/login');
  }, [router]);

  const onUnauthenticated = useCallback(() => {
    router.replace(`/login?next=${encodeURIComponent('/ingest')}`);
  }, [router]);

  if (!authed) {
    return (
      <>
        <Head>
          <title>Ingest · {INSTANCE_NAME}</title>
        </Head>
        <Layout showNav={false}>
          <p data-testid="auth-gate" className={subtle}>
            Loading…
          </p>
        </Layout>
      </>
    );
  }

  const isOwner = user.role === 'owner';

  return (
    <>
      <Head>
        <title>Ingest · {INSTANCE_NAME}</title>
      </Head>
      <Layout onSignOut={() => void handleSignOut()}>
        <div className="min-w-0">
          <h1 className={display}>Ingest</h1>
          <p className={`mt-1 ${subtle}`}>
            Write a verbatim document into a chosen instance&rsquo;s vault — paste,
            upload, or dictate.
          </p>
        </div>

        <div className="mt-6">
          {isOwner ? (
            <IngestForm
              user={user}
              originInstance={INSTANCE_NAME}
              onUnauthenticated={onUnauthenticated}
            />
          ) : (
            // Intentionally-left-blank: explicit owner-only state, not a dead form.
            <EmptyState
              icon="🔒"
              title="Owner only"
              message="Document ingest is restricted to the instance owner."
              testId="ingest-owner-only"
            />
          )}
        </div>
      </Layout>
    </>
  );
}
