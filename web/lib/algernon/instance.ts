// SHARED (browser + server). The "home" instance this web deployment signs in
// against — the sole session-minting instance (Salem in the canonical deploy).
// Cross-instance chat relays to OTHER instances via per-target peer tokens (see
// transport.ts listChatTargets/callChatTo); the home instance keeps the existing
// session-token path UNCHANGED.
//
// Config-driven via NEXT_PUBLIC_INSTANCE_NAME (inlined into the browser bundle at
// build, also readable server-side) — NOT a hardcoded instance literal, matching
// the no-hardcoded-instance-name discipline used in Layout/index/ingest. The
// 'Algernon' fallback mirrors those existing call sites; a real deploy always
// sets NEXT_PUBLIC_INSTANCE_NAME (e.g. "Salem").
export const HOME_INSTANCE_NAME = process.env.NEXT_PUBLIC_INSTANCE_NAME || 'Algernon';

/**
 * True when an instance selector refers to the home instance (or is absent) — the
 * routing predicate every BFF chat route uses to decide session-path vs relay.
 * Case-insensitive so 'salem'/'Salem'/'SALEM' and an absent selector all route
 * home; any other name routes cross-instance (relay).
 */
export function isHomeInstance(instance: string | undefined | null): boolean {
  const v = (instance || '').trim();
  if (!v) return true;
  return v.toUpperCase() === HOME_INSTANCE_NAME.toUpperCase();
}
