import { useCallback, useEffect, useMemo, useState } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import { UNDO_MS } from '../../lib/algernon/feedConstants';
import { evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';
import {
  BOARD_UNSLOTTED,
  boardSlotsWithADone,
  boardStacks,
  carryoverReason,
  type BoardStack,
} from '../../lib/algernon/board';
import {
  COMPLETION_UNAVAILABLE_HINT,
  effectiveStageOf,
  ringItemCompletable,
  ringItemUndoable,
  type RingItemStage,
} from '../../lib/algernon/rings';
import { RingsHeader } from './RingsHeader';
import { useBoardGrace } from './useBoardGrace';
import type { UseRingCompletionResult } from './useRingCompletion';
import { useSlotAccept } from './useSlotAccept';

// ═══════════ THE DAY BOARD — home's top module (Phase C, lane C1) ═══════════
//
// Rings headline (tier — "when does this press") over three co-equal slot stacks
// (Duty / Rhythm / Fuel — "what does this do for the day"). The two axes are
// orthogonal and both render; see `lib/algernon/board.ts` for why this is not a
// relabelling of T1/T2/T3.
//
// This is a RENDER of machinery that already works. Every write it performs goes
// through hooks that already existed — `useRingCompletion` for done/undo,
// `useSlotAccept` for accept — and every field it groups, ranks or explains by is
// one the producer already stamps. No endpoint was added for this surface.
//
// Per stack, in the operator's own planning order:
//   today's commitments → carryover (capped, attention-ranked) → 1-3 candidates
//   → done (drill) → the long tail (browse-on-swap).
//
// TODO(voicing): all operator-facing copy here is a plain placeholder. The
// permission-granting no-shame voice is the prompt-tuner's; the signal ships now.

export interface SlotBoardProps {
  /** Today's feed items (the full set, incl. acted — the board date-scopes). */
  items: FeedItem[] | null;
  /** The host page's ONE completion instance, shared with the rings + counts. */
  completion: UseRingCompletionResult;
  onAuthExpired?: () => void;
  /** Test seam: "now" for the today date-scope (defaults to the real clock). */
  now?: Date;
}

export function SlotBoard({ items, completion, onAuthExpired, now: nowProp }: SlotBoardProps) {
  const accept = useSlotAccept({ onAuthExpired });
  const grace = useBoardGrace(completion);
  const now = useMemo(() => nowProp ?? new Date(), [nowProp]);
  const [openDone, setOpenDone] = useState<ReadonlySet<string>>(() => new Set());
  const [openBrowse, setOpenBrowse] = useState<ReadonlySet<string>>(() => new Set());
  const [openItemId, setOpenItemId] = useState<string | null>(null);

  // The one stage seam for the board: the grace window's optimistic done wins
  // over everything (nothing has been written yet, but the operator has decided),
  // then the shared completion/accept overlay resolves the rest. `effectiveStageOf`
  // stays the single owner of that precedence — this only layers the board's own
  // held write on top of it.
  const stageOf = useCallback(
    (it: FeedItem): RingItemStage =>
      grace.optimisticallyDone(it) ? 'done' : effectiveStageOf(it, completion, accept),
    [grace, completion, accept],
  );

  // Retire the board's own optimistic flags that a fresh render has answered —
  // the page already does this for the shared completion hook, and a held write
  // whose result is now in the list has nothing left to add.
  useEffect(() => {
    if (items) grace.reconcile(items);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items]);

  // A row completed HERE stays where it is (green, struck) rather than jumping
  // into the collapsed done drill — completion is a stage, not a disappearance,
  // and during the grace window the row the operator may want back must be on
  // screen. `optimisticallyDone` is exactly "this board completed it this
  // session"; once a fresh render confirms it, reconcile drops the flag and the
  // item settles into the drill.
  const stacks = useMemo(
    () => boardStacks(items ?? [], stageOf, now, { stayInPlace: grace.optimisticallyDone }),
    [items, stageOf, now, grace.optimisticallyDone],
  );
  const balanced = boardSlotsWithADone(stacks);
  const totalOnBoard = stacks.reduce(
    (n, s) => n + s.today.length + s.carryover.length + s.candidates.length + s.done.length + s.overflow.length,
    0,
  );

  const toggle = (setter: (fn: (prev: ReadonlySet<string>) => ReadonlySet<string>) => void, key: string) => {
    setter((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const renderRow = (it: FeedItem, opts: { reason?: string | null } = {}) => {
    const stage = stageOf(it);
    const done = stage === 'done';
    const suggested = stage === 'suggested';
    const pending = grace.pendingId === it.id;
    const completable = ringItemCompletable(it);
    const undoable = ringItemUndoable(it);
    const compBusy = completion.busy(it.id);
    const acceptBusy = accept.busy(it.id);
    const itemError = completion.errorFor(it.id) ?? accept.errorFor(it.id);
    const notice = completion.noticeFor(it.id);
    const rows = evidenceRows(it.evidence);
    const expanded = openItemId === it.id;
    const dot = done ? 'bg-status-done-fg' : suggested ? 'bg-honeydew-400' : 'bg-status-progress-fg';

    return (
      <li
        key={it.id}
        data-testid="board-item"
        data-stage={stage}
        data-pending={pending}
        className="border-t border-dashed border-honeydew-200 pt-2 first:border-0 first:pt-0"
      >
        <div className="flex items-start justify-between gap-2">
          <button
            type="button"
            data-testid="board-item-row"
            onClick={() => setOpenItemId((cur) => (cur === it.id ? null : it.id))}
            aria-expanded={expanded}
            className="flex min-w-0 flex-1 items-start gap-2 text-left"
          >
            <span aria-hidden className={`mt-1 h-2 w-2 shrink-0 rounded-full ${dot}`} />
            <span className="min-w-0">
              <span className={`block break-words text-sm font-semibold text-honeydew-700 ${done ? 'line-through opacity-70' : ''}`}>
                {it.title || it.id}
              </span>
              {opts.reason && (
                // WHY this is still on the board — the question a carried item
                // provokes, answered in place rather than left mysterious.
                <span data-testid="board-item-reason" className="mt-0.5 block text-[11px] text-honeydew-600">
                  {opts.reason}
                </span>
              )}
            </span>
          </button>

          {done ? (
            <div className="flex shrink-0 items-center gap-1.5">
              <span data-testid="board-item-done" className="text-[10px] font-bold uppercase tracking-wider text-status-done-fg">
                ✓ Done
              </span>
              {/* No row-level Undo while the write is still HELD: the toast's
                  Undo cancels it, and a control here would POST `undo_done` for
                  something that was never written. */}
              {!pending && undoable && (
                <button
                  type="button"
                  data-testid="board-undo"
                  disabled={compBusy}
                  onClick={() => completion.undo(it)}
                  className="rounded-lg border border-honeydew-300 px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-honeydew-600 disabled:opacity-50"
                >
                  {compBusy ? '…' : 'Undo'}
                </button>
              )}
            </div>
          ) : suggested ? (
            <button
              type="button"
              data-testid="board-accept"
              disabled={acceptBusy}
              onClick={() => accept.accept(it)}
              className="shrink-0 rounded-lg border border-honeydew-500 bg-honeydew-50 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-honeydew-700 disabled:opacity-50"
            >
              {acceptBusy ? '…' : 'Accept'}
            </button>
          ) : completable ? (
            <button
              type="button"
              data-testid="board-complete"
              disabled={compBusy}
              onClick={() => grace.tap(it)}
              className="shrink-0 rounded-lg border border-honeydew-400 px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-honeydew-700 disabled:opacity-50"
            >
              {compBusy ? '…' : '✓ Done'}
            </button>
          ) : (
            // Honestly DISABLED — an unknown / unstamped lane has no writer, and a
            // control that looks live and does nothing is the failure this whole
            // surface was built to end. `disabled` and `opacity-50` are pinned
            // together so un-disabling forces a conscious restyle.
            <button
              type="button"
              data-testid="board-complete"
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
          <p data-testid="board-item-error" role="alert" className="mt-1 pl-4 text-[11px] text-danger">
            {itemError}
          </p>
        )}
        {!itemError && notice && (
          <p data-testid="board-item-notice" className="mt-1 pl-4 text-[11px] text-honeydew-600">
            {notice}
          </p>
        )}

        {expanded && rows.length > 0 && (
          <dl data-testid="board-item-evidence" className="mt-1.5 space-y-1 pl-4 text-xs text-honeydew-600">
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

  const renderStack = (stack: BoardStack) => {
    const residue = stack.key === BOARD_UNSLOTTED;
    const showDone = openDone.has(stack.key);
    const browsing = openBrowse.has(stack.key);
    const nothing =
      stack.today.length === 0 &&
      stack.carryover.length === 0 &&
      stack.candidates.length === 0 &&
      stack.done.length === 0 &&
      stack.overflow.length === 0;

    return (
      <section
        key={stack.key}
        data-testid={`board-stack-${stack.key}`}
        data-residue={residue}
        className={`rounded-xl border p-3 shadow-soft ${residue ? 'border-dashed border-honeydew-200 bg-honeydew-50/40' : 'border-honeydew-200 bg-cream'}`}
      >
        <h3 className="flex items-baseline justify-between gap-2 text-xs font-bold uppercase tracking-wider text-honeydew-700">
          <span data-testid={`board-stack-label-${stack.key}`}>{stack.label}</span>
          {!residue && stack.committedCount > 0 && (
            <span data-testid={`board-stack-score-${stack.key}`} className="font-mono text-[11px] font-semibold tracking-normal text-honeydew-600">
              {stack.doneCount}/{stack.committedCount} done
            </span>
          )}
        </h3>

        {residue && (
          // The classifier refused to guess, and says so. These are excluded from
          // every count above — an unreachable daily goal would read as personal
          // failure rather than as an unclassified item.
          <p data-testid="board-residue-note" className="mt-1 text-[11px] text-honeydew-600">
            No slot rule matched these yet — they don&rsquo;t count for or against your day.
          </p>
        )}

        {nothing ? (
          // Intentionally-left-blank: an empty stack is a fact about the day, and
          // an empty box that says nothing is indistinguishable from a broken one.
          <p data-testid={`board-stack-empty-${stack.key}`} className="mt-2 text-xs text-honeydew-600">
            Nothing here today.
          </p>
        ) : (
          <>
            {stack.today.length > 0 && (
              <ul data-testid={`board-today-${stack.key}`} className="mt-2 flex flex-col gap-2">
                {stack.today.map((it) => renderRow(it))}
              </ul>
            )}

            {stack.carryover.length > 0 && (
              <div className="mt-3">
                <p className="text-[10px] font-bold uppercase tracking-wider text-honeydew-600">
                  Carried over
                </p>
                <ul data-testid={`board-carryover-${stack.key}`} className="mt-1.5 flex flex-col gap-2">
                  {stack.carryover.map((it) => renderRow(it, { reason: carryoverReason(it, now) }))}
                </ul>
              </div>
            )}

            {stack.candidates.length > 0 && (
              <div className="mt-3">
                <p className="text-[10px] font-bold uppercase tracking-wider text-honeydew-600">
                  Worth considering
                </p>
                <ul data-testid={`board-candidates-${stack.key}`} className="mt-1.5 flex flex-col gap-2">
                  {stack.candidates.map((it) => renderRow(it))}
                </ul>
              </div>
            )}

            {stack.today.length === 0 && stack.carryover.length === 0 && stack.candidates.length === 0 && (
              // Everything in this slot is done (or demoted). Say which, rather
              // than rendering a box whose only content is a drill-down button.
              <p data-testid={`board-stack-clear-${stack.key}`} className="mt-2 text-xs font-semibold text-status-done-fg">
                Nothing left here today.
              </p>
            )}

            {stack.done.length > 0 && (
              <div className="mt-3">
                <button
                  type="button"
                  data-testid={`board-show-done-${stack.key}`}
                  onClick={() => toggle(setOpenDone, stack.key)}
                  aria-expanded={showDone}
                  className="text-[11px] font-semibold uppercase tracking-wider text-honeydew-600 underline underline-offset-2"
                >
                  {showDone ? 'Hide done' : `Show done (${stack.done.length})`}
                </button>
                {showDone && (
                  <ul data-testid={`board-done-${stack.key}`} className="mt-2 flex flex-col gap-2">
                    {stack.done.map((it) => renderRow(it))}
                  </ul>
                )}
              </div>
            )}

            {stack.overflow.length > 0 && (
              // BROWSE-ON-SWAP: the caps demote, they never drop. Everything past
              // a cap is one tap away, so the board stays short without the
              // operator having to wonder what it decided not to show him.
              <div className="mt-3">
                <button
                  type="button"
                  data-testid={`board-browse-${stack.key}`}
                  onClick={() => toggle(setOpenBrowse, stack.key)}
                  aria-expanded={browsing}
                  className="text-[11px] font-semibold uppercase tracking-wider text-honeydew-600 underline underline-offset-2"
                >
                  {browsing ? 'Hide the rest' : `Browse the rest (${stack.overflow.length})`}
                </button>
                {browsing && (
                  <ul data-testid={`board-overflow-${stack.key}`} className="mt-2 flex flex-col gap-2">
                    {stack.overflow.map((it) => renderRow(it, { reason: carryoverReason(it, now) }))}
                  </ul>
                )}
              </div>
            )}
          </>
        )}
      </section>
    );
  };

  return (
    <section aria-label="Your day" data-testid="slot-board">
      {/* The rings stay the headline: they are the URGENCY glance, and they are
          a different question from the slots below, not a smaller version of it. */}
      <RingsHeader items={items ?? []} completion={completion} now={nowProp} onAuthExpired={onAuthExpired} />

      {items == null ? (
        // Intentionally-left-blank: an explicit loading signal, not a blank board.
        <p data-testid="board-loading" className="mt-3 text-sm text-honeydew-600">
          Loading your day…
        </p>
      ) : (
        <>
          <p data-testid="board-balance" className="mt-3 text-xs text-honeydew-600">
            {totalOnBoard === 0
              ? // Intentionally-left-blank: three empty stacks could read as a
                // broken board — say that it ran and found nothing.
                'Nothing on the board yet today — it fills as the day’s items surface.'
              : `${balanced} of 3 slots have something done.`}
          </p>
          <div data-testid="board-stacks" className="mt-2 flex flex-col gap-3">
            {stacks.map(renderStack)}
          </div>
        </>
      )}

      {grace.toast && (
        <div
          data-testid="board-toast"
          role="status"
          className="fixed inset-x-0 bottom-20 z-50 mx-auto flex w-fit items-center gap-3.5 overflow-hidden rounded-xl bg-honeydew-700 px-3.5 py-2.5 text-sm text-cream shadow-card"
        >
          <span>Done: {grace.toast.title}</span>
          <button
            type="button"
            data-testid="board-toast-undo"
            onClick={grace.undo}
            className="text-[11px] font-bold uppercase tracking-[0.14em] underline underline-offset-4"
          >
            Undo
          </button>
          {/* The remaining time, on screen (D8). Duration is READ FROM `UNDO_MS`,
              never re-typed, so the bar and the timer that actually fires the
              write cannot disagree. */}
          <span
            data-testid="board-toast-bar"
            aria-hidden="true"
            style={{ animationDuration: `${UNDO_MS}ms` }}
            className="deck-undo-bar absolute inset-x-0 bottom-0 h-[3px] origin-left bg-cream/70"
          />
        </div>
      )}
    </section>
  );
}
