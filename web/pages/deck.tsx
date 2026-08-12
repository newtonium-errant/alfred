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
import { CONSOLE_LABEL } from '../lib/algernon/consoleTokens';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// Client-side snooze hide-list (this session only) — a snoozed card is deferred to
// the next sync, so it must not re-enter the deck if the page re-mounts. No store
// write (Phase B; the board is Phase C).
const SNOOZE_KEY = 'algernon_deck_snoozed';

function readSnoozed(): Set<string> {
  if (typeof window === 'undefined') return new Set();
  try {
    const raw = window.sessionStorage.getItem(SNOOZE_KEY);
    const arr = raw ? (JSON.parse(raw) as unknown) : [];
    return new Set(Array.isArray(arr) ? arr.filter((x): x is string => typeof x === 'string') : []);
  } catch {
    return new Set();
  }
}

function writeSnoozed(ids: Set<string>): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(SNOOZE_KEY, JSON.stringify([...ids]));
  } catch {
    /* sessionStorage unavailable → snooze is best-effort in-memory only */
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
  const unactionableCount = (items?.length ?? 0) - actionable.length;

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

        {items != null && !error && actionable.length === 0 && unactionableCount === 0 && (
          // ILB: empty because there is genuinely nothing open to decide.
          <p data-testid="deck-empty" className="mt-6 text-sm text-console-ink-dim">
            Nothing to decide right now — new decisions arrive with each sync.
          </p>
        )}

        {items != null && !error && actionable.length === 0 && unactionableCount > 0 && (
          // ILB: nothing to SWIPE, but there are non-deck-dealt open items — PLANNED
          // slots (committed, awaiting their ✓). Those are worklist items, actionable
          // inline on the Feed / rings, not deck cards.
          <p data-testid="deck-unactionable" className="mt-6 text-sm text-console-ink-dim">
            {unactionableCount} item{unactionableCount > 1 ? 's are' : ' is'} on your worklist —
            see {unactionableCount > 1 ? 'them' : 'it'} on the{' '}
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
