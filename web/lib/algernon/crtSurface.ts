/**
 * The utility rooms' shared surface name — ingest, batch, login.
 *
 * ONE register across THREE pages, which is the whole point: these are the
 * rooms the operator visits to do a job and leave. They do not each deserve an
 * identity; they deserve the SAME one, so that arriving in any of them reads as
 * "you are at a terminal" rather than as three unrelated screens.
 *
 * Phosphor-terminal: a near-black ground with terminal-green text. The
 * constraint that shapes every token below it — and the one most likely to be
 * lost in a later edit — is that **the four function roles keep their hues
 * inside CRT**. A register restyles CHROME. It never restyles a verdict: an
 * error is the negative hue on every surface in the app, because an operator
 * who has learned one colour vocabulary must not have to re-learn it per room.
 *
 * Same open-seam contract as `viewscreenSurface.ts` — not in `KnownSurface`.
 */
export const CRT_SURFACE = 'crt';
