// The contact-surface router's rule evaluation (C4) — PURE, no React, no fetch.
//
// The consumer of `preference/Algernon — contact-surface routing.md`. The vault
// record holds the policy; this holds the evaluation of it, and the server holds
// the state and the levers. Nothing here carries a lever DEFAULT: every number
// the rules compare against arrives in the payload, so editing the preference
// record (Hypatia-side, no deploy) is the only way either threshold moves.
//
// SPLIT DELIBERATELY FROM THE HOOK. Rules are a decision over data; navigation
// and logging are effects. Keeping the decision pure is what makes every rung
// testable without a router, a fetch mock, or a rendered tree — and the rungs
// are the part a future lane will edit when rule 1 arrives.

// --- the shared vocabulary --------------------------------------------------
// SECOND SPELLING, DELIBERATELY. TypeScript cannot import a Python tuple, so
// these two arrays are copies of `alfred.web.contact_state.RULE_ORDER` and
// `SURFACES`. The copy is made safe by the parity pin in
// `tests/web/test_contact_router_vocabulary_parity.py`, which reads THIS file as
// text and asserts both lists equal the Python ones, in order — the same
// SEGMENT_ORDER pattern the narration player uses.

export const CONTACT_RULE_ORDER = [
  'resume_pending_capture',
  'unresolved_notification',
  'first_contact_after_gap',
  'default',
] as const;

export const CONTACT_SURFACES = [
  'home',
  'chat',
  'feed',
  'brief',
  'deck',
  'player',
  'ingest',
  'batch',
] as const;

export type ContactRule = (typeof CONTACT_RULE_ORDER)[number];
export type ContactSurface = (typeof CONTACT_SURFACES)[number];

// Surface → the route it opens. Client-only (the server speaks surface names and
// has no opinion about URLs), but EXHAUSTIVE over the vocabulary by type: adding
// a surface without a path stops compiling rather than routing to `undefined`.
export const SURFACE_PATHS: Record<ContactSurface, string> = {
  home: '/',
  chat: '/chat',
  feed: '/feed',
  // The brief SURFACE now opens the player: /brief is retired and the player
  // replaces it, carrying the narration AND the brief text. The surface NAME
  // stays 'brief' — it is wire vocabulary, parity-pinned against Python's
  // SURFACES, and the parity test compares map KEYS. Only the route moved.
  brief: '/player',
  deck: '/deck',
  player: '/player',
  ingest: '/ingest',
  batch: '/batch',
};

export function isContactSurface(raw: unknown): raw is ContactSurface {
  return typeof raw === 'string' && (CONTACT_SURFACES as readonly string[]).includes(raw);
}

// --- the wire shape ---------------------------------------------------------

export interface DayStateLevers {
  gap_hours_new_day: number;
  brief_read_decay_hours: number;
}

/**
 * `GET /api/day/state`. Optional fields are optional ON PURPOSE — rule 1's
 * inputs are absent rather than false while the rule is unarmed, so a client
 * that starts reading them gets `undefined` (visibly missing) instead of a
 * fabricated `false` that looks like a real answer.
 */
export interface DayState {
  last_session_ended: string | null;
  time_since_last_session_hours: number | null;
  brief_read_today: boolean;
  unresolved_flagged_notifications: number;
  first_unresolved_notification_id: string | null;
  last_active_surface: string;
  rule_order: string[];
  armed_rules: string[];
  unarmed_rules: Record<string, string>;
  adopted_defaults: Record<string, string>;
  levers: DayStateLevers;
  levers_source: string;
  configured: boolean;
}

export interface RouteDecision {
  rule: ContactRule;
  surface: ContactSurface;
  path: string;
  /** Set when the surface came from an operator-adopted default, not the rule. */
  adopted: boolean;
  /** The notification the feed should land on, when rule 2 fired. */
  scrollTo?: string;
}

// --- evaluation -------------------------------------------------------------

/**
 * The surface each rule opens when it fires, per the spec's rule set. Rule 1 is
 * present and maps to nothing: it is unarmed, the server never lists it in
 * `armed_rules`, and a `null` here is what makes "unarmed" unroutable by
 * construction rather than by remembering to check.
 */
const RULE_SURFACE: Record<ContactRule, ContactSurface | null> = {
  resume_pending_capture: null,
  unresolved_notification: 'feed',
  first_contact_after_gap: 'brief',
  default: 'chat',
};

function ruleFires(rule: ContactRule, state: DayState): boolean {
  switch (rule) {
    case 'unresolved_notification':
      return state.unresolved_flagged_notifications > 0;
    case 'first_contact_after_gap': {
      const hours = state.time_since_last_session_hours;
      // A null gap means there is no previous contact at all — a first-ever
      // open. That IS "first contact after a gap" in every sense the rule
      // cares about, so it fires (subject to the brief not already being read).
      const gapMet = hours === null || hours > state.levers.gap_hours_new_day;
      return gapMet && !state.brief_read_today;
    }
    case 'default':
      return true;
    // Rule 1 never reaches here: it is filtered out by `armed_rules` before
    // evaluation. Returning false rather than throwing keeps an armed-list /
    // vocabulary disagreement from breaking the open — the router degrades to
    // the next rung instead of failing the app-open.
    default:
      return false;
  }
}

/**
 * Evaluate the rule set in the spec's priority order; return what to open.
 *
 * Returns `null` when the router must NOT act — an unconfigured instance, or a
 * payload whose armed rules produce no decision. Staying where the operator
 * already is is the fail-safe: a router that guesses on missing state moves them
 * somewhere they did not ask to be, and there is no undo for attention.
 */
export function evaluateRoute(state: DayState | null): RouteDecision | null {
  if (!state || !state.configured) return null;

  const armed = new Set(state.armed_rules);
  // The SERVER's order, not this file's: the priority order lives in the policy,
  // and reading it from the payload means a reordering there is honoured here
  // without a deploy. Falls back to the local copy only if the payload omits it.
  const order = state.rule_order.length ? state.rule_order : CONTACT_RULE_ORDER;

  for (const raw of order) {
    if (!armed.has(raw)) continue;
    const rule = raw as ContactRule;
    if (!ruleFires(rule, state)) continue;

    // An operator-adopted default wins over the rule's own surface — that tap
    // is the only thing in C4 that changes what the router does.
    const adoptedRaw = state.adopted_defaults?.[rule];
    const adopted = isContactSurface(adoptedRaw);
    const surface = adopted ? (adoptedRaw as ContactSurface) : RULE_SURFACE[rule];
    if (!surface) continue;

    const decision: RouteDecision = {
      rule,
      surface,
      path: SURFACE_PATHS[surface],
      adopted,
    };
    // Only meaningful on the rule that has a notification to point at, and only
    // when the surface is still the feed (an adopted default may have moved it).
    if (
      rule === 'unresolved_notification'
      && surface === 'feed'
      && state.first_unresolved_notification_id
    ) {
      decision.scrollTo = state.first_unresolved_notification_id;
    }
    return decision;
  }
  return null;
}
