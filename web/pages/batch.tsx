import { useCallback, useEffect } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { CRT_SURFACE } from '../lib/algernon/crtSurface';
import { EmptyState } from '../components/EmptyState';
import { BatchForm } from '../components/ingest/BatchForm';
import { authApi } from '../lib/algernon/authClient';
import { useSession } from '../lib/algernon/useSession';
import { display, subtle } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// Bulk scan upload surface (#83). Same shell and same gates as /ingest: a
// signed-out visitor goes to /login, and a non-owner sees an explicit notice
// rather than a form that would 403 on submit.
//
// Separate from /ingest on purpose. Ingest writes ONE verbatim document and the
// operator sees the result immediately; this queues MANY scans for extraction
// that happens later, on a schedule. Sharing one form would mean one submit
// button with two very different meanings.
export default function BatchPage() {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const authed = !sessionLoading && user !== null;

  useEffect(() => {
    if (!sessionLoading && !user) {
      router.replace(`/login?next=${encodeURIComponent('/batch')}`);
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
    router.replace(`/login?next=${encodeURIComponent('/batch')}`);
  }, [router]);

  if (!authed) {
    return (
      <>
        <Head>
          <title>Bulk scans · {INSTANCE_NAME}</title>
        </Head>
        <Layout showNav={false} surface={CRT_SURFACE}>
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
        <title>Bulk scans · {INSTANCE_NAME}</title>
      </Head>
      <Layout onSignOut={() => void handleSignOut()} surface={CRT_SURFACE}>
        <div className="min-w-0">
          <h1 className={display}>Bulk scans</h1>
          <p className={`mt-1 ${subtle}`}>
            Upload a set of scans with one instruction that applies to all of them.
            They are read one at a time in the background — results fill in on the
            record as they finish.
          </p>
        </div>

        <div className="mt-6">
          {isOwner ? (
            <BatchForm onUnauthenticated={onUnauthenticated} />
          ) : (
            // Intentionally-left-blank: explicit owner-only state, not a dead form.
            <EmptyState
              icon="🔒"
              title="Owner only"
              message="Bulk scan upload is restricted to the instance owner."
              testId="batch-owner-only"
            />
          )}
        </div>
      </Layout>
    </>
  );
}
