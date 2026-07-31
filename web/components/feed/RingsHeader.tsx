import { useCallback, useEffect, useMemo, useState } from 'react';
import { feedApi, type FeedItem } from '../../lib/algernon/feed';
import { ApiError } from '../../lib/algernon/http';
import { evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';
import {
  arcPath,
  RING_CENTER,
  RING_RADIUS,
  RING_STROKE_CLASS,
  RING_STROKE_WIDTH,
  RING_VIEWBOX,
  ringSegments,
  segmentStroke,
} from '../../lib/algernon/ringGeometry';
import { COMPLETION_UNAVAILABLE_HINT, ringItemCompletable, ringItemUndoable, tierRingBuckets, type RingBucket } from '../../lib/algernon/rings';
import { useRingCompletion } from './useRingCompletion';

// The segmented "balanced day" rings header. Three tier rings (T1/T2/T3) from
// open `slot_suggestion` items (see lib/algernon/rings.ts for why tier). Tap a
// ring to expand its bucket; tap a row for its evidence. Phase C: the panel's ✓
// completes an item per-lane (task + routine + free-text T3 wired; unknown/
// unstamped-origin stays honestly disabled) — optimistic green on success, single-
// step undo on done rows (task is done-only: no board undo, see rings.ts).

export interface RingsHeaderProps {
  /** 401 handler (bubbles a session expiry to the host page, like the deck/feed). */
  onAuthExpired?: () => void;
  /**
   * Composition / test seam: when provided, these items are rendered directly
   * and the internal fetch is skipped (the composer can share one feed load).
   * The set may include today's DONE (state=acted) slot items — the rings show
   * planned + done all day (`tierRingBuckets` date-scopes the acted ones).
   */
  items?: FeedItem[];
  /** Test seam: "now" for the today date-scope (defaults to the real clock). */
  now?: Date;
}

export function RingsHeader({ onAuthExpired, items: itemsProp, now: nowProp }: RingsHeaderProps) {
  const controlled = itemsProp !== undefined;
  const [fetched, setFetched] = useState<FeedItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [openItemId, setOpenItemId] = useState<string | null>(null);
  const [showDone, setShowDone] = useState(false);
  const completion = useRingCompletion({ onAuthExpired });
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

  const toggleRing = useCallback((key: string) => {
    setOpenItemId(null);
    setShowDone(false);
    setOpenKey((cur) => (cur === key ? null : key));
  }, []);
  const toggleItem = useCallback((id: string) => {
    setOpenItemId((cur) => (cur === id ? null : id));
  }, []);

  // One worklist row — used for both the remaining (not-done) list and the
  // revealed done list. Its ✓ / done+undo / disabled affordance is per-lane.
  const renderItem = (it: FeedItem) => {
    const done = completion.effectiveDone(it);
    const completable = ringItemCompletable(it);
    const undoable = ringItemUndoable(it);
    const busy = completion.busy(it.id);
    const itemError = completion.errorFor(it.id);
    const rows = evidenceRows(it.evidence);
    const expanded = openItemId === it.id;
    return (
      <li key={it.id} data-testid="ring-panel-item" data-done={done} className="border-t border-dashed border-honeydew-200 pt-2 first:border-0 first:pt-0">
        <div className="flex items-start justify-between gap-2">
          <button
            type="button"
            data-testid="ring-item-row"
            onClick={() => toggleItem(it.id)}
            aria-expanded={expanded}
            className="flex min-w-0 flex-1 items-start gap-2 text-left"
          >
            <span
              aria-hidden
              className={`mt-1 h-2 w-2 shrink-0 rounded-full ${done ? 'bg-status-done-fg' : 'bg-status-progress-fg'}`}
            />
            <span className={`min-w-0 break-words text-sm font-semibold text-honeydew-700 ${done ? 'line-through opacity-70' : ''}`}>
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
                  disabled={busy}
                  onClick={() => completion.undo(it)}
                  className="rounded-lg border border-honeydew-300 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-honeydew-600 disabled:opacity-50"
                >
                  {busy ? '…' : 'Undo'}
                </button>
              )}
            </div>
          ) : completable ? (
            // A LIVE control — this lane has a wired writer.
            <button
              type="button"
              data-testid="ring-complete"
              disabled={busy}
              onClick={() => completion.complete(it)}
              className="shrink-0 rounded-lg border border-honeydew-400 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-honeydew-700 disabled:opacity-50"
            >
              {busy ? '…' : '✓ Done'}
            </button>
          ) : (
            // Non-completable lane (unknown / unstamped origin — task is completable
            // now, see rings.ts) — honestly DISABLED. The `disabled` attr AND
            // `opacity-50` are pinned together so un-disabling forces a conscious
            // restyle.
            <button
              type="button"
              data-testid="ring-complete"
              disabled
              aria-disabled="true"
              title={COMPLETION_UNAVAILABLE_HINT}
              className="shrink-0 cursor-default rounded-lg border border-honeydew-200 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-honeydew-400 opacity-50"
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
          <dl data-testid="ring-item-evidence" className="mt-1.5 space-y-1 pl-4 text-xs text-honeydew-600">
            {rows.map((r) => (
              <div key={r.key} className="flex gap-2">
                <dt className="shrink-0 font-semibold text-honeydew-700">{evidenceLabel(r.key)}:</dt>
                <dd className="min-w-0 break-words">{r.value}</dd>
              </div>
            ))}
          </dl>
        )}
      </li>
    );
  };

  if (error) {
    return (
      <section aria-label="Today's tier rings" data-testid="rings-header">
        <div role="alert" data-testid="rings-error" className="rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger">
          {error}
        </div>
      </section>
    );
  }

  return (
    <section aria-label="Today's tier rings" data-testid="rings-header">
      <div className="flex items-center gap-3 rounded-xl border border-honeydew-200 bg-cream px-3 py-2 shadow-soft">
        {loading ? (
          // Intentionally-left-blank: an explicit loading signal, not a blank strip.
          <p data-testid="rings-loading" className="text-sm text-honeydew-600">
            Loading your rings…
          </p>
        ) : (
          buckets.map((b) => (
            <Ring key={b.key} bucket={b} active={openKey === b.key} isDone={completion.effectiveDone} onTap={() => toggleRing(b.key)} />
          ))
        )}
      </div>

      {!loading && totalItems === 0 && (
        // Intentionally-left-blank: three empty rings could read as broken — say so.
        <p data-testid="rings-empty" className="mt-1.5 text-xs text-honeydew-600">
          No tier suggestions yet — your rings fill as the day&rsquo;s tiers surface.
        </p>
      )}

      {activeBucket &&
        (() => {
          const bucketItems = activeBucket.items;
          const doneItems = bucketItems.filter(completion.effectiveDone);
          const notDone = bucketItems.filter((it) => !completion.effectiveDone(it));
          return (
            <div data-testid={`ring-panel-${activeBucket.key}`} className="mt-2 rounded-xl border border-honeydew-200 bg-cream p-3 shadow-soft">
              {/* Honest ratio over the full (planned + done-today) set: "T2 · 1/1 DONE". */}
              <h3 className="text-xs font-bold uppercase tracking-wider text-honeydew-700">
                {activeBucket.label} · {doneItems.length}/{bucketItems.length} done
              </h3>

              {bucketItems.length === 0 ? (
                // Genuinely nothing planned in this tier (the red-empty ring).
                <p data-testid="ring-panel-empty" className="mt-2 text-xs text-honeydew-600">
                  Empty — nothing in this tier yet. Suggestions arrive with each sync.
                </p>
              ) : (
                <>
                  {notDone.length > 0 ? (
                    // The WORKLIST — remaining items only, by default.
                    <ul data-testid="ring-panel-worklist" className="mt-2 flex flex-col gap-2">
                      {notDone.map(renderItem)}
                    </ul>
                  ) : (
                    // A WIN — everything planned is done. Distinct from the red-empty
                    // "nothing planned" language the operator's screenshot showed.
                    <p data-testid="ring-panel-all-done" className="mt-2 text-xs font-semibold text-status-done-fg">
                      All {bucketItems.length} done ✓
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
                        className="text-[11px] font-semibold uppercase tracking-wider text-honeydew-600 underline underline-offset-2"
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
                </>
              )}
            </div>
          );
        })()}
    </section>
  );
}

// One ring: n segments (amber = planned, green = done) or, for an empty bucket,
// a faint red circle. A tap toggles the bucket panel. `isDone` is threaded from
// the completion state so a just-completed segment goes green optimistically.
function Ring({
  bucket,
  active,
  isDone,
  onTap,
}: {
  bucket: RingBucket;
  active: boolean;
  isDone: (item: FeedItem) => boolean;
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
      className={`relative block h-9 w-9 rounded-full ${active ? 'ring-2 ring-honeydew-400' : ''}`}
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
              className={segmentStroke(isDone(bucket.items[i]))}
            />
          ))
        )}
      </svg>
      <span className="pointer-events-none absolute inset-0 flex items-center justify-center text-[8.5px] font-semibold text-honeydew-600">
        {bucket.label}
      </span>
    </button>
  );
}
