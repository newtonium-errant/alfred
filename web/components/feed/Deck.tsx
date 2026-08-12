import { useCallback, useEffect, useRef, useState } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import {
  DRAG_Y_CLAMP,
  EMAIL_PRIORITY_TIERS,
  ONE_OFF_ACTION,
  ROUTINE_MATCH_KIND,
  SNOOZE_ACTIONS,
  SNOOZE_HOLD_MOVE_TOLERANCE,
  SNOOZE_HOLD_MS,
  SNOOZE_LABELS,
  UNDO_MS,
  isHeavyVerb,
  inSnoozeHoldBand,
  snoozeIsBacked,
  type SnoozeAction,
  emailPriority,
  kindLabel,
  verbsFromActions,
  routineCandidatesFor,
  routineProposedItem,
  sameRoutineItem,
  stampOpacity,
  swipeActsFor,
  verdictForDrag,
} from '../../lib/algernon/feedConstants';
import {
  CONSOLE_LABEL,
  ROLE_TEXT_CLASS,
  roleChipClass,
} from '../../lib/algernon/consoleTokens';
import { useDeck } from './useDeck';
import { DeckCard, DECK_CARD_BASE_Z } from './DeckCard';

// Overlays (the snoozed drill, the re-tier picker) must sit ABOVE the whole card stack —
// the top card is at DECK_CARD_BASE_Z, so an overlay below it opens invisibly behind the
// card and reads as a dead tap (the #28 re-tier bug). Derived from the card base so it
// can't drift below. Inline (not a Tailwind z-class) so the ordering is jsdom-testable.
const OVERLAY_Z_INDEX = DECK_CARD_BASE_Z + 10;

// The three full-card overlays (snoozed drill, re-tier picker, correction
// picker) are the same object wearing different content, so they share one set
// of classes rather than three drifting copies. The snooze duration menu is
// deliberately NOT one of them: it is a bottom sheet attached to a frozen
// gesture, and it carries the caution edge that says so.
const OVERLAY_PANEL_CLASS =
  'absolute inset-0 flex flex-col rounded-sm border border-console-edge-bright bg-console-panel p-4';
const OVERLAY_TITLE_CLASS =
  'text-[11px] font-bold uppercase tracking-[0.18em] text-console-ink';
const OVERLAY_DISMISS_CLASS =
  'text-[11px] font-bold uppercase tracking-[0.14em] text-console-ink-faint underline underline-offset-4';
const OVERLAY_CHOICE_CLASS =
  'rounded-sm border border-console-edge-bright bg-console-raise px-3 py-2 text-left text-sm font-semibold text-console-ink hover:border-affirm hover:text-affirm';

// The gesture buttons stay ROUND on an otherwise square identity. That is not
// an inconsistency: everything else on this surface is a panel bolted to a
// hull, and these four are the only things meant to read as a thing you press.
// Sizing is unchanged from the honeydew build — it was already a comfortable
// thumb target on the phone posture, which is the posture that matters here.
const DECK_BUTTON_CLASS =
  'flex h-13 w-13 items-center justify-center rounded-full border-[1.5px] p-3 disabled:opacity-30';

export interface DeckProps {
  items: FeedItem[];
  onAuthExpired?: () => void;
  onSnoozePersist?: (id: string) => void;
  onUnsnoozePersist?: (id: string) => void;
}

export function Deck({ items, onAuthExpired, onSnoozePersist, onUnsnoozePersist }: DeckProps) {
  const deck = useDeck({ items, onAuthExpired, onSnoozePersist, onUnsnoozePersist });
  const { current, upcoming, confirmingId, confirmingVerdict, toast, banner, snoozed } = deck;
  const [expanded, setExpanded] = useState(false);
  // The snoozed drill-down (task #26): snoozed cards stay hidden by default, viewable
  // behind this drill so the operator can deal one back without waiting for the sync.
  const [snoozedOpen, setSnoozedOpen] = useState(false);
  // The re-tier picker (task #28) — a deliberate multi-choice correction on the email card.
  const [reTierOpen, setReTierOpen] = useState(false);
  // The correction picker (task #13) — "what did this mean?" on a routine match.
  const [correctOpen, setCorrectOpen] = useState(false);
  // The snooze duration menu (#14) — opened by HOLDING a partial ↑ swipe in the
  // stamp band, or by the ↑ button / ArrowUp on a card the backend can snooze.
  const [snoozeMenuOpen, setSnoozeMenuOpen] = useState(false);
  const topRef = useRef<HTMLDivElement>(null);
  // The in-band hold timer, and the flag that says this gesture already spent
  // itself opening the menu. Without the flag the pointerup that ends the hold
  // would ALSO resolve a verdict, so one gesture would both open the menu and
  // act on the card behind it.
  const holdTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const gestureConsumedRef = useRef(false);

  // Collapse the evidence expand + close every picker whenever the top card changes.
  useEffect(() => {
    setExpanded(false);
    setReTierOpen(false);
    setCorrectOpen(false);
    setSnoozeMenuOpen(false);
    gestureConsumedRef.current = false;
  }, [current?.id]);

  // Spring the FROZEN card back when the snooze menu closes without a pick.
  //
  // While the menu is open the card holds the offset it was held at, so the menu
  // reads as attached to the gesture rather than as a dialog that appeared out of
  // nowhere. That freeze is just the absence of further updates (the drag
  // listeners are torn down by `inputBlocked`), which means nothing resets the
  // inline transform on dismiss — this does. On a PICK the card advances and the
  // frozen element unmounts, so this is a no-op there.
  const releaseFrozenCard = useCallback(() => {
    gestureConsumedRef.current = false;
    const el = topRef.current;
    if (!el) return;
    el.style.transition = '';
    el.style.transform = '';
    el.querySelectorAll<HTMLElement>('[data-stamp]').forEach((stamp) => {
      stamp.style.opacity = '0';
    });
  }, []);

  const closeSnoozeMenu = useCallback(() => {
    setSnoozeMenuOpen(false);
    releaseFrozenCard();
  }, [releaseFrozenCard]);

  const confirming = current != null && confirmingId === current.id;
  // Any open overlay (snoozed drill, re-tier picker, correction picker) blocks deck swipe +
  // keyboard input, so a gesture can't act on the card hidden underneath (the snoozed-panel
  // guard lesson). Every new overlay MUST join this list — an overlay that doesn't gate
  // input reads as a card acting on its own.
  const inputBlocked = snoozedOpen || reTierOpen || correctOpen || snoozeMenuOpen;

  // Imperative pointer drag on the top card — no React re-render per move. The
  // DISCRETE outcome (verdictForDrag → deck handler) is what the unit tests pin.
  useEffect(() => {
    const el = topRef.current;
    if (!el || !current || confirming || inputBlocked) return;
    // The current card's verbs — so a swipe toward a NO-OP verdict (an ACK-only kind's
    // left/reject, e.g. email_urgent) springs the card back instead of leaving it stuck
    // half-dragged (there was no advance to unmount the stale transform).
    const verbs = verbsFromActions(current);
    // Durations only mean something where a store is behind them; on every other
    // kind the ↑ gesture is the session-local set-aside it has always been, and a
    // menu offering "3 days" would be promising persistence that doesn't exist.
    //
    // THE CONTRACT IS THE OBSERVABLE PROPERTY — an unbacked kind never shows a
    // duration menu — and it is deliberately enforced twice (#48): here, so the
    // hold never arms, and again on the menu's own JSX via `snoozeIsBacked`, so
    // it could not render even if it did. Neither guard alone reds a test;
    // removing both reds the "an unbacked kind never opens a duration menu" pin.
    // Keep both — the pin defends the property, and the property is what the
    // operator experiences.
    const durationsAvailable = snoozeIsBacked(current);
    let sx = 0;
    let sy = 0;
    let dx = 0;
    let dy = 0;
    let holdX = 0;
    let holdY = 0;
    let dragging = false;
    const clearHold = () => {
      if (holdTimerRef.current !== null) {
        clearTimeout(holdTimerRef.current);
        holdTimerRef.current = null;
      }
    };
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
      setStamp('snooze', 0);
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
      setStamp('snooze', dy < 0 && Math.abs(dx) < 60 ? stampOpacity(-dy) : 0);
      // HOLD-IN-BAND (#14) — a partial ↑ held still opens the duration menu.
      // Additive to the release-time verdict: verdictForDrag still decides what a
      // RELEASE means, and this only fires while the finger is down and steady.
      if (!durationsAvailable) return;
      if (!inSnoozeHoldBand(dx, dy)) {
        clearHold();
        return;
      }
      if (holdTimerRef.current !== null) {
        // Already counting. Drift past the tolerance means this is a slow swipe,
        // not a hold — RE-ANCHOR rather than fire, so the menu can't ambush a
        // finger that is still travelling toward the full-swipe threshold.
        if (Math.abs(dx - holdX) <= SNOOZE_HOLD_MOVE_TOLERANCE && Math.abs(dy - holdY) <= SNOOZE_HOLD_MOVE_TOLERANCE) {
          return;
        }
        clearHold();
      }
      holdX = dx;
      holdY = dy;
      holdTimerRef.current = setTimeout(() => {
        holdTimerRef.current = null;
        // Spend the gesture: the card FREEZES where it was held (we simply stop
        // updating it), and the release that follows must not spring it back.
        //
        // THESE TWO LINES ARE ONE GUARD, deliberately doubled (#48). Either
        // alone already stops onUp from reaching `resetVisual()` — `dragging =
        // false` via its `if (!dragging) return`, the ref via its own
        // early-return — so deleting either changes no behaviour and no test.
        // Only removing BOTH reds the suite (`deckHoldBand.test.tsx`, the
        // "RELEASING under the open menu does not unfreeze the card" pin). Keep
        // the pair: the thing being protected is the freeze, and it is the
        // whole reason the hold fires mid-drag rather than on release.
        //
        // What this does NOT protect against, despite how it reads: a double
        // VERDICT. The hold can only fire from inside the band, and every
        // in-band coordinate sits strictly inside every threshold
        // `verdictForDrag` tests (|dx| < SNOOZE_X_TOLERANCE < SWIPE_X_THRESHOLD,
        // and up ≤ SNOOZE_Y_THRESHOLD so the ↑ verdict can't fire either), so
        // the release's verdict there is always null. An earlier comment here
        // claimed otherwise; a future editor "simplifying" on that basis would
        // delete the guard that actually matters.
        gestureConsumedRef.current = true;
        dragging = false;
        setSnoozeMenuOpen(true);
      }, SNOOZE_HOLD_MS);
    };
    const onUp = () => {
      clearHold();
      if (gestureConsumedRef.current) {
        // This gesture already opened the menu — releasing does nothing, and the
        // card stays frozen under it until the menu is dismissed or picked.
        // Deliberately redundant with the `dragging = false` the hold sets (see
        // the note there): belt and braces on the freeze, not dead code.
        dx = 0;
        dy = 0;
        return;
      }
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
        else deck.snooze();
      } else {
        resetVisual();
      }
    };

    el.addEventListener('pointerdown', onDown);
    el.addEventListener('pointermove', onMove);
    el.addEventListener('pointerup', onUp);
    el.addEventListener('pointercancel', onUp);
    return () => {
      clearHold(); // a torn-down card must never open a menu over its successor
      el.removeEventListener('pointerdown', onDown);
      el.removeEventListener('pointermove', onMove);
      el.removeEventListener('pointerup', onUp);
      el.removeEventListener('pointercancel', onUp);
    };
  }, [current, confirming, inputBlocked, deck]);

  // The DELIBERATE ↑ affordance (the button and ArrowUp), as opposed to the swipe.
  //
  // On a card the backend can snooze it opens the durations, because a menu
  // reachable only by holding a partial drag is a menu no keyboard and no
  // assistive pointer can reach — the gesture is the shortcut, not the only door.
  // On every other kind there are no durations to choose, so it defers directly.
  const onSnoozeAffordance = useCallback(() => {
    if (!current) return;
    if (snoozeIsBacked(current)) {
      setSnoozeMenuOpen(true);
      return;
    }
    deck.snooze();
  }, [current, deck]);

  // Pick a duration: close the menu and commit. Deliberately NOT awaited — this
  // is the same optimistic, undoable commit a swipe makes (the toast carries the
  // 3.5s Undo), so choosing "7 days" costs no more certainty than flicking ↑.
  const onPickSnooze = useCallback(
    (action: SnoozeAction) => {
      setSnoozeMenuOpen(false);
      gestureConsumedRef.current = false;
      deck.snooze(action);
    },
    [deck],
  );

  // Keyboard alternates (accessibility): ← reject · → affirm · ↑ snooze · ↓ details.
  // While an overlay (snoozed drill OR re-tier picker) is open it blocks pointer input on
  // the hidden card, so the keyboard MUST be gated too — otherwise an arrow key acts on
  // the card underneath the overlay (a first-contact-shaped edge).
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      if (!current || confirming || inputBlocked) return;
      if (e.key === 'ArrowRight') deck.affirm();
      else if (e.key === 'ArrowLeft') deck.reject();
      else if (e.key === 'ArrowUp') onSnoozeAffordance();
      else if (e.key === 'ArrowDown') setExpanded((v) => !v);
    },
    [current, confirming, inputBlocked, deck, onSnoozeAffordance],
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

  const verbs = current ? verbsFromActions(current) : null;
  // The current email card's assigned tier (#28) — the picker offers the OTHERS.
  const assignedTier = current ? emailPriority(current) : null;
  // The #13 pick-list: every active routine item MINUS the one this card proposed
  // (picking the rejected item is contradictory, and the server refuses it — hiding
  // it here keeps the UI from offering a door that can only fail).
  const proposedItem = current ? routineProposedItem(current) : '';
  const correctionCandidates = current
    ? routineCandidatesFor(current).filter((c) => !sameRoutineItem(c.text, proposedItem))
    : [];
  // How many of the cards still to come will ARM rather than commit. Counted
  // per-direction (a card is heavy if EITHER direction is), which is the same
  // question the card's own rail answers — one number, one predicate.
  const aheadHeavyCount = deck.ahead.filter((it) => {
    const v = verbsFromActions(it);
    return isHeavyVerb(v, 'affirm') || isHeavyVerb(v, 'reject');
  }).length;
  const stack: Array<{ item: FeedItem; depth: number }> = [];
  if (current) stack.push({ item: current, depth: 0 });
  upcoming.forEach((item, i) => stack.push({ item, depth: i + 1 }));

  return (
    <div data-testid="deck" className="flex flex-1 flex-col" onKeyDown={onKeyDown}>
      {banner && (
        <div role="alert" data-testid="deck-banner" className="mb-3 rounded-sm border-l-2 border-negative bg-negative-wash px-3 py-2 text-sm text-negative">
          {banner}
        </div>
      )}

      {/* THE TWO POSTURES. Phone = tricorder: one column, the card is the
          screen. Tablet/desktop = workstation: the same deck with a standing
          pane beside it, because a workstation has the room to show what is
          coming and a tricorder does not. Both are deliberate; the pane is
          additive and the deck column is identical in both. */}
      <div className="flex min-h-0 flex-1 gap-3">
      <div className="flex min-w-0 flex-1 flex-col">
      <div className={`mb-1.5 flex items-center justify-between px-0.5 ${CONSOLE_LABEL}`}>
        <span data-testid="deck-count" className="text-console-ink-dim">
          {deck.remaining > 0 ? `${deck.remaining} card${deck.remaining > 1 ? 's' : ''}` : 'Clear'}
        </span>
        {deck.snoozedCount > 0 && (
          // Never a number you can't tap — the label advertises its own verb.
          <button
            type="button"
            data-testid="deck-snoozed"
            aria-haspopup="dialog"
            onClick={() => setSnoozedOpen(true)}
            className={`${ROLE_TEXT_CLASS.caution} underline underline-offset-4`}
          >
            Snoozed: {deck.snoozedCount} — view
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
            confirmingVerdict={confirmingVerdict}
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
            className="absolute inset-0 m-auto flex max-h-[380px] flex-col items-center justify-center gap-2 rounded-sm border border-console-edge bg-console-panel p-5 text-center"
          >
            <p className="text-xl font-bold uppercase tracking-[0.22em] text-affirm">Deck clear.</p>
            {deck.snoozedCount > 0 ? (
              <>
                <p className="text-sm text-console-ink-dim">
                  {deck.snoozedCount} snoozed — the next sync will re-offer them.
                </p>
                <button
                  type="button"
                  data-testid="deck-cleared-view"
                  aria-haspopup="dialog"
                  onClick={() => setSnoozedOpen(true)}
                  className={`mt-1 text-sm ${ROLE_TEXT_CLASS.caution} underline underline-offset-4`}
                >
                  View snoozed
                </button>
              </>
            ) : (
              <p className="text-sm text-console-ink-dim">Nothing left to decide right now.</p>
            )}
          </div>
        )}

        {/* The snoozed drill-down (task #26): the worklist behind the "view" — list the
            snoozed cards (title + kind), each with Deal now (un-snooze + re-enter the queue
            immediately). Overlays the card area; z-above the cleared state. */}
        {snoozedOpen && (
          <div
            data-testid="deck-snoozed-panel"
            role="dialog"
            aria-label="Snoozed cards"
            style={{ zIndex: OVERLAY_Z_INDEX }}
            className={OVERLAY_PANEL_CLASS}
          >
            <div className="mb-2 flex items-center justify-between">
              <p className={OVERLAY_TITLE_CLASS}>
                Snoozed ({snoozed.length})
              </p>
              <button
                type="button"
                data-testid="deck-snoozed-close"
                onClick={() => setSnoozedOpen(false)}
                className={OVERLAY_DISMISS_CLASS}
              >
                Close
              </button>
            </div>

            {snoozed.length === 0 ? (
              // ILB: the drill is open but everything's been dealt back — say so, don't blank.
              <p data-testid="deck-snoozed-empty" className="mt-2 text-sm text-console-ink-dim">
                No snoozed cards — you&rsquo;ve dealt them all back in.
              </p>
            ) : (
              // The one genuinely ACCRETED surface on the deck: cards that have
              // piled up over the session, as opposed to the authored card in
              // front of you (D5's authored-vs-accreted distinction).
              <ul className="console-accreted flex flex-col gap-2 overflow-y-auto">
                {snoozed.map((p) => (
                  <li
                    key={p.id}
                    data-testid="deck-snoozed-row"
                    className="flex items-center gap-2 rounded-sm border border-console-edge bg-console-raise p-2.5"
                  >
                    <span className={`shrink-0 rounded-sm border border-console-edge-bright px-1.5 py-0.5 ${CONSOLE_LABEL}`}>
                      {kindLabel(p.kind)}
                    </span>
                    <span className="min-w-0 flex-1 truncate text-sm text-console-ink">{p.title}</span>
                    <button
                      type="button"
                      data-testid="deck-snoozed-deal"
                      onClick={() => deck.dealNow(p)}
                      className={`shrink-0 rounded-sm border px-3 py-1 ${CONSOLE_LABEL} ${roleChipClass('affirm')}`}
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
            className={OVERLAY_PANEL_CLASS}
          >
            <div className="mb-2 flex items-center justify-between">
              <p className={OVERLAY_TITLE_CLASS}>Adjust tier</p>
              <button
                type="button"
                data-testid="deck-retier-cancel"
                disabled={deck.reTiering !== null}
                onClick={() => setReTierOpen(false)}
                className={`${OVERLAY_DISMISS_CLASS} disabled:opacity-40`}
              >
                Cancel
              </button>
            </div>
            <p className="mb-2 text-xs text-console-ink-dim">
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
                  className={`${OVERLAY_CHOICE_CLASS} uppercase tracking-[0.14em] disabled:opacity-40`}
                >
                  {tier}
                </button>
              ))}
            </div>
            {deck.reTiering && (
              // Intentionally-left-blank: an explicit working signal — nothing greens until acted.
              <p data-testid="deck-retier-pending" className="mt-3 text-xs text-caution">
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
            className={OVERLAY_PANEL_CLASS}
          >
            <div className="mb-2 flex shrink-0 items-center justify-between">
              <p className={OVERLAY_TITLE_CLASS}>
                What did this mean?
              </p>
              <button
                type="button"
                data-testid="deck-correct-cancel"
                disabled={deck.correcting !== null}
                onClick={() => setCorrectOpen(false)}
                className={`${OVERLAY_DISMISS_CLASS} disabled:opacity-40`}
              >
                Cancel
              </button>
            </div>
            <p className="mb-2 shrink-0 text-xs text-console-ink-dim">
              {proposedItem ? `Not “${proposedItem}”. It was:` : 'It was:'}
            </p>

            <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto">
              {correctionCandidates.length === 0 ? (
                // ILB: an empty pick-list is a real state (the vault path isn't wired
                // on this instance), not a broken picker. Say which it is — the one-off
                // door below still works.
                <p data-testid="deck-correct-empty" className="text-xs text-console-ink-dim">
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
                    className={`shrink-0 ${OVERLAY_CHOICE_CLASS} disabled:opacity-40`}
                  >
                    {c.text}
                    {c.record && (
                      <span className="ml-1.5 text-[10px] font-normal uppercase tracking-[0.14em] text-console-ink-faint">
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
              className="mt-2 shrink-0 rounded-sm border border-dashed border-console-edge-bright px-3 py-2 text-left text-sm text-console-ink-dim disabled:opacity-40"
            >
              Nothing — this was a one-off
            </button>

            {deck.correcting && (
              // Intentionally-left-blank: an explicit working signal — nothing greens
              // until the server confirms the verdict landed.
              <p data-testid="deck-correct-pending" className="mt-2 shrink-0 text-xs text-caution">
                {deck.correcting === ONE_OFF_ACTION
                  ? 'Recording a one-off…'
                  : `Recording “${deck.correcting}”…`}
              </p>
            )}
          </div>
        )}

        {/* The snooze duration menu (#14) — the one defer verb's ladder.
            Reached by HOLDING a partial ↑ swipe in the band where the Snooze
            stamp is already showing, or by the ↑ button / ArrowUp. The card
            underneath stays frozen at the offset it was held at, so the menu
            reads as part of the gesture rather than as a dialog from nowhere;
            dismissing springs it back.

            Only rendered for kinds the backend can actually snooze — everywhere
            else ↑ is a session set-aside and there is no duration to choose.
            The second half of the doubled kind gate (see `durationsAvailable`
            in the drag effect); deliberate, not a leftover. */}
        {snoozeMenuOpen && current && snoozeIsBacked(current) && (
          <div
            data-testid="deck-snooze-menu"
            role="dialog"
            aria-label="Snooze for how long?"
            style={{ zIndex: OVERLAY_Z_INDEX }}
            className="absolute inset-x-0 bottom-0 flex flex-col rounded-sm border-t-2 border-caution bg-console-panel p-4"
          >
            <div className="mb-2 flex items-center justify-between">
              <p className={`${OVERLAY_TITLE_CLASS} ${ROLE_TEXT_CLASS.caution}`}>
                Snooze for
              </p>
              <button
                type="button"
                data-testid="deck-snooze-cancel"
                onClick={closeSnoozeMenu}
                className={OVERLAY_DISMISS_CLASS}
              >
                Cancel
              </button>
            </div>
            <div className="flex flex-col gap-2">
              {SNOOZE_ACTIONS.map((action) => (
                <button
                  key={action}
                  type="button"
                  data-testid={`deck-snooze-choice-${action}`}
                  onClick={() => onPickSnooze(action)}
                  className={OVERLAY_CHOICE_CLASS}
                >
                  {SNOOZE_LABELS[action]}
                </button>
              ))}
            </div>
            <p className="mt-2 text-[11px] italic text-console-ink-faint">
              It stays off the board until then — unless it gets more urgent than
              it is now.
            </p>
          </div>
        )}
      </div>

      {/* Button + toast affordances (the accessible, testable alternates).
          Each is drawn in ITS OWN verdict's role, so the button row teaches the
          same axis the swipe does: reject left in negative, defer up in
          caution, affirm right in affirm. The details control carries no
          verdict, so it is neutral structure. */}
      <div className="flex justify-center gap-2.5 py-1">
        <button
          type="button"
          data-testid="deck-btn-reject"
          aria-label={verbs?.rejectLabel || 'Reject'}
          disabled={!current || confirming || !(verbs?.reject || verbs?.rejectDefers)}
          onClick={deck.reject}
          className={`${DECK_BUTTON_CLASS} ${verbs?.rejectDefers ? 'border-caution text-caution' : 'border-negative text-negative'}`}
        >
          ✕
        </button>
        <button
          type="button"
          data-testid="deck-btn-snooze"
          aria-label={current && snoozeIsBacked(current) ? 'Snooze — choose how long' : 'Set aside for now'}
          aria-haspopup={current && snoozeIsBacked(current) ? 'dialog' : undefined}
          disabled={!current || confirming}
          onClick={onSnoozeAffordance}
          className={`${DECK_BUTTON_CLASS} border-caution text-caution`}
        >
          ↑
        </button>
        <button
          type="button"
          data-testid="deck-btn-details"
          aria-label="Toggle details"
          disabled={!current}
          onClick={() => setExpanded((v) => !v)}
          className={`${DECK_BUTTON_CLASS} border-console-edge-bright text-console-ink-dim`}
        >
          ···
        </button>
        <button
          type="button"
          data-testid="deck-btn-affirm"
          aria-label={confirming && confirmingVerdict === 'affirm' ? 'Confirm' : 'Affirm'}
          disabled={!current || !verbs?.affirm}
          onClick={confirming && confirmingVerdict === 'affirm' ? deck.confirmHeavy : deck.affirm}
          className={`${DECK_BUTTON_CLASS} border-affirm text-affirm`}
        >
          ✓
        </button>
      </div>
      </div>

      {/* WORKSTATION PANE — the posture difference, and nothing else.
          Hidden below lg, so the phone stays a tricorder: one card, the whole
          screen, no peripheral vision competing with the decision in front of
          you. Everything in here is DERIVED from the same queue the deck deals
          from (`deck.ahead` is the real remainder, not the two-card render
          slice), so it can never disagree with the count above it. */}
      <aside data-testid="deck-queue-pane" className="hidden w-64 shrink-0 flex-col lg:flex">
        <p className={`mb-1.5 px-0.5 ${CONSOLE_LABEL}`}>Queue</p>
        <div className="min-h-0 flex-1 overflow-y-auto rounded-sm border border-console-edge bg-console-panel p-2.5">
          {aheadHeavyCount > 0 && (
            // Worth knowing BEFORE you start: how much of what is coming will
            // ask for a second tap rather than a flick.
            <p data-testid="deck-queue-heavy" className={`mb-2 ${CONSOLE_LABEL} ${ROLE_TEXT_CLASS.caution}`}>
              {aheadHeavyCount} need{aheadHeavyCount === 1 ? 's' : ''} a second look
            </p>
          )}
          {deck.ahead.length === 0 ? (
            // ILB: an empty pane is a real state — this is the last card, or
            // the deck is clear — and saying which is the difference between
            // "nothing behind this" and "the pane is broken".
            <p data-testid="deck-queue-empty" className="text-xs text-console-ink-faint">
              {current ? 'Nothing behind this one.' : 'Queue empty.'}
            </p>
          ) : (
            <ul className="flex flex-col">
              {deck.ahead.map((it, i) => (
                <li
                  key={(it as { __deckKey?: string }).__deckKey ?? it.id}
                  data-testid="deck-queue-row"
                  className="flex gap-2 border-b border-console-edge py-2 last:border-b-0"
                >
                  <span className="w-4 shrink-0 pt-0.5 font-mono text-[10px] tabular-nums text-console-ink-ghost">
                    {i + 1}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-xs text-console-ink-dim">{it.title || it.id}</span>
                    <span className={`mt-0.5 block ${CONSOLE_LABEL}`}>{kindLabel(it.kind)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>
      </div>

      {toast && (
        <div
          data-testid="deck-toast"
          className="fixed inset-x-0 bottom-20 z-50 mx-auto flex w-fit items-center gap-3.5 overflow-hidden rounded-sm border border-console-edge-bright bg-console-raise px-3.5 py-2.5 text-sm text-console-ink shadow-[0_12px_34px_rgba(0,0,0,0.6)]"
        >
          <span>{toast.message}</span>
          {toast.canUndo && (
            <button
              type="button"
              data-testid="deck-toast-undo"
              onClick={deck.undo}
              className="text-[11px] font-bold uppercase tracking-[0.14em] text-affirm underline underline-offset-4"
            >
              Undo
            </button>
          )}
          {/* D8 — the remaining time, on screen. The window was already the
              strong part (the POST has not fired yet, so Undo cancels rather
              than reverses); what was missing was any way to see how much of it
              is left. The duration is READ FROM `UNDO_MS`, never re-typed, so
              the bar and the timer that actually flushes cannot disagree. */}
          {toast.canUndo && (
            <span
              data-testid="deck-toast-bar"
              aria-hidden="true"
              style={{ animationDuration: `${UNDO_MS}ms` }}
              className="deck-undo-bar absolute inset-x-0 bottom-0 h-[3px] origin-left bg-affirm-deep"
            />
          )}
        </div>
      )}
    </div>
  );
}
