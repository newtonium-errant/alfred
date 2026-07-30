import { useCallback, useEffect, useState } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { FeedRow } from '../components/feed/FeedRow';
import { useFeedBoard } from '../components/feed/useFeedBoard';
import { authApi } from '../lib/algernon/authClient';
import { feedApi, type FeedItem } from '../lib/algernon/feed';
import { ApiError } from '../lib/algernon/http';
import { useSession } from '../lib/algernon/useSession';
import { display, subtle, title as titleClass } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// The Awareness feed: open items grouped needs-you (→ the deck) above FYI (Ack).
// Auth-gated like the brief/deck pages.
export default function FeedPage() {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const authed = !sessionLoading && user !== null;

  const [items, setItems] = useState<FeedItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unauthenticated, setUnauthenticated] = useState(false);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());

  useEffect(() => {
    if ((!sessionLoading && !user) || unauthenticated) {
      router.replace(`/login?next=${encodeURIComponent('/feed')}`);
    }
  }, [sessionLoading, user, unauthenticated, router]);

  useEffect(() => {
    if (!authed) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await feedApi.list({ state: 'open' });
        if (cancelled) return;
        setItems(res.items);
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) {
          setUnauthenticated(true);
          return;
        }
        setError('Could not load the feed. Try refreshing.');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authed]);

  const onAuthExpired = useCallback(() => setUnauthenticated(true), []);
  const board = useFeedBoard({ items: items ?? [], onAuthExpired });

  const toggleExpanded = useCallback((id: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleSignOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      /* best-effort */
    }
    router.replace('/login');
  }, [router]);

  if (!authed) {
    return (
      <>
        <Head>
          <title>Feed · {INSTANCE_NAME}</title>
        </Head>
        <Layout showNav={false}>
          <p data-testid="auth-gate" className={subtle}>
            Loading…
          </p>
        </Layout>
      </>
    );
  }

  const loaded = items != null && !error;
  const empty = loaded && board.needsYou.length === 0 && board.fyi.length === 0;

  return (
    <>
      <Head>
        <title>Feed · {INSTANCE_NAME}</title>
      </Head>
      <Layout onSignOut={() => void handleSignOut()}>
        <h1 className={display}>Feed</h1>
        <p className={`mt-1 ${subtle}`}>What {INSTANCE_NAME} is tracking — decisions up top, glance items below.</p>

        {board.banner && (
          <div role="alert" data-testid="feed-banner" className="mt-4 rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger">
            {board.banner}
          </div>
        )}
        {error && (
          <div role="alert" data-testid="feed-error" className="mt-6 rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger">
            {error}
          </div>
        )}

        {items == null && !error && (
          <p data-testid="feed-loading" className={`mt-6 ${subtle}`}>
            Loading the feed…
          </p>
        )}

        {empty && (
          // Intentionally-left-blank: an explicit "all clear" state.
          <p data-testid="feed-empty" className={`mt-6 ${subtle}`}>
            All clear — nothing needs you and nothing new to glance at.
          </p>
        )}

        {loaded && board.needsYou.length > 0 && (
          <section data-testid="feed-needs-you" className="mt-6">
            <h2 className={titleClass}>Needs you</h2>
            <Link
              href="/deck"
              data-testid="feed-deck-link"
              className="mt-2 flex items-center justify-between rounded-xl border border-honeydew-300 bg-honeydew-50 px-4 py-3 text-sm font-semibold text-honeydew-700"
            >
              <span>
                {board.needsYou.length} decision{board.needsYou.length > 1 ? 's' : ''} waiting
              </span>
              <span aria-hidden>Open the deck →</span>
            </Link>
          </section>
        )}

        {loaded && board.fyi.length > 0 && (
          <section data-testid="feed-fyi" className="mt-6">
            <h2 className={titleClass}>For your awareness</h2>
            <ul className="mt-2 flex flex-col gap-2">
              {board.fyi.map((it) => (
                <FeedRow
                  key={it.id}
                  item={it}
                  expanded={expanded.has(it.id)}
                  onToggleEvidence={() => toggleExpanded(it.id)}
                  onAck={() => board.ack(it.id)}
                />
              ))}
            </ul>
          </section>
        )}

        {board.toast && (
          <div data-testid="feed-toast" role="status" className="fixed inset-x-0 bottom-20 z-50 mx-auto flex w-fit items-center gap-3 rounded-xl bg-honeydew-700 px-3.5 py-2.5 text-sm text-cream shadow-card">
            <span>{board.toast.message}</span>
            <button type="button" onClick={board.dismissToast} className="font-bold uppercase tracking-wider underline">
              Dismiss
            </button>
          </div>
        )}
      </Layout>
    </>
  );
}
