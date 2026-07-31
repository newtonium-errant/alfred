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
import { ringItemDone, tierRingBuckets, type RingBucket } from '../../lib/algernon/rings';

// The segmented "balanced day" rings header (B3-4). DISPLAY ONLY in Phase B:
// three tier rings (T1/T2/T3) rendered from open `slot_suggestion` feed items
// (see lib/algernon/rings.ts for why tier, not duty/rhythm/fuel). Tap a ring to
// expand its bucket; tap a row for its evidence. Completion is Phase C — the ✓
// button is a disabled, honest placeholder, with NO mutation path at all.

export interface RingsHeaderProps {
  /** 401 handler (bubbles a session expiry to the host page, like the deck/feed). */
  onAuthExpired?: () => void;
  /**
   * Composition / test seam: when provided, these items are rendered directly
   * and the internal fetch is skipped (the composer can share one feed load).
   */
  items?: FeedItem[];
}

export function RingsHeader({ onAuthExpired, items: itemsProp }: RingsHeaderProps) {
  const controlled = itemsProp !== undefined;
  const [fetched, setFetched] = useState<FeedItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openKey, setOpenKey] = useState<string | null>(null);
  const [openItemId, setOpenItemId] = useState<string | null>(null);

  useEffect(() => {
    if (controlled) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await feedApi.list({ kind: 'slot_suggestion', state: 'open' });
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
  const buckets = useMemo(() => tierRingBuckets(items ?? []), [items]);
  const totalItems = useMemo(() => buckets.reduce((n, b) => n + b.items.length, 0), [buckets]);
  const activeBucket = useMemo(() => buckets.find((b) => b.key === openKey) ?? null, [buckets, openKey]);

  const toggleRing = useCallback((key: string) => {
    setOpenItemId(null);
    setOpenKey((cur) => (cur === key ? null : key));
  }, []);
  const toggleItem = useCallback((id: string) => {
    setOpenItemId((cur) => (cur === id ? null : id));
  }, []);

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
          buckets.map((b) => <Ring key={b.key} bucket={b} active={openKey === b.key} onTap={() => toggleRing(b.key)} />)
        )}
      </div>

      {!loading && totalItems === 0 && (
        // Intentionally-left-blank: three empty rings could read as broken — say so.
        <p data-testid="rings-empty" className="mt-1.5 text-xs text-honeydew-600">
          No tier suggestions yet — your rings fill as the day&rsquo;s tiers surface.
        </p>
      )}

      {activeBucket && (
        <div data-testid={`ring-panel-${activeBucket.key}`} className="mt-2 rounded-xl border border-honeydew-200 bg-cream p-3 shadow-soft">
          <h3 className="text-xs font-bold uppercase tracking-wider text-honeydew-700">
            {activeBucket.label} · {activeBucket.items.filter(ringItemDone).length}/{activeBucket.items.length} done
          </h3>

          {activeBucket.items.length === 0 ? (
            // Intentionally-left-blank: an explicit empty-bucket line.
            <p data-testid="ring-panel-empty" className="mt-2 text-xs text-honeydew-600">
              Empty — nothing in this tier yet. Suggestions arrive with each sync.
            </p>
          ) : (
            <ul className="mt-2 flex flex-col gap-2">
              {activeBucket.items.map((it) => {
                const done = ringItemDone(it);
                const rows = evidenceRows(it.evidence);
                const expanded = openItemId === it.id;
                return (
                  <li key={it.id} data-testid="ring-panel-item" className="border-t border-dashed border-honeydew-200 pt-2 first:border-0 first:pt-0">
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
                        <span className="min-w-0 break-words text-sm font-semibold text-honeydew-700">{it.title || it.id}</span>
                      </button>
                      {/* Completion is Phase C — an honest DISABLED placeholder, no
                          mutation path. Styled visibly inert (muted + reduced
                          opacity, default cursor) so it never reads as a live
                          control. The `disabled` attr AND the `opacity-50` muted
                          class are pinned together — un-disabling this forces a
                          conscious restyle too. */}
                      <button
                        type="button"
                        data-testid="ring-complete"
                        disabled
                        aria-disabled="true"
                        title="arrives with the board"
                        className="shrink-0 cursor-default rounded-lg border border-honeydew-200 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-honeydew-400 opacity-50"
                      >
                        ✓ Done
                      </button>
                    </div>
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
              })}
            </ul>
          )}

          <p className="mt-2 text-[11px] text-honeydew-600">Completing arrives with the board.</p>
        </div>
      )}
    </section>
  );
}

// One ring: n segments (amber = planned, green = done) or, for an empty bucket,
// a faint red circle. A tap toggles the bucket panel.
function Ring({ bucket, active, onTap }: { bucket: RingBucket; active: boolean; onTap: () => void }) {
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
              className={segmentStroke(ringItemDone(bucket.items[i]))}
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
