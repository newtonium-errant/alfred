// Pure home-composer rules (B3-3). The landing composes one of three views from
// the operator's America/Halifax local hour — a rule-based v0 (the Shapeshifter's
// trust ramp); the learning from composer_log is Phase D. DOM-free so the exact
// boundary hours are unit-pinned.

export type ComposeMode = 'brief' | 'checkin' | 'feed';

/**
 * The composed view for a Halifax local hour (0–23):
 *   hour < 10        → 'brief'   (morning: the brief leads)
 *   11 ≤ hour < 14   → 'checkin' (midday: rings + what needs you)
 *   otherwise        → 'feed'    (the awareness board leads)
 *
 * RATIFIED edges (2026-07-30 sketch): 10:00–10:59 → 'feed' (10 is not < 10 and
 * not ≥ 11), and ≥ 14:00 → 'feed'.
 */
export function composeMode(hour: number): ComposeMode {
  if (hour < 10) return 'brief';
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
