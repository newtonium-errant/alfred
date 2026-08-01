import { useCallback, useEffect, useMemo, useState } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { Deck } from '../components/feed/Deck';
import { authApi } from '../lib/algernon/authClient';
import Link from 'next/link';
import { feedApi, type FeedItem } from '../lib/algernon/feed';
import { isDeckDealt } from '../lib/algernon/feedConstants';
import { ApiError } from '../lib/algernon/http';
import { useSession } from '../lib/algernon/useSession';
import { display, subtle } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// Client-side park hide-list (this session only) — a parked card is deferred to
// the next sync, so it must not re-enter the deck if the page re-mounts. No store
// write (Phase B; the board is Phase C).
const PARK_KEY = 'algernon_deck_parked';

function readParked(): Set<string> {
  if (typeof window === 'undefined') return new Set();
  try {
    const raw = window.sessionStorage.getItem(PARK_KEY);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : []);
  } catch {
    return new Set();
  }
}

function writeParked(ids: Set<string>): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(PARK_KEY, JSON.stringify([...ids]));
  } catch {
    /* sessionStorage unavailable → park is best-effort in-memory only */
  }
}

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
        const parked = readParked();
        setItems(res.items.filter((it) => !parked.has(it.id)));
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

  const onParkPersist = useCallback((id: string) => {
    const p = readParked();
    p.add(id);
    writeParked(p);
  }, []);
  const onUnparkPersist = useCallback((id: string) => {
    const p = readParked();
    p.delete(id);
    writeParked(p);
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
  const unactionableCount = (items?.length ?? 0) - actionable.length;

  if (!authed) {
    return (
      <>
        <Head>
          <title>Deck · {INSTANCE_NAME}</title>
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
        <title>Deck · {INSTANCE_NAME}</title>
      </Head>
      <Layout onSignOut={() => void handleSignOut()}>
        <h1 className={display}>Deck</h1>
        <p className={`mt-1 ${subtle}`}>Decisions {INSTANCE_NAME} needs from you — swipe, tap, or use the arrow keys.</p>

        {error && (
          <div role="alert" data-testid="deck-error" className="mt-6 rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger">
            {error}
          </div>
        )}

        {items == null && !error && (
          <p data-testid="deck-loading" className={`mt-6 ${subtle}`}>
            Loading the deck…
          </p>
        )}

        {items != null && !error && actionable.length === 0 && unactionableCount === 0 && (
          // ILB: empty because there is genuinely nothing open to decide.
          <p data-testid="deck-empty" className={`mt-6 ${subtle}`}>
            Nothing to decide right now — new decisions arrive with each sync.
          </p>
        )}

        {items != null && !error && actionable.length === 0 && unactionableCount > 0 && (
          // ILB: nothing to SWIPE, but there are non-deck-dealt open items — PLANNED
          // slots (committed, awaiting their ✓). Those are worklist items, actionable
          // inline on the Feed / rings, not deck cards.
          <p data-testid="deck-unactionable" className={`mt-6 ${subtle}`}>
            {unactionableCount} item{unactionableCount > 1 ? 's are' : ' is'} on your worklist —
            complete {unactionableCount > 1 ? 'them' : 'it'} on the{' '}
            <Link href="/feed" className="underline underline-offset-2">
              Feed
            </Link>
            .
          </p>
        )}

        {actionable.length > 0 && (
          <div className="mt-6 flex min-h-[440px] flex-col">
            <Deck
              items={actionable}
              onAuthExpired={onAuthExpired}
              onParkPersist={onParkPersist}
              onUnparkPersist={onUnparkPersist}
            />
          </div>
        )}
      </Layout>
    </>
  );
}
