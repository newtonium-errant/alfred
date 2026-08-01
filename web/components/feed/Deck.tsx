import { useCallback, useEffect, useRef, useState } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import {
  DRAG_Y_CLAMP,
  deckVerbsFor,
  kindLabel,
  stampOpacity,
  verdictForDrag,
} from '../../lib/algernon/feedConstants';
import { useDeck } from './useDeck';
import { DeckCard } from './DeckCard';

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
  const topRef = useRef<HTMLDivElement>(null);

  // Collapse the evidence expand whenever the top card changes.
  useEffect(() => setExpanded(false), [current?.id]);

  const confirming = current != null && confirmingId === current.id;

  // Imperative pointer drag on the top card — no React re-render per move. The
  // DISCRETE outcome (verdictForDrag → deck handler) is what the unit tests pin.
  useEffect(() => {
    const el = topRef.current;
    if (!el || !current || confirming) return;
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
      if (verdict === 'affirm') deck.affirm();
      else if (verdict === 'reject') deck.reject();
      else if (verdict === 'park') deck.park();
      else resetVisual();
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
  }, [current, confirming, deck]);

  // Keyboard alternates (accessibility): ← reject · → affirm · ↑ park · ↓ details.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!current || confirming) return;
      if (e.key === 'ArrowRight') deck.affirm();
      else if (e.key === 'ArrowLeft') deck.reject();
      else if (e.key === 'ArrowUp') deck.park();
      else if (e.key === 'ArrowDown') setExpanded((v) => !v);
    },
    [current, confirming, deck],
  );

  const verbs = current ? deckVerbsFor(current.kind) : null;
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
            className="absolute inset-0 z-20 flex flex-col rounded-2xl border border-honeydew-300 bg-cream p-4 shadow-card"
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
