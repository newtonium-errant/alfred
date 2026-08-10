import { useCallback, useEffect, useMemo, useState } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { PushToggle } from '../components/PushToggle';
import { FeedRow } from '../components/feed/FeedRow';
import { RingsHeader } from '../components/feed/RingsHeader';
import { useFeedBoard } from '../components/feed/useFeedBoard';
import { useRingCompletion } from '../components/feed/useRingCompletion';
import { useResumeRefetch } from '../lib/algernon/useResumeRefetch';
import { authApi } from '../lib/algernon/authClient';
import {
  briefCardDetail,
  briefCardHeadline,
  briefFreshness,
  composeMode,
  halifaxHour,
  type BriefFreshness,
  type ComposeMode,
} from '../lib/algernon/composer';
import { useComposerLog } from '../lib/algernon/composerLog';
import { isDeckDealt } from '../lib/algernon/feedConstants';
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

  // #51 — the brief card must know the DATE of what it advertises. The window is
  // a guess about when the artifact exists; this is the artifact itself. `null`
  // means "not looked up yet" and renders the neutral copy, so a slow or failed
  // lookup never upgrades into a freshness claim.
  const [briefFresh, setBriefFresh] = useState<BriefFreshness | null>(null);
  useEffect(() => {
    if (!authed || mode !== 'brief') return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch('/api/brief/latest?kind=brief');
        if (!res.ok) throw new Error(`brief_latest_${res.status}`);
        const body = (await res.json()) as { date?: string | null };
        if (!cancelled) setBriefFresh(briefFreshness(body?.date ?? null));
      } catch {
        // Degrade to "no brief yet" rather than to the fresh claim. An
        // unreachable API is not evidence that today's brief landed.
        if (!cancelled) setBriefFresh('none');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authed, mode]);

  useEffect(() => {
    if ((!sessionLoading && !user) || unauthenticated) {
      router.replace(`/login?next=${encodeURIComponent('/')}`);
    }
  }, [sessionLoading, user, unauthenticated, router]);

  // Hoisted so the RESUME path can call it too (#62) — a load that lives only
  // inside useEffect runs only at mount, and an iOS PWA resumes without one.
  const loadFeed = useCallback(async () => {
    try {
      // No state filter: the rings need today's DONE (state=acted) slot items
      // too (completion is a STAGE, not a disappearance). The needs-you / deck /
      // board surfaces below split back to open-only. One fetch, client-split.
      const res = await feedApi.list({});
      setItems(res.items);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setUnauthenticated(true);
      }
      // A non-401 feed failure is non-fatal for the composer: the surfaces
      // degrade to their own empty/loading states rather than blocking home.
    }
  }, []);

  useEffect(() => {
    if (!authed) return;
    void loadFeed();
  }, [authed, loadFeed]);

  const onAuthExpired = useCallback(() => setUnauthenticated(true), []);
  // OPEN-only for the remaining-work surfaces (board / needs-you / deck) — the
  // rings (below) get the full set incl. today's done. One fetch, split here.
  const openItems = useMemo(() => (items ?? []).filter((it) => it.state === 'open'), [items]);
  const board = useFeedBoard({ items: openItems, onAuthExpired });
  // ONE completion instance for the whole composer, HOISTED out of RingsHeader and
  // threaded back in below. The rings are where a slot actually gets completed, so
  // when the hook lived inside them this count (reading the raw `ringItemDone`) had
  // no way to see the flip and lagged a whole fetch behind the green segment.
  const completion = useRingCompletion({ onAuthExpired });

  // #62 (3) resume freshness + (2) override supersession, on the composer too —
  // this is the surface the operator actually reopens in the morning.
  useResumeRefetch(() => { if (authed) void loadFeed(); });
  useEffect(() => {
    if (items) completion.reconcile(items);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);
  // How many things need you (the FEED truth) vs how many the DECK can actually
  // deal. `isDeckDealt` is the ONLY authority on which items the deck deals —
  // don't restate its rule here, because paraphrases of it are what #22 retired
  // (the old one said slot_suggestion "isn't swipeable"; a SUGGESTED one is). The
  // deck PROMISE counts deck-able only, so it can never over-promise the deck
  // (mirrors feed.tsx, b1). A DONE slot item (board-completed) no longer needs you, so it's excluded
  // from the needs-you total (Phase C) — via the shared hook, so a completion in the
  // rings drops the count in the SAME render. No override → `effectiveDone` is
  // exactly `ringItemDone`, so the server-truth baseline is unchanged.
  const needsYouCount = board.needsYou.filter(
    (it) => !(it.kind === 'slot_suggestion' && completion.effectiveDone(it)),
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
  // exist. Controlled `items` seam → no second feed fetch; controlled `completion`
  // seam → no second optimistic state (the needs-you count above reads the same one).
  const ringsHeader = (
    <div className="mt-3">
      <RingsHeader items={items ?? []} completion={completion} onAuthExpired={onAuthExpired} />
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
              {/* #51 — date-gated copy. `null` (lookup in flight) renders the
                  neutral "no brief yet" wording, so the fresh claim is only ever
                  made once the artifact's own date has confirmed it. */}
              <h2 className={titleClass} data-testid="composer-brief-headline">
                {briefCardHeadline(briefFresh ?? 'none')}
              </h2>
              <p className={`mt-1 ${subtle}`} data-testid="composer-brief-detail">
                {briefCardDetail(briefFresh ?? 'none', INSTANCE_NAME)}
              </p>
            </Link>
            {/* Offer the interruptible player (C3b) — the /player page degrades gracefully
                if there's no brief / no audio, so this is safe to offer in the brief window. */}
            <Link
              href="/player"
              data-testid="composer-player-link"
              className="mt-3 flex items-center justify-between rounded-xl border border-honeydew-300 bg-honeydew-50 px-4 py-3 text-sm font-semibold text-honeydew-700"
            >
              <span>Play your briefing</span>
              <span aria-hidden>▶</span>
            </Link>
            {deckableCount > 0 && deckPill}
          </section>
        )}

        {mode === 'checkin' && (
          <section data-testid="compose-checkin" className="mt-6">
            <h2 className={titleClass}>Midday check-in</h2>
            {/* WHAT THIS NUMBER IS: open feed items flagged as needing a decision
                (attention `needs_you`, or mode `decide` on older items), minus slot
                items already completed today. That is all it is.

                It used to claim `total = deck + rings`. That identity is false in
                BOTH directions (#22 / D5), so it is stated nowhere now:
                  * a slot_suggestion is deck-able when `ringItemSuggested`
                    (isDeckDealt), so "slot_suggestion" and "non-deck-able" are not
                    the same set; and
                  * any kind with no wired deck verb is non-deck-able and is NOT in
                    the rings either — so total - deck can include items that live
                    in neither.
                The real partition is deliberately NOT redrawn here: the
                interface-reimagine arc's Decide/Awareness split will redraw these
                buckets, and deciding it twice in weeks is waste. Copy says what is
                COUNTED and where the counted things can be acted on — it does not
                account for a remainder it cannot honestly place. */}
            <p data-testid="composer-needs-you" className={`mt-3 ${subtle}`}>
              {needsYouCount > 0
                ? `${needsYouCount} thing${needsYouCount > 1 ? 's' : ''} need${needsYouCount > 1 ? '' : 's'} you.`
                : 'Nothing needs you right now.'}
            </p>
            {deckableCount > 0 && deckableCount < needsYouCount && (
              <p data-testid="composer-needs-you-deckable" className={`mt-1 ${subtle}`}>
                {deckableCount} of them can be handled in the deck; the rest are on
                the feed.
              </p>
            )}
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
