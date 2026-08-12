import type { Verdict, VerbWeight } from './feedConstants';

// ═══════════════ ALGERNON CONSOLE — the code half of the shared layer ═══════
//
// styles/console.css defines WHAT the identity's colours are and what each
// function role MEANS. This module is the part that has to be decided in code
// rather than in CSS: the mapping from a domain fact (a verdict, a verb weight,
// an instance name) onto a role, plus the class strings that spend it.
//
// PORTABLE BY CONSTRUCTION. The Decide deck is the first consumer; the
// Awareness feed / sensor-log is the second and the brief-as-viewscreen the
// third. Nothing here mentions the deck, a card, or a swipe — it is stated in
// terms of the FeedItem-level vocabulary all three surfaces share. A surface
// that needs a role class asks for it here rather than typing `text-affirm`
// inline, so that when the identity moves, it moves in one place.
//
// WHY CLASS CONSTANTS RATHER THAN COMPUTED STRINGS: Tailwind's content scanner
// reads source text, so a class must appear literally somewhere it scans.
// tailwind.config.cjs scans ./lib/**, which is why these live here as complete
// literals and are never assembled as `text-${role}`.

/**
 * The four function roles of the identity. Colour carries meaning: pick the
 * role by what the element DOES, never by what reads well in the slot.
 *
 *   affirm    yes · confirm · accept · it landed
 *   negative  no · reject · spam · this destroys something
 *   caution   not now, or not without a second look
 *   info      neutral structure; carries no verdict at all
 */
export type ConsoleRole = 'affirm' | 'negative' | 'caution' | 'info';

/**
 * The role a gesture verdict is drawn in — the D3 axis made visible.
 *
 * Horizontal is the judgement axis (right affirms, left declines) and vertical
 * is the defer family, so `snooze` and `skip` BOTH map to caution even though
 * they mean different things to the store ("yes, but later" vs "not this one").
 * That is not a collapse of the distinction: the two are told apart by their
 * words — labels, stamps and toasts — while the axis is told apart by colour.
 * Giving `skip` the negative role instead would be the actively wrong choice,
 * because a set-aside that LOOKED like a rejection would misreport a card that
 * is coming back as one that was turned down.
 *
 * A null verdict is `info`: nothing has been judged.
 */
export function roleForVerdict(verdict: Verdict): ConsoleRole {
  if (verdict === 'affirm') return 'affirm';
  if (verdict === 'reject') return 'negative';
  if (verdict === 'snooze' || verdict === 'skip') return 'caution';
  return 'info';
}

/**
 * The role a verb's WEIGHT is drawn in.
 *
 * Heavy is caution — the same amber the defer family uses, and for the same
 * reason: both mean *slow down*. A heavy verb does not commit on the motion,
 * it arms and shows what it will write (D4), so at a glance it belongs with
 * "not yet" rather than with either verdict.
 */
export function roleForWeight(weight: VerbWeight): ConsoleRole {
  return weight === 'heavy' ? 'caution' : 'info';
}

// --- role → class ------------------------------------------------------------
// Four spends of a role, matching the three-rung ramp in console.css:
//   TEXT    the signal colour on a dark ground
//   BORDER  a hairline in the role, for a chip or an outlined control
//   WASH    a tinted backing that stays subordinate to the panel it sits on
//   FILL    a solid block of the role, with ink that reads ON it

export const ROLE_TEXT_CLASS: Record<ConsoleRole, string> = {
  affirm: 'text-affirm',
  negative: 'text-negative',
  caution: 'text-caution',
  info: 'text-info',
};

export const ROLE_BORDER_CLASS: Record<ConsoleRole, string> = {
  affirm: 'border-affirm',
  negative: 'border-negative',
  caution: 'border-caution',
  info: 'border-info',
};

export const ROLE_WASH_CLASS: Record<ConsoleRole, string> = {
  affirm: 'bg-affirm-wash',
  negative: 'bg-negative-wash',
  caution: 'bg-caution-wash',
  info: 'bg-info-wash',
};

export const ROLE_FILL_CLASS: Record<ConsoleRole, string> = {
  affirm: 'bg-affirm-deep text-console-ink',
  negative: 'bg-negative-deep text-console-ink',
  caution: 'bg-caution text-console-on-fill',
  info: 'bg-info-deep text-console-ink',
};

/**
 * A chip in one role: washed backing, role-coloured hairline, role-coloured
 * text. The single most repeated shape in the identity, so it is stated once.
 */
export function roleChipClass(role: ConsoleRole): string {
  return `${ROLE_WASH_CLASS[role]} ${ROLE_BORDER_CLASS[role]} ${ROLE_TEXT_CLASS[role]}`;
}

// --- instance provenance -----------------------------------------------------

/** The accent slots. Identity only — none of these is a function role. */
export const INSTANCE_ACCENT_CLASSES: readonly string[] = [
  'bg-accent-1-wash border-accent-1 text-accent-1',
  'bg-accent-2-wash border-accent-2 text-accent-2',
  'bg-accent-3-wash border-accent-3 text-accent-3',
  'bg-accent-4-wash border-accent-4 text-accent-4',
];

/**
 * The chip classes for an instance, chosen by a STABLE HASH of its name.
 *
 * Deliberately not a `{salem: …, kalle: …}` map. Instance names are config on
 * this platform, not constants (`NEXT_PUBLIC_INSTANCE_NAME`, the peer configs),
 * so a literal map is wrong the day a fifth instance exists and wrong forever
 * on any deployment whose instances aren't the ones we happened to type. The
 * hash gives every instance — including ones nobody has named yet — a stable
 * colour with no registry to maintain.
 *
 * Two instances CAN collide onto one accent. That is an accepted cost: the
 * chip's text is the instance name, so the colour is a recognition aid, never
 * the identifying channel.
 *
 * The hash must stay stable across releases — a chip that changed colour on
 * deploy would train and then betray recognition — so this is djb2 over the
 * case-folded name and must not be "improved" without a reason that outweighs
 * repainting every operator's mental map.
 */
export function instanceAccentClass(instance: string | null | undefined): string {
  const name = (instance || '').trim().toLowerCase();
  if (!name) return INSTANCE_ACCENT_CLASSES[1]; // unattributed → the neutral slate slot
  let h = 5381;
  for (let i = 0; i < name.length; i += 1) {
    h = ((h << 5) + h + name.charCodeAt(i)) >>> 0;
  }
  return INSTANCE_ACCENT_CLASSES[h % INSTANCE_ACCENT_CLASSES.length];
}

// --- shared shape vocabulary -------------------------------------------------

/**
 * The card/pane edge rail — the vertical colour block that is LCARS's most
 * recognisable move, and here the card's weight signal at a glance.
 *
 * `anyHeavy` rather than a per-direction colour, because one rail cannot
 * express two directions and this card's directions genuinely differ: an
 * attribution confirm is light while its reject strips a record body. So the
 * rail answers the glance-tier question — *can this card be cleared with one
 * motion?* — and the per-direction "Heavy · <verb>" chips on the face answer
 * which direction is the heavy one. Hatched rather than merely amber so the
 * weight survives for a colour-blind operator.
 */
export function railClassForWeight(anyHeavy: boolean): string {
  return anyHeavy ? 'console-hatch' : 'bg-affirm-deep';
}

/** Tracked-out uppercase micro-label — the identity's structural type style. */
export const CONSOLE_LABEL = 'text-[10px] font-bold uppercase tracking-[0.16em] text-console-ink-faint';

/** A data value: tabular mono, so columns of numbers line up and read as read-outs. */
export const CONSOLE_DATUM = 'font-mono tabular-nums text-console-ink-dim';
