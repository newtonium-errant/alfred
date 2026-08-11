/**
 * The unified composer's deploy gate (#97).
 *
 * DEFAULTS OFF, and the default is the whole point: the chip defaults decide
 * where an operator's documents go, and they get an operator walkthrough before
 * anything routes by them. Until this is set the chat page mounts the composer
 * that has been live all along, unchanged — so the flag being absent is not a
 * degraded mode, it is the shipped one.
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
