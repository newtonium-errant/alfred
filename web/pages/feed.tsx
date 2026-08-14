import { useCallback, useEffect, useState } from 'react';
import Head from 'next/head';
import Link from 'next/link';
import { useRouter } from 'next/router';
import { Layout } from '../components/Layout';
import { FeedRow } from '../components/feed/FeedRow';
import { TimelineView } from '../components/feed/TimelineView';
import { FEED_VIEWS, type FeedView, feedViewFromQuery, isFeedView } from '../lib/algernon/feedView';
import { SENSOR_SURFACE } from '../lib/algernon/sensorSurface';
import { useFeedBoard } from '../components/feed/useFeedBoard';
import { authApi } from '../lib/algernon/authClient';
import { useRingCompletion } from '../components/feed/useRingCompletion';
import { useResumeRefetch } from '../lib/algernon/useResumeRefetch';
import { useSlotAccept } from '../components/feed/useSlotAccept';
import { useSnooze } from '../components/feed/useSnooze';
import { feedApi, type FeedItem } from '../lib/algernon/feed';
import { contestableItem, isDeckDealt } from '../lib/algernon/feedConstants';
import { readDeckSnoozed } from '../lib/algernon/deckSnooze';
import { ApiError } from '../lib/algernon/http';
import { useSession } from '../lib/algernon/useSession';
import { subtle, title as titleClass } from '../lib/typography';

const INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

// Section headings keep the shared type scale (`title`) but not its warm colour:
// honeydew-700 is a dark green that all but disappears on the console's ground.
// One constant so the two headings cannot drift apart again.
const SENSOR_HEADING = { color: 'var(--sensor-ink)' } as const;

// The Awareness feed — the platform's SENSOR LOG (canonical-surface ruling:
// brief = viewscreen, deck = tactical console, feed = sensor log). It reports
// what the instance is tracking; it does not ask for judgment, which is the
// deck's job (D1).
//
// TWO VIEWS OF ONE FEED. The list is the shipped worklist form. The timeline is
// the Waterline element the operator graduated: the same items laid out against
// time, so a fog window occupies its four hours instead of becoming one more row
// sorted by urgency. Neither is a different surface and neither has verbs the
// other lacks — both render the same `FeedRow` from the same hooks.
//
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
  // The DECK's own hide-list, re-read on every feed load. Starts empty so the
  // server render and the first client render agree; a hide-list that cannot be
  // read hides nothing, which is the safe direction (see deckSnooze.ts).
  const [deckHidden, setDeckHidden] = useState<ReadonlySet<string>>(() => new Set());
  // The chosen view. Held in state AND mirrored to the URL rather than read
  // straight off the router: the state is what makes the toggle respond on the
  // spot, the URL is what makes a view linkable and reload-stable. Seeded from
  // the query below once the router has hydrated it.
  const [view, setView] = useState<FeedView>('list');

  useEffect(() => {
    if ((!sessionLoading && !user) || unauthenticated) {
      router.replace(`/login?next=${encodeURIComponent('/feed')}`);
    }
  }, [sessionLoading, user, unauthenticated, router]);

  // Hoisted out of the mount effect so the RESUME path can call it too (#62).
  // A load that only exists inside useEffect is a load that only ever runs at
  // mount — and an iOS PWA resumes without mounting.
  const loadFeed = useCallback(async () => {
    try {
      const res = await feedApi.list({ state: 'open' });
      setItems(res.items);
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        setUnauthenticated(true);
        return;
      }
      setError('Could not load the feed. Try refreshing.');
    }
  }, []);

  useEffect(() => {
    if (!authed) return;
    let cancelled = false;
    void (async () => {
      const res = await feedApi.list({ state: 'open' }).catch((e: unknown) => e);
      if (cancelled) return;
      if (res instanceof ApiError && res.status === 401) {
        setUnauthenticated(true);
        return;
      }
      if (res instanceof Error) {
        setError('Could not load the feed. Try refreshing.');
        return;
      }
      setItems((res as { items: FeedItem[] }).items);
    })();
    return () => {
      cancelled = true;
    };
  }, [authed]);

  // Re-read the DECK's hide-list whenever the feed re-reads, keyed on `items`
  // rather than hung off a loader. THIS PAGE HAS TWO LOAD PATHS — the mount
  // effect above and the hoisted `loadFeed` the resume refetch calls — and
  // putting the sync in one of them is precisely the standing trap where a
  // feature is live on the path the tests drive and dead on the other. Every
  // load lands in `items` by definition, so a third loader inherits this for
  // free instead of having to remember it.
  //
  // The operator's usual path is feed → deck → snooze → back, so the resume
  // refetch is exactly when this page needs to learn what the deck set aside
  // while it wasn't looking.
  useEffect(() => {
    setDeckHidden(readDeckSnoozed());
  }, [items]);

  // Seed from `?view=` when the router hydrates it. Deliberately one-directional
  // and value-guarded: it only ever adopts a RECOGNISED view, so a mistyped URL
  // leaves the operator on the default rather than on nothing, and choosing a
  // view (which writes the same value back) can't cycle.
  const queryView = router.query.view;
  useEffect(() => {
    if (isFeedView(Array.isArray(queryView) ? queryView[0] : queryView)) {
      setView(feedViewFromQuery(queryView));
    }
  }, [queryView]);

  const chooseView = useCallback(
    (next: FeedView) => {
      setView(next);
      // Shallow + replace: switching view is not a navigation and must not
      // stack history entries the back button then has to walk through.
      router.replace({ pathname: '/feed', query: next === 'list' ? {} : { view: next } }, undefined, {
        shallow: true,
      });
    },
    [router],
  );

  const onAuthExpired = useCallback(() => setUnauthenticated(true), []);
  const board = useFeedBoard({ items: items ?? [], onAuthExpired });
  // ONE completion implementation, shared with the rings panel (never a second).
  const completion = useRingCompletion({ onAuthExpired });

  // #62 defect (3): an iOS PWA resumes with its heap intact — same tree, same
  // rows, no mount and no fetch. Without this a deck reopened in the morning
  // renders last night's DOM, which is exactly what showed a completed item as
  // pending twelve hours after it committed.
  useResumeRefetch(() => { if (authed) void loadFeed(); });

  // #62 defect (2): retire overrides the server has now answered. An override
  // records what happened to a tap; once a render carries the server's own
  // answer, keeping it means preferring a stale opinion over a fact.
  useEffect(() => {
    if (items) completion.reconcile(items);
    // `completion` is intentionally not a dep: reconcile is stable and
    // including the hook result would re-run this on every override change,
    // which is the loop it exists to break.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);
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
  //
  // …AND MINUS WHAT THE DECK WILL REFUSE TO DEAL. `isDeckDealt` answers "is this
  // kind dealable", which is not the same question as "will the deck actually
  // deal it today". A snooze lives in one of two places and this banner has to
  // respect both: the deck's own sessionStorage hide-list (`deckHidden` — it
  // filters its load through exactly this set) and this page's server-confirmed
  // `useSnooze`. Counting either as "waiting" sends the operator to a deck that
  // has already set it aside — the promise-a-trip-to-a-wall bug his 2026-08-12
  // screenshots caught ("2 decisions waiting" → "DECK CLEAR — 2 snoozed").
  const setAside = (it: FeedItem) => snooze.snoozed(it.id) || deckHidden.has(it.id);
  const deckable = activeNeedsYou.filter((it) => isDeckDealt(it) && !setAside(it));
  // What the deck is holding rather than dealing — counted so the zero case can
  // explain itself instead of going quiet.
  const setAsideDeckable = activeNeedsYou.filter((it) => isDeckDealt(it) && setAside(it));
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

  // ONE row renderer, shared by the timeline's detail panel and both of its
  // registers. It is the mechanism behind "neither view has verbs the other
  // lacks": a row reached through the band is the SAME component with the SAME
  // hooks it would have in the list, so the two cannot drift apart.
  const renderRow = useCallback(
    (it: FeedItem) => {
      const common = {
        item: it,
        expanded: expanded.has(it.id),
        onToggleEvidence: () => toggleExpanded(it.id),
      };
      if (board.fyi.some((f) => f.id === it.id)) {
        return (
          <FeedRow
            key={it.id}
            {...common}
            onAck={() => board.ack(it.id)}
            onContest={contestableItem(it) ? (section?: string) => board.contest(it.id, section) : undefined}
          />
        );
      }
      if (it.kind === 'slot_suggestion') {
        return <FeedRow key={it.id} {...common} completion={completion} accept={slotAccept} snooze={snooze} />;
      }
      // A deck-able decision on the band renders at AWARENESS depth only —
      // glance and expand, no verbs. Judgment is the deck's (D1), and the deck
      // link above is the route to it.
      return <FeedRow key={it.id} {...common} />;
    },
    [board, completion, expanded, slotAccept, snooze, toggleExpanded],
  );

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
        {/* The surface prop belongs on BOTH branches. Without it here the feed
            renders warm chrome for the length of the session probe and then
            snaps to the console hull — a visible flash on every cold open, and
            the pre-auth gate is the FIRST thing the operator sees. The authed
            branch below had it from the start, which is exactly why the gap
            survived: the surface was correct everywhere anyone looked. */}
        <Layout showNav={false} surface={SENSOR_SURFACE}>
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
      {/* Wider than the default reading column: the workstation posture has a
          time axis to lay out, and the tricorder posture is capped by the
          viewport anyway. */}
      <Layout onSignOut={() => void handleSignOut()} maxWidthClassName="max-w-4xl" surface={SENSOR_SURFACE}>
        {/* The console. Bleeds to the edges on a phone (tricorder, held close)
            and insets into a panel on a tablet (workstation, at arm's length).
            `data-surface` is also the scope that lets styles/sensorLog.css skin
            the SHARED rows here without touching the brief's copies of them. */}
        <div
          data-testid="feed-console"
          className="sensor-console -mx-5 -my-4 min-h-[80vh] px-4 py-5 sm:mx-0 sm:rounded-xl sm:px-6"
        >
          {/* LCARS: elbow into a pill-terminated bar. */}
          <div className="flex items-stretch gap-1">
            <div className="sensor-elbow w-10 shrink-0 sm:w-16" aria-hidden />
            <div className="sensor-bar flex flex-1 flex-wrap items-center gap-x-3 gap-y-1 px-4 py-2">
              <h1 className="text-base font-extrabold uppercase tracking-[0.26em]" style={{ color: 'var(--sensor-affirm)' }}>
                Feed
              </h1>
              <span className="sensor-label">Sensor log · {INSTANCE_NAME}</span>
            </div>
          </div>
          <p className="mt-2 text-sm" style={{ color: 'var(--sensor-ink-dim)' }}>
            What {INSTANCE_NAME} is tracking — decisions up top, glance items below.
          </p>

          <div
            role="group"
            aria-label="Feed view"
            data-testid="feed-view-switch"
            className="mt-3 inline-flex gap-0.5 rounded-full p-0.5"
            style={{ backgroundColor: 'var(--sensor-hull)' }}
          >
            {FEED_VIEWS.map((v) => (
              <button
                key={v.id}
                type="button"
                data-testid={`feed-view-${v.id}`}
                aria-pressed={view === v.id}
                title={v.hint}
                onClick={() => chooseView(v.id)}
                className="sensor-label rounded-full px-4 py-1.5"
                style={
                  view === v.id
                    ? { backgroundColor: 'var(--sensor-affirm-wash)', color: 'var(--sensor-affirm)' }
                    : { color: 'var(--sensor-ink-ghost)' }
                }
              >
                {v.label}
              </button>
            ))}
          </div>

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

        {/* Deck-able decisions (a wired verb) route to the swipe deck; the count
            matches what the deck actually deals (see deck.tsx). HOISTED out of
            the worklist section so it shows in BOTH views — it is the surface's
            route to judgment, not a member of any one list. Gating on
            `deckable` alone is equivalent to the old `activeNeedsYou &&
            deckable` pair, since deckable is a subset of activeNeedsYou. */}
        {loaded && deckable.length > 0 && (
          <Link
            href="/deck"
            data-testid="feed-deck-link"
            className="mt-4 flex items-center justify-between rounded-lg px-4 py-3 text-sm font-semibold"
            style={{
              backgroundColor: 'var(--sensor-affirm-wash)',
              color: 'var(--sensor-affirm)',
              border: '1px solid var(--sensor-affirm-deep)',
            }}
          >
            <span>
              {deckable.length} decision{deckable.length > 1 ? 's' : ''} waiting
            </span>
            <span aria-hidden>Open the deck →</span>
          </Link>
        )}

        {/* Intentionally-left-blank, and the other half of the honesty fix: when
            the deck has nothing to deal BECAUSE things are set aside, say which
            rather than rendering nothing. Silence here is what let the old
            banner's disappearance read as "handled" when it meant "deferred".

            VOICING PASS (2026-08-12): the count is a UNION OF TWO STORES, and
            NO CLAUSE HERE MAY BE FALSE FOR EITHER HALF. `setAside` is
            `snooze.snoozed(id) || deckHidden.has(id)`:

              - deckHidden — the deck's SESSION hide-list. Returns at the next
                sync, on its own, without the operator.
              - snooze.snoozed — the SERVER rungs (1d / 3d / 7d / until-I-say).
                Returns when the rung expires, and the indefinite rung never
                expires at all — as this same file says at the snoozed-rows
                comment above: "the indefinite rung has no next sync to wait for".

            The two mechanisms differ in LIFETIME and in RECOVERY, so the
            truth-table over the union is narrow (verified, review round 2):

              "back at the next sync"      false for B — the bug this replaced
              "until you bring them back"  false for A — it returns unaided
              "you can bring them back"    false for A — no un-hide control here
              "they'll come back"          TRUE for every member — but the
                                           indefinite rung returns by
                                           breakthrough or unsnooze, NEVER by
                                           timer ("what earns a row its way
                                           back is urgency, not the calendar"
                                           — tier/snooze.py, on that very rung).
                                           Which is precisely why the line may
                                           assert return and must not say when.
              "set aside" / nothing lost   TRUE for every member

            THE RULE THAT FALLS OUT: ASSERT RETURN, NEVER TIMING AND NEVER
            TRIGGER. Return is the only universally true claim; when it comes
            back and what brings it back both vary by store. So the line asserts
            exactly that and stops.

            A timing claim is UNWRITEABLE while the count is a union — that is a
            builder change (split the count), boarded separately, not a copy
            problem to solve harder. This is the `carryoverReason` fallback
            reasoning at a second site: a line covering a heterogeneous
            population may claim only what is common to all of it.

            "Snoozed" went the same way, and for the same reason rather than for
            warmth: it is the word on the backed control ("Snooze — choose how
            long"), but the session half's control says "Set aside for now" and
            its own constant carries the warning that "the copy on that path
            must not promise otherwise". "Set aside" is the honest union term —
            it is what this testid has always been called, and what the comment
            above has always called them.

            Splitting the counts per store was the alternative; rejected because
            backed-vs-session is an implementation distinction the operator does
            not model, and it buys precision he cannot act on at the cost of a
            longer line on a phone. */}
        {loaded && deckable.length === 0 && setAsideDeckable.length > 0 && (
          <p data-testid="feed-deck-set-aside" className="mt-4 text-sm text-honeydew-600">
            {setAsideDeckable.length} set aside — they&rsquo;ll come back. Nothing else is
            waiting in the deck.
          </p>
        )}

        {loaded && view === 'timeline' && (
          <TimelineView items={items ?? []} renderDetail={renderRow} />
        )}

        {loaded && view === 'list' && pendingRows.length > 0 && (
          <section data-testid="feed-needs-you" className="mt-6">
            <h2 className={titleClass} style={SENSOR_HEADING}>
              Needs you
            </h2>
            {/* Slot rows (non-deck-able needs-you) carry the SAME live per-lane
                completion control as the rings panel — completable lanes get a
                real ✓; task/unknown lanes get an honest note (no dead control). */}
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
          </section>
        )}

        {loaded && view === 'list' && snoozedRows.length > 0 && (
          <section data-testid="feed-snoozed" className="mt-4">
            <button
              type="button"
              data-testid="feed-show-snoozed"
              onClick={() => setShowSnoozed((s) => !s)}
              aria-expanded={showSnoozed}
              className="text-[11px] font-semibold uppercase tracking-wider underline underline-offset-2"
              style={{ color: 'var(--sensor-ink-dim)' }}
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

        {loaded && view === 'list' && doneRows.length > 0 && (
          // The staged section (#26). Deliberately a SIBLING of "Needs you"
          // rather than living inside it: the enclosing section is gated on
          // its own pending rows, so completing the last slot unmounts it — and
          // staging a done row under a "Needs you" heading would
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
              className="text-[11px] font-semibold uppercase tracking-wider underline underline-offset-2"
              style={{ color: 'var(--sensor-ink-dim)' }}
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

        {loaded && view === 'list' && board.fyi.length > 0 && (
          <section data-testid="feed-fyi" className="mt-6">
            <h2 className={titleClass} style={SENSOR_HEADING}>
              For your awareness
            </h2>
            <ul className="mt-2 flex flex-col gap-2">
              {board.fyi.map((it) => (
                <FeedRow
                  key={it.id}
                  item={it}
                  expanded={expanded.has(it.id)}
                  onToggleEvidence={() => toggleExpanded(it.id)}
                  onAck={() => board.ack(it.id)}
                  onContest={contestableItem(it) ? (section?: string) => board.contest(it.id, section) : undefined}
                />
              ))}
            </ul>
          </section>
        )}

        {board.toast && (
          <div data-testid="feed-toast" role="status" className="fixed inset-x-0 bottom-20 z-50 mx-auto flex w-fit items-center gap-3 rounded-lg px-3.5 py-2.5 text-sm shadow-card"
            style={{ backgroundColor: 'var(--sensor-raise)', color: 'var(--sensor-ink)', border: '1px solid var(--sensor-edge-bright)' }}>
            <span>{board.toast.message}</span>
            <button type="button" onClick={board.dismissToast} className="font-bold uppercase tracking-wider underline">
              Dismiss
            </button>
          </div>
        )}
        </div>
      </Layout>
    </>
  );
}
