import { useCallback, useEffect, useMemo, useState } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import { UNDO_MS } from '../../lib/algernon/feedConstants';
import { evidenceLabel, evidenceRows } from '../../lib/algernon/feedEvidence';
import {
  BOARD_UNSLOTTED,
  boardCoverage,
  boardCoverageIsLow,
  boardSlotsWithADone,
  boardStackSize,
  boardStacks,
  carryoverReason,
  SLOT_EMPTY_COPY,
  SLOT_EMPTY_FALLBACK,
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
// VOICE: every operator-facing string here is written to the permission-granting
// no-shame register — facts about the day, never verdicts about the operator; a
// shortfall names the MECHANISM as its subject. The rule and its vocabulary
// (Duty=obligation, Rhythm=practice, Fuel=restoration) live at the top of
// `lib/algernon/board.ts` — as does THE FENCE, which bars any slot-based GOAL
// claim while the balanced-day metric is still tier-based, names the two
// adjudicated exceptions to it, and carries the instruction for whoever ships
// the stage-3 flip. Read it before editing any copy below.

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
  const [openSnoozed, setOpenSnoozed] = useState<ReadonlySet<string>>(() => new Set());
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
  // Which unrecorded rows are still reachable on this board — read from the
  // STACKS rather than from `items`, so the sentence is about what the operator
  // can actually get to rather than about what the fetch returned.
  const onBoard = useMemo(() => {
    const ids = new Set<string>();
    for (const s of stacks) {
      // `snoozed` included: this set answers "can he still get to this row?", and
      // a snoozed row IS on the board (one drill in). Omitting it would tell him
      // an unrecorded ✓ "isn't on the board now" about a row sitting right there.
      for (const bucket of [s.today, s.carryover, s.candidates, s.done, s.snoozed, s.overflow]) {
        for (const it of bucket) ids.add(it.id);
      }
    }
    return ids;
  }, [stacks]);
  // The residue stack exists only when something went unclassified — see the
  // ILB line below for what renders in its place when it doesn't.
  const hasResidue = stacks.some((s) => s.key === BOARD_UNSLOTTED);
  const totalOnBoard = stacks.reduce((n, s) => n + boardStackSize(s), 0);
  // How much of today the classifier could answer for. Derived from the same
  // stacks rendered below, so the warning and the board can never disagree.
  const coverage = boardCoverage(stacks);
  const coverageLow = boardCoverageIsLow(coverage);

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
    // DELAYED, and rendered as delayed. Not struck through (the strikethrough
    // below is gated on `done` alone), not counted, and — the part that matters
    // most on a row — carrying no ✓ control, because offering "mark it done" on
    // something the operator just pushed out of today is the same claim the
    // struck-through row was making, in a button.
    const snoozed = stage === 'snoozed';
    const pending = grace.pendingId === it.id;
    const completable = ringItemCompletable(it);
    const undoable = ringItemUndoable(it);
    const compBusy = completion.busy(it.id);
    const acceptBusy = accept.busy(it.id);
    const itemError = completion.errorFor(it.id) ?? accept.errorFor(it.id);
    const unrecordedHere = grace.unrecordedIds.has(it.id);
    const notice = completion.noticeFor(it.id);
    const rows = evidenceRows(it.evidence);
    const expanded = openItemId === it.id;
    const dot = done
      ? 'bg-status-done-fg'
      : snoozed
        ? 'bg-caution'
        : suggested
          ? 'bg-console-ink-faint'
          : 'bg-status-progress-fg';

    return (
      <li
        key={it.id}
        data-testid="board-item"
        data-stage={stage}
        data-pending={pending}
        className="border-t border-dashed border-console-edge pt-2 first:border-0 first:pt-0"
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
              <span className={`block break-words text-sm font-semibold text-console-ink ${done ? 'line-through opacity-70' : ''}`}>
                {it.title || it.id}
              </span>
              {opts.reason && (
                // WHY this is still on the board — the question a carried item
                // provokes, answered in place rather than left mysterious.
                <span data-testid="board-item-reason" className="mt-0.5 block text-[11px] text-console-ink-dim">
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
                  className="rounded-lg border border-console-edge px-2 py-1 text-[10px] font-bold uppercase tracking-wider text-console-ink-dim disabled:opacity-50"
                >
                  {compBusy ? '…' : 'Undo'}
                </button>
              )}
            </div>
          ) : snoozed ? (
            // A STATEMENT, not a control. The board has no snooze hook wired, so
            // there is nothing here that could take the tap back — and a live-
            // looking button with no writer behind it is the dead-control failure
            // this surface was built to end. Unsnooze lives on the feed's staged
            // list, which does hold the hook. Wiring it here too is a follow-up,
            // declared rather than faked.
            <span
              data-testid="board-item-snoozed"
              className="shrink-0 text-[10px] font-bold uppercase tracking-wider text-caution"
            >
              Snoozed
            </span>
          ) : suggested ? (
            <button
              type="button"
              data-testid="board-accept"
              disabled={acceptBusy}
              onClick={() => accept.accept(it)}
              className="shrink-0 rounded-lg border border-console-edge-bright bg-console-raise px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-console-ink disabled:opacity-50"
            >
              {acceptBusy ? '…' : 'Accept'}
            </button>
          ) : completable ? (
            <button
              type="button"
              data-testid="board-complete"
              disabled={compBusy}
              onClick={() => grace.tap(it)}
              className="shrink-0 rounded-lg border border-console-edge-bright px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-console-ink disabled:opacity-50"
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
              className="shrink-0 cursor-default rounded-lg border border-console-edge px-2.5 py-1 text-[10px] font-bold uppercase tracking-wider text-console-ink-faint opacity-50"
            >
              ✓ Done
            </button>
          )}
        </div>

        {/* A row carrying an unrecorded ✓ shows THAT and not the transient error
            line, which reports the same failure in weaker words and disappears
            at the next poll (the shared hook supersedes its own overrides once
            a render answers them). Same call the deck made when it stopped
            toasting a refusal: one statement of a failure, and the durable one
            wins. The line below is the sentence; the notice above the board
            carries the server's own words and the row's name. */}
        {itemError && !unrecordedHere && (
          <p data-testid="board-item-error" role="alert" className="mt-1 pl-4 text-[11px] text-danger">
            {itemError}
          </p>
        )}
        {unrecordedHere && (
          // Said HERE, at the point of decision, because the notice above is
          // read once and then the operator is looking at rows — and the thing
          // they must know before moving on is that this one didn't take.
          // Permission-granting, not a nag: the row is still tappable, and when
          // they get to it is their business.
          <p data-testid="board-item-unrecorded" className="mt-1 pl-4 text-[11px] text-negative">
            Your ✓ wasn&rsquo;t recorded — mark it again whenever you like.
          </p>
        )}
        {!itemError && !unrecordedHere && notice && (
          <p data-testid="board-item-notice" className="mt-1 pl-4 text-[11px] text-console-ink-dim">
            {notice}
          </p>
        )}

        {expanded && rows.length > 0 && (
          <dl data-testid="board-item-evidence" className="mt-1.5 space-y-1 pl-4 text-xs text-console-ink-dim">
            {rows.map((r) => (
              <div key={r.key} className="flex gap-2">
                <dt className="shrink-0 font-semibold text-console-ink">{evidenceLabel(r.key)}:</dt>
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
    const showSnoozed = openSnoozed.has(stack.key);
    const browsing = openBrowse.has(stack.key);
    // `snoozed` is in this conjunction for the same reason it is in
    // `boardStackSize`: a stack holding three pushed duties is not an empty one,
    // and rendering SLOT_EMPTY_COPY ("Nothing owed today.") over them would erase
    // three real items — the same lie as "✓ Done", told by omission instead.
    const nothing =
      stack.today.length === 0 &&
      stack.carryover.length === 0 &&
      stack.candidates.length === 0 &&
      stack.done.length === 0 &&
      stack.snoozed.length === 0 &&
      stack.overflow.length === 0;

    return (
      <section
        key={stack.key}
        data-testid={`board-stack-${stack.key}`}
        data-residue={residue}
        className={`rounded-xl border p-3 shadow-soft ${residue ? 'border-dashed border-console-edge bg-console-raise' : 'border-console-edge bg-console-panel'}`}
      >
        <h3 className="flex items-baseline justify-between gap-2 text-xs font-bold uppercase tracking-wider text-console-ink">
          <span data-testid={`board-stack-label-${stack.key}`}>{stack.label}</span>
          {!residue && stack.committedCount > 0 && (
            <span data-testid={`board-stack-score-${stack.key}`} className="font-mono text-[11px] font-semibold tracking-normal text-console-ink-dim">
              {stack.doneCount}/{stack.committedCount} done
            </span>
          )}
        </h3>

        {residue && (
          // The classifier refused to guess, and says so. These are excluded from
          // every count above — an unreachable daily goal would read as personal
          // failure rather than as an unclassified item.
          //
          // ADJUDICATED EXCEPTION to the fence's bar on scoring vocabulary (see
          // `board.ts`, which names this site). "Count for or against" is the
          // RECKONING idiom — to be held for or against someone — not the
          // tallying sense the fence pulled from the scoreline. The distinction
          // is the whole value of the line: it does not say these are omitted
          // from a total, it says they cannot be held against him, which is the
          // one thing the operator needs to hear about work the classifier
          // failed on. It is also the shape `tier/slots.py` reaches for in its
          // own words ("EXCLUDED from the balanced-day denominator rather than
          // counted against it").
          //
          // A round-2 rewrite to "they stay out of the balance, either way" was
          // reverted: it read as placement and lost the held-against-him sense.
          // Ruled KEEP — do not "fix" this back.
          <p data-testid="board-residue-note" className="mt-1 text-[11px] text-console-ink-dim">
            No slot rule matched these yet — they don&rsquo;t count for or against your day.
          </p>
        )}

        {nothing ? (
          // Intentionally-left-blank: an empty stack is a fact about the day, and
          // an empty box that says nothing is indistinguishable from a broken one.
          //
          // The line is PER SLOT because the flat one landed under Fuel every
          // morning, where a bare "nothing here" reads as a verdict on the exact
          // slot the taxonomy exists to protect. See `SLOT_EMPTY_COPY`.
          <p data-testid={`board-stack-empty-${stack.key}`} className="mt-2 text-xs text-console-ink-dim">
            {SLOT_EMPTY_COPY[stack.key] ?? SLOT_EMPTY_FALLBACK}
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
                <p className="text-[10px] font-bold uppercase tracking-wider text-console-ink-dim">
                  Carried over
                </p>
                <ul data-testid={`board-carryover-${stack.key}`} className="mt-1.5 flex flex-col gap-2">
                  {stack.carryover.map((it) => renderRow(it, { reason: carryoverReason(it, now) }))}
                </ul>
              </div>
            )}

            {stack.candidates.length > 0 && (
              <div className="mt-3">
                {/* Not "Worth considering" — that is the system asserting these
                    have value. These are OFFERS awaiting a yes/no, and declining
                    costs nothing; the heading says who holds the choice. */}
                <p className="text-[10px] font-bold uppercase tracking-wider text-console-ink-dim">
                  If you want
                </p>
                <ul data-testid={`board-candidates-${stack.key}`} className="mt-1.5 flex flex-col gap-2">
                  {stack.candidates.map((it) => renderRow(it))}
                </ul>
              </div>
            )}

            {stack.today.length === 0 && stack.carryover.length === 0 && stack.candidates.length === 0 && (
              // Everything in this slot is done. Say which, rather than
              // rendering a box whose only content is a drill-down button.
              //
              // This must not sound like the EMPTY line: "finished this slot"
              // and "this slot never had anything" are opposite facts, and the
              // old pair ("Nothing here today." / "Nothing left here today.")
              // differed by one word. Overflow cannot be present here — an empty
              // carryover means `carriedAll` was empty, so its post-cap slice is
              // too, and likewise for candidates — so "all done" is exact.
              //
              // …EXACT ONLY WHEN NOTHING WAS SNOOZED, and that is the sentence
              // this lane came for. On 2026-08-16 the operator snoozed three
              // overdue duties and read "All done here today." underneath them.
              // A stack emptied by DELAY has not been finished, so the claim is
              // gated on `stack.snoozed` being empty and the delay gets its own
              // sentence naming what actually happened. Both branches are
              // reachable with `done` non-empty: some finished, some pushed still
              // may not claim "all done".
              stack.snoozed.length > 0 ? (
                <p
                  data-testid={`board-stack-snoozed-clear-${stack.key}`}
                  className="mt-2 text-xs font-semibold text-caution"
                >
                  Nothing left here today — {stack.snoozed.length} snoozed for later.
                </p>
              ) : (
                <p data-testid={`board-stack-clear-${stack.key}`} className="mt-2 text-xs font-semibold text-status-done-fg">
                  All done here today.
                </p>
              )
            )}

            {stack.done.length > 0 && (
              <div className="mt-3">
                <button
                  type="button"
                  data-testid={`board-show-done-${stack.key}`}
                  onClick={() => toggle(setOpenDone, stack.key)}
                  aria-expanded={showDone}
                  className="text-[11px] font-semibold uppercase tracking-wider text-console-ink-dim underline underline-offset-2"
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

            {stack.snoozed.length > 0 && (
              // THE SNOOZED DRILL — the same shape as the done drill above and
              // deliberately NOT inside it. Collapsed by default (the board reads
              // as remaining work), reachable in one tap, and labelled with the
              // word the operator's own gesture used.
              //
              // It exists because a delay has to stay VISIBLE and DISTINCT. The
              // bug this lane closes had these rows inside the done drill wearing
              // "✓ Done"; hiding them entirely would have been the same erasure
              // with better manners — he would have had no way to see what he
              // moved, or to notice he had moved something by accident.
              <div className="mt-3">
                <button
                  type="button"
                  data-testid={`board-show-snoozed-${stack.key}`}
                  onClick={() => toggle(setOpenSnoozed, stack.key)}
                  aria-expanded={showSnoozed}
                  className="text-[11px] font-semibold uppercase tracking-wider text-caution underline underline-offset-2"
                >
                  {showSnoozed ? 'Hide snoozed' : `Show snoozed (${stack.snoozed.length})`}
                </button>
                {showSnoozed && (
                  <ul data-testid={`board-snoozed-${stack.key}`} className="mt-2 flex flex-col gap-2">
                    {stack.snoozed.map((it) => renderRow(it))}
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
                  className="text-[11px] font-semibold uppercase tracking-wider text-console-ink-dim underline underline-offset-2"
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
      {/* THE UNRECORDED-COMPLETION NOTICE — the honest half of a held write.
          FIRST on the board, above even the coverage caveat: everything else
          here describes the day, and this describes something the system failed
          to do with what the operator already decided.

          A LIST, not a toast, for the reason the deck's is (see `Deck.tsx`): a
          burst is the normal shape of this failure, and one line shown for three
          and a half seconds could report exactly one of them. It accumulates,
          NAMES each row, says in plain words that the ✓ was not recorded, and
          stays until the operator says they have read it.

          It renders NOTHING when there is no debt — including on the server,
          where the ledger is unreadable by construction, so `/`'s precached
          shell HTML is the same shape it was. */}
      {grace.unrecorded.length > 0 && (
        <div
          role="alert"
          data-testid="board-unrecorded"
          className="mb-2 rounded-xl border-l-2 border-negative bg-negative-wash px-3 py-2.5"
        >
          <div className="mb-1.5 flex items-baseline justify-between gap-3">
            <p className="text-sm font-bold text-negative">
              {grace.unrecorded.length === 1
                ? 'A completion wasn’t recorded.'
                : `${grace.unrecorded.length} completions weren’t recorded.`}
            </p>
            <button
              type="button"
              data-testid="board-unrecorded-ack"
              onClick={grace.acknowledgeUnrecorded}
              className="shrink-0 text-[11px] font-bold uppercase tracking-[0.14em] text-negative underline underline-offset-4"
            >
              Acknowledge
            </button>
          </div>
          <ul className="flex max-h-40 flex-col gap-1 overflow-y-auto">
            {grace.unrecorded.map((u) => (
              <li key={u.id} data-testid="board-unrecorded-row" className="text-sm text-console-ink">
                <span className="font-semibold">{u.title || u.id}</span>
                {' — your ✓ didn’t stick'}
                {u.reason ? `: ${u.reason}` : ''}
                {'. '}
                {/* WHICH ROWS CAN BE MARKED AGAIN, said per row. Telling the
                    operator to re-tap something that is no longer on the board
                    would be a wrong steer dressed as a helpful one — and a row
                    that aged out is precisely the one they have no other way to
                    hear about. */}
                <span className="text-console-ink-dim">
                  {onBoard.has(u.id)
                    ? 'It’s still on the board.'
                    : 'It’s not on the board now — nothing was recorded.'}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {coverageLow && (
        // COVERAGE FLOOR (operator-ratified 0.80). When too much of the day went
        // unclassified, the module says so BEFORE he reads any of it — a
        // completeness caveat belongs at the top even though its subject is below.
        //
        // NAMING THE RIGHT VICTIM: this does NOT say the rings are missing part of
        // the day, because they aren't. `tierRingBuckets` filters on kind,
        // visible-today and a valid TIER — there is no slot filter — so an
        // unslotted item still draws its ring segment. What genuinely excludes
        // them is the balance scoreline and the three stacks
        // (`boardSlotsWithADone` counts only `SLOT_ORDER` keys). Blaming the one
        // part of the module that IS showing him everything would teach exactly
        // the wrong distrust.
        //
        // The SUBJECT of the sentence is the slot rules, not the operator and not
        // a passive "items couldn't be sorted" — a shortfall the machine owns
        // should say so in its own grammar. "No slot rule matched" is the same
        // vocabulary the residue note uses one level down, and both trace to
        // `tier/slots.py` rule 7 ("anything else → unslotted → refuse to guess").
        //
        // The tail is the fence's OTHER adjudicated exception (named in
        // `board.ts`). "The balance BELOW" is deictic — it points at the line on
        // this screen, which is `boardSlotsWithADone`, not at the server's
        // `balanced_day` metric — so stating what that on-screen line is computed
        // over is a fact about this render and claims nothing about the tier-based
        // metric. Stating the DENOMINATOR is the sentence's whole job and it may
        // not be softened into vagueness. A round-2 rewrite to "leaves them out"
        // was reverted. Ruled KEEP.
        <p data-testid="board-coverage-warning" role="status" className="mb-2 text-[11px] text-console-ink-dim">
          The slot rules couldn&rsquo;t place {coverage.unslotted} of {coverage.total} items — the
          balance below counts only the rest.
        </p>
      )}

      {/* The rings stay the headline: they are the URGENCY glance, and they are
          a different question from the slots below, not a smaller version of it. */}
      <RingsHeader items={items ?? []} completion={completion} now={nowProp} onAuthExpired={onAuthExpired} />

      {items == null ? (
        // Intentionally-left-blank: an explicit loading signal, not a blank board.
        <p data-testid="board-loading" className="mt-3 text-sm text-console-ink-dim">
          Loading your day…
        </p>
      ) : (
        <>
          <p data-testid="board-balance" className="mt-3 text-xs text-console-ink-dim">
            {totalOnBoard === 0
              ? // Intentionally-left-blank: three empty stacks could read as a
                // broken board — say that it ran and found nothing.
                'Nothing on the board yet today — it fills as the day’s items arrive.'
              : // The scoreline is the SIGNAL; the clause after it is the
                // interpretation, and it is the one sentence carrying the whole
                // taxonomy ruling — that the three slots are a permission system
                // and not a priority stack.
                //
                // IT IS A CLAIM ABOUT ARRANGEMENT, NEVER ABOUT SCORE, and the
                // distinction is load-bearing rather than stylistic. The
                // `balanced_day` METRIC is still tier-based server-side until a
                // gated future flip (see `boardSlotsWithADone`, which is named
                // apart from it for exactly this reason), so no string on this
                // board may claim a slot-based GOAL. An earlier draft read "all
                // three count the same" and was pulled: "count" is scoring
                // vocabulary, and on a surface whose scoring is not slot-based
                // yet it would have promised a flip that has not happened.
                // "Outranks" keeps the sentence on standing, which IS true today
                // and is what the operator needs to hear.
                //
                // It also MITIGATES — it cannot close — the one hierarchy signal
                // still on screen: the stacks render in a fixed order with Fuel
                // last, the very shape the ruling named when it said the numbered
                // tiers "re-encoded the hierarchy [they were] built to escape (T3
                // named last, rendered last)". A copy line can speak against a
                // layout; it cannot undo one. Closing it is a design pass, and
                // this sentence is not a substitute for that work.
                //
                // Deliberately NOT a progress nudge. No "keep going", no streak,
                // no target language: a scoreline that pressures the operator
                // toward a complete set would rebuild that same hierarchy, with
                // Fuel as the box left unticked.
                `${balanced} of 3 slots have something done — no slot outranks another.`}
          </p>
          <div data-testid="board-stacks" className="mt-2 flex flex-col gap-3">
            {stacks.map(renderStack)}
          </div>
          {totalOnBoard > 0 && !hasResidue && (
            // Intentionally-left-blank for the RESIDUE: when the classifier
            // answered for everything there is no fourth stack, and an absent
            // stack makes "it sorted all of today" and "the residue feature
            // isn't wired" look identical. One subordinate line rather than a
            // permanently-empty container: the signal always fires, and it costs
            // a line instead of a box on the operator's primary morning surface.
            //
            // Gated on the board having ITEMS. On an empty board nothing was
            // sorted, so this sentence would be a small lie told on the quietest
            // morning — the board-level line above already says the true thing.
            <p data-testid="board-residue-clear" className="mt-2 text-[11px] text-console-ink-dim">
              Everything on the board found a slot.
            </p>
          )}
        </>
      )}

      {grace.toast && (
        <div
          data-testid="board-toast"
          role="status"
          className="fixed inset-x-0 bottom-20 z-50 mx-auto flex w-fit items-center gap-3.5 overflow-hidden rounded-xl bg-console-raise px-3.5 py-2.5 text-sm text-console-ink shadow-card"
        >
          {/* "Got it", not "Done" — during the grace window NOTHING HAS BEEN
              WRITTEN yet, so this acknowledges the tap it actually has rather
              than asserting a completion it has not yet made. On the surface
              built to end done-but-unrecorded, the toast must not be the first
              thing on it that overstates. */}
          <span>Got it: {grace.toast.title}</span>
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
            className="deck-undo-bar absolute inset-x-0 bottom-0 h-[3px] origin-left bg-affirm-deep"
          />
        </div>
      )}
    </section>
  );
}
