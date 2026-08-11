import { forwardRef } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import { affirmLabelFor, armNoteFor, deckVerbsFor, emailPriority, isHeavyVerb, kindLabel, verbLabelFor, type EmailPriority } from '../../lib/algernon/feedConstants';
import { ringTierOf } from '../../lib/algernon/rings';
import { evidenceBody, evidenceExternalLink, evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';
import { EvidenceBody } from './EvidenceBody';

// The top card's z-index base — each card sits at `DECK_CARD_BASE_Z - depth` (the top at
// the base). Any overlay meant to sit ABOVE the card stack (the snoozed drill, the re-tier
// picker) MUST exceed this, or it opens BEHIND the top card and reads as a dead tap on the
// real device (the #28 re-tier bug: z-20 overlay < z-100 card → invisible, jsdom can't see
// stacking). Exported so those overlays derive their z from it and can't drift below.
export const DECK_CARD_BASE_Z = 100;

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
  /** Which direction is armed — decides what the confirm overlay promises. */
  confirmingVerdict?: 'affirm' | 'reject' | null;
  onToggleEvidence: () => void;
  onConfirmHeavy: () => void;
  onCancelHeavy: () => void;
  /** Open the re-tier picker (#28) — email_tier top card only; absent elsewhere. */
  onReTierOpen?: () => void;
  /** Open the correction picker (#13) — routine_match top card only; absent elsewhere. */
  onCorrectOpen?: () => void;
}

export const DeckCard = forwardRef<HTMLDivElement, DeckCardProps>(function DeckCard(
  { item, depth, expanded, confirming, confirmingVerdict = 'affirm', onToggleEvidence, onConfirmHeavy, onCancelHeavy, onReTierOpen, onCorrectOpen },
  ref,
) {
  const verbs = deckVerbsFor(item.kind);
  // PER-DIRECTION. A card is no longer heavy or light as a whole: an attribution
  // confirm is light and its reject is heavy, and a badge that averaged the two
  // would be wrong in both directions at once.
  const affirmHeavy = isHeavyVerb(item.kind, 'affirm');
  const rejectHeavy = isHeavyVerb(item.kind, 'reject');
  const armNote = armNoteFor(item.kind, confirmingVerdict ?? 'affirm');
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
  // #27 email_urgent — the INTERRUPT card. Distinct from the calibration card: it's not
  // "judge my classification", it's "this email needs you". The high_source chip is HONEST
  // provenance — a sender-override forced the high vs the LLM's own call.
  const urgent = item.kind === 'email_urgent';
  const urgentSource = urgent ? (item.evidence as Record<string, unknown> | null | undefined)?.high_source : null;
  const urgentWhy = urgentSource === 'override' ? 'Priority sender' : urgentSource === 'llm' ? 'Classifier: high' : null;
  // Prose body (generic — any kind's evidence.body) OR a safe external link (the
  // email "Open in Gmail" deep-link, #26) also makes the card expandable.
  const hasDetails =
    rows.length > 0 || evidenceBody(item.evidence) !== null || evidenceExternalLink(item.evidence) !== null;

  return (
    <div
      ref={ref}
      data-testid="deck-card"
      data-kind={item.kind}
      className="absolute inset-0 m-auto flex max-h-[360px] touch-none select-none flex-col rounded-2xl border border-honeydew-300 bg-cream p-4 shadow-card"
      style={{
        zIndex: DECK_CARD_BASE_Z - depth,
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
        {urgent && (
          // The interrupt signal, on the face — danger-toned so it reads distinct from the
          // calibration card at a glance ("needs you NOW", not "confirm a classification").
          <span
            data-testid="deck-urgent-badge"
            className="rounded border border-danger bg-danger-bg px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-danger"
          >
            Needs you
          </span>
        )}
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
        {affirmHeavy && (
          <span
            data-testid="deck-heavy-affirm"
            className="rounded border border-status-progress-fg px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-status-progress-fg"
          >
            Heavy · {verbs?.affirmLabel}
          </span>
        )}
        {rejectHeavy && (
          <span
            data-testid="deck-heavy-reject"
            className="rounded border border-danger px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-danger"
          >
            Heavy · {verbs?.rejectLabel}
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

      {urgentWhy && (
        // The urgent card's honest provenance, on the face — why THIS email interrupted.
        <p data-testid="deck-urgent-why" className="mb-1.5 shrink-0 text-xs italic text-honeydew-600">
          {urgentWhy}
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

      {/* Re-tier affordance (#28) — a DELIBERATE tap (not a swipe) to correct the
          classifier's tier. Email top card only; the "…" control is taken (details),
          so this is an on-face button. Swipes stay judge-the-claim; this is the heavier
          multi-choice door. */}
      {item.kind === 'email_tier' && depth === 0 && onReTierOpen && (
        <button
          type="button"
          data-testid="deck-retier-open"
          onClick={onReTierOpen}
          className="mt-1 shrink-0 self-start text-xs font-semibold text-status-progress-fg underline underline-offset-2"
        >
          Adjust tier…
        </button>
      )}

      {/* Correction affordance (#13) — the third door on a routine match. Left-swipe
          still means a plain "no"; this is the deliberate tap that also SAYS what the
          completion meant, so a NO teaches instead of only suppressing. Routine-match
          top card only, mirroring the re-tier button's placement. */}
      {item.kind === 'routine_match' && depth === 0 && onCorrectOpen && (
        <button
          type="button"
          data-testid="deck-correct-open"
          onClick={onCorrectOpen}
          className="mt-1 shrink-0 self-start text-xs font-semibold text-status-progress-fg underline underline-offset-2"
        >
          What did this mean?
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
        <span className={verbs?.reject ? 'text-danger' : verbs?.rejectDefers ? 'text-status-progress-fg' : 'text-honeydew-400'}>
          {verbs?.reject || verbs?.rejectDefers ? `← ${verbs.rejectLabel}` : '—'}
        </span>
        <span className="text-status-progress-fg">↑ Snooze</span>
        <span className={verbs?.affirm ? 'text-honeydew-600' : 'text-honeydew-400'}>
          {verbs?.affirm ? `${affirmLabel} →` : '—'}
        </span>
      </div>

      {/* Verdict stamps — Deck sets their opacity imperatively during a drag. */}
      <span data-stamp="affirm" className="pointer-events-none absolute right-4 top-4 rotate-[-8deg] rounded border-2 border-honeydew-600 px-2.5 py-1 text-sm font-extrabold uppercase tracking-widest text-honeydew-600 opacity-0">
        {priority ? priority : urgent ? affirmLabel : item.kind === 'slot_suggestion' ? 'Take it' : affirmHeavy ? 'Review' : 'Yes'}
      </span>
      <span data-stamp="reject" className="pointer-events-none absolute left-4 top-4 rotate-[8deg] rounded border-2 border-danger px-2.5 py-1 text-sm font-extrabold uppercase tracking-widest text-danger opacity-0">
        {verbs?.rejectDefers ? 'Skip' : rejectHeavy ? 'Review' : 'No'}
      </span>
      <span data-stamp="snooze" className="pointer-events-none absolute left-1/2 top-4 -ml-10 rounded border-2 border-status-progress-fg px-2.5 py-1 text-sm font-extrabold uppercase tracking-widest text-status-progress-fg opacity-0">
        Snooze
      </span>

      {confirming && (
        <div
          data-testid="deck-confirm"
          className="absolute inset-0 flex flex-col items-center justify-center gap-3.5 rounded-2xl bg-cream p-5 text-center"
        >
          {/* D4: the arm shows exactly what will be written. The old copy asked
              "Write this to the vault?" for every heavy verb, which is not what
              an attribution REJECT does — it removes text. A verb whose
              consequence is stated wrongly is worse than one stated vaguely. */}
          <p className="text-sm text-honeydew-600">
            {verbLabelFor(item.kind, confirmingVerdict ?? 'affirm')} this?
          </p>
          <p className="text-base font-bold text-honeydew-700">{item.title || item.id}</p>
          {armNote ? (
            <p data-testid="deck-confirm-note" className="text-xs font-semibold text-danger">
              {armNote}
            </p>
          ) : (
            // ILB: a heavy verb with no declared consequence says so, rather
            // than reading as a reassuring blank.
            <p data-testid="deck-confirm-note-missing" className="text-xs text-honeydew-600">
              This writes to the vault. The exact change isn’t described for this card type.
            </p>
          )}
          <div className="flex gap-3">
            <button
              type="button"
              data-testid="deck-confirm-yes"
              onClick={onConfirmHeavy}
              className="rounded-lg border border-honeydew-600 bg-honeydew-600 px-4 py-2.5 text-xs font-bold uppercase tracking-wider text-cream"
            >
              {verbLabelFor(item.kind, confirmingVerdict ?? 'affirm') || 'Confirm'}
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
