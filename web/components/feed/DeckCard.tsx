import { forwardRef } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import { affirmLabelFor, deckVerbsFor, emailPriority, kindLabel, HEAVY_KINDS, type EmailPriority } from '../../lib/algernon/feedConstants';
import { ringTierOf } from '../../lib/algernon/rings';
import { evidenceBody, evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';
import { EvidenceBody } from './EvidenceBody';

// Presentational deck card. All content is rendered as React text children
// (auto-escaped) — evidence is untrusted display data, so NOTHING here uses
// dangerouslySetInnerHTML or renders an href from item data (#22 XSS precedent).
// The drag transform + stamp opacities are driven IMPERATIVELY by Deck via the
// forwarded ref (query `[data-stamp]`) so a pointermove never re-renders React.

// Tier badge colour by assigned priority — bordered-chip idiom (matches the
// card's other chips, not the rounded Badge pill). Reuses existing status tokens.
const PRIORITY_CHIP_CLASS: Record<EmailPriority, string> = {
  high: 'border-danger text-danger',
  medium: 'border-status-progress-fg text-status-progress-fg',
  low: 'border-honeydew-500 text-honeydew-600',
  // Distinct neutral so SPAM isn't visually equated with HIGH-urgency (it's junk,
  // not important) — still legible on the face.
  spam: 'border-status-todo-fg text-status-todo-fg',
};

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
  // Email-tier decisions carry their assigned tier on the FACE (no blind confirm):
  // a colour-coded badge + a dynamic affirm label ("Confirm HIGH"). Absent/spam →
  // no badge + the plain verb.
  const priority = emailPriority(item);
  const affirmLabel = affirmLabelFor(item);
  // C2 slot candidate face: the tier on a badge + the WHY (surface_reason) on the
  // face, so the card carries its own claim (no blind swipe).
  const slotTier = item.kind === 'slot_suggestion' ? ringTierOf(item) : null;
  const rawWhy = item.kind === 'slot_suggestion' ? (item.evidence as Record<string, unknown> | null | undefined)?.surface_reason : null;
  const whyText = typeof rawWhy === 'string' && rawWhy.trim() ? rawWhy : null;
  // Prose body (generic — any kind's evidence.body) also makes the card expandable.
  const hasDetails = rows.length > 0 || evidenceBody(item.evidence) !== null;

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
      <div className="mb-2 flex shrink-0 flex-wrap gap-1.5">
        <span className="rounded border border-honeydew-300 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-honeydew-600">
          {kindLabel(item.kind)}
        </span>
        {priority && (
          // The assigned tier, on the face — the decision content the operator
          // was otherwise blind-confirming.
          <span
            data-testid="deck-tier-badge"
            className={`rounded border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider ${PRIORITY_CHIP_CLASS[priority]}`}
          >
            {priority}
          </span>
        )}
        {slotTier && (
          // The candidate's tier, on the face — the decision content, not a blind swipe.
          <span
            data-testid="deck-slot-tier"
            className="rounded border border-status-progress-fg px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-status-progress-fg"
          >
            T{slotTier}
          </span>
        )}
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

      <h2 className="mb-1.5 shrink-0 text-lg font-extrabold leading-snug text-honeydew-700">{item.title || item.id}</h2>

      {whyText && (
        // The WHY, on the face (surface_reason) — the card's own claim for being here.
        <p data-testid="deck-slot-why" className="mb-1.5 shrink-0 text-xs italic text-honeydew-600">
          {whyText}
        </p>
      )}

      {hasDetails && (
        <button
          type="button"
          data-testid="deck-evidence-toggle"
          onClick={onToggleEvidence}
          aria-expanded={expanded}
          className="shrink-0 self-start text-xs text-honeydew-600 underline underline-offset-2"
        >
          {expanded ? 'Hide details' : 'Show details'}
        </button>
      )}
      {expanded && (
        // Evidence SCROLLS within the card — flex-1 + min-h-0 lets it take the
        // space between the title and the pinned verb footer, overflow-y-auto keeps
        // long snippets / a long digest body from spilling past the card onto the
        // buttons below.
        <div
          data-testid="deck-evidence"
          className="mt-2 min-h-0 flex-1 space-y-1 overflow-y-auto border-t border-dashed border-honeydew-300 pt-2 text-xs text-honeydew-600"
        >
          <EvidenceBody evidence={item.evidence} />
          {rows.length > 0 && (
            <dl className="space-y-1">
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

      <div className="mt-auto flex shrink-0 items-center justify-between pt-3 text-[10px] font-semibold uppercase tracking-wider text-honeydew-600">
        <span className={verbs?.reject ? 'text-danger' : verbs?.rejectParks ? 'text-status-progress-fg' : 'text-honeydew-400'}>
          {verbs?.reject || verbs?.rejectParks ? `← ${verbs.rejectLabel}` : '—'}
        </span>
        <span className="text-status-progress-fg">↑ Park</span>
        <span className={verbs?.affirm ? 'text-honeydew-600' : 'text-honeydew-400'}>
          {verbs?.affirm ? `${affirmLabel} →` : '—'}
        </span>
      </div>

      {/* Verdict stamps — Deck sets their opacity imperatively during a drag. */}
      <span data-stamp="affirm" className="pointer-events-none absolute right-4 top-4 rotate-[-8deg] rounded border-2 border-honeydew-600 px-2.5 py-1 text-sm font-extrabold uppercase tracking-widest text-honeydew-600 opacity-0">
        {priority ? affirmLabel : item.kind === 'slot_suggestion' ? 'Take it' : heavy ? 'Review' : 'Yes'}
      </span>
      <span data-stamp="reject" className="pointer-events-none absolute left-4 top-4 rotate-[8deg] rounded border-2 border-danger px-2.5 py-1 text-sm font-extrabold uppercase tracking-widest text-danger opacity-0">
        {verbs?.rejectParks ? 'Skip' : 'No'}
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
