// SERVER-ONLY. Web-push VAPID config + the two feature gates (B4). Push is built
// INERT: nothing fires until the operator sets VAPID keys AND flips PUSH_ENABLED.
//
// Two independent gates:
//   * VAPID present (isPushConfigured) — the SUBSCRIBE + PUBLIC-KEY routes need
//     it; any of the three keys absent → those routes 503 not_configured, and the
//     poller never sends. Keys are NEVER committed — generation is a deploy step:
//       npx web-push generate-vapid-keys
//     → set ALGERNON_VAPID_PUBLIC / ALGERNON_VAPID_PRIVATE in the box env, and
//       ALGERNON_VAPID_SUBJECT to a `mailto:` (or https) contact URL.
//   * PUSH_ENABLED — the POLLER gate on top of VAPID; absent/≠"true" → the
//     background sender never constructs (isPushEnabled false). Lets the operator
//     stage keys + subscribe first, then flip the sender on for the trial.

export interface VapidConfig {
  publicKey: string;
  privateKey: string;
  subject: string;
}

/** The VAPID triple, or null when ANY of the three is unset/blank (→ inert). */
export function readVapidConfig(): VapidConfig | null {
  const publicKey = (process.env.ALGERNON_VAPID_PUBLIC || '').trim();
  const privateKey = (process.env.ALGERNON_VAPID_PRIVATE || '').trim();
  const subject = (process.env.ALGERNON_VAPID_SUBJECT || '').trim();
  if (!publicKey || !privateKey || !subject) return null;
  return { publicKey, privateKey, subject };
}

/** True when the VAPID triple is fully set — the subscribe/public-key gate. */
export function isPushConfigured(): boolean {
  return readVapidConfig() !== null;
}

/**
 * True when the background poller may run: PUSH_ENABLED === "true" AND VAPID is
 * configured. The poller singleton checks this and no-ops otherwise, so an
 * unset/false flag means the interval is never even constructed.
 */
export function isPushEnabled(): boolean {
  return (process.env.PUSH_ENABLED || '').trim().toLowerCase() === 'true' && isPushConfigured();
}
