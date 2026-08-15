/**
 * The unified composer's deploy gate (#97).
 *
 * LIVE: set in the box build, so /chat serves the unified composer today. The
 * flag's default is still OFF — that was the whole point while the chip
 * defaults awaited their operator walkthrough, since those defaults decide
 * where a document ends up — but the walkthrough happened and the variable is
 * baked ON. Absent now means the ROLLBACK door, not the shipped one.
 *
 * ROLLBACK IS A REBUILD. `NEXT_PUBLIC_*` is inlined into the bundle at build
 * time (see the read below), so unsetting this on a running box changes
 * nothing: the old value is already compiled into the JavaScript being served.
 * Clearing it and rebuilding is the rollback; clearing it alone is a no-op that
 * looks like one.
 *
 * A DISPLAY flag, in the same family as NEXT_PUBLIC_VOICE_ENABLED: it decides
 * which composer renders and nothing else. Every route behind it keeps its own
 * server-side gate (a target that is not configured refuses, flag or no flag),
 * so turning this on cannot enable a pipeline that was not already reachable
 * from its own page.
 *
 * Read per-render (Next inlines NEXT_PUBLIC_* to a literal at build) so the flag
 * is testable via stubbed env without a module reset — the pattern VoicePanel
 * uses.
 */

/** Values that mean ON. Anything else — including unset and blank — is OFF. */
const TRUTHY = ['1', 'true', 'on', 'yes'];

export function unifiedComposerEnabled(): boolean {
  const raw = process.env.NEXT_PUBLIC_UNIFIED_COMPOSER;
  return TRUTHY.includes((raw ?? '').trim().toLowerCase());
}
