import { useCallback, useEffect, useState } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { FeedRow } from '../components/feed/FeedRow';
import { useFeedBoard } from '../components/feed/useFeedBoard';
import { authApi } from '../lib/algernon/authClient';
import { useRingCompletion } from '../components/feed/useRingCompletion';
import { useSlotAccept } from '../components/feed/useSlotAccept';
import { useSnooze } from '../components/feed/useSnooze';
import { feedApi, type FeedItem } from '../lib/algernon/feed';
import { isDeckDealt } from '../lib/algernon/feedConstants';
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
  // Collapsed by default — parity with the rings drill (#26).
  const [showDone, setShowDone] = useState(false);
  // Collapsed by default — parity with the done drill (#26) and the rings.
  const [showSnoozed, setShowSnoozed] = useState(false);

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
  // ONE completion implementation, shared with the rings panel (never a second).
  const completion = useRingCompletion({ onAuthExpired });
  // The C2 accept hook — drives a SUGGESTED slot row's [Accept] + optimistic flip.
  const slotAccept = useSlotAccept({ onAuthExpired });
  // The #14 defer verb for the worklist rows — the same four durations the
  // deck's hold-band menu offers, behind a labelled button.
  const snooze = useSnooze({ onAuthExpired });
  // A DONE slot item no longer needs you (isDone counting — mirrors the composer).
  // Reads the COMPLETION hook, not the raw `ringItemDone`, so the drop-out lands in
  // the SAME render as the row's own ✓ flip — the raw stage only catches up on the
  // next fetch, which left a completed row sitting under "Needs you" (and the
  // "All clear" state suppressed) for a whole poll interval. With no override in
  // play `effectiveDone` IS `ringItemDone`, so the server-truth baseline is
  // unchanged; a FAILED act reverts the override and the row returns with its error.
  const activeNeedsYou = board.needsYou.filter(
    (it) => !(it.kind === 'slot_suggestion' && completion.effectiveDone(it)),
  );
  // Deck-dealt = classic decisions + SUGGESTED slots (isDeckDealt — the one predicate
  // that also drives the deck-link count, so it matches what /deck deals). Slot rows
  // (suggested → Accept, planned → ✓) ALSO render inline as the worklist — a suggested
  // slot is legitimately on both the deck link and the inline list (team-lead ruling).
  const deckable = activeNeedsYou.filter(isDeckDealt);
  const pendingRows = activeNeedsYou.filter(
    (it) => it.kind === 'slot_suggestion' && !snooze.snoozed(it.id),
  );
  // #14 — a snoozed row STAGES, it does not vanish. Same shape as the done
  // section deliberately: collapsed by default so the page still reads as
  // remaining work, but reachable, with Unsnooze one tap in. A defer whose only
  // escape hatch is the next sync would be a roach motel, and the indefinite
  // rung has no next sync to wait for.
  const snoozedRows = board.needsYou.filter(
    (it) => it.kind === 'slot_suggestion' && snooze.snoozed(it.id),
  );
  // #26 (D4) — a completed slot STAGES, it does not vanish. `activeNeedsYou`
  // drops done slots on purpose (they no longer need you, and must not inflate
  // the deck count), which is right for the COUNTS and wrong for the LIST: the
  // row simply disappeared, so a mis-tap was unrecoverable here while home's
  // rings offered Undo behind their drill. Derived from the unfiltered
  // `board.needsYou` so the counting rule above is untouched.
  //
  // Same shape as RingsHeader's drill deliberately, not a second idea about what
  // "done" means: same `completion.effectiveDone` predicate, same
  // Show done (N) / Hide done copy, same collapsed-by-default. One rule
  // everywhere — done stages, and Undo lives one tap in.
  const doneRows = board.needsYou.filter(
    (it) => it.kind === 'slot_suggestion' && completion.effectiveDone(it),
  );

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
  const empty = loaded && activeNeedsYou.length === 0 && board.fyi.length === 0;

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

        {loaded && activeNeedsYou.length > 0 && (
          <section data-testid="feed-needs-you" className="mt-6">
            <h2 className={titleClass}>Needs you</h2>
            {/* Deck-able decisions (a wired verb) route to the swipe deck; the
                count matches what the deck actually deals (see deck.tsx). */}
            {deckable.length > 0 && (
              <Link
                href="/deck"
                data-testid="feed-deck-link"
                className="mt-2 flex items-center justify-between rounded-xl border border-honeydew-300 bg-honeydew-50 px-4 py-3 text-sm font-semibold text-honeydew-700"
              >
                <span>
                  {deckable.length} decision{deckable.length > 1 ? 's' : ''} waiting
                </span>
                <span aria-hidden>Open the deck →</span>
              </Link>
            )}
            {/* Slot rows (non-deck-able needs-you) carry the SAME live per-lane
                completion control as the rings panel — completable lanes get a
                real ✓; task/unknown lanes get an honest note (no dead control). */}
            {pendingRows.length > 0 && (
              <ul data-testid="feed-pending" className="mt-2 flex flex-col gap-2">
                {pendingRows.map((it) => (
                  <FeedRow
                    key={it.id}
                    item={it}
                    expanded={expanded.has(it.id)}
                    onToggleEvidence={() => toggleExpanded(it.id)}
                    completion={completion}
                    accept={slotAccept}
                    snooze={snooze}
                  />
                ))}
              </ul>
            )}

          </section>
        )}

        {loaded && snoozedRows.length > 0 && (
          <section data-testid="feed-snoozed" className="mt-4">
            <button
              type="button"
              data-testid="feed-show-snoozed"
              onClick={() => setShowSnoozed((s) => !s)}
              aria-expanded={showSnoozed}
              className="text-[11px] font-semibold uppercase tracking-wider text-honeydew-600 underline underline-offset-2"
            >
              {showSnoozed ? 'Hide snoozed' : `Show snoozed (${snoozedRows.length})`}
            </button>
            {showSnoozed && (
              <ul data-testid="feed-snoozed-rows" className="mt-2 flex flex-col gap-2">
                {snoozedRows.map((it) => (
                  <li
                    key={it.id}
                    data-testid="feed-snoozed-row"
                    className="flex items-center gap-2 rounded-xl border border-honeydew-200 bg-cream p-3 shadow-soft"
                  >
                    <span className="min-w-0 flex-1 truncate text-sm text-honeydew-700 opacity-70">{it.title || it.id}</span>
                    <button
                      type="button"
                      data-testid="feed-row-unsnooze"
                      disabled={snooze.busy(it.id)}
                      onClick={() => void snooze.unsnooze(it)}
                      className="shrink-0 rounded-lg border border-honeydew-400 px-3 py-1 text-[11px] font-bold uppercase tracking-wider text-honeydew-700 disabled:opacity-50"
                    >
                      {snooze.busy(it.id) ? '…' : 'Unsnooze'}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}

        {loaded && doneRows.length > 0 && (
          // The staged section (#26). Deliberately a SIBLING of "Needs you"
          // rather than living inside it: the enclosing section is gated on
          // `activeNeedsYou.length > 0`, so completing the last slot unmounted
          // it — and staging a done row under a "Needs you" heading would
          // contradict the heading anyway. Done work is its own quiet band.
          //
          // Collapsed by default so the page still reads as remaining work, but
          // the row is REACHABLE — Undo lives on the row, one tap in, exactly as
          // it does in the rings. Same predicate, same copy, same default.
          <section data-testid="feed-staged" className="mt-4">
            <button
              type="button"
              data-testid="feed-show-done"
              onClick={() => setShowDone((s) => !s)}
              aria-expanded={showDone}
              className="text-[11px] font-semibold uppercase tracking-wider text-honeydew-600 underline underline-offset-2"
            >
              {showDone ? 'Hide done' : `Show done (${doneRows.length})`}
            </button>
            {showDone && (
              <ul data-testid="feed-done" className="mt-2 flex flex-col gap-2">
                {doneRows.map((it) => (
                  <FeedRow
                    key={it.id}
                    item={it}
                    expanded={expanded.has(it.id)}
                    onToggleEvidence={() => toggleExpanded(it.id)}
                    completion={completion}
                    accept={slotAccept}
                  />
                ))}
              </ul>
            )}
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
