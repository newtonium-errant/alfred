import { forwardRef } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import { deckVerbsFor, kindLabel, HEAVY_KINDS } from '../../lib/algernon/feedConstants';
import { evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';

// Presentational deck card. All content is rendered as React text children
// (auto-escaped) — evidence is untrusted display data, so NOTHING here uses
// dangerouslySetInnerHTML or renders an href from item data (#22 XSS precedent).
// The drag transform + stamp opacities are driven IMPERATIVELY by Deck via the
// forwarded ref (query `[data-stamp]`) so a pointermove never re-renders React.

export interface DeckCardProps {
  item: FeedItem;
  depth: number; // 0 = top (interactive), 1..n = stacked behind
  expanded: boolean;
  confirming: boolean;
  onToggleEvidence: () => void;
  onConfirmHeavy: () => void;
  onCancelHeavy: () => void;
}

export const DeckCard = forwardRef<HTMLDivElement, DeckCardProps>(function DeckCard(
  { item, depth, expanded, confirming, onToggleEvidence, onConfirmHeavy, onCancelHeavy },
  ref,
) {
  const verbs = deckVerbsFor(item.kind);
  const heavy = HEAVY_KINDS.has(item.kind);
  const rows = evidenceRows(item.evidence);

  return (
    <div
      ref={ref}
      data-testid="deck-card"
      data-kind={item.kind}
      className="absolute inset-0 m-auto flex max-h-[360px] touch-none select-none flex-col rounded-2xl border border-honeydew-300 bg-cream p-4 shadow-card"
      style={{
        zIndex: 100 - depth,
        transform: `translateY(${depth * 10}px) scale(${1 - depth * 0.035})`,
        opacity: depth > 2 ? 0 : 1,
        pointerEvents: depth === 0 ? 'auto' : 'none',
        transition: 'transform .22s ease, opacity .22s ease',
      }}
    >
      <div className="mb-2 flex flex-wrap gap-1.5">
        <span className="rounded border border-honeydew-300 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-honeydew-600">
          {kindLabel(item.kind)}
        </span>
        {item.instance && (
          <span className="rounded border border-honeydew-400 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-honeydew-600">
            {item.instance}
          </span>
        )}
        {heavy && (
          <span className="rounded border border-status-progress-fg px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-status-progress-fg">
            Heavy · writes a record
          </span>
        )}
      </div>

      <h2 className="mb-1.5 text-lg font-extrabold leading-snug text-honeydew-700">{item.title || item.id}</h2>

      {rows.length > 0 && (
        <button
          type="button"
          data-testid="deck-evidence-toggle"
          onClick={onToggleEvidence}
          aria-expanded={expanded}
          className="self-start text-xs text-honeydew-600 underline underline-offset-2"
        >
          {expanded ? 'Hide details' : 'Show details'}
        </button>
      )}
      {expanded && (
        <dl data-testid="deck-evidence" className="mt-2 space-y-1 border-t border-dashed border-honeydew-300 pt-2 text-xs text-honeydew-600">
          {rows.map((r) => (
            <div key={r.key} className="flex gap-2">
              <dt className="shrink-0 font-semibold text-honeydew-700">{evidenceLabel(r.key)}:</dt>
              <dd className="min-w-0 break-words">{r.value}</dd>
            </div>
          ))}
        </dl>
      )}

      <div className="mt-auto flex items-center justify-between pt-3 text-[10px] font-semibold uppercase tracking-wider text-honeydew-600">
        <span className={verbs?.reject ? 'text-danger' : 'text-honeydew-400'}>
          {verbs?.reject ? `← ${verbs.rejectLabel}` : '—'}
        </span>
        <span className="text-status-progress-fg">↑ Park</span>
        <span className={verbs?.affirm ? 'text-honeydew-600' : 'text-honeydew-400'}>
          {verbs?.affirm ? `${verbs.affirmLabel} →` : '—'}
        </span>
      </div>

      {/* Verdict stamps — Deck sets their opacity imperatively during a drag. */}
      <span data-stamp="affirm" className="pointer-events-none absolute right-4 top-4 rotate-[-8deg] rounded border-2 border-honeydew-600 px-2.5 py-1 text-sm font-extrabold uppercase tracking-widest text-honeydew-600 opacity-0">
        {heavy ? 'Review' : 'Yes'}
      </span>
      <span data-stamp="reject" className="pointer-events-none absolute left-4 top-4 rotate-[8deg] rounded border-2 border-danger px-2.5 py-1 text-sm font-extrabold uppercase tracking-widest text-danger opacity-0">
        No
      </span>
      <span data-stamp="park" className="pointer-events-none absolute left-1/2 top-4 -ml-10 rounded border-2 border-status-progress-fg px-2.5 py-1 text-sm font-extrabold uppercase tracking-widest text-status-progress-fg opacity-0">
        Park
      </span>

      {confirming && (
        <div
          data-testid="deck-confirm"
          className="absolute inset-0 flex flex-col items-center justify-center gap-3.5 rounded-2xl bg-cream p-5 text-center"
        >
          <p className="text-sm text-honeydew-600">
            {item.kind === 'proposal' ? 'Create this canonical record?' : 'Write this to the vault?'}
          </p>
          <p className="text-base font-bold text-honeydew-700">{item.title || item.id}</p>
          <div className="flex gap-3">
            <button
              type="button"
              data-testid="deck-confirm-yes"
              onClick={onConfirmHeavy}
              className="rounded-lg border border-honeydew-600 bg-honeydew-600 px-4 py-2.5 text-xs font-bold uppercase tracking-wider text-cream"
            >
              Confirm
            </button>
            <button
              type="button"
              data-testid="deck-confirm-cancel"
              onClick={onCancelHeavy}
              className="rounded-lg border border-honeydew-300 px-4 py-2.5 text-xs font-bold uppercase tracking-wider text-honeydew-600"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
});
