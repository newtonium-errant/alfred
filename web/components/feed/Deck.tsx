import { useCallback, useEffect, useRef, useState } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import {
  DRAG_Y_CLAMP,
  EMAIL_PRIORITY_TIERS,
  ONE_OFF_ACTION,
  ROUTINE_MATCH_KIND,
  deckVerbsFor,
  emailPriority,
  kindLabel,
  routineCandidatesFor,
  routineProposedItem,
  sameRoutineItem,
  stampOpacity,
  swipeActsFor,
  verdictForDrag,
} from '../../lib/algernon/feedConstants';
import { useDeck } from './useDeck';
import { DeckCard, DECK_CARD_BASE_Z } from './DeckCard';

// Overlays (the parked drill, the re-tier picker) must sit ABOVE the whole card stack —
// the top card is at DECK_CARD_BASE_Z, so an overlay below it opens invisibly behind the
// card and reads as a dead tap (the #28 re-tier bug). Derived from the card base so it
// can't drift below. Inline (not a Tailwind z-class) so the ordering is jsdom-testable.
const OVERLAY_Z_INDEX = DECK_CARD_BASE_Z + 10;

export interface DeckProps {
  items: FeedItem[];
  onAuthExpired?: () => void;
  onParkPersist?: (id: string) => void;
  onUnparkPersist?: (id: string) => void;
}

export function Deck({ items, onAuthExpired, onParkPersist, onUnparkPersist }: DeckProps) {
  const deck = useDeck({ items, onAuthExpired, onParkPersist, onUnparkPersist });
  const { current, upcoming, confirmingId, toast, banner, parked } = deck;
  const [expanded, setExpanded] = useState(false);
  // The parked drill-down (task #26): parked cards stay hidden by default, viewable
  // behind this drill so the operator can deal one back without waiting for the sync.
  const [parkedOpen, setParkedOpen] = useState(false);
  // The re-tier picker (task #28) — a deliberate multi-choice correction on the email card.
  const [reTierOpen, setReTierOpen] = useState(false);
  // The correction picker (task #13) — "what did this mean?" on a routine match.
  const [correctOpen, setCorrectOpen] = useState(false);
  const topRef = useRef<HTMLDivElement>(null);

  // Collapse the evidence expand + close both pickers whenever the top card changes.
  useEffect(() => {
    setExpanded(false);
    setReTierOpen(false);
    setCorrectOpen(false);
  }, [current?.id]);

  const confirming = current != null && confirmingId === current.id;
  // Any open overlay (parked drill, re-tier picker, correction picker) blocks deck swipe +
  // keyboard input, so a gesture can't act on the card hidden underneath (the parked-panel
  // guard lesson). Every new overlay MUST join this list — an overlay that doesn't gate
  // input reads as a card acting on its own.
  const inputBlocked = parkedOpen || reTierOpen || correctOpen;

  // Imperative pointer drag on the top card — no React re-render per move. The
  // DISCRETE outcome (verdictForDrag → deck handler) is what the unit tests pin.
  useEffect(() => {
    const el = topRef.current;
    if (!el || !current || confirming || inputBlocked) return;
    // The current card's verbs — so a swipe toward a NO-OP verdict (an ACK-only kind's
    // left/reject, e.g. email_urgent) springs the card back instead of leaving it stuck
    // half-dragged (there was no advance to unmount the stale transform).
    const verbs = deckVerbsFor(current.kind);
    let sx = 0;
    let sy = 0;
    let dx = 0;
    let dy = 0;
    let dragging = false;
    const stamps = el.querySelectorAll<HTMLElement>('[data-stamp]');
    const setStamp = (name: string, v: number) => {
      stamps.forEach((s) => {
        if (s.dataset.stamp === name) s.style.opacity = String(v);
      });
    };
    const resetVisual = () => {
      el.style.transform = '';
      setStamp('affirm', 0);
      setStamp('reject', 0);
      setStamp('park', 0);
    };

    const onDown = (e: PointerEvent) => {
      dragging = true;
      sx = e.clientX;
      sy = e.clientY;
      el.setPointerCapture(e.pointerId);
      el.style.transition = 'none';
    };
    const onMove = (e: PointerEvent) => {
      if (!dragging) return;
      dx = e.clientX - sx;
      dy = e.clientY - sy;
      el.style.transform = `translate(${dx}px, ${Math.min(dy, DRAG_Y_CLAMP)}px) rotate(${dx / 18}deg)`;
      setStamp('affirm', dx > 0 ? stampOpacity(dx) : 0);
      setStamp('reject', dx < 0 ? stampOpacity(-dx) : 0);
      setStamp('park', dy < 0 && Math.abs(dx) < 60 ? stampOpacity(-dy) : 0);
    };
    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      el.style.transition = '';
      const verdict = verdictForDrag(dx, dy);
      dx = 0;
      dy = 0;
      // Only fire a verb that actually acts; a no-op verdict (an ACK-only kind's reject,
      // an affirm-less kind) springs back so the card isn't left stuck half-swiped.
      if (verdict && swipeActsFor(verbs, verdict)) {
        if (verdict === 'affirm') deck.affirm();
        else if (verdict === 'reject') deck.reject();
        else deck.park();
      } else {
        resetVisual();
      }
    };

    el.addEventListener('pointerdown', onDown);
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', onUp);
    return () => {
      el.removeEventListener('pointerdown', onDown);
      el.removeEventListener('pointermove', onMove);
      el.removeEventListener('pointerup', onUp);
      el.removeEventListener('pointercancel', onUp);
    };
  }, [current, confirming, inputBlocked, deck]);

  // Keyboard alternates (accessibility): ← reject · → affirm · ↑ park · ↓ details.
  // While an overlay (parked drill OR re-tier picker) is open it blocks pointer input on
  // the hidden card, so the keyboard MUST be gated too — otherwise an arrow key acts on
  // the card underneath the overlay (a first-contact-shaped edge).
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!current || confirming || inputBlocked) return;
      if (e.key === 'ArrowRight') deck.affirm();
      else if (e.key === 'ArrowLeft') deck.reject();
      else if (e.key === 'ArrowUp') deck.park();
      else if (e.key === 'ArrowDown') setExpanded((v) => !v);
    },
    [current, confirming, inputBlocked, deck],
  );

  // Re-tier the current email card to a chosen tier, then close the picker once the act
  // resolves (on success the card has flipped away; on error the toast shows + card stays).
  const onPickTier = useCallback(
    async (tier: string) => {
      await deck.reTier(tier);
      setReTierOpen(false);
    },
    [deck],
  );

  // Record a correction on the current routine-match card, then close the picker once
  // the act resolves. `target === null` is the one-off door. On refusal the card stays
  // (deck.correctRoutine keeps it) and the toast carries the server's reason.
  const onPickCorrection = useCallback(
    async (target: string | null) => {
      await deck.correctRoutine(target);
      setCorrectOpen(false);
    },
    [deck],
  );

  const verbs = current ? deckVerbsFor(current.kind) : null;
  // The current email card's assigned tier (#28) — the picker offers the OTHERS.
  const assignedTier = current ? emailPriority(current) : null;
  // The #13 pick-list: every active routine item MINUS the one this card proposed
  // (picking the rejected item is contradictory, and the server refuses it — hiding
  // it here keeps the UI from offering a door that can only fail).
  const proposedItem = current ? routineProposedItem(current) : '';
  const correctionCandidates = current
    ? routineCandidatesFor(current).filter((c) => !sameRoutineItem(c.text, proposedItem))
    : [];
  const stack: Array<{ item: FeedItem; depth: number }> = [];
  if (current) stack.push({ item: current, depth: 0 });
  upcoming.forEach((item, i) => stack.push({ item, depth: i + 1 }));

  return (
    <div data-testid="deck" className="flex flex-1 flex-col" onKeyDown={onKeyDown}>
      {banner && (
        <div role="alert" data-testid="deck-banner" className="mb-3 rounded-xl bg-danger-bg px-3 py-2 text-sm text-danger">
          {banner}
        </div>
      )}

      <div className="mb-1.5 flex items-center justify-between px-0.5 text-[11px] font-semibold uppercase tracking-wider text-honeydew-600">
        <span data-testid="deck-count">
          {deck.remaining > 0 ? `${deck.remaining} card${deck.remaining > 1 ? 's' : ''}` : 'Clear'}
        </span>
        {deck.parkedCount > 0 && (
          // Never a number you can't tap — the label advertises its own verb.
          <button
            type="button"
            data-testid="deck-parked"
            aria-haspopup="dialog"
            onClick={() => setParkedOpen(true)}
            className="font-semibold uppercase tracking-wider text-status-progress-fg underline underline-offset-2"
          >
            Parked: {deck.parkedCount} — view
          </button>
        )}
      </div>

      <div className="relative min-h-[340px] flex-1">
        {stack.map(({ item, depth }) => (
          <DeckCard
            key={(item as { __deckKey?: string }).__deckKey ?? item.id}
            ref={depth === 0 ? topRef : undefined}
            item={item}
            depth={depth}
            expanded={depth === 0 && expanded}
            confirming={depth === 0 && confirming}
            onToggleEvidence={() => setExpanded((v) => !v)}
            onConfirmHeavy={deck.confirmHeavy}
            onCancelHeavy={deck.cancelHeavy}
            onReTierOpen={depth === 0 ? () => setReTierOpen(true) : undefined}
            onCorrectOpen={depth === 0 ? () => setCorrectOpen(true) : undefined}
          />
        ))}

        {deck.cleared && (
          <div
            data-testid="deck-cleared"
            className="absolute inset-0 m-auto flex max-h-[360px] flex-col items-center justify-center gap-2 rounded-2xl border border-honeydew-300 bg-cream p-5 text-center shadow-card"
          >
            <p className="text-2xl font-extrabold text-honeydew-700">Deck clear.</p>
            {deck.parkedCount > 0 ? (
              <>
                <p className="text-sm text-honeydew-600">
                  {deck.parkedCount} parked — the next sync will re-offer them.
                </p>
                <button
                  type="button"
                  data-testid="deck-cleared-view"
                  aria-haspopup="dialog"
                  onClick={() => setParkedOpen(true)}
                  className="mt-1 text-sm font-semibold text-status-progress-fg underline underline-offset-2"
                >
                  View parked
                </button>
              </>
            ) : (
              <p className="text-sm text-honeydew-600">Nothing left to decide right now.</p>
            )}
          </div>
        )}

        {/* The parked drill-down (task #26): the worklist behind the "view" — list the
            parked cards (title + kind), each with Deal now (un-park + re-enter the queue
            immediately). Overlays the card area; z-above the cleared state. */}
        {parkedOpen && (
          <div
            data-testid="deck-parked-panel"
            role="dialog"
            aria-label="Parked cards"
            style={{ zIndex: OVERLAY_Z_INDEX }}
            className="absolute inset-0 flex flex-col rounded-2xl border border-honeydew-300 bg-cream p-4 shadow-card"
          >
            <div className="mb-2 flex items-center justify-between">
              <p className="text-sm font-extrabold uppercase tracking-wider text-honeydew-700">
                Parked ({parked.length})
              </p>
              <button
                type="button"
                data-testid="deck-parked-close"
                onClick={() => setParkedOpen(false)}
                className="text-sm font-semibold text-honeydew-600 underline underline-offset-2"
              >
                Close
              </button>
            </div>

            {parked.length === 0 ? (
              // ILB: the drill is open but everything's been dealt back — say so, don't blank.
              <p data-testid="deck-parked-empty" className="mt-2 text-sm text-honeydew-600">
                No parked cards — you&rsquo;ve dealt them all back in.
              </p>
            ) : (
              <ul className="flex flex-col gap-2 overflow-y-auto">
                {parked.map((p) => (
                  <li
                    key={p.id}
                    data-testid="deck-parked-row"
                    className="flex items-center gap-2 rounded-xl border border-honeydew-200 bg-white p-2.5"
                  >
                    <span className="shrink-0 rounded-md border border-honeydew-300 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wider text-honeydew-600">
                      {kindLabel(p.kind)}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-sm text-honeydew-700">{p.title}</span>
                    <button
                      type="button"
                      data-testid="deck-parked-deal"
                      onClick={() => deck.dealNow(p)}
                      className="shrink-0 rounded-lg border border-honeydew-600 px-3 py-1 text-xs font-bold uppercase tracking-wider text-honeydew-700"
                    >
                      Deal now
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {/* The re-tier picker (task #28): a deliberate correction of the classifier's
            tier — the tiers OTHER than the assigned one (spam included, two honest doors).
            Each choice AWAITS its act; nothing greens until acted returns (deck.reTiering
            drives the pending signal). Overlays the card; deck input is gated while open. */}
        {reTierOpen && current && (
          <div
            data-testid="deck-retier-picker"
            role="dialog"
            aria-label="Adjust email tier"
            style={{ zIndex: OVERLAY_Z_INDEX }}
            className="absolute inset-0 flex flex-col rounded-2xl border border-honeydew-300 bg-cream p-4 shadow-card"
          >
            <div className="mb-2 flex items-center justify-between">
              <p className="text-sm font-extrabold uppercase tracking-wider text-honeydew-700">Adjust tier</p>
              <button
                type="button"
                data-testid="deck-retier-cancel"
                disabled={deck.reTiering !== null}
                onClick={() => setReTierOpen(false)}
                className="text-sm font-semibold text-honeydew-600 underline underline-offset-2 disabled:opacity-40"
              >
                Cancel
              </button>
            </div>
            <p className="mb-2 text-xs text-honeydew-600">
              {assignedTier ? `Now: ${assignedTier.toUpperCase()}. Move it to:` : 'Set the tier:'}
            </p>
            <div className="flex flex-col gap-2">
              {EMAIL_PRIORITY_TIERS.filter((tier) => tier !== assignedTier).map((tier) => (
                <button
                  key={tier}
                  type="button"
                  data-testid={`deck-retier-choice-${tier}`}
                  disabled={deck.reTiering !== null}
                  onClick={() => void onPickTier(tier)}
                  className="rounded-xl border border-honeydew-400 px-3 py-2 text-left text-sm font-semibold uppercase tracking-wider text-honeydew-700 disabled:opacity-40"
                >
                  {tier}
                </button>
              ))}
            </div>
            {deck.reTiering && (
              // Intentionally-left-blank: an explicit working signal — nothing greens until acted.
              <p data-testid="deck-retier-pending" className="mt-3 text-xs text-honeydew-600">
                Adjusting to {deck.reTiering.toUpperCase()}…
              </p>
            )}
          </div>
        )}

        {/* The correction picker (task #13): a NO that teaches. Pick the routine item
            the completion actually meant, or say it was a one-off — either way the
            proposed match is rejected. Choices are the vault's REAL items (the server
            re-validates the pick), never free text. Nothing greens until the act
            returns `acted`; a refusal keeps the card and toasts the server's reason. */}
        {correctOpen && current && current.kind === ROUTINE_MATCH_KIND && (
          <div
            data-testid="deck-correct-picker"
            role="dialog"
            aria-label="What did this completion mean?"
            style={{ zIndex: OVERLAY_Z_INDEX }}
            className="absolute inset-0 flex flex-col rounded-2xl border border-honeydew-300 bg-cream p-4 shadow-card"
          >
            <div className="mb-2 flex shrink-0 items-center justify-between">
              <p className="text-sm font-extrabold uppercase tracking-wider text-honeydew-700">
                What did this mean?
              </p>
              <button
                type="button"
                data-testid="deck-correct-cancel"
                disabled={deck.correcting !== null}
                onClick={() => setCorrectOpen(false)}
                className="text-sm font-semibold text-honeydew-600 underline underline-offset-2 disabled:opacity-40"
              >
                Cancel
              </button>
            </div>
            <p className="mb-2 shrink-0 text-xs text-honeydew-600">
              {proposedItem ? `Not “${proposedItem}”. It was:` : 'It was:'}
            </p>

            <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
              {correctionCandidates.length === 0 ? (
                // ILB: an empty pick-list is a real state (the vault path isn't wired
                // on this instance), not a broken picker. Say which it is — the one-off
                // door below still works.
                <p data-testid="deck-correct-empty" className="text-xs text-honeydew-600">
                  No routine items available to pick from right now — you can still mark
                  it a one-off.
                </p>
              ) : (
                correctionCandidates.map((c) => (
                  <button
                    key={`${c.record}|${c.text}`}
                    type="button"
                    data-testid="deck-correct-choice"
                    data-item={c.text}
                    disabled={deck.correcting !== null}
                    onClick={() => void onPickCorrection(c.text)}
                    className="shrink-0 rounded-xl border border-honeydew-400 px-3 py-2 text-left text-sm font-semibold text-honeydew-700 disabled:opacity-40"
                  >
                    {c.text}
                    {c.record && (
                      <span className="ml-1.5 text-[10px] font-normal uppercase tracking-wider text-honeydew-500">
                        {c.record}
                      </span>
                    )}
                  </button>
                ))
              )}
            </div>

            {/* The honest third verdict — kept visually apart from the items because it
                is a different KIND of answer, not another item. */}
            <button
              type="button"
              data-testid="deck-correct-one-off"
              disabled={deck.correcting !== null}
              onClick={() => void onPickCorrection(null)}
              className="mt-2 shrink-0 rounded-xl border border-dashed border-honeydew-400 px-3 py-2 text-left text-sm text-honeydew-600 disabled:opacity-40"
            >
              Nothing — this was a one-off
            </button>

            {deck.correcting && (
              // Intentionally-left-blank: an explicit working signal — nothing greens
              // until the server confirms the verdict landed.
              <p data-testid="deck-correct-pending" className="mt-2 shrink-0 text-xs text-honeydew-600">
                {deck.correcting === ONE_OFF_ACTION
                  ? 'Recording a one-off…'
                  : `Recording “${deck.correcting}”…`}
              </p>
            )}
          </div>
        )}
      </div>

      {/* Button + toast affordances (the accessible, testable alternates). */}
      <div className="flex justify-center gap-2.5 py-1">
        <button
          type="button"
          data-testid="deck-btn-reject"
          aria-label={verbs?.rejectLabel || 'Reject'}
          disabled={!current || confirming || !(verbs?.reject || verbs?.rejectParks)}
          onClick={deck.reject}
          className={`flex h-13 w-13 items-center justify-center rounded-full border-[1.5px] p-3 disabled:opacity-30 ${verbs?.rejectParks ? 'border-status-progress-fg text-status-progress-fg' : 'border-danger text-danger'}`}
        >
          ✕
        </button>
        <button
          type="button"
          data-testid="deck-btn-park"
          aria-label="Park to the next sync"
          disabled={!current || confirming}
          onClick={deck.park}
          className="flex h-13 w-13 items-center justify-center rounded-full border-[1.5px] border-status-progress-fg p-3 text-status-progress-fg disabled:opacity-30"
        >
          ↑
        </button>
        <button
          type="button"
          data-testid="deck-btn-details"
          aria-label="Toggle details"
          disabled={!current}
          onClick={() => setExpanded((v) => !v)}
          className="flex h-13 w-13 items-center justify-center rounded-full border-[1.5px] border-honeydew-400 p-3 text-honeydew-600 disabled:opacity-30"
        >
          ···
        </button>
        <button
          type="button"
          data-testid="deck-btn-affirm"
          aria-label={confirming ? 'Confirm' : 'Affirm'}
          disabled={!current || !verbs?.affirm}
          onClick={confirming ? deck.confirmHeavy : deck.affirm}
          className="flex h-13 w-13 items-center justify-center rounded-full border-[1.5px] border-honeydew-600 p-3 text-honeydew-600 disabled:opacity-30"
        >
          ✓
        </button>
      </div>

      {toast && (
        <div
          data-testid="deck-toast"
          className="fixed inset-x-0 bottom-20 z-50 mx-auto flex w-fit items-center gap-3.5 rounded-xl bg-honeydew-700 px-3.5 py-2.5 text-sm text-cream shadow-card"
        >
          <span>{toast.message}</span>
          {toast.canUndo && (
            <button
              type="button"
              data-testid="deck-toast-undo"
              onClick={deck.undo}
              className="font-bold uppercase tracking-wider underline"
            >
              Undo
            </button>
          )}
        </div>
      )}
    </div>
  );
}
