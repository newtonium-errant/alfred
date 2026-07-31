import { useCallback, useEffect, useState } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { FeedRow } from '../components/feed/FeedRow';
import { RingsHeader } from '../components/feed/RingsHeader';
import { useFeedBoard } from '../components/feed/useFeedBoard';
import { authApi } from '../lib/algernon/authClient';
import { composeMode, halifaxHour, type ComposeMode } from '../lib/algernon/composer';
import { useComposerLog } from '../lib/algernon/composerLog';
import { feedApi, type FeedItem } from '../lib/algernon/feed';
import { ApiError } from '../lib/algernon/http';
import { useSession } from '../lib/algernon/useSession';
import { display, subtle, title as titleClass } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// The home COMPOSER (B3-3). The landing composes one of three views from the
// operator's Halifax-local hour (brief-first / check-in / feed-first) — a
// rule-based v0. It is LAYOUT over existing surfaces (brief link, rings, the feed
// board), not new data logic: one feed load drives the counts, the rings, and the
// feed-first board. Every composition + any within-10s navigation-away is logged
// (capture-only, via useComposerLog) so the rule can be tuned in Phase D. A
// self-explaining `COMPOSED · <rule>` line always renders (the trust ramp).

export default function HomePage() {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const authed = !sessionLoading && user !== null;

  const [items, setItems] = useState<FeedItem[] | null>(null);
  const [unauthenticated, setUnauthenticated] = useState(false);
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());

  // The composed mode is a CLIENT-side decision (it reads the local clock), set
  // after mount so SSR and hydration never disagree.
  const [mode, setMode] = useState<ComposeMode | null>(null);
  useEffect(() => {
    setMode(composeMode(halifaxHour(new Date())));
  }, []);
  useComposerLog(mode, router);

  useEffect(() => {
    if ((!sessionLoading && !user) || unauthenticated) {
      router.replace(`/login?next=${encodeURIComponent('/')}`);
    }
  }, [sessionLoading, user, unauthenticated, router]);

  useEffect(() => {
    if (!authed) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await feedApi.list({ state: 'open' });
        if (!cancelled) setItems(res.items);
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) {
          setUnauthenticated(true);
        }
        // A non-401 feed failure is non-fatal for the composer: the surfaces
        // degrade to their own empty/loading states rather than blocking home.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authed]);

  const onAuthExpired = useCallback(() => setUnauthenticated(true), []);
  const board = useFeedBoard({ items: items ?? [], onAuthExpired });
  const deckCount = board.needsYou.length;

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

  if (!authed || mode == null) {
    return (
      <>
        <Head>
          <title>{INSTANCE_NAME}</title>
        </Head>
        <Layout showNav={false}>
          <p data-testid="auth-gate" className={subtle}>
            Loading…
          </p>
        </Layout>
      </>
    );
  }

  const deckPill = (
    <Link
      href="/deck"
      data-testid="composer-deck-pill"
      className="mt-3 flex items-center justify-between rounded-xl border border-honeydew-300 bg-honeydew-50 px-4 py-3 text-sm font-semibold text-honeydew-700"
    >
      <span>
        {deckCount > 0 ? `${deckCount} decision${deckCount > 1 ? 's' : ''} waiting` : 'Nothing to decide right now'}
      </span>
      <span aria-hidden>Open the deck →</span>
    </Link>
  );

  return (
    <>
      <Head>
        <title>{INSTANCE_NAME}</title>
      </Head>
      <Layout onSignOut={() => void handleSignOut()}>
        <h1 className={display}>Good to see you{user.name ? `, ${user.name}` : ''}.</h1>
        {/* Self-explaining composition line (verbatim requirement) — the trust ramp. */}
        <p data-testid="composed-line" className={`mt-1 font-mono text-xs uppercase tracking-widest ${subtle}`}>
          COMPOSED · {mode}
        </p>

        {mode === 'brief' && (
          <section data-testid="compose-brief" className="mt-6">
            <Link
              href="/brief"
              data-testid="composer-brief-card"
              className="block rounded-xl border border-honeydew-200 bg-cream p-4 shadow-soft"
            >
              <h2 className={titleClass}>Your morning brief is ready</h2>
              <p className={`mt-1 ${subtle}`}>The brief and Daily Sync {INSTANCE_NAME} prepared — open to read →</p>
            </Link>
            {deckPill}
          </section>
        )}

        {mode === 'checkin' && (
          <section data-testid="compose-checkin" className="mt-6">
            <h2 className={titleClass}>Midday check-in</h2>
            <div className="mt-3">
              <RingsHeader items={items ?? []} onAuthExpired={onAuthExpired} />
            </div>
            <p data-testid="composer-needs-you" className={`mt-3 ${subtle}`}>
              {deckCount > 0
                ? `${deckCount} thing${deckCount > 1 ? 's' : ''} need${deckCount > 1 ? '' : 's'} you.`
                : 'Nothing needs you right now.'}
            </p>
            {deckPill}
          </section>
        )}

        {mode === 'feed' && (
          <section data-testid="compose-feed" className="mt-6">
            {board.banner && (
              <div role="alert" data-testid="composer-feed-banner" className="mb-4 rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger">
                {board.banner}
              </div>
            )}
            {items == null ? (
              // Intentionally-left-blank: an explicit loading signal, not a blank home.
              <p data-testid="composer-feed-loading" className={subtle}>
                Loading the feed…
              </p>
            ) : board.needsYou.length === 0 && board.fyi.length === 0 ? (
              // Intentionally-left-blank: an explicit "all clear" state.
              <p data-testid="composer-feed-empty" className={subtle}>
                All clear — nothing needs you and nothing new to glance at.
              </p>
            ) : (
              <>
                {board.needsYou.length > 0 && deckPill}
                {board.fyi.length > 0 && (
                  <ul className="mt-4 flex flex-col gap-2">
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
                )}
              </>
            )}
          </section>
        )}

        {board.toast && (
          <div data-testid="composer-toast" role="status" className="fixed inset-x-0 bottom-20 z-50 mx-auto flex w-fit items-center gap-3 rounded-xl bg-honeydew-700 px-3.5 py-2.5 text-sm text-cream shadow-card">
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
