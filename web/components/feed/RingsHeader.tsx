import { useCallback, useEffect, useMemo, useState } from 'react';
import { feedApi, type FeedItem } from '../../lib/algernon/feed';
import { ApiError } from '../../lib/algernon/http';
import { holdChoicesForVerb } from '../../lib/algernon/feedConstants';
import { evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';
import {
  arcPath,
  RING_CENTER,
  RING_RADIUS,
  RING_STROKE_CLASS,
  RING_STROKE_WIDTH,
  RING_VIEWBOX,
  ringSegments,
  segmentStageClass,
} from '../../lib/algernon/ringGeometry';
import {
  COMPLETION_UNAVAILABLE_HINT,
  RING_ACTION_DONE,
  effectiveStageOf,
  ringItemCompletable,
  ringItemUndoable,
  tierRingBuckets,
  type RingBucket,
  type RingItemStage,
} from '../../lib/algernon/rings';
import { HoldPressButton } from './HoldPressButton';
import { HoldSelector } from './HoldSelector';
import { useRingCompletion, type UseRingCompletionResult } from './useRingCompletion';
import { useSlotAccept } from './useSlotAccept';
import { WHEN_SELECTOR_NOTE, WHEN_SELECTOR_TITLE } from './whenSelector';

// The segmented "balanced day" rings header. Three tier rings (T1/T2/T3) from
// `slot_suggestion` items (see lib/algernon/rings.ts for why tier). Tap a ring to
// expand its bucket; tap a row for its evidence.
//
// Phase C2 — three STAGES per the scope-first matrix, one seam (`effectiveStage`):
//   SUGGESTED (candidate, not on the plan) → an [Accept] control (useSlotAccept),
//     rendered as a MUTED ring segment, EXCLUDED from the done/total count.
//   PLANNED (committed, open) → the C1b ✓ (task/routine/T3 wired; unknown disabled),
//     an AMBER segment, counted in the denominator.
//   DONE → a green marker + single-step undo where board-undoable, a GREEN segment,
//     the numerator.
// Optimistic overlays: a completed item flips green (useRingCompletion), an accepted
// candidate flips candidate→PLANNED (useSlotAccept, render-present gated) — both honest
// degrades that reconcile at the next fetch.

export interface RingsHeaderProps {
  /** 401 handler (bubbles a session expiry to the host page, like the deck/feed). */
  onAuthExpired?: () => void;
  /**
   * Composition / test seam: when provided, these items are rendered directly
   * and the internal fetch is skipped (the composer can share one feed load).
   * The set may include today's DONE (state=acted) slot items — the rings show
   * suggested + planned + done all day (`tierRingBuckets` date-scopes + dedups).
   */
  items?: FeedItem[];
  /** Test seam: "now" for the today date-scope (defaults to the real clock). */
  now?: Date;
  /**
   * Composition seam (mirrors `items`): when provided, this completion hook drives
   * the rings instead of the internal one, so a HOST page's own surfaces (the
   * composer's needs-you count) and the rings read ONE optimistic state in the same
   * render. Omitted → the rings own their state and stand alone.
   */
  completion?: UseRingCompletionResult;
}

export function RingsHeader({
  onAuthExpired,
  items: itemsProp,
  now: nowProp,
  completion: completionProp,
}: RingsHeaderProps) {
  const controlled = itemsProp !== undefined;
  const [fetched, setFetched] = useState<FeedItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [openItemId, setOpenItemId] = useState<string | null>(null);
  const [showDone, setShowDone] = useState(false);
  const [showSnoozed, setShowSnoozed] = useState(false);
  // Which row has its ✓-hold when-selector open (backdated completion).
  // Per-panel, one at a time — the sort picker's reasoning.
  const [whenForId, setWhenForId] = useState<string | null>(null);
  // Hooks can't be conditional, so the internal instance is always constructed and
  // simply unused when the host supplies one. Still ONE implementation either way.
  const ownCompletion = useRingCompletion({ onAuthExpired });
  const completion = completionProp ?? ownCompletion;
  const accept = useSlotAccept({ onAuthExpired });
  const now = useMemo(() => nowProp ?? new Date(), [nowProp]);

  useEffect(() => {
    if (controlled) return;
    let cancelled = false;
    void (async () => {
      try {
        // No state filter — the rings need today's DONE (acted) slots too.
        const res = await feedApi.list({ kind: 'slot_suggestion' });
        if (!cancelled) setFetched(res.items);
      } catch (e) {
        if (cancelled) return;
        if (e instanceof ApiError && e.status === 401) {
          onAuthExpired?.();
          return;
        }
        setError('Could not load your rings.');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [controlled, onAuthExpired]);

  const items = controlled ? (itemsProp as FeedItem[]) : fetched;
  const loading = !controlled && items == null && !error;
  const buckets = useMemo(() => tierRingBuckets(items ?? [], now), [items, now]);
  const totalItems = useMemo(() => buckets.reduce((n, b) => n + b.items.length, 0), [buckets]);
  const activeBucket = useMemo(() => buckets.find((b) => b.key === openKey) ?? null, [buckets, openKey]);

  // The single stage seam for rendering: a completion optimistic-done wins, then an
  // accept optimistic-committed (candidate→planned) overlays, else the base stage.
  // An optimistic UNDO (completion says not-done while the raw stage is still done —
  // the item hasn't reconciled yet) returns the item to PLANNED, not its raw done.
  // The C2 stage overlay is the shared rings.ts seam (#16 item 8) — this is just the
  // rings-panel binding of its hooks; the logic lives in one place now.
  const effectiveStage = useCallback(
    (it: FeedItem): RingItemStage => effectiveStageOf(it, completion, accept),
    [completion, accept],
  );

  const toggleRing = useCallback((key: string) => {
    setOpenItemId(null);
    setShowDone(false);
    // Reset alongside `showDone` — both drills are per-panel, and leaving one
    // latched open would carry a previous tier's disclosure into the next ring.
    setShowSnoozed(false);
    setOpenKey((cur) => (cur === key ? null : key));
  }, []);
  const toggleItem = useCallback((id: string) => {
    setOpenItemId((cur) => (cur === id ? null : id));
  }, []);

  // One worklist row — used for the remaining (suggested + planned) list and the
  // revealed done list. Affordance is per STAGE: Accept (suggested) / ✓ (planned) /
  // done+undo (done).
  const renderItem = (it: FeedItem) => {
    const stage = effectiveStage(it);
    const done = stage === 'done';
    const suggested = stage === 'suggested';
    // DELAYED — see rings.ts. No strikethrough (gated on `done`), no ✓ control.
    const snoozed = stage === 'snoozed';
    const completable = ringItemCompletable(it);
    const undoable = ringItemUndoable(it);
    const compBusy = completion.busy(it.id);
    const acceptBusy = accept.busy(it.id);
    const itemError = completion.errorFor(it.id) ?? accept.errorFor(it.id);
    const rows = evidenceRows(it.evidence);
    const expanded = openItemId === it.id;
    // The ✓-hold's when-family, wire-derived (null = plain ✓). Only where a
    // live ✓ renders.
    const whenChoices =
      completable && !done && !snoozed && !suggested
        ? holdChoicesForVerb(it, RING_ACTION_DONE)
        : null;
    const dotClass = done
      ? 'bg-status-done-fg'
      : snoozed
        ? 'bg-caution'
        : suggested
          ? 'bg-console-ink-faint'
          : 'bg-status-progress-fg';
    return (
      <li key={it.id} data-testid="ring-panel-item" data-done={done} data-stage={stage} className="relative border-t border-dashed border-console-edge pt-2 first:border-0 first:pt-0">
        <div className="flex items-start justify-between gap-2">
          <button
            type="button"
            data-testid="ring-item-row"
            onClick={() => toggleItem(it.id)}
            aria-expanded={expanded}
            className="flex min-w-0 flex-1 items-start gap-2 text-left"
          >
            <span aria-hidden className={`mt-1 h-2 w-2 shrink-0 rounded-full ${dotClass}`} />
            <span className={`min-w-0 break-words text-sm font-semibold text-console-ink ${done ? 'line-through opacity-70' : ''}`}>
              {it.title || it.id}
            </span>
          </button>

          {done ? (
            // Completed — a green marker, plus a single-step undo where the lane is
            // board-UNDOABLE (routine + T3; a task is completable but not board-
            // undoable, and a vault-done item has no board writer).
            <div className="flex shrink-0 items-center gap-1.5">
              <span data-testid="ring-item-done" className="text-[10px] font-bold uppercase tracking-wider text-status-done-fg">
                ✓ Done
              </span>
              {undoable && (
                <button
                  type="button"
                  data-testid="ring-undo"
                  disabled={compBusy}
                  onClick={() => completion.undo(it)}
                  className="rounded-lg border border-console-edge px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-console-ink-dim disabled:opacity-50"
                >
                  {compBusy ? '…' : 'Undo'}
                </button>
              )}
            </div>
          ) : snoozed ? (
            // SNOOZED — pushed to a later day. A statement, not a control: the
            // rings panel holds no snooze hook, and Unsnooze lives on the feed's
            // staged list. Rendering a live-looking ✓ here would re-assert the
            // completion this whole change exists to stop asserting.
            <span
              data-testid="ring-item-snoozed"
              className="shrink-0 text-[10px] font-bold uppercase tracking-wider text-caution"
            >
              Snoozed
            </span>
          ) : suggested ? (
            // SUGGESTED — an auto-surfaced candidate NOT yet on the plan. The one
            // affordance is Accept (commit it); NO ✓ (candidates aren't completable
            // until committed). Optimistic candidate→planned on a render-bearing 200.
            <button
              type="button"
              data-testid="ring-accept"
              disabled={acceptBusy}
              onClick={() => accept.accept(it)}
              className="shrink-0 rounded-lg border border-console-edge-bright bg-console-raise px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-console-ink disabled:opacity-50"
            >
              {acceptBusy ? '…' : 'Accept'}
            </button>
          ) : completable ? (
            // PLANNED + a wired writer — a LIVE ✓. Quick tap = done TODAY,
            // unchanged; HOLDING opens the when-selector when the wire served
            // a when-family (backdated completion).
            <HoldPressButton
              data-testid="ring-complete"
              disabled={compBusy}
              onTap={() => completion.complete(it)}
              onHold={whenChoices ? () => setWhenForId(it.id) : null}
              className="shrink-0 rounded-lg border border-console-edge-bright px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-console-ink disabled:opacity-50"
            >
              {compBusy ? '…' : '✓ Done'}
            </HoldPressButton>
          ) : (
            // PLANNED, non-completable lane (unknown / unstamped origin) — honestly
            // DISABLED. The `disabled` attr AND `opacity-50` are pinned together so
            // un-disabling forces a conscious restyle.
            <button
              type="button"
              data-testid="ring-complete"
              disabled
              aria-disabled="true"
              title={COMPLETION_UNAVAILABLE_HINT}
              className="shrink-0 cursor-default rounded-lg border border-console-edge px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-console-ink-faint opacity-50"
            >
              ✓ Done
            </button>
          )}
        </div>

        {itemError && (
          <p data-testid="ring-item-error" role="alert" className="mt-1 pl-4 text-[11px] text-danger">
            {itemError}
          </p>
        )}

        {expanded && rows.length > 0 && (
          <dl data-testid="ring-item-evidence" className="mt-1.5 space-y-1 pl-4 text-xs text-console-ink-dim">
            {rows.map((r) => (
              <div key={r.key} className="flex gap-2">
                <dt className="shrink-0 font-semibold text-console-ink">{evidenceLabel(r.key)}:</dt>
                <dd className="min-w-0 break-words">{r.value}</dd>
              </div>
            ))}
          </dl>
        )}

        {/* The when-selector — the ✓-hold's alternatives (backdated
            completion). Choosing IS the completion, one interaction. */}
        {whenForId === it.id && whenChoices && (
          <HoldSelector
            title={WHEN_SELECTOR_TITLE}
            note={WHEN_SELECTOR_NOTE}
            choices={whenChoices}
            onPick={(verb) => {
              setWhenForId(null);
              completion.completeWith(it, verb);
            }}
            onCancel={() => setWhenForId(null)}
          />
        )}
      </li>
    );
  };

  if (error) {
    return (
      <section aria-label="Today's tier rings" data-testid="rings-header">
        <div role="alert" data-testid="rings-error" className="ui-alert rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger">
          {error}
        </div>
      </section>
    );
  }

  return (
    <section aria-label="Today's tier rings" data-testid="rings-header">
      <div className="flex items-center gap-3 rounded-xl border border-console-edge bg-console-panel px-3 py-2 shadow-soft">
        {loading ? (
          // Intentionally-left-blank: an explicit loading signal, not a blank strip.
          <p data-testid="rings-loading" className="text-sm text-console-ink-dim">
            Loading your rings…
          </p>
        ) : (
          buckets.map((b) => (
            <Ring key={b.key} bucket={b} active={openKey === b.key} stageOf={effectiveStage} onTap={() => toggleRing(b.key)} />
          ))
        )}
      </div>

      {!loading && totalItems === 0 && (
        // Intentionally-left-blank: three empty rings could read as broken — say so.
        <p data-testid="rings-empty" className="mt-1.5 text-xs text-console-ink-dim">
          No tier suggestions yet — your rings fill as the day&rsquo;s tiers surface.
        </p>
      )}

      {activeBucket &&
        (() => {
          const bucketItems = activeBucket.items;
          const doneItems = bucketItems.filter((it) => effectiveStage(it) === 'done');
          const snoozedItems = bucketItems.filter((it) => effectiveStage(it) === 'snoozed');
          // THE WORKLIST — what is still live in this tier. Named for what it holds
          // rather than as "notDone": a snoozed item is also not done, and calling
          // this set by a negation is how it would quietly acquire one. Snoozed rows
          // have their own drill below; they are not remaining work.
          const worklist = bucketItems.filter((it) => {
            const s = effectiveStage(it);
            return s !== 'done' && s !== 'snoozed';
          });
          // COMMITTED = planned + done (the count denominator); SUGGESTED excluded,
          // and SNOOZED excluded — it is in neither half of the ratio (rings.ts).
          const committedCount = bucketItems.filter((it) => {
            const s = effectiveStage(it);
            return s !== 'suggested' && s !== 'snoozed';
          }).length;
          const suggestedCount = bucketItems.length - committedCount - snoozedItems.length;
          // The header when nothing is committed. Built from PARTS so a tier holding
          // both candidates and snoozed rows reports both — the old single-branch
          // fallback called every uncommitted item "suggested", which would have
          // relabelled three snoozed duties as offers the operator had not answered.
          const uncommittedParts = [
            suggestedCount > 0 ? `${suggestedCount} suggested` : null,
            snoozedItems.length > 0 ? `${snoozedItems.length} snoozed` : null,
          ].filter((p): p is string => p !== null);
          // EMPTY BUCKET. `uncommittedParts` is empty exactly when the tier holds
          // nothing at all: a non-empty uncommitted tier is made of suggested and
          // snoozed rows, and both counts feed the list above, so the only way to
          // reach zero parts is zero items. Left bare, the h3 rendered "T2 · " —
          // a separator introducing nothing, which is the ILB principle pointing
          // backwards: the panel went quiet at precisely the moment it should say
          // what it is. Base said "0 suggested" here, and this restores that word
          // for word rather than inventing new empty-state copy, so the lane's
          // visible delta stays the snooze behaviour it exists for.
          const uncommittedLabel =
            uncommittedParts.length > 0 ? uncommittedParts.join(' · ') : '0 suggested';
          return (
            <div data-testid={`ring-panel-${activeBucket.key}`} className="mt-2 rounded-xl border border-console-edge bg-console-panel p-3 shadow-soft">
              {/* Honest header: the committed ratio ("T2 · 1/2 done"), or — when nothing
                  is committed yet, only candidates — the suggested count. */}
              <h3 className="text-xs font-bold uppercase tracking-wider text-console-ink">
                {activeBucket.label} ·{' '}
                {committedCount > 0
                  ? `${doneItems.length}/${committedCount} done`
                  : uncommittedLabel}
              </h3>

              {bucketItems.length === 0 ? (
                // Genuinely nothing in this tier (the red-empty ring).
                <p data-testid="ring-panel-empty" className="mt-2 text-xs text-console-ink-dim">
                  Empty — nothing in this tier yet. Suggestions arrive with each sync.
                </p>
              ) : (
                <>
                  {worklist.length > 0 ? (
                    // The WORKLIST — remaining (suggested + planned) items, by default.
                    <ul data-testid="ring-panel-worklist" className="mt-2 flex flex-col gap-2">
                      {worklist.map(renderItem)}
                    </ul>
                  ) : committedCount > 0 ? (
                    // A WIN — everything committed is done (no suggested, no planned left).
                    // Gated on `committedCount`: with the tier emptied by SNOOZING
                    // instead, this line read "All 0 done ✓" — a congratulation for a
                    // completion that never happened, which is the tier-level twin of
                    // the board's "All done here today."
                    <p data-testid="ring-panel-all-done" className="mt-2 text-xs font-semibold text-status-done-fg">
                      All {committedCount} done ✓
                    </p>
                  ) : (
                    // Nothing committed and nothing live — the tier holds only snoozed
                    // rows. Say that, rather than claiming a win or going silent.
                    <p data-testid="ring-panel-all-snoozed" className="mt-2 text-xs font-semibold text-caution">
                      Nothing left in this tier — {snoozedItems.length} snoozed for later.
                    </p>
                  )}

                  {doneItems.length > 0 && (
                    // Completed items live behind a drill-down (Undo lives there).
                    <div className="mt-2">
                      <button
                        type="button"
                        data-testid="ring-show-done"
                        onClick={() => setShowDone((s) => !s)}
                        aria-expanded={showDone}
                        className="text-[11px] font-semibold uppercase tracking-wider text-console-ink-dim underline underline-offset-2"
                      >
                        {showDone ? 'Hide done' : `Show done (${doneItems.length})`}
                      </button>
                      {showDone && (
                        <ul data-testid="ring-panel-done" className="mt-2 flex flex-col gap-2">
                          {doneItems.map(renderItem)}
                        </ul>
                      )}
                    </div>
                  )}

                  {snoozedItems.length > 0 && (
                    // THE SNOOZED DRILL — beside the done drill and never inside
                    // it, the same ruling this panel's twin (SlotBoard) makes.
                    //
                    // Without it, `showSnoozed` was dead state and snoozed rows
                    // rendered NOWHERE: filtered out of the worklist, absent from
                    // the done drill, present only as a number in the header. The
                    // operator could see that three things had moved and not
                    // WHICH three — so an accidental snooze would be unfindable,
                    // and a delay he wanted to reverse had no row to reverse it
                    // from. Hiding them entirely is the same erasure as calling
                    // them done, with better manners.
                    <div className="mt-2">
                      <button
                        type="button"
                        data-testid="ring-show-snoozed"
                        onClick={() => setShowSnoozed((s) => !s)}
                        aria-expanded={showSnoozed}
                        className="text-[11px] font-semibold uppercase tracking-wider text-caution underline underline-offset-2"
                      >
                        {showSnoozed ? 'Hide snoozed' : `Show snoozed (${snoozedItems.length})`}
                      </button>
                      {showSnoozed && (
                        <ul data-testid="ring-panel-snoozed" className="mt-2 flex flex-col gap-2">
                          {snoozedItems.map(renderItem)}
                        </ul>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          );
        })()}
    </section>
  );
}

// One ring: n segments (muted = suggested, amber = planned, green = done) or, for an
// empty bucket, a faint red circle. A tap toggles the bucket panel. `stageOf` is
// threaded from the render seam so a just-accepted/-completed segment recolours
// optimistically.
function Ring({
  bucket,
  active,
  stageOf,
  onTap,
}: {
  bucket: RingBucket;
  active: boolean;
  stageOf: (item: FeedItem) => RingItemStage;
  onTap: () => void;
}) {
  const n = bucket.items.length;
  const segs = ringSegments(n);
  return (
    <button
      type="button"
      data-testid={`ring-${bucket.key}`}
      aria-label={`${bucket.label} ring — ${n} item${n === 1 ? '' : 's'}, tap to view`}
      aria-expanded={active}
      onClick={onTap}
      className={`relative block h-9 w-9 rounded-full ${active ? 'ring-2 ring-console-edge-bright' : ''}`}
    >
      <svg viewBox={`0 0 ${RING_VIEWBOX} ${RING_VIEWBOX}`} width="34" height="34" className="block">
        {n === 0 ? (
          <circle
            cx={RING_CENTER}
            cy={RING_CENTER}
            r={RING_RADIUS}
            fill="none"
            stroke="currentColor"
            strokeWidth={RING_STROKE_WIDTH}
            opacity={0.5}
            data-testid={`ring-empty-${bucket.key}`}
            className={RING_STROKE_CLASS.empty}
          />
        ) : (
          segs.map((s, i) => (
            <path
              key={bucket.items[i].id}
              d={arcPath(RING_CENTER, RING_CENTER, RING_RADIUS, s.a0, s.a1)}
              fill="none"
              stroke="currentColor"
              strokeWidth={RING_STROKE_WIDTH}
              strokeLinecap="round"
              className={segmentStageClass(stageOf(bucket.items[i]))}
            />
          ))
        )}
      </svg>
      <span className="pointer-events-none absolute inset-0 flex items-center justify-center text-[8.5px] font-semibold text-console-ink-dim">
        {bucket.label}
      </span>
    </button>
  );
}
