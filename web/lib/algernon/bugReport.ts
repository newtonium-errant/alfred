/**
 * Pure, dependency-free helpers for the in-app bug reporter (#95).
 *
 * No React, no html2canvas, no fetch — so the bounds and the context shape are
 * unit-testable without booting a browser. The box is the authority on every
 * limit here (`src/alfred/transport/routes_bugreport.py`); these are mirrors,
 * for two reasons that are not the same reason:
 *
 *   * UX — we never round-trip a value the box would only bounce back, and the
 *     character counter the reporter watches is honest about the real ceiling.
 *   * Defense in depth — the BFF's zod schema derives its bounds from these
 *     constants, so an oversize payload is refused at the edge rather than
 *     relayed onward.
 *
 * A mirror that DRIFTS is worse than no mirror, because the client would refuse
 * something the box accepts (or promise something it does not). The pin lives
 * in `tests/test_bugreport_route.py::test_client_caps_match_box_caps`, which
 * reads both sides.
 */

/** Description ceiling — mirrors `DEFAULT_BUGREPORT_MAX_DESCRIPTION_CHARS`. */
export const MAX_BUGREPORT_DESCRIPTION_CHARS = 5000;

/** Screenshot ceiling in DECODED bytes — mirrors `DEFAULT_BUGREPORT_MAX_SCREENSHOT_BYTES`. */
export const MAX_BUGREPORT_SCREENSHOT_BYTES = 5 * 1024 * 1024;

/**
 * Accepted screenshot MIME types — mirrors `ALLOWED_SHOT_MEDIA_TYPES` on the box.
 * The auto-capture always produces PNG; the other two exist for a future
 * attach-a-file affordance and cost nothing to allow now.
 */
export const ALLOWED_SHOT_MIME = ['image/png', 'image/jpeg', 'image/webp'] as const;
export type ShotMime = (typeof ALLOWED_SHOT_MIME)[number];

/** True when a blob's MIME type is one the box will store. */
export function isAllowedShotType(mimeType: string | null | undefined): boolean {
  return (ALLOWED_SHOT_MIME as readonly string[]).includes(mimeType ?? '');
}

/**
 * Bound the description to a non-empty, length-capped string; `null` when empty
 * after trimming.
 *
 * `null` is what keeps the Send button disabled rather than round-tripping a
 * value the box refuses with `empty_description`. Over-long input is TRUNCATED
 * rather than rejected: the textarea already caps at the same number, so a
 * longer value can only arrive from a paste that outran the maxLength, and
 * silently keeping the first 5000 characters of a bug report is kinder than
 * refusing the whole thing.
 */
export function boundDescription(value: string): string | null {
  const trimmed = (value ?? '').trim();
  if (trimmed === '') return null;
  return trimmed.length > MAX_BUGREPORT_DESCRIPTION_CHARS
    ? trimmed.slice(0, MAX_BUGREPORT_DESCRIPTION_CHARS)
    : trimmed;
}

// Each context string is capped independently. A pathological user-agent or a
// route carrying a huge query string should not be able to inflate the payload,
// and the box caps these again on arrival.
const MAX_CONTEXT_STR = 512;

function capStr(value: unknown, limit = MAX_CONTEXT_STR): string {
  const s = typeof value === 'string' ? value : '';
  return s.length > limit ? s.slice(0, limit) : s;
}

/** The auto-captured breadcrumb context attached to every report. */
export type BugReportContext = {
  route: string;
  instance: string;
  user_agent: string;
  viewport_w: number;
  viewport_h: number;
  app_version: string;
  ts: string;
};

export type BuildContextInput = {
  route: string;
  instance: string;
  userAgent: string;
  viewportW: number;
  viewportH: number;
  appVersion: string;
  now?: Date;
};

/**
 * Assemble the context block. Numbers are floored to non-negative integers and
 * strings are length-capped, so the shape the box receives is always the shape
 * it validates — a NaN viewport (which `window.innerWidth` can be in a detached
 * test environment) becomes 0, which the box reads as "not captured" rather
 * than as a real zero-pixel viewport.
 */
export function buildBugReportContext(input: BuildContextInput): BugReportContext {
  const w = Number.isFinite(input.viewportW) ? Math.max(0, Math.floor(input.viewportW)) : 0;
  const h = Number.isFinite(input.viewportH) ? Math.max(0, Math.floor(input.viewportH)) : 0;
  return {
    route: capStr(input.route),
    instance: capStr(input.instance, 120),
    user_agent: capStr(input.userAgent),
    viewport_w: w,
    viewport_h: h,
    app_version: capStr(input.appVersion, 120),
    ts: (input.now ?? new Date()).toISOString(),
  };
}

/**
 * Human-readable byte size for refusal copy ("5 MB", "1.4 MB").
 *
 * Refusals name the limit IN THE UNITS THE REPORTER SEES. "Over the limit" is
 * not a sentence anyone can act on; "that screenshot is 7.2 MB and the limit is
 * 5 MB" tells them both that they must shrink it and by roughly how much.
 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '0 MB';
  const mb = bytes / (1024 * 1024);
  if (mb >= 10) return `${Math.round(mb)} MB`;
  if (mb >= 0.1) return `${mb.toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

/**
 * The wire error codes the box can return, mapped to what the reporter reads.
 *
 * Every code gets its OWN sentence. Collapsing them into one "couldn't send
 * that" is the failure this exists to prevent: a screenshot that is too big and
 * a description that is too long need two different actions from the reporter,
 * and a single message tells them neither which one happened nor what to do.
 *
 * `wrong_peer` and `bugreport_not_configured` deliberately do NOT blame the
 * reporter — those are deploy states, and the honest thing is to say the
 * feature is not switched on here rather than to imply they did something wrong.
 */
export function bugReportErrorMessage(code: string, detail?: string): string {
  switch (code) {
    case 'empty_description':
      return 'Please describe what went wrong before sending.';
    case 'description_too_long':
      return `That description is over the ${MAX_BUGREPORT_DESCRIPTION_CHARS.toLocaleString()}-character limit — please shorten it.`;
    case 'screenshot_too_large':
      return `That screenshot is over the ${formatBytes(MAX_BUGREPORT_SCREENSHOT_BYTES)} limit — remove it and send just the description, or attach a smaller one.`;
    case 'unsupported_media_type':
      return 'That image type isn’t supported — PNG, JPEG or WebP only.';
    case 'invalid_base64':
    case 'invalid_screenshot':
    case 'empty_screenshot':
      return 'That screenshot couldn’t be read — remove it and send just the description.';
    case 'bugreport_not_configured':
    case 'not_configured':
      return 'Bug reporting isn’t switched on for this instance yet.';
    case 'wrong_peer':
      return 'Bug reporting is misconfigured on the server — nothing was sent.';
    case 'forbidden':
      return 'Your account isn’t allowed to file bug reports.';
    case 'invalid_session':
      return 'Your session expired — please sign in again.';
    case 'gateway_timeout':
      // HONEST UNCERTAINTY, not a verdict. A 504 means the BFF stopped waiting
      // — it does NOT mean nothing landed. The route writes the report to disk
      // and THEN responds, so a timed-out request may well have filed it. The
      // earlier wording here ("your report was NOT saved") asserted the one
      // thing this status cannot tell us, and it invited the duplicate filing
      // the notification design deliberately refuses to invite elsewhere.
      return 'We didn’t hear back in time. Your report may or may not have been filed — check your notifications before sending it again.';
    case 'unknown_target':
      return 'That instance isn’t configured to receive bug reports.';
    default:
      return detail
        ? `We couldn’t send your report (${detail}).`
        : 'We couldn’t send your report. Please try again.';
  }
}
