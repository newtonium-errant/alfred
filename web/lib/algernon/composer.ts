// Pure home-composer rules (B3-3). The landing composes one of three views from
// the operator's America/Halifax local hour — a rule-based v0 (the Shapeshifter's
// trust ramp); the learning from composer_log is Phase D. DOM-free so the exact
// boundary hours are unit-pinned.

export type ComposeMode = 'brief' | 'checkin' | 'feed';

/** The morning window, inclusive start / exclusive end. */
export const BRIEF_WINDOW_START_HOUR = 5;
export const BRIEF_WINDOW_END_HOUR = 10;

/**
 * The composed view for a Halifax local hour (0–23):
 *   0 ≤ hour < 5     → 'feed'    (night: nothing is "morning" yet)
 *   5 ≤ hour < 10    → 'brief'   (morning: the brief leads)
 *   11 ≤ hour < 14   → 'checkin' (midday: rings + what needs you)
 *   otherwise        → 'feed'    (the awareness board leads)
 *
 * RATIFIED edges (2026-07-30 sketch): 10:00–10:59 → 'feed' (10 is not < 10 and
 * not ≥ 11), and ≥ 14:00 → 'feed'.
 *
 * The 05:00 LOWER BOUND is #51. The rule was `hour < 10` with no floor, so
 * 00:00–04:59 composed as morning: at 00:07 the home page led with "Your
 * morning brief is ready" beside correctly-rolled-over empty rings — two
 * elements on one screen disagreeing about what day it was. Midnight is not
 * morning; the night hours fall through to the quiet default.
 *
 * This half is the smaller one. A window is a guess about when the artifact
 * exists, and the brief actually generates ~06:00 — so the card must ALSO check
 * the date of what it is advertising. See `briefFreshness`: a stale artifact
 * must never call itself fresh, whatever the clock says.
 */
export function composeMode(hour: number): ComposeMode {
  if (hour >= BRIEF_WINDOW_START_HOUR && hour < BRIEF_WINDOW_END_HOUR) return 'brief';
  if (hour >= 11 && hour < 14) return 'checkin';
  return 'feed';
}

/**
 * The America/Halifax local hour (0–23) for a Date, via the same Intl TZ pattern
 * ingest/shortcut.ts uses. `hour12: false` can render midnight as '24' in some
 * ICU builds — normalise with % 24.
 */
export function halifaxHour(d: Date): number {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Halifax',
    hour: '2-digit',
    hour12: false,
  }).formatToParts(d);
  const raw = parts.find((p) => p.type === 'hour')?.value ?? '0';
  const h = parseInt(raw, 10);
  return Number.isFinite(h) ? h % 24 : 0;
}

/** The composed mode for a given instant (Halifax-local). */
export function composeModeForDate(d: Date): ComposeMode {
  return composeMode(halifaxHour(d));
}

/**
 * `YYYY-MM-DD` for an instant in the instance timezone — the calendar-day
 * identity the brief's own `date` field is written in.
 *
 * Same shape as `rings.instanceDayKey`, duplicated rather than imported because
 * that one is module-private and `rings` is about slot items; hoisting it into a
 * shared time module is a bigger move than #51. If a third copy appears, hoist.
 */
export function halifaxDayKey(d: Date): string {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/Halifax',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(d);
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')}`;
}

/** Whether the brief the card is about to advertise is today's, older, or absent. */
export type BriefFreshness = 'today' | 'stale' | 'none';

/**
 * Compare the brief's own `date` against today, in the instance timezone.
 *
 * STRING COMPARISON, DELIBERATELY. `date` is a bare `YYYY-MM-DD` already written
 * in instance-local terms by the daemon, so it is compared to the day key of
 * `now` directly. It must NOT be fed through `Date` first: `new Date('2026-08-05')`
 * parses as UTC midnight, which is 21:00 on 2026-08-04 in Halifax — measured — so
 * routing a date-only string through the existing `rings.isTodayInstanceTz`
 * helper would report TODAY's brief as stale every single day, inverting exactly
 * the honesty this function exists to provide.
 *
 * `none` for a null/blank date is distinct from `stale` on purpose: an empty
 * spool ("no brief has ever landed") and a yesterday's-brief are different facts
 * and the card says different things about them.
 */
export function briefFreshness(
  briefDate: string | null | undefined,
  now: Date = new Date(),
): BriefFreshness {
  const raw = (briefDate ?? '').trim();
  if (!raw) return 'none';
  return raw === halifaxDayKey(now) ? 'today' : 'stale';
}

/** The brief card's headline — never claims freshness it cannot support. */
export function briefCardHeadline(freshness: BriefFreshness): string {
  if (freshness === 'today') return 'Your morning brief is ready';
  if (freshness === 'stale') return "Yesterday's brief";
  return 'No brief yet';
}

/**
 * The brief card's supporting line. Both non-today cases name WHEN today's
 * arrives, because "not ready" without "and here is when" is the half-answer
 * that sends the operator to check again in five minutes.
 */
export function briefCardDetail(freshness: BriefFreshness, instanceName: string): string {
  if (freshness === 'today') {
    return `The brief and Daily Sync ${instanceName} prepared — open to read →`;
  }
  if (freshness === 'stale') {
    return "Still readable — today's arrives around 6:00 →";
  }
  return "Today's arrives around 6:00 →";
}
