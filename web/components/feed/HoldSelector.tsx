import { OVERLAY_Z_INDEX } from './deckOverlay';
import type { HoldChoice } from '../../lib/algernon/feedConstants';

// The hold-selector — the AFFIRM-WITH-HOLD-MODIFIER pattern's option sheet
// (operator ratification, 2026-08-19: "affirm as suggested, or swipe hold to
// change the suggestion while affirming"; platform-wide pattern, this is the
// reference implementation with the deck rotation's sort card as first
// consumer).
//
// GENERIC BY CONSTRUCTION. It renders `HoldChoice[]` — the wire-derived
// co-equal family from `holdChoicesFor` — and knows nothing about kinds,
// slots, or verbs beyond their labels. A future suggestion-bearing kind
// reaches this component by serving a `group` on its verbs; nothing here
// changes. Direction-generality: the choices are computed per DIRECTION by
// the caller (`holdChoicesFor(item, 'affirm' | 'reject')`); this sheet is
// direction-blind. Only the affirm side is wired today (reject-hold is
// ratified-in-principle, not built).
//
// THE ONE-INTERACTION INVARIANT (the ruling's second clause, pinned in
// deckHoldSelector.test.tsx): choosing from this sheet IS the affirm — the
// pick routes through the SAME delayed-act commit the plain gesture uses
// (`useDeck.affirmWith`), with the same undo window, and NEVER a second
// confirm stage between the pick and the act. Cancel is the only non-commit
// exit, and it commits nothing.
//
// Visually a bottom sheet attached to the frozen gesture, exactly like the #14
// snooze duration menu (this family's prior art): the card underneath holds
// the offset it was held at, so the sheet reads as part of the gesture rather
// than a dialog from nowhere.

export interface HoldSelectorProps {
  /** Sheet title — names the decision, e.g. "Sort into". */
  title: string;
  /** The co-equal choices, suggested member marked (from `holdChoicesFor`). */
  choices: HoldChoice[];
  /** Commit THIS choice — the one interaction. */
  /**
   * Commit the pick. ``target`` is the open-set family's chosen value and is
   * undefined for a static family, where the VERB is the whole choice.
   */
  onPick: (verb: string, target?: string) => void;
  /** Close without committing anything; the frozen card springs back. */
  onCancel: () => void;
  /**
   * The footer's undo promise, overridable because it is SURFACE truth, not
   * pattern truth: the deck's picks ride the delayed-act cancel window, while
   * the board's ✓-hold posts straight away with a real reversing Undo on the
   * row — same one-interaction commit, different way back, and the sentence
   * must say the one that is true where it renders.
   */
  note?: string;
}

export function HoldSelector({
  title,
  choices,
  onPick,
  onCancel,
  note = 'Choosing records it straight away — same undo window as a swipe.',
}: HoldSelectorProps) {
  return (
    <div
      data-testid="deck-hold-selector"
      role="dialog"
      aria-label={title}
      style={{ zIndex: OVERLAY_Z_INDEX }}
      className="absolute inset-x-0 bottom-0 flex flex-col rounded-sm border-t-2 border-affirm bg-console-panel p-4"
    >
      <div className="mb-2 flex items-center justify-between">
        <p className="text-[11px] font-bold uppercase tracking-[0.18em] text-affirm">
          {title}
        </p>
        <button
          type="button"
          data-testid="deck-hold-cancel"
          onClick={onCancel}
          className="text-[11px] font-bold uppercase tracking-[0.14em] text-console-ink-faint underline underline-offset-4"
        >
          Cancel
        </button>
      </div>
      {/*
        KEYED ON THE TARGET WHEN THERE IS ONE. A STATIC family's members are
        distinct verbs, so the verb identifies the row. An OPEN-SET family's
        members all share one verb (the anchor's) and differ only by target —
        so keying on the verb gives every row the SAME key and the SAME
        testid: React logs a duplicate-key warning and no test can address an
        individual row. `c.target ?? c.verb` is the identity of a choice on
        either arm.
      */}
      <div className="flex flex-col gap-2">
        {choices.map((c) => (
          <button
            key={c.target ?? c.verb}
            type="button"
            data-testid={`deck-hold-choice-${c.target ?? c.verb}`}
            onClick={() => onPick(c.verb, c.target)}
            className="flex items-baseline justify-between rounded-sm border border-console-edge-bright bg-console-raise px-3 py-2 text-left text-sm font-semibold text-console-ink hover:border-affirm hover:text-affirm"
          >
            <span>{c.label}</span>
            {c.suggested && (
              // The suggested member is MARKED, not promoted: same row style,
              // same tap, one quiet tag — co-equality is the taxonomy's rule
              // and the sheet must not re-rank what the group refuses to.
              <span
                data-testid="deck-hold-suggested"
                className="text-[10px] font-bold uppercase tracking-[0.14em] text-console-ink-faint"
              >
                Suggested
              </span>
            )}
          </button>
        ))}
      </div>
      <p className="mt-2 text-[11px] italic text-console-ink-faint">
        {note}
      </p>
    </div>
  );
}
