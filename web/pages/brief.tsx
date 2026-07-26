import { useCallback, useEffect, useState } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { BriefView } from '../components/brief/BriefView';
import { authApi } from '../lib/algernon/authClient';
import { ApiError, getJson } from '../lib/algernon/http';
import { useSession } from '../lib/algernon/useSession';
import { display, subtle } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

type OutboundLatest = {
  kind: string;
  date: string | null;
  markdown: string | null;
};

// The Brief surface (#30 READ-ON-OPEN): the morning brief + Daily Sync the
// daemons already pushed to Telegram, readable in the PWA on open. Auth-gated
// like the chat page (signed-out → /login?next=/brief). Both kinds are fetched
// on mount; each section renders its own ILB empty state when nothing has been
// spooled yet today (the backend's 200 {date:null} — never an error).
export default function BriefPage() {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const authed = !sessionLoading && user !== null;

  const [brief, setBrief] = useState<OutboundLatest | null>(null);
  const [dailySync, setDailySync] = useState<OutboundLatest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unauthenticated, setUnauthenticated] = useState(false);

  // Redirect signed-out visitors to login — either /api/auth/me said "no
  // session" or a /api/brief call returned 401 invalid_session.
  useEffect(() => {
    if ((!sessionLoading && !user) || unauthenticated) {
      router.replace(`/login?next=${encodeURIComponent('/brief')}`);
    }
  }, [sessionLoading, user, unauthenticated, router]);

  useEffect(() => {
    if (!authed) return;
    let cancelled = false;
    const fetchKind = async (kind: 'brief' | 'daily_sync') =>
      getJson<OutboundLatest>(`/api/brief/latest?kind=${kind}`);
    void (async () => {
      try {
        const [b, d] = await Promise.all([fetchKind('brief'), fetchKind('daily_sync')]);
        if (cancelled) return;
        setBrief(b);
        setDailySync(d);
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) {
          setUnauthenticated(true);
          return;
        }
        setError('Could not load the brief. Try refreshing.');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authed]);

  const handleSignOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      /* clearing is best-effort; redirect regardless */
    }
    router.replace('/login');
  }, [router]);

  // Pre-auth (resolving session, or about to redirect): an explicit loading
  // signal, never a flash of content or a blank pane.
  if (!authed) {
    return (
      <>
        <Head>
          <title>Brief · {INSTANCE_NAME}</title>
        </Head>
        <Layout showNav={false}>
          <p data-testid="auth-gate" className={subtle}>
            Loading…
          </p>
        </Layout>
      </>
    );
  }

  const loading = brief === null && dailySync === null && !error;

  return (
    <>
      <Head>
        <title>Brief · {INSTANCE_NAME}</title>
      </Head>
      <Layout onSignOut={() => void handleSignOut()}>
        <h1 className={display}>Brief</h1>
        <p className={`mt-1 ${subtle}`}>
          Today&apos;s morning brief and Daily Sync from {INSTANCE_NAME}.
        </p>

        {error && (
          <div
            role="alert"
            data-testid="brief-error"
            className="mt-6 rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger"
          >
            {error}
          </div>
        )}

        {loading ? (
          // Intentionally-left-blank: an explicit loading signal.
          <p data-testid="brief-loading" className={`mt-6 ${subtle}`}>
            Loading the brief…
          </p>
        ) : (
          <div className="mt-6 flex flex-col gap-8">
            {brief && (
              <BriefView
                testId="brief-view"
                title="Morning Brief"
                date={brief.date}
                markdown={brief.markdown}
                emptyMessage="No brief yet today — it lands ~06:00."
              />
            )}
            {dailySync && (
              <BriefView
                testId="daily-sync-view"
                title="Daily Sync"
                date={dailySync.date}
                markdown={dailySync.markdown}
                emptyMessage="No Daily Sync yet today — it lands ~09:00."
              />
            )}
          </div>
        )}
      </Layout>
    </>
  );
}
