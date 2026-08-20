import { forwardRef } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import { affirmLabelFor, armNoteFor, emailPriority, holdChoicesFor, isHeavyVerb, kindLabel, verbLabelFor, verbsFromActions, type EmailPriority } from '../../lib/algernon/feedConstants';
import {
  CONSOLE_LABEL,
  ROLE_TEXT_CLASS,
  instanceAccentClass,
  railClassForWeight,
  roleChipClass,
  roleForVerdict,
} from '../../lib/algernon/consoleTokens';
import { ringTierOf } from '../../lib/algernon/rings';
import { evidenceBody, evidenceExternalLink, evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';
import { EvidenceBody } from './EvidenceBody';
import { TimeExtent } from './TimeExtent';

// The top card's z-index base — each card sits at `DECK_CARD_BASE_Z - depth` (the top at
// the base). Any overlay meant to sit ABOVE the card stack (the snoozed drill, the re-tier
// picker) MUST exceed this, or it opens BEHIND the top card and reads as a dead tap on the
// real device (the #28 re-tier bug: z-20 overlay < z-100 card → invisible, jsdom can't see
// stacking). Exported so those overlays derive their z from it and can't drift below.
export const DECK_CARD_BASE_Z = 100;

// How far DOWN each card behind the top one is pushed. This is what makes the
// stack read as a stack — and it is also why the stack does not end where its
// container does: the deepest card hangs `OFFSET * MAX_VISIBLE_DEPTH` below the
// box, which is how the cards came to sit on the ✕ ↑ ⋯ ✓ buttons underneath
// (the operator's overlap report). Deck reserves clearance DERIVED from these
// two, so the reservation cannot drift from the transform that needs it.
export const DECK_CARD_DEPTH_OFFSET_PX = 10;

// Cards deeper than this render fully transparent, so the visible stack is
// depth 0..2 — and the furthest spill is bounded by that. `useDeck` hands over
// two upcoming cards, which is the same bound arrived at from the other side;
// this constant is the one the RENDER obeys.
export const DECK_MAX_VISIBLE_DEPTH = 2;

// THE CARD'S DROP SHADOW, as a Tailwind arbitrary value.
//
// WRITTEN OUT, never composed from the constants below. Tailwind's JIT scans
// source TEXT, so a template-built class name emits no CSS at all — which is the
// identical species as the `h-13` that had been silently sizing nothing on the
// gesture buttons (see Deck.tsx). So the literal is the source of truth for what
// renders, the constants are the source of truth for the arithmetic, and
// `deckLayout.test.tsx` parses the literal and asserts the two agree.
export const DECK_CARD_SHADOW_CLASS = 'shadow-[0_18px_40px_rgba(0,0,0,0.5)]';
export const DECK_CARD_SHADOW_Y_PX = 18;
export const DECK_CARD_SHADOW_BLUR_PX = 40;

// How far the shadow reaches PAST the edge it falls from: its offset plus half
// its blur (a Gaussian blur of radius r spreads r/2 beyond the shadow's edge).
// This is not layout — it paints over whatever is beneath it — but it is part of
// the card's visual footprint, and it is the part the operator actually
// photographed lying over the buttons. Clearance that counted only the geometry
// would have fixed the measurement and left the complaint.
export const DECK_CARD_SHADOW_REACH_PX = DECK_CARD_SHADOW_Y_PX + DECK_CARD_SHADOW_BLUR_PX / 2;

// Presentational deck card. All content is rendered as React text children
// (auto-escaped) — evidence is untrusted display data, so NOTHING here uses
// dangerouslySetInnerHTML or renders an href from item data (#22 XSS precedent).
// The drag transform + stamp opacities are driven IMPERATIVELY by Deck via the
// forwarded ref (query `[data-stamp]`) so a pointermove never re-renders React.
//
// VISUAL IDENTITY (interface-reimagine arc, Phase B): the console — LCARS
// grammar on the modern palette, via the shared tokens in
// lib/algernon/consoleTokens.ts. The card is a PANEL with a function-coloured
// edge rail, not a rounded paper card. Colour is spent by role and never for
// decoration; see console.css for what each role means.
//
// D5 (authored-complete): the card renders whole. The "Show details" step is
// the three-depth grammar (glance → expand → investigate), which is depth, not
// earn-progression — nothing here grows as the operator uses it.

// Tier badge role by assigned priority. Roles, not decoration: HIGH is the
// alert role, medium is "needs a beat", low is neutral structure. SPAM is
// deliberately the quietest treatment on the card — it is junk, not urgency,
// and the original honeydew palette's note applies unchanged: a spam chip must
// never read as equivalent to HIGH.
const PRIORITY_CHIP_CLASS: Record<EmailPriority, string> = {
  high: roleChipClass('negative'),
  medium: roleChipClass('caution'),
  low: roleChipClass('info'),
  spam: 'bg-console-raise border-console-edge-bright text-console-ink-faint',
};

const CHIP_BASE = 'rounded-sm border px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-[0.14em]';

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
  /**
   * Open the hold-selector (affirm-with-hold-modifier) — the accessible door
   * to the gesture's alternatives, top card only. Rendered ONLY when the item
   * actually serves a co-equal choice group (`holdChoicesFor`), so the door
   * cannot exist where it opens onto nothing.
   */
  onHoldOpen?: () => void;
  /**
   * This card came BACK because the server refused the last verdict given on it.
   *
   * Marked on the card face, not only in the notice above the stack, because the
   * notice is read once and the decision is made here — the operator needs to
   * know, at the moment they are about to swipe, that their last swipe on this
   * card did not stick.
   */
  unrecorded?: boolean;
}

export const DeckCard = forwardRef<HTMLDivElement, DeckCardProps>(function DeckCard(
  { item, depth, expanded, confirming, confirmingVerdict = 'affirm', onToggleEvidence, onConfirmHeavy, onCancelHeavy, onReTierOpen, onCorrectOpen, onHoldOpen, unrecorded = false },
  ref,
) {
  const verbs = verbsFromActions(item);
  // A suggestion card (affirm-with-hold-modifier): its affirm is one member of
  // a served co-equal group. Drives the on-face suggestion line, the "Change…"
  // door, and the affirm stamp's label — all derived from the wire, no kinds.
  const holdChoices = holdChoicesFor(item, 'affirm');
  // PER-DIRECTION. A card is no longer heavy or light as a whole: an attribution
  // confirm is light and its reject is heavy, and a badge that averaged the two
  // would be wrong in both directions at once.
  const affirmHeavy = isHeavyVerb(verbs, 'affirm');
  const rejectHeavy = isHeavyVerb(verbs, 'reject');
  const armNote = armNoteFor(verbs, confirmingVerdict ?? 'affirm');
  const rows = evidenceRows(item.evidence);
  // Email-tier decisions carry their assigned tier on the FACE (no blind confirm):
  // a role-coded badge + a dynamic affirm label ("Confirm HIGH"). Absent/spam →
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
  // The rail answers the glance question — can this card be cleared with one
  // motion? — while the per-direction Heavy chips below say which direction is
  // the heavy one. One rail cannot express two directions, and on an attribution
  // the two genuinely differ.
  const railClass = railClassForWeight(affirmHeavy || rejectHeavy);
  // The arm stage is drawn in the ARMED direction's role: a reject that strips a
  // record body is negative, a confirm that writes records is caution. One
  // colour for both would misdescribe whichever it wasn't chosen for.
  const armRole = roleForVerdict(confirmingVerdict ?? 'affirm');

  return (
    <div
      ref={ref}
      data-testid="deck-card"
      data-kind={item.kind}
      className={`absolute inset-0 m-auto flex max-h-[380px] touch-none select-none overflow-hidden rounded-sm border border-console-edge bg-console-panel ${DECK_CARD_SHADOW_CLASS}`}
      style={{
        zIndex: DECK_CARD_BASE_Z - depth,
        transform: `translateY(${depth * DECK_CARD_DEPTH_OFFSET_PX}px) scale(${1 - depth * 0.035})`,
        opacity: depth > DECK_MAX_VISIBLE_DEPTH ? 0 : 1,
        pointerEvents: depth === 0 ? 'auto' : 'none',
        transition: 'transform .22s ease, opacity .22s ease',
      }}
    >
      {/* The edge rail — LCARS's most recognisable move, carrying the weight
          signal. Hatched when either direction is heavy, so the signal survives
          without relying on colour alone. */}
      <span aria-hidden="true" data-testid="deck-card-rail" className={`w-1.5 shrink-0 ${railClass}`} />

      <div className="flex min-w-0 flex-1 flex-col p-4">
        <div className="mb-2 flex shrink-0 flex-wrap items-center gap-1.5">
          <span className={`${CHIP_BASE} border-console-edge-bright text-console-ink-faint`}>
            {kindLabel(item.kind)}
          </span>
          {unrecorded && (
            // The card came back: the server refused the last verdict given on
            // it. The alert role, because this is the one chip on the face that
            // reports a FAILURE OF THE SYSTEM rather than a property of the item.
            <span data-testid="deck-unrecorded-mark" className={`${CHIP_BASE} ${roleChipClass('negative')}`}>
              Not recorded
            </span>
          )}
          {urgent && (
            // The interrupt signal, on the face — the alert role, so it reads
            // distinct from the calibration card at a glance ("needs you NOW",
            // not "confirm a classification").
            <span data-testid="deck-urgent-badge" className={`${CHIP_BASE} ${roleChipClass('negative')}`}>
              Needs you
            </span>
          )}
          {priority && (
            // The assigned tier, on the face — the decision content the operator
            // was otherwise blind-confirming.
            <span data-testid="deck-tier-badge" className={`${CHIP_BASE} ${PRIORITY_CHIP_CLASS[priority]}`}>
              {priority}
            </span>
          )}
          {slotTier && (
            // The candidate's tier, on the face — the decision content, not a blind swipe.
            <span data-testid="deck-slot-tier" className={`${CHIP_BASE} ${roleChipClass('info')}`}>
              T{slotTier}
            </span>
          )}
          {item.instance && (
            // Provenance, in an accent that is deliberately NOT a function role —
            // where an item came from is not a judgement about it.
            <span data-testid="deck-instance" className={`${CHIP_BASE} ${instanceAccentClass(item.instance)}`}>
              {item.instance}
            </span>
          )}
          {affirmHeavy && (
            <span data-testid="deck-heavy-affirm" className={`${CHIP_BASE} ${roleChipClass('caution')}`}>
              Heavy · {verbs?.affirmLabel}
            </span>
          )}
          {rejectHeavy && (
            <span data-testid="deck-heavy-reject" className={`${CHIP_BASE} ${roleChipClass('caution')}`}>
              Heavy · {verbs?.rejectLabel}
            </span>
          )}
        </div>

        <h2 className="mb-1.5 shrink-0 text-[19px] font-medium leading-snug tracking-[-0.005em] text-console-ink">
          {item.title || item.id}
        </h2>

        {unrecorded && (
          // The chip above is the glance; this is the sentence. Said HERE, at the
          // point of decision, because the notice over the stack is read once and
          // then the operator is looking at cards — and the thing they must know
          // before swiping is that their last swipe on this one did not stick.
          <p data-testid="deck-unrecorded-note" className="mb-1.5 shrink-0 text-xs text-negative">
            Your last verdict on this card wasn&rsquo;t recorded. Decide it again.
          </p>
        )}

        {/* D7 — an item that carries a real interval renders as its EXTENT.
            Draws nothing at all when the item has no time dimension. */}
        <TimeExtent item={item} className="mb-2 shrink-0" />

        {whyText && (
          // The WHY, on the face (surface_reason) — the card's own claim for being here.
          <p data-testid="deck-slot-why" className="mb-1.5 shrink-0 text-xs italic text-console-ink-dim">
            {whyText}
          </p>
        )}

        {holdChoices && (
          // THE PROPOSAL, ON THE FACE (the ruling: each dealt card PROPOSES).
          // The suggested member by name, so the affirm swipe's meaning is
          // stated where the decision is made — plus the accessible door to
          // the alternatives (top card only): a selector reachable only by
          // holding a drag is a selector no keyboard or assistive pointer can
          // reach. The ↑ button precedent, as an on-face control.
          <p data-testid="deck-suggested" className="mb-1.5 shrink-0 text-xs text-console-ink-dim">
            Suggested:{' '}
            <span className="font-semibold text-console-ink">
              {holdChoices.find((c) => c.suggested)?.label ?? '—'}
            </span>
            {depth === 0 && onHoldOpen && (
              <button
                type="button"
                data-testid="deck-hold-open"
                onClick={onHoldOpen}
                className={`ml-2 ${CONSOLE_LABEL} ${ROLE_TEXT_CLASS.caution} underline underline-offset-4`}
              >
                Change…
              </button>
            )}
          </p>
        )}

        {urgentWhy && (
          // The urgent card's honest provenance, on the face — why THIS email interrupted.
          <p data-testid="deck-urgent-why" className="mb-1.5 shrink-0 text-xs italic text-console-ink-dim">
            {urgentWhy}
          </p>
        )}

        {hasDetails && (
          <button
            type="button"
            data-testid="deck-evidence-toggle"
            onClick={onToggleEvidence}
            aria-expanded={expanded}
            className={`shrink-0 self-start ${CONSOLE_LABEL} underline underline-offset-4 hover:text-console-ink-dim`}
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
            className={`mt-1 shrink-0 self-start ${CONSOLE_LABEL} ${ROLE_TEXT_CLASS.caution} underline underline-offset-4`}
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
            className={`mt-1 shrink-0 self-start ${CONSOLE_LABEL} ${ROLE_TEXT_CLASS.caution} underline underline-offset-4`}
          >
            What did this mean?
          </button>
        )}

        {expanded && (
          // Evidence SCROLLS within the card — flex-1 + min-h-0 lets it take the
          // space between the title and the pinned verb footer, overflow-y-auto keeps
          // long snippets / a long digest body from spilling past the card onto the
          // buttons below.
          //
          // THE CHROME IS NOT INSIDE THE SCROLLER. The divider used to be a
          // `border-t` on the scrolling element itself, so scrolled content slid up
          // under the padding and the rule was drawn THROUGH whatever row happened to
          // straddle the boundary — a half-cut line of caps with a line across it,
          // which is what the operator read as two text layers stacked on each other.
          // A border on a box that doesn't move can't be crossed by content that does.
          <div className="mt-2 flex min-h-0 flex-1 flex-col border-t border-console-edge pt-2">
            <div
              data-testid="deck-evidence"
              className="min-h-0 flex-1 space-y-1 overflow-y-auto text-xs text-console-ink-dim"
            >
              <EvidenceBody evidence={item.evidence} surface="console" />
              {rows.length > 0 && (
                <dl className="space-y-2">
                  {rows.map((r) => (
                    // LABEL ABOVE VALUE, not beside it. The card is ~330px wide; a
                    // `shrink-0` label ate half of that ("Cluster Record Paths" alone
                    // is 157px), leaving long values a narrow column that ran for a
                    // dozen wrapped lines DOWN THE SIDE of its own label. Stacking
                    // gives the value the full width and leaves the label a line of
                    // its own, which is the half of the garbling that wasn't the rule.
                    <div key={r.key}>
                      <dt className={CONSOLE_LABEL}>{evidenceLabel(r.key)}</dt>
                      <dd className="mt-0.5 min-w-0 break-words font-mono text-[11.5px] text-console-ink-dim">
                        {r.list ? (
                          <ul data-testid="deck-evidence-list" className="space-y-0.5">
                            {r.list.entries.map((entry, i) => (
                              <li key={`${entry.prefix}${entry.name}#${i}`}>
                                {entry.prefix && (
                                  // Provenance, quieter than the name it belongs to —
                                  // the correction picker's split, reused rather than
                                  // re-invented.
                                  <span className="text-console-ink-faint">{entry.prefix}</span>
                                )}
                                {entry.name}
                              </li>
                            ))}
                            {r.list.total > r.list.entries.length && (
                              // ILB: a capped list SAYS it was capped. Silently drawing
                              // the first eight of twelve is a card that lies by omission.
                              <li data-testid="deck-evidence-list-more" className="italic text-console-ink-faint">
                                +{r.list.total - r.list.entries.length} more
                              </li>
                            )}
                          </ul>
                        ) : (
                          r.value
                        )}
                      </dd>
                    </div>
                  ))}
                </dl>
              )}
            </div>
          </div>
        )}

        {/* THE VERB STRIP — the card advertises its own grammar, so there is
            nothing left to memorise (F3's fix built into the render rather than
            documented). Each cell names its gesture and its verb, and is drawn
            in that verb's role: right affirms, left declines, up defers (D3). */}
        <div className="-mx-4 -mb-4 mt-auto flex shrink-0 border-t border-console-edge bg-console-raise">
          <span
            data-testid="deck-verb-reject"
            className={`flex-1 border-r border-console-void px-2 py-2.5 text-center ${CONSOLE_LABEL} ${
              verbs?.reject ? ROLE_TEXT_CLASS.negative : verbs?.rejectDefers ? ROLE_TEXT_CLASS.caution : 'text-console-ink-ghost'
            }`}
          >
            {verbs?.reject || verbs?.rejectDefers ? `← ${verbs.rejectLabel}` : '—'}
          </span>
          <span
            data-testid="deck-verb-snooze"
            className={`flex-1 border-r border-console-void px-2 py-2.5 text-center ${CONSOLE_LABEL} ${ROLE_TEXT_CLASS.caution}`}
          >
            ↑ Snooze
          </span>
          <span
            data-testid="deck-verb-affirm"
            className={`flex-1 px-2 py-2.5 text-center ${CONSOLE_LABEL} ${
              verbs?.affirm ? ROLE_TEXT_CLASS.affirm : 'text-console-ink-ghost'
            }`}
          >
            {verbs?.affirm ? `${affirmLabel} →` : '—'}
          </span>
        </div>
      </div>

      {/* Verdict stamps — Deck sets their opacity imperatively during a drag.
          Each is its verdict's role, which is what makes the axis legible
          mid-gesture: teal to the right, rust to the left, amber upward. */}
      <span
        data-stamp="affirm"
        className={`pointer-events-none absolute right-4 top-4 rotate-[-8deg] rounded-sm border-2 px-2.5 py-1 text-sm font-extrabold uppercase tracking-[0.18em] opacity-0 ${roleChipClass('affirm')}`}
      >
        {priority ? priority : urgent ? affirmLabel : item.kind === 'slot_suggestion' ? 'Take it' : holdChoices ? affirmLabel : affirmHeavy ? 'Review' : 'Yes'}
      </span>
      <span
        data-stamp="reject"
        className={`pointer-events-none absolute left-6 top-4 rotate-[8deg] rounded-sm border-2 px-2.5 py-1 text-sm font-extrabold uppercase tracking-[0.18em] opacity-0 ${roleChipClass('negative')}`}
      >
        {verbs?.rejectDefers ? 'Skip' : rejectHeavy ? 'Review' : 'No'}
      </span>
      <span
        data-stamp="snooze"
        className={`pointer-events-none absolute left-1/2 top-4 -ml-10 rounded-sm border-2 px-2.5 py-1 text-sm font-extrabold uppercase tracking-[0.18em] opacity-0 ${roleChipClass('caution')}`}
      >
        Snooze
      </span>

      {confirming && (
        <div
          data-testid="deck-confirm"
          className="absolute inset-0 flex flex-col items-center justify-center gap-3.5 bg-console-panel p-5 text-center"
        >
          {/* D4: the arm shows exactly what will be written. The old copy asked
              "Write this to the vault?" for every heavy verb, which is not what
              an attribution REJECT does — it removes text. A verb whose
              consequence is stated wrongly is worse than one stated vaguely. */}
          <p className={CONSOLE_LABEL}>{verbLabelFor(verbs, confirmingVerdict ?? 'affirm')} this?</p>
          <p className="text-base font-bold text-console-ink">{item.title || item.id}</p>
          {armNote ? (
            <p data-testid="deck-confirm-note" className={`text-xs font-semibold ${ROLE_TEXT_CLASS[armRole]}`}>
              {armNote}
            </p>
          ) : (
            // ILB: a heavy verb with no declared consequence says so, rather
            // than reading as a reassuring blank.
            <p data-testid="deck-confirm-note-missing" className="text-xs text-console-ink-dim">
              This makes a durable change to the vault. The exact change isn’t described for this card type.
            </p>
          )}
          <div className="flex gap-3">
            <button
              type="button"
              data-testid="deck-confirm-yes"
              className={`rounded-sm px-4 py-2.5 text-xs font-bold uppercase tracking-[0.14em] ${
                armRole === 'negative' ? 'bg-negative text-console-ink' : 'bg-caution text-console-on-fill'
              }`}
              onClick={onConfirmHeavy}
            >
              {verbLabelFor(verbs, confirmingVerdict ?? 'affirm') || 'Confirm'}
            </button>
            <button
              type="button"
              data-testid="deck-confirm-cancel"
              onClick={onCancelHeavy}
              className="rounded-sm border border-console-edge-bright px-4 py-2.5 text-xs font-bold uppercase tracking-[0.14em] text-console-ink-dim"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
});
