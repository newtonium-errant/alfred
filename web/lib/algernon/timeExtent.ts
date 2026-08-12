// ═══════════════ D7 — time-extent rendering ═══════════════
//
// The ratified ruling (sketch review 2026-08-11, amendment (a)): wherever an
// item carries a real interval, render it as ITS EXTENT rather than as a
// timestamp. The Waterline sketch's portable insight — most of what Algernon
// holds is already timed, and a screen of cards that re-sorts everything by
// urgency throws that structure away. Fog from 11:00 to 15:00 is four hours,
// not a bullet at 11:00.
//
// The backend half already shipped (`alfred.feed.model.FeedItem` gained
// `starts_at` / `ends_at`; the `event` and `weather` producers stamp them).
// This is the render half, and it lives in lib/ rather than in a card because
// the two producers that stamp extents today are both FYI kinds — so the FEED
// is where this has data, and the deck is simply the first surface wired to
// show one if a decide kind ever carries it.
//
// BOTH FIELDS ARE INDEPENDENTLY OPTIONAL, and the backend's contract is
// explicit that `ends_at = null` means "no known end" and NEVER "ends
// immediately". An instant (the 09:30 run) is start-only and must not be
// rendered as a zero-length interval.

/** What kind of time an item carries. `none` is the overwhelmingly common case. */
export type ExtentKind = 'none' | 'instant' | 'interval' | 'all_day' | 'unparseable';

export interface ExtentRead {
  kind: ExtentKind;
  start: Date | null;
  end: Date | null;
  /** The wire values, kept so an unparseable extent can be shown rather than dropped. */
  rawStart: string | null;
  rawEnd: string | null;
}

/** The shape this reads off — a structural subset of FeedItem, so anything can pass one. */
export interface HasExtent {
  starts_at?: string | null;
  ends_at?: string | null;
}

const DATE_ONLY = /^\d{4}-\d{2}-\d{2}$/;

function parse(raw: string | null | undefined): Date | null {
  if (typeof raw !== 'string' || !raw.trim()) return null;
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? null : d;
}

/**
 * Read an item's interval extent.
 *
 * `unparseable` is a REAL, reported outcome rather than a silent fallback to
 * `none`. The backend takes the same position on its side (a present-but-
 * unparseable extent logs `upcoming_events.extent_unparseable` rather than
 * quietly dropping), and for the same reason: "this event has no time" and "we
 * were told a time and could not read it" are different facts, and collapsing
 * them makes a data bug indistinguishable from a data absence.
 *
 * An end that is not after its start is also `unparseable` — a backwards
 * interval is a producer bug, and rendering it as a tidy negative duration
 * would be laundering it.
 */
export function readTimeExtent(item: HasExtent | null | undefined): ExtentRead {
  const rawStart = item?.starts_at ?? null;
  const rawEnd = item?.ends_at ?? null;
  const base: ExtentRead = { kind: 'none', start: null, end: null, rawStart, rawEnd };

  const hasStart = typeof rawStart === 'string' && rawStart.trim() !== '';
  const hasEnd = typeof rawEnd === 'string' && rawEnd.trim() !== '';
  if (!hasStart && !hasEnd) return base;

  // An end with no start is not an interval and not an instant — there is
  // nothing coherent to draw, and pretending otherwise invents a beginning.
  if (!hasStart) return { ...base, kind: 'unparseable' };

  const start = parse(rawStart);
  if (start === null) return { ...base, kind: 'unparseable' };

  // Date-only means an all-day item. Checked on the STRING, because
  // `new Date("2026-08-11")` happily invents midnight UTC and would otherwise
  // turn an all-day event into a zero-length one — the same trap the backend
  // hit and pinned on its side.
  const allDay = DATE_ONLY.test(rawStart.trim());

  if (!hasEnd) {
    return { ...base, kind: allDay ? 'all_day' : 'instant', start };
  }

  const end = parse(rawEnd);
  if (end === null || end.getTime() < start.getTime()) {
    return { ...base, kind: 'unparseable', start };
  }
  // An end EQUAL to the start is a point, not a span.
  if (end.getTime() === start.getTime()) {
    return { ...base, kind: allDay ? 'all_day' : 'instant', start };
  }
  return { ...base, kind: allDay ? 'all_day' : 'interval', start, end };
}

export interface ExtentFormatOptions {
  /** Injected in tests; defaults to the real clock. */
  now?: Date;
  /** Injected in tests so an assertion doesn't depend on the runner's zone. */
  timeZone?: string;
  locale?: string;
}

/** How long a span is, in the coarsest honest unit. */
export function formatDuration(ms: number): string {
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rem = minutes % 60;
  if (hours < 24) return rem === 0 ? `${hours}h` : `${hours}h ${rem}m`;
  const days = Math.floor(hours / 24);
  const remH = hours % 24;
  return remH === 0 ? `${days}d` : `${days}d ${remH}h`;
}

function clock(d: Date, opts: ExtentFormatOptions): string {
  return new Intl.DateTimeFormat(opts.locale, {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    timeZone: opts.timeZone,
  }).format(d);
}

function day(d: Date, opts: ExtentFormatOptions): string {
  return new Intl.DateTimeFormat(opts.locale, {
    weekday: 'short',
    day: 'numeric',
    month: 'short',
    timeZone: opts.timeZone,
  }).format(d);
}

function sameDay(a: Date, b: Date, opts: ExtentFormatOptions): boolean {
  const f = new Intl.DateTimeFormat(opts.locale ?? 'en-CA', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    timeZone: opts.timeZone,
  });
  return f.format(a) === f.format(b);
}

export interface FormattedExtent {
  /** The span itself — "11:00 – 15:00", or the instant, or the day. */
  label: string;
  /** "4h" for a real interval; null for an instant or an all-day item. */
  duration: string | null;
  /** A full sentence for assistive tech, where a dash is not read as "to". */
  description: string;
}

/**
 * Render an extent as an extent.
 *
 * Returns null for `none` — an item with no time dimension draws NO empty
 * chrome. That is deliberate rather than defensive: most feed items have no
 * time, and a row of blank time slots would make the ones that do have a time
 * harder to spot, which is the opposite of what D7 is for.
 */
export function formatTimeExtent(
  read: ExtentRead,
  opts: ExtentFormatOptions = {},
): FormattedExtent | null {
  if (read.kind === 'none') return null;

  if (read.kind === 'unparseable') {
    // ILB: say that a time was carried and could not be read, and show what was
    // actually on the wire. Silently rendering nothing here would make a
    // producer bug look exactly like an item that simply has no time.
    const raw = [read.rawStart, read.rawEnd].filter(Boolean).join(' → ') || '(empty)';
    return {
      label: 'Unreadable time',
      duration: null,
      description: `This item carries a time that could not be read: ${raw}`,
    };
  }

  const start = read.start as Date;

  if (read.kind === 'all_day') {
    const label = read.end && !sameDay(start, read.end, opts)
      ? `${day(start, opts)} – ${day(read.end, opts)}`
      : day(start, opts);
    return { label, duration: null, description: `All day, ${label}` };
  }

  if (read.kind === 'instant') {
    const label = clock(start, opts);
    return { label, duration: null, description: `At ${label} on ${day(start, opts)}` };
  }

  const end = read.end as Date;
  const crossesDay = !sameDay(start, end, opts);
  const label = crossesDay
    ? `${day(start, opts)} ${clock(start, opts)} – ${day(end, opts)} ${clock(end, opts)}`
    : `${clock(start, opts)} – ${clock(end, opts)}`;
  const duration = formatDuration(end.getTime() - start.getTime());
  return {
    label,
    duration,
    description: `From ${clock(start, opts)} to ${clock(end, opts)}${crossesDay ? '' : ''}, lasting ${duration}`,
  };
}

/**
 * How far through its own extent an interval currently is, 0..1 — or null when
 * there is no span to be part-way through.
 *
 * This is what makes the render an EXTENT rather than a label: a four-hour
 * window that is two hours old should look half spent. Clamped at both ends, so
 * a window that has not opened reads empty and one that has closed reads full
 * rather than overflowing.
 */
export function extentProgress(read: ExtentRead, now: Date = new Date()): number | null {
  if (read.kind !== 'interval' || !read.start || !read.end) return null;
  const span = read.end.getTime() - read.start.getTime();
  if (span <= 0) return null;
  const elapsed = now.getTime() - read.start.getTime();
  return Math.min(1, Math.max(0, elapsed / span));
}
