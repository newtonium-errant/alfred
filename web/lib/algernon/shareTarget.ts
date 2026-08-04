// Web Share Target payload normalisation (G2, Android pre-switch bundle).
//
// An installed PWA registered as a share target receives the share as a plain
// GET navigation to /share with `title`, `text`, `url` query params (the mapping
// is declared in manifest.webmanifest). This module turns that loose,
// app-dependent payload into the exact shape the existing session-authed ingest
// form expects — and nothing else. It is deliberately pure and DOM-free so the
// derivation rules can be tested without rendering a page.
//
// WHY A SEPARATE DOOR FROM THE BEARER ROUTE: share-target runs INSIDE the
// installed PWA, so it carries the session cookie and belongs on the
// session-authed `/api/ingest/submit`. The bearer route
// (`/api/ingest/shortcut`) is for clients with NO session — Tasker, a
// Quick-Settings tile. Two entry points, two auth models; see
// docs/android_capture_recipes.md.

// Mirrors ingestBodySchema's `title: z.string().trim().min(1).max(300)`.
// `shareTarget.test.ts` cross-checks every derived title against that schema
// rather than trusting this literal to stay in step.
export const SHARE_MAX_TITLE_CHARS = 300;

// Where a share is parked across a sign-in round trip. See the note on
// SHARE_RESTORE_PATH for why the payload cannot simply ride the redirect.
export const SHARE_STASH_KEY = 'algernon.share.pending';

// The post-sign-in return path. It carries NO payload on purpose: `safeNextPath`
// rejects any codepoint <= 0x20, so a `next=` holding real shared text (which
// almost always contains a space) would be silently downgraded to '/' and the
// capture LOST. The text goes to sessionStorage instead and this path just says
// "come back and restore". `shareTarget.test.ts` pins that safeNextPath passes
// this exact string through unchanged.
export const SHARE_RESTORE_PATH = '/share?restore=1';

// Honest provenance when the share carried no URL to attribute it to. Neutral by
// design — this door is not Android-specific even though Android is what makes it
// reachable today. The operator can edit it before submitting.
export const SHARE_SOURCE_FALLBACK = 'Share sheet';

// Used only when a share arrives with no words at all to derive a title from.
export const SHARE_TITLE_FALLBACK = 'Shared capture';

type QueryValue = string | string[] | undefined;

export interface SharedCapture {
  title: string;
  body: string;
  source: string;
  /** True when the share carried nothing usable — the page shows an explicit state. */
  empty: boolean;
}

/** A repeated query param arrives as string[]; take the first usable string. */
function first(value: QueryValue): string {
  if (Array.isArray(value)) return typeof value[0] === 'string' ? value[0] : '';
  return typeof value === 'string' ? value : '';
}

/** Device-local `YYYY-MM-DD HHMM`. Deliberately NOT a fixed timezone — this runs
 *  on the operator's own device, so local time is the correct and portable read
 *  (a hardcoded zone would be wrong on any other deploy). */
function localStamp(d: Date): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}${p(d.getMinutes())}`;
}

/**
 * First ~8 words of the shared text plus a local timestamp.
 *
 * The stamp is load-bearing, not decoration: the backend answers a duplicate
 * title with 409 title_collision, and sharing two selections from the same
 * article would otherwise derive the identical title and bounce. The lead is
 * truncated to leave room for the stamp so uniqueness survives the length cap.
 */
export function deriveShareTitle(body: string, now: Date): string {
  const suffix = ` — shared ${localStamp(now)}`;
  const room = SHARE_MAX_TITLE_CHARS - suffix.length;
  const lead = body.trim().split(/\s+/).slice(0, 8).join(' ');
  return `${(lead || SHARE_TITLE_FALLBACK).slice(0, room)}${suffix}`;
}

/**
 * Normalise a share-target navigation into a prefillable capture.
 *
 * Body prefers the shared selection (`text`) and falls back to `url`, because
 * sharing a bare link from some apps sends only `url` — falling back means a
 * real share is never dropped as "empty". `source` takes the URL when there is
 * one, which is exactly what the ingest form's Source field wants.
 */
export function parseSharedCapture(
  query: Record<string, QueryValue>,
  now: Date,
): SharedCapture {
  const sharedTitle = first(query.title).trim();
  const sharedText = first(query.text);
  const sharedUrl = first(query.url).trim();

  const body = sharedText.trim() ? sharedText : sharedUrl;
  const empty = body.trim().length === 0;

  return {
    title: (sharedTitle || deriveShareTitle(body, now)).slice(0, SHARE_MAX_TITLE_CHARS),
    body,
    source: sharedUrl || SHARE_SOURCE_FALLBACK,
    empty,
  };
}

// --- sign-in round-trip parking ---------------------------------------------
// All three swallow storage errors: Android private mode / quota / a disabled
// storage partition must never take down the capture surface. The in-page copy
// of the capture is the primary; the stash is only what survives a navigation.

export function stashSharedCapture(capture: SharedCapture): void {
  try {
    sessionStorage.setItem(SHARE_STASH_KEY, JSON.stringify(capture));
  } catch {
    /* storage unavailable — the page still holds the capture in state. */
  }
}

export function readStashedCapture(): SharedCapture | null {
  try {
    const raw = sessionStorage.getItem(SHARE_STASH_KEY);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return null;
    const rec = parsed as Record<string, unknown>;
    // A stash with no usable body is indistinguishable from no stash at all.
    if (typeof rec.body !== 'string' || rec.body.trim().length === 0) return null;
    return {
      title: typeof rec.title === 'string' ? rec.title : '',
      body: rec.body,
      source: typeof rec.source === 'string' && rec.source ? rec.source : SHARE_SOURCE_FALLBACK,
      empty: false,
    };
  } catch {
    return null;
  }
}

export function clearStashedCapture(): void {
  try {
    sessionStorage.removeItem(SHARE_STASH_KEY);
  } catch {
    /* nothing to clear if storage is unavailable. */
  }
}
