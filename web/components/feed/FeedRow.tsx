import type { FeedItem } from '../../lib/algernon/feed';
import { kindLabel } from '../../lib/algernon/feedConstants';
import { evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';

// One FYI feed row: a glance item with an Ack. Content is escaped React text
// only (evidence is untrusted display data — no innerHTML, no href).

export interface FeedRowProps {
  item: FeedItem;
  expanded: boolean;
  onToggleEvidence: () => void;
  onAck: () => void;
}

export function FeedRow({ item, expanded, onToggleEvidence, onAck }: FeedRowProps) {
  const rows = evidenceRows(item.evidence);
  return (
    <li data-testid="feed-row" data-kind={item.kind} className="rounded-xl border border-honeydew-200 bg-cream p-3 shadow-soft">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="mb-1 flex flex-wrap gap-1.5">
            <span className="rounded border border-honeydew-300 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-honeydew-600">
              {kindLabel(item.kind)}
            </span>
            {item.instance && (
              <span className="rounded border border-honeydew-400 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-honeydew-600">
                {item.instance}
              </span>
            )}
          </div>
          <p className="break-words text-sm font-semibold text-honeydew-700">{item.title || item.id}</p>
          {rows.length > 0 && (
            <button
              type="button"
              data-testid="feed-row-details"
              onClick={onToggleEvidence}
              aria-expanded={expanded}
              className="mt-1 text-xs text-honeydew-600 underline underline-offset-2"
            >
              {expanded ? 'Hide details' : 'Show details'}
            </button>
          )}
        </div>
        <button
          type="button"
          data-testid="feed-row-ack"
          aria-label={`Acknowledge: ${item.title || item.id}`}
          onClick={onAck}
          className="shrink-0 rounded-lg border border-honeydew-400 px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-honeydew-600"
        >
          Ack
        </button>
      </div>
      {expanded && rows.length > 0 && (
        <dl data-testid="feed-row-evidence" className="mt-2 space-y-1 border-t border-dashed border-honeydew-200 pt-2 text-xs text-honeydew-600">
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
}
