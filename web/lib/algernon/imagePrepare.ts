/**
 * The one preparation an image goes through before ANY upload door (#83).
 *
 * Extracted from the chat Composer's `addFiles` so the bulk scan form runs the
 * SAME sequence. Two copies would drift, and the drift is invisible until an
 * image the composer sends fine is refused by the batch door, or the reverse.
 *
 * WHY ITS OWN MODULE rather than another export of `imageDownscale`. A function
 * living beside `downscaleImage` would call it through the module's own
 * closure, and an intra-module call cannot be intercepted by
 * `vi.mock('imageDownscale')`. The Composer's wiring pin
 * (`composerDownscale.test.tsx`) works precisely by swapping `downscaleImage`
 * for a marker-returning spy and asserting the marker's bytes reach `onSend`.
 * Keeping this here — a CROSS-module import — is what keeps that pin able to
 * see through the helper to the call underneath it. The alternative was a
 * helper whose composition no test could observe, which is how the "downscale
 * before the gate" order would silently invert.
 */

import {
  STEP_DOWN_EDGE_PX,
  STEP_DOWN_QUALITY,
  downscaleImage,
} from './imageDownscale';

export interface PreparedImage {
  /** The file to upload — downscaled, stepped down, or the original. */
  file: File;
  /** False when even the step-down could not get it under `maxBytes`. */
  withinBudget: boolean;
}

/**
 * Prepare one picked image for upload: downscale, then step down if still heavy.
 *
 * The ORDER is the load-bearing part and is easy to get backwards: downscale
 * runs BEFORE the size gate. A 4000px scan is routinely over the per-image byte
 * cap at full size and comfortably under it once capped at the tier resolution,
 * so gating first would reject images that upload perfectly well.
 *
 * Then ONE step-down retry — smaller edge, lower quality — for the dense
 * full-page scan that clears the budget even at the tier cap. Refusing over a
 * byte budget we can simply re-encode into would break `imageDownscale`'s
 * never-blocks-the-user principle. One retry, not a loop: a second failure
 * means the image is genuinely unusable here and the operator needs telling
 * rather than being made to wait.
 *
 * Never throws — `downscaleImage` returns the original on any failure, so the
 * worst case is an unmodified file judged against the same gate it would have
 * met anyway.
 */
export async function prepareImageForUpload(
  file: File,
  maxBytes: number,
): Promise<PreparedImage> {
  let prepared = (await downscaleImage(file)).file;
  if (prepared.size > maxBytes) {
    prepared = (await downscaleImage(file, STEP_DOWN_EDGE_PX, STEP_DOWN_QUALITY)).file;
  }
  return { file: prepared, withinBudget: prepared.size <= maxBytes };
}
