import { useCallback, useEffect, useMemo, useState } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { Deck } from '../components/feed/Deck';
import { authApi } from '../lib/algernon/authClient';
import Link from 'next/link';
import { feedApi, type FeedItem } from '../lib/algernon/feed';
import { arrivedVerbless, isDeckDealt } from '../lib/algernon/feedConstants';
import { readDeckSnoozed, writeDeckSnoozed } from '../lib/algernon/deckSnooze';
import { ApiError } from '../lib/algernon/http';
import { useSession } from '../lib/algernon/useSession';
import { CONSOLE_LABEL } from '../lib/algernon/consoleTokens';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// The snooze hide-list moved to `lib/algernon/deckSnooze.ts` — unchanged logic,
// same key, now with ONE owner. The feed's "N decisions waiting" banner has to
// consult it too, and while it lived here as a module-private the feed could not:
// it counted cards this deck had already refused to deal. See that module for
// the incident.
const readSnoozed = readDeckSnoozed;
const writeSnoozed = writeDeckSnoozed;

// The Decide deck: open decide-mode feed items as swipeable cards. Auth-gated
// like the brief page (signed-out → /login?next=/deck).
export default function DeckPage() {
  const router = useRouter();
  const { user, loading: sessionLoading } = useSession();
  const authed = !sessionLoading && user !== null;

  const [items, setItems] = useState<FeedItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [unauthenticated, setUnauthenticated] = useState(false);

  useEffect(() => {
    if ((!sessionLoading && !user) || unauthenticated) {
      router.replace(`/login?next=${encodeURIComponent('/deck')}`);
    }
  }, [sessionLoading, user, unauthenticated, router]);

  useEffect(() => {
    if (!authed) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await feedApi.list({ state: 'open', mode: 'decide' });
        if (cancelled) return;
        const snoozed = readSnoozed();
        setItems(res.items.filter((it) => !snoozed.has(it.id)));
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) {
          setUnauthenticated(true);
          return;
        }
        setError('Could not load the deck. Try refreshing.');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authed]);

  const onSnoozePersist = useCallback((id: string) => {
    const p = readSnoozed();
    p.add(id);
    writeSnoozed(p);
  }, []);
  const onUnsnoozePersist = useCallback((id: string) => {
    const p = readSnoozed();
    p.delete(id);
    writeSnoozed(p);
  }, []);
  const onAuthExpired = useCallback(() => setUnauthenticated(true), []);

  const handleSignOut = useCallback(async () => {
    try {
      await authApi.logout();
    } catch {
      /* best-effort */
    }
    router.replace('/login');
  }, [router]);

  // The deck deals only isDeckDealt items — classic decisions + SUGGESTED slots (C2).
  // A non-deck-dealt decide item (a PLANNED slot — committed, awaiting its ✓) is a
  // worklist item, not a deck card; it lives on the Feed / rings. Filter client-side;
  // the API contract is untouched.
  const actionable = useMemo(
    () => (items ?? []).filter(isDeckDealt),
    [items],
  );
  // The non-dealt items, SPLIT BY CAUSE — because the two causes are opposite
  // and need opposite copy. This list is decide-mode only (see the fetch), so
  // there are exactly two ways to be here:
  //   worklist — a committed slot, carrying board verbs but no swipe verb. This
  //              is the system working; the Feed really can action it.
  //   verbless — nothing arrived at all. A fault upstream (a half-deployed box,
  //              a stamp failure, a decide kind with no ceiling entry). The Feed
  //              cannot action it either, so sending the operator there would be
  //              a wrong steer dressed as a helpful one.
  // ILB: absence has to be legible as deliberate vs broken, and "N on your
  // worklist" is precisely the deliberate-sounding sentence.
  const notDealt = useMemo(
    () => (items ?? []).filter((it) => !isDeckDealt(it)),
    [items],
  );
  const verblessCount = useMemo(() => notDealt.filter(arrivedVerbless).length, [notDealt]);
  const worklistCount = notDealt.length - verblessCount;

  if (!authed) {
    return (
      <>
        <Head>
          <title>Deck · {INSTANCE_NAME}</title>
        </Head>
        <Layout showNav={false} surface="console">
          <p data-testid="auth-gate" className="text-sm text-console-ink-dim">
            Loading…
          </p>
        </Layout>
      </>
    );
  }

  return (
    <>
      <Head>
        <title>Deck · {INSTANCE_NAME}</title>
      </Head>
      <Layout onSignOut={() => void handleSignOut()} surface="console" maxWidthClassName="max-w-5xl">
        {/* The deck is a TACTICAL CONSOLE, so its title is a read-out rather
            than a page heading — tracked-out caps on the affirm rail colour,
            with the instance it belongs to stated beside it. */}
        <div className="mb-3 flex items-baseline gap-3">
          {/* Still "Deck", not "Decide". D1 makes this THE Decide surface, but
              the nav link, the document title and every test id call it the
              deck — renaming only the heading would be drift, and renaming the
              surface is an arc-level call, not this lane's. */}
          <h1 className="text-lg font-bold uppercase tracking-[0.26em] text-affirm">Deck</h1>
          <p className={CONSOLE_LABEL}>{INSTANCE_NAME} · swipe, tap, or arrow keys</p>
        </div>

        {error && (
          <div role="alert" data-testid="deck-error" className="mt-6 rounded-sm border-l-2 border-negative bg-negative-wash px-3 py-2 text-sm text-negative">
            {error}
          </div>
        )}

        {items == null && !error && (
          <p data-testid="deck-loading" className="mt-6 text-sm text-console-ink-dim">
            Loading the deck…
          </p>
        )}

        {items != null && !error && actionable.length === 0 && notDealt.length === 0 && (
          // ILB: empty because there is genuinely nothing open to decide.
          <p data-testid="deck-empty" className="mt-6 text-sm text-console-ink-dim">
            Nothing to decide right now — new decisions arrive with each sync.
          </p>
        )}

        {items != null && !error && actionable.length === 0 && verblessCount > 0 && (
          // ILB: the FAULT case, and it takes precedence over the worklist line
          // when both are present — one of the two says something is wrong, and
          // that is the half worth the operator's attention.
          //
          // The copy states the fault, refuses to claim loss, and does NOT send
          // them to the Feed: the Feed cannot action a verbless item either, and
          // pointing there would spend a trip to arrive at the same wall. It also
          // promises no remedy, because there may not be one on this side — box
          // skew clears on deploy, a ceiling gap does not clear at all.
          <p data-testid="deck-verbless" className="mt-6 text-sm text-caution">
            {verblessCount} decision{verblessCount > 1 ? 's' : ''} arrived without controls, so
            there{verblessCount > 1 ? ' is' : "'s"} nothing here to swipe. That{"'"}s a fault on the
            way here, not an empty queue —{' '}
            {verblessCount > 1 ? 'the items are' : 'the item is'} still held, and nothing has been
            decided or lost.
          </p>
        )}

        {items != null && !error && actionable.length === 0 && verblessCount === 0 && worklistCount > 0 && (
          // ILB: nothing to SWIPE, but there are non-deck-dealt open items — PLANNED
          // slots (committed, awaiting their ✓). Those are worklist items, actionable
          // inline on the Feed / rings, not deck cards.
          <p data-testid="deck-unactionable" className="mt-6 text-sm text-console-ink-dim">
            {worklistCount} item{worklistCount > 1 ? 's are' : ' is'} on your worklist —
            see {worklistCount > 1 ? 'them' : 'it'} on the{' '}
            <Link href="/feed" className="text-affirm underline underline-offset-4">
              Feed
            </Link>
            .
          </p>
        )}

        {actionable.length > 0 && (
          <div className="mt-4 flex min-h-[460px] flex-col">
            <Deck
              items={actionable}
              onAuthExpired={onAuthExpired}
              onSnoozePersist={onSnoozePersist}
              onUnsnoozePersist={onUnsnoozePersist}
            />
          </div>
        )}
      </Layout>
    </>
  );
}
