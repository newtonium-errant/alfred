import type { FeedItem } from '../../lib/algernon/feed';
import { kindLabel } from '../../lib/algernon/feedConstants';
import { evidenceBody, evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';
import { COMPLETION_UNAVAILABLE_HINT, ringItemCompletable, ringItemUndoable } from '../../lib/algernon/rings';
import type { UseRingCompletionResult } from './useRingCompletion';
import { EvidenceBody } from './EvidenceBody';

// One feed row. Content is escaped React text only (evidence is untrusted display
// data — no innerHTML, no href). The right-hand affordance is one of: an Ack
// (FYI rows), the SAME per-lane completion control the rings panel uses (slot rows
// — via the shared useRingCompletion, never a second implementation), or nothing.

export interface FeedRowProps {
  item: FeedItem;
  expanded: boolean;
  onToggleEvidence: () => void;
  /** Present → an Ack button (FYI rows). */
  onAck?: () => void;
  /**
   * Present → this is a completable slot row: render the live ✓ / done+undo /
   * honest-note affordance driven by the SHARED completion hook (the identical
   * per-lane logic the rings panel uses). Takes precedence over onAck.
   */
  completion?: UseRingCompletionResult;
}

export function FeedRow({ item, expanded, onToggleEvidence, onAck, completion }: FeedRowProps) {
  const rows = evidenceRows(item.evidence);
  // A digest/prose body (peer_digest et al.) also makes the row expandable, even
  // when it has no key:value rows of its own.
  const hasBody = evidenceBody(item.evidence) !== null;
  const hasDetails = rows.length > 0 || hasBody;
  const done = completion ? completion.effectiveDone(item) : false;
  const busy = completion ? completion.busy(item.id) : false;
  const completable = completion ? ringItemCompletable(item) : false;
  const undoable = completion ? ringItemUndoable(item) : false;
  const completionError = completion ? completion.errorFor(item.id) : null;
  return (
    <li data-testid="feed-row" data-kind={item.kind} data-done={done} className="rounded-xl border border-honeydew-200 bg-cream p-3 shadow-soft">
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
          <p className={`break-words text-sm font-semibold text-honeydew-700 ${done ? 'line-through opacity-70' : ''}`}>
            {item.title || item.id}
          </p>
          {hasDetails && (
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

        {completion ? (
          // Slot completion — the SAME per-lane affordance as the rings panel.
          done ? (
            <div className="flex shrink-0 items-center gap-1.5">
              <span data-testid="feed-row-done" className="text-[10px] font-bold uppercase tracking-wider text-status-done-fg">
                ✓ Done
              </span>
              {undoable && (
                <button
                  type="button"
                  data-testid="feed-row-undo"
                  disabled={busy}
                  onClick={() => completion.undo(item)}
                  className="rounded-lg border border-honeydew-300 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-honeydew-600 disabled:opacity-50"
                >
                  {busy ? '…' : 'Undo'}
                </button>
              )}
            </div>
          ) : completable ? (
            <button
              type="button"
              data-testid="feed-row-complete"
              disabled={busy}
              onClick={() => completion.complete(item)}
              className="shrink-0 rounded-lg border border-honeydew-400 px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-honeydew-700 disabled:opacity-50"
            >
              {busy ? '…' : '✓ Done'}
            </button>
          ) : (
            // Unknown / unstamped-origin lane — honest note, NO button (task is
            // completable now; only a slot with no writer-able origin lands here).
            <span data-testid="feed-row-unavailable" className="shrink-0 self-center text-[11px] italic text-honeydew-600/80">
              {COMPLETION_UNAVAILABLE_HINT}
            </span>
          )
        ) : onAck ? (
          <button
            type="button"
            data-testid="feed-row-ack"
            aria-label={`Acknowledge: ${item.title || item.id}`}
            onClick={onAck}
            className="shrink-0 rounded-lg border border-honeydew-400 px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-honeydew-600"
          >
            Ack
          </button>
        ) : null}
      </div>

      {completionError && (
        <p data-testid="feed-row-completion-error" role="alert" className="mt-1 text-[11px] text-danger">
          {completionError}
        </p>
      )}
      {expanded && hasDetails && (
        <div className="border-t border-dashed border-honeydew-200 pt-2">
          {/* Prose body first (the digest content), then any key:value rows. */}
          <EvidenceBody evidence={item.evidence} />
          {rows.length > 0 && (
            <dl data-testid="feed-row-evidence" className="mt-2 space-y-1 text-xs text-honeydew-600">
              {rows.map((r) => (
                <div key={r.key} className="flex gap-2">
                  <dt className="shrink-0 font-semibold text-honeydew-700">{evidenceLabel(r.key)}:</dt>
                  <dd className="min-w-0 break-words">{r.value}</dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      )}
    </li>
  );
}
