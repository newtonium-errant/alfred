import { useCallback, useEffect, useMemo, useState } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { PushToggle } from '../components/PushToggle';
import { FeedRow } from '../components/feed/FeedRow';
import { RingsHeader } from '../components/feed/RingsHeader';
import { useFeedBoard } from '../components/feed/useFeedBoard';
import { authApi } from '../lib/algernon/authClient';
import { composeMode, halifaxHour, type ComposeMode } from '../lib/algernon/composer';
import { useComposerLog } from '../lib/algernon/composerLog';
import { isDeckDealt } from '../lib/algernon/feedConstants';
import { feedApi, type FeedItem } from '../lib/algernon/feed';
import { ringItemDone } from '../lib/algernon/rings';
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
        // No state filter: the rings need today's DONE (state=acted) slot items
        // too (completion is a STAGE, not a disappearance). The needs-you / deck /
        // board surfaces below split back to open-only. One fetch, client-split.
        const res = await feedApi.list({});
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
  // OPEN-only for the remaining-work surfaces (board / needs-you / deck) — the
  // rings (below) get the full set incl. today's done. One fetch, split here.
  const openItems = useMemo(() => (items ?? []).filter((it) => it.state === 'open'), [items]);
  const board = useFeedBoard({ items: openItems, onAuthExpired });
  // How many things need you (the FEED truth) vs how many the DECK can actually
  // deal. The deck only handles kinds with a wired verb — slot_suggestion et al.
  // aren't swipeable — so the deck PROMISE counts deck-able only (mirrors feed.tsx,
  // b1). A DONE slot item (board-completed) no longer needs you, so it's excluded
  // from the needs-you total (Phase C).
  const needsYouCount = board.needsYou.filter(
    (it) => !(it.kind === 'slot_suggestion' && ringItemDone(it)),
  ).length;
  const deckableCount = board.needsYou.filter(isDeckDealt).length;

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

  // The deck entry — only rendered when there's something DECK-ABLE (call sites
  // gate on deckableCount > 0), so the count always matches what the deck deals.
  const deckPill = (
    <Link
      href="/deck"
      data-testid="composer-deck-pill"
      className="mt-3 flex items-center justify-between rounded-xl border border-honeydew-300 bg-honeydew-50 px-4 py-3 text-sm font-semibold text-honeydew-700"
    >
      <span>
        {deckableCount} decision{deckableCount > 1 ? 's' : ''} waiting
      </span>
      <span aria-hidden>Open the deck →</span>
    </Link>
  );

  // The rings are PERSISTENT across every composer mode — the completion surface
  // must exist all day, not only during the 11:00–14:00 check-in window. The mode
  // rules govern what LEADS (brief card / rings / feed board), not whether rings
  // exist. Controlled `items` seam → no second feed fetch.
  const ringsHeader = (
    <div className="mt-3">
      <RingsHeader items={items ?? []} onAuthExpired={onAuthExpired} />
    </div>
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

        {/* The rings are a PERSISTENT glance strip (the ratified sketch) — present
            in every mode so the completion surface exists all day, not only during
            the check-in window. The mode's lead content follows below. */}
        {ringsHeader}

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
            {deckableCount > 0 && deckPill}
          </section>
        )}

        {mode === 'checkin' && (
          <section data-testid="compose-checkin" className="mt-6">
            <h2 className={titleClass}>Midday check-in</h2>
            {/* The feed total (true) — distinct from the deck pill below, which is
                the deck-able subset. In check-in the non-deck-able needs-you items
                (slot_suggestion) are the rings above, so total = deck + rings. */}
            <p data-testid="composer-needs-you" className={`mt-3 ${subtle}`}>
              {needsYouCount > 0
                ? `${needsYouCount} thing${needsYouCount > 1 ? 's' : ''} need${needsYouCount > 1 ? '' : 's'} you.`
                : 'Nothing needs you right now.'}
            </p>
            {deckableCount > 0 && deckPill}
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
                {deckableCount > 0 && deckPill}
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

        {/* Push opt-in — renders nothing unless push is supported + configured. */}
        <PushToggle />

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
