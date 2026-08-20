import { useCallback, useEffect, useMemo, useState } from 'react';
import Head from 'next/head';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { Deck, DECK_COLUMN_MIN_PX } from '../components/feed/Deck';
import { authApi } from '../lib/algernon/authClient';
import Link from 'next/link';
import { feedApi, type FeedItem } from '../lib/algernon/feed';
import { arrivedVerbless, isDeckCandidate, isDeckDealt } from '../lib/algernon/feedConstants';
import { readDeckSnoozed, writeDeckSnoozed } from '../lib/algernon/deckSnooze';
import { ApiError } from '../lib/algernon/http';
import { useResumeRefetch } from '../lib/algernon/useResumeRefetch';
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

  // Named (rather than inline in the effect) so the mount and the RESUME share
  // one loader — the shape `pages/feed.tsx` and `pages/index.tsx` already use.
  const loadDeck = useCallback(async () => {
    try {
      // ALL open items, narrowed client-side by `isDeckCandidate` below. This
      // WAS `mode: 'decide'` server-side — which was exactly the wall that
      // kept a quiet (fyi/fyi) suggestion card produced, served, verb-carrying
      // and rendered nowhere: the mode filter dropped it before any predicate
      // could deal it. The candidate question is richer than one field
      // (decide-mode OR a served suggestion group), so it is asked where the
      // served actions are visible.
      const res = await feedApi.list({ state: 'open' });
      const snoozed = readSnoozed();
      setItems(res.items.filter((it) => !snoozed.has(it.id)));
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setUnauthenticated(true);
        return;
      }
      setError('Could not load the deck. Try refreshing.');
    }
  }, []);

  useEffect(() => {
    if (!authed) return;
    void loadDeck();
  }, [authed, loadDeck]);

  // #62 defect (3): an iOS PWA resumes with its heap intact — same tree, same
  // cards, no mount and no fetch. The deck was the surface the incident was
  // PHOTOGRAPHED on (an 11:43 screenshot of a deck rendered at 23:43) and was
  // the one member of this family never wired to the fix.
  //
  // It is also what makes the deck's own honesty copy true. A verdict lost to a
  // timeout is told "the next sync will reconcile it" — and until this line, on
  // this page, there was no next sync to do it: /deck fetched once, on mount,
  // and never again. The sentence was a promise the surface did not keep.
  useResumeRefetch(() => { if (authed) void loadDeck(); });

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

  // THE CANDIDATE POPULATION (one predicate, shared with the feed link and the
  // home pill so the counts cannot disagree): decide-mode decisions + quiet
  // suggestion cards (affirm-with-hold-modifier — fyi/fyi, so they deal here
  // without ever ringing the phone). Glance FYI rows are not candidates and
  // never reach either list below.
  const candidates = useMemo(
    () => (items ?? []).filter(isDeckCandidate),
    [items],
  );
  // The deck deals only isDeckDealt candidates — classic decisions + SUGGESTED
  // slots (C2) + suggestion cards. A non-deck-dealt decide item (a PLANNED slot
  // — committed, awaiting its ✓) is a worklist item, not a deck card; it lives
  // on the Feed / rings. Filter client-side; the API contract is untouched.
  const actionable = useMemo(
    () => candidates.filter(isDeckDealt),
    [candidates],
  );
  // The non-dealt candidates, SPLIT BY CAUSE — because the two causes are
  // opposite and need opposite copy. Two ways to be here:
  //   worklist — a committed slot, carrying board verbs but no swipe verb. This
  //              is the system working; the Feed really can action it.
  //   verbless — nothing arrived at all. A fault upstream (a half-deployed box,
  //              a stamp failure, a decide kind with no ceiling entry). The Feed
  //              cannot action it either, so sending the operator there would be
  //              a wrong steer dressed as a helpful one.
  // ILB: absence has to be legible as deliberate vs broken, and "N on your
  // worklist" is precisely the deliberate-sounding sentence.
  const notDealt = useMemo(
    () => candidates.filter((it) => !isDeckDealt(it)),
    [candidates],
  );
  const verblessCount = useMemo(() => notDealt.filter(arrivedVerbless).length, [notDealt]);
  const worklistCount = notDealt.length - verblessCount;

  // THE CURSOR ACROSS A REFETCH — required by the line above it, not decoration.
  //
  // The deck is the only member of the resume-refetch family that walks an INDEX
  // into its items array; the feed and the home composer render lists and have no
  // cursor to lose. Until the refetch was wired, /deck fetched once and that array
  // never changed underneath the index — so nothing had ever had to be true here.
  //
  // Wiring the refetch alone REGRESSES: decide three of five, background the app,
  // come back, and the server (correctly) serves only the two still open. The
  // index is still 3, the queue is now length 2, and the deck reports "Deck
  // clear." over two cards nobody has decided. Measured, not reasoned — that is
  // exactly what the probe printed before this line existed.
  //
  // Keying on the dealable id SET makes the cursor's meaning explicit: it is a
  // position within one dealing. A dealing whose cards changed is a different
  // dealing and starts at the top; a refetch that returns the same cards — the
  // common resume, where the operator merely glanced away — produces the same key
  // and does not disturb the cursor at all. Both halves are pinned, because a
  // remount on EVERY refetch would "fix" the first at the cost of throwing away
  // the operator's progress every time the app lost focus.
  //
  // What a re-deal costs is the in-memory session state (the snoozed drill-down,
  // this dealing's returned cards). Both have persistent backing that survives
  // it: the snooze hide-list in sessionStorage keeps those cards out of the new
  // dealing, and the unrecorded-verdict ledger in localStorage re-marks a refused
  // card in the fresh list. That the honesty mechanism survives a remount is a
  // property of having put it in storage rather than in state — pinned.
  const dealKey = actionable.map((it) => it.id).join('|');

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
          // The floor comes FROM the deck (DECK_COLUMN_MIN_PX), not from a number
          // typed here. The deck reserves clearance under its card stack so the
          // cards stop landing on the verb buttons, and this wrapper is a
          // min-height — so a literal left behind here would have paid for that
          // clearance out of the card instead of out of the page.
          <div className="mt-4 flex flex-col" style={{ minHeight: DECK_COLUMN_MIN_PX }}>
            <Deck
              key={dealKey}
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
