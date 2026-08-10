import { useState } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import { CONTEST_SECTIONS, SNOOZE_ACTIONS, SNOOZE_LABELS, contestSectionSlug, kindLabel, snoozeIsBacked } from '../../lib/algernon/feedConstants';
import { evidenceBody, evidenceExternalLink, evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';
import { COMPLETION_UNAVAILABLE_HINT, effectiveStageOf, ringItemCompletable, ringItemUndoable, type RingItemStage } from '../../lib/algernon/rings';
import type { UseRingCompletionResult } from './useRingCompletion';
import type { UseSlotAcceptResult } from './useSlotAccept';
import type { UseSnoozeResult } from './useSnooze';
import { EvidenceBody } from './EvidenceBody';

// One feed row. Content is escaped React text only (evidence is untrusted display
// data — no innerHTML, no href). The right-hand affordance is one of: an Ack
// (FYI rows), or the SAME per-STAGE slot affordance the rings panel uses (slot rows
// — SUGGESTED→Accept / PLANNED→✓ / DONE→marker+undo, via the shared hooks, never a
// second implementation), or nothing.

export interface FeedRowProps {
  item: FeedItem;
  expanded: boolean;
  onToggleEvidence: () => void;
  /** Present → an Ack button (FYI rows). */
  onAck?: () => void;
  /**
   * Present → this FYI row offers the #63a contest door ("Not right"). Supplied
   * only for attribution rows (see `contestableItem`), which are the one kind the
   * backend admits `contest` for.
   *
   * Rendered ALONGSIDE the Ack rather than replacing it: Ack means "seen, fine"
   * and contest means "seen, wrong", and the ruling that demoted these cards
   * depends on both being one tap away — the glance tier is only safe while
   * disagreeing stays as cheap as agreeing.
   *
   * The optional `section` (#72 item 4) is the capture-summary heading the
   * operator named via the picker below. WHY THE PICKER IS A SECOND CONTROL
   * rather than an expansion behind "Not right": the section has to ride the
   * contest act itself (the server writes the corpus row only in the branch
   * that flips the vault entry, and a second contest is an idempotent no-op
   * that records nothing), so a picker in front of "Not right" would make every
   * card-level contest cost two taps. That directly contradicts the invariant
   * the demotion rests on — disagreeing must stay as cheap as agreeing. The
   * house precedent is routine_match: the left-swipe reject says "not that
   * item" and stops there, while the richer "what did this mean?" doors sit one
   * tap further in.
   */
  onContest?: (section?: string) => void;
  /**
   * Present → this is a slot row: render the per-STAGE affordance (SUGGESTED→Accept /
   * PLANNED→✓ / DONE→marker+undo) driven by the SHARED hooks (the identical per-lane
   * logic the rings panel uses). Takes precedence over onAck.
   */
  completion?: UseRingCompletionResult;
  /**
   * Present alongside `completion` → the C2 accept hook, driving the SUGGESTED
   * candidate's [Accept] control + its optimistic candidate→planned flip.
   */
  accept?: UseSlotAcceptResult;
  /**
   * Present → this row offers the #14 defer verb: a LABELLED `Snooze` button
   * opening the same four durations the deck's hold-band menu offers. Labelled
   * on purpose — the ruling is that Skip and Snooze each carry their own visible
   * word, and no control means both.
   *
   * Ignored on a kind the backend can't snooze; the control simply isn't drawn,
   * because a duration with no store behind it is a promise that gets broken at
   * the next sync.
   */
  snooze?: UseSnoozeResult;
}

export function FeedRow({ item, expanded, onToggleEvidence, onAck, onContest, completion, accept, snooze }: FeedRowProps) {
  // The row's own duration menu. Local because it is per-row transient UI, not
  // board state — nothing outside this row needs to know it's open.
  const [snoozeMenuOpen, setSnoozeMenuOpen] = useState(false);
  // Same reasoning for the #72 section picker: transient, per-row, never lifted.
  const [sectionPickerOpen, setSectionPickerOpen] = useState(false);
  const rows = evidenceRows(item.evidence);
  // A digest/prose body (peer_digest et al.) OR a safe external link (the email
  // "Open in Gmail" deep-link, #26) also makes the row expandable, even when it has
  // no key:value rows of its own.
  const hasBody = evidenceBody(item.evidence) !== null;
  const hasDetails = rows.length > 0 || hasBody || evidenceExternalLink(item.evidence) !== null;
  // C2 stage overlay — the shared rings.ts seam (#16 item 8). A row with no completion
  // context has no stage badge at all (null); otherwise it's the same overlay the rings
  // panel uses, from one implementation.
  const stage: RingItemStage | null = completion ? effectiveStageOf(item, completion, accept) : null;
  const done = stage === 'done';
  const suggested = stage === 'suggested';
  const busy = completion ? completion.busy(item.id) : false;
  const acceptBusy = accept ? accept.busy(item.id) : false;
  const completable = completion ? ringItemCompletable(item) : false;
  const undoable = completion ? ringItemUndoable(item) : false;
  const completionError = (completion ? completion.errorFor(item.id) : null) ?? (accept ? accept.errorFor(item.id) : null);
  // The defer verb is offered on a row that is still WORK: a done row has
  // nothing to put off (the router refuses it outright), and an already-snoozed
  // row's affordance is Unsnooze, which the staged section draws instead.
  const snoozeBusy = snooze ? snooze.busy(item.id) : false;
  const snoozeError = snooze ? snooze.errorFor(item.id) : null;
  const snoozeOffered = !!snooze && snoozeIsBacked(item) && !done && !snooze.snoozed(item.id);
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
          // Slot — the SAME per-STAGE affordance as the rings panel.
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
          ) : suggested ? (
            // SUGGESTED — an auto-surfaced candidate not yet on the plan. The one
            // affordance is Accept (commit it); NO ✓. Optimistic candidate→planned on
            // a render-bearing 200 (useSlotAccept's render-present gate).
            <button
              type="button"
              data-testid="feed-row-accept"
              disabled={acceptBusy || !accept}
              onClick={() => accept?.accept(item)}
              className="shrink-0 rounded-lg border border-honeydew-500 bg-honeydew-50 px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-honeydew-700 disabled:opacity-50"
            >
              {acceptBusy ? '…' : 'Accept'}
            </button>
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
          <div className="flex shrink-0 items-center gap-1.5">
            {onContest && (
              <button
                type="button"
                data-testid="feed-row-contest"
                aria-label={`Not right — send back for review: ${item.title || item.id}`}
                onClick={() => onContest()}
                className="rounded-lg border border-honeydew-300 px-2 py-1.5 text-[11px] font-bold uppercase tracking-wider text-honeydew-600"
              >
                Not right
              </button>
            )}
            <button
              type="button"
              data-testid="feed-row-ack"
              aria-label={`Acknowledge: ${item.title || item.id}`}
              onClick={onAck}
              className="rounded-lg border border-honeydew-400 px-3 py-1.5 text-xs font-bold uppercase tracking-wider text-honeydew-600"
            >
              Ack
            </button>
          </div>
        ) : null}
      </div>

      {onContest && (
        <div className="mt-2 border-t border-dashed border-honeydew-200 pt-2">
          {!sectionPickerOpen ? (
            <button
              type="button"
              data-testid="feed-row-contest-which"
              aria-haspopup="true"
              aria-expanded={false}
              onClick={() => setSectionPickerOpen(true)}
              className="text-[11px] font-bold uppercase tracking-wider text-honeydew-600 underline underline-offset-2"
            >
              Which part was wrong?
            </button>
          ) : (
            <div data-testid="feed-row-contest-sections" className="flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] font-bold uppercase tracking-wider text-honeydew-600">
                Section
              </span>
              {CONTEST_SECTIONS.map((section) => (
                <button
                  key={section}
                  type="button"
                  data-testid={`feed-row-contest-section-${contestSectionSlug(section)}`}
                  aria-label={`Not right — ${section}: ${item.title || item.id}`}
                  onClick={() => {
                    setSectionPickerOpen(false);
                    onContest(section);
                  }}
                  className="rounded-lg border border-honeydew-400 px-2 py-1 text-[11px] font-semibold text-honeydew-700"
                >
                  {section}
                </button>
              ))}
              <button
                type="button"
                data-testid="feed-row-contest-section-cancel"
                onClick={() => setSectionPickerOpen(false)}
                className="text-[11px] font-semibold text-honeydew-600 underline underline-offset-2"
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      )}

      {snoozeOffered && (
        <div className="mt-2 border-t border-dashed border-honeydew-200 pt-2">
          {!snoozeMenuOpen ? (
            <button
              type="button"
              data-testid="feed-row-snooze"
              aria-haspopup="true"
              aria-expanded={false}
              disabled={snoozeBusy}
              onClick={() => setSnoozeMenuOpen(true)}
              className="text-[11px] font-bold uppercase tracking-wider text-status-progress-fg underline underline-offset-2 disabled:opacity-50"
            >
              {snoozeBusy ? 'Snoozing…' : 'Snooze'}
            </button>
          ) : (
            <div data-testid="feed-row-snooze-menu" className="flex flex-wrap items-center gap-1.5">
              <span className="text-[11px] font-bold uppercase tracking-wider text-honeydew-600">For</span>
              {SNOOZE_ACTIONS.map((action) => (
                <button
                  key={action}
                  type="button"
                  data-testid={`feed-row-snooze-${action}`}
                  disabled={snoozeBusy}
                  onClick={() => {
                    setSnoozeMenuOpen(false);
                    void snooze?.snooze(item, action);
                  }}
                  className="rounded-lg border border-honeydew-400 px-2 py-1 text-[11px] font-semibold text-honeydew-700 disabled:opacity-50"
                >
                  {SNOOZE_LABELS[action]}
                </button>
              ))}
              <button
                type="button"
                data-testid="feed-row-snooze-cancel"
                onClick={() => setSnoozeMenuOpen(false)}
                className="text-[11px] font-semibold text-honeydew-600 underline underline-offset-2"
              >
                Cancel
              </button>
            </div>
          )}
        </div>
      )}

      {snoozeError && (
        <p data-testid="feed-row-snooze-error" role="alert" className="mt-1 text-[11px] text-danger">
          {snoozeError}
        </p>
      )}
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
