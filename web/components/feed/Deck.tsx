import { useCallback, useEffect, useRef, useState } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import {
  DRAG_Y_CLAMP,
  deckVerbsFor,
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
  const { current, upcoming, confirmingId, toast, banner } = deck;
  const [expanded, setExpanded] = useState(false);
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
        {deck.parkedCount > 0 && <span data-testid="deck-parked">Parked: {deck.parkedCount}</span>}
      </div>

      <div className="relative min-h-[340px] flex-1">
        {stack.map(({ item, depth }) => (
          <DeckCard
            key={item.id}
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
            <p className="text-sm text-honeydew-600">
              {deck.parkedCount > 0
                ? `${deck.parkedCount} parked — the next sync will re-offer them.`
                : 'Nothing left to decide right now.'}
            </p>
          </div>
        )}
      </div>

      {/* Button + toast affordances (the accessible, testable alternates). */}
      <div className="flex justify-center gap-2.5 py-1">
        <button
          type="button"
          data-testid="deck-btn-reject"
          aria-label="Reject"
          disabled={!current || confirming || !verbs?.reject}
          onClick={deck.reject}
          className="flex h-13 w-13 items-center justify-center rounded-full border-[1.5px] border-danger p-3 text-danger disabled:opacity-30"
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
