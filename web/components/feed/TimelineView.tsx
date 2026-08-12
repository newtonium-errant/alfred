import { type ReactNode, useMemo, useState } from 'react';
import type { FeedItem } from '../../lib/algernon/feed';
import { kindLabel } from '../../lib/algernon/feedConstants';
import {
  type BandPlacement,
  type TimeWindow,
  byTimeOrder,
  formatClock,
  formatDuration,
  formatExtent,
  hourTicks,
  itemExtent,
  packLanes,
  placeExtent,
  positionPct,
  splitByWindow,
  timelineWindow,
  weatherPeriods,
} from '../../lib/algernon/feedTime';

// THE TIMELINE — the Awareness feed rendered as a time substrate rather than a
// list. This is the Waterline's portable insight (D7), graduated by the operator
// as a selectable VIEW OF THE SAME FEED, not a new surface: identical items,
// identical verbs, different layout.
//
// TIME FLOWS DOWN. The sketch drew a horizontal band; a log reads top to bottom,
// which is the identity this surface was given (feed = sensor log), and a
// vertical axis is the one that survives both postures — tablet-as-workstation
// and phone-as-tricorder — without a fixed-height band clipping its own content
// on the narrow one.
//
// WHAT THIS COMPONENT DELIBERATELY DOES NOT DO: judge. D1 put judgment on the
// deck, and the feed's verbs are the feed's. So the band carries no gestures and
// invents no actions — selecting a trace expands the SAME `FeedRow` the list
// view renders, supplied by the page through `renderDetail`. Verb drift between
// the two views is impossible by construction rather than by discipline.

export interface TimelineViewProps {
  items: readonly FeedItem[];
  /** Injectable clock. Defaults to the real one; tests pass a fixed instant. */
  now?: number;
  /**
   * How the page renders a selected item's detail. The page owns this because
   * the row's verbs live on the page's hooks (completion / accept / snooze /
   * ack); the timeline owns layout and nothing else.
   */
  renderDetail?: (item: FeedItem) => ReactNode;
}

/** A trace's own colour register. Weather is the environment, not an errand. */
function traceTone(item: FeedItem): 'weather' | 'quiet' | 'live' {
  if (item.kind === 'weather') return 'weather';
  return item.attention === 'needs_you' ? 'live' : 'quiet';
}

const TONE_STYLE: Record<'weather' | 'quiet' | 'live', { border: string; fill: string; text: string }> = {
  weather: { border: 'var(--sensor-amber2)', fill: 'rgba(201,154,76,0.10)', text: 'var(--sensor-amber)' },
  live: { border: 'var(--sensor-teal2)', fill: 'rgba(79,167,161,0.10)', text: 'var(--sensor-teal)' },
  quiet: { border: 'var(--sensor-slate2)', fill: 'rgba(119,131,155,0.08)', text: 'var(--sensor-slate)' },
};

function clippedClass(p: BandPlacement): string {
  return [p.clippedStart ? 'sensor-clipped-start' : '', p.clippedEnd ? 'sensor-clipped-end' : '']
    .filter(Boolean)
    .join(' ');
}

/** The forecast blocks of a weather item, drawn inside its own column. */
function WeatherPeriods({ item, window: w }: { item: FeedItem; window: TimeWindow }) {
  const periods = weatherPeriods(item.evidence);
  if (periods.length === 0) return null;
  return (
    <div data-testid="timeline-weather-periods" aria-hidden className="pointer-events-none absolute inset-0">
      {periods.map((p, i) => {
        const place = placeExtent(p.extent, w);
        return (
          <div
            key={`${p.change}-${p.extent.start}-${i}`}
            data-testid="timeline-weather-period"
            data-possible={p.possible}
            data-change={p.change}
            className={`absolute inset-x-0 ${p.possible ? 'sensor-maybe' : ''}`}
            style={{
              top: `${place.topPct}%`,
              height: `${Math.max(place.heightPct, 0.6)}%`,
              // A CERTAIN block tints; a MAYBE block is hatched by the class
              // above and left untinted, so the two never read alike. The
              // producer keeps probabilistic blocks out of the item's extent
              // precisely so a renderer can hold them in a separate register.
              backgroundColor: p.possible ? 'transparent' : 'rgba(201,154,76,0.13)',
              borderTop: '1px solid rgba(201,154,76,0.35)',
            }}
          />
        );
      })}
    </div>
  );
}

export function TimelineView({ items, now, renderDetail }: TimelineViewProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // `now` is read once per render rather than per item so every position on a
  // given paint shares one clock — otherwise the NOW line and the traces could
  // disagree by a tick and the band would appear to jitter.
  const clock = now ?? Date.now();

  const { split, window: w, packing } = useMemo(() => {
    const s = splitByWindow(items, clock);
    const win = timelineWindow(s.onBand, clock);
    const ordered = byTimeOrder(s.onBand);
    const entries = ordered.map((item) => ({ item, placement: placeExtent(itemExtent(item)!, win) }));
    return { split: s, window: win, packing: packLanes(entries) };
  }, [items, clock]);

  // Derived ONCE and consumed twice (gutter labels + gridlines). A gutter
  // labelled 09:00 sitting above a line drawn somewhere else is worse than no
  // axis at all, so the two can never be computed independently.
  const ticks = useMemo(() => hourTicks(w), [w.from, w.to]); // eslint-disable-line react-hooks/exhaustive-deps

  const { traces, laneCount } = packing;
  const laneWidth = laneCount > 0 ? 100 / laneCount : 100;
  const nowPct = positionPct(clock, w);
  const selected = selectedId ? items.find((i) => i.id === selectedId) ?? null : null;

  return (
    <section data-surface="sensor-log" data-testid="feed-timeline" className="mt-4 rounded-lg p-3">
      <header className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
        <span className="sensor-label" style={{ color: 'var(--sensor-teal)' }}>
          Sensor log
        </span>
        <span data-testid="timeline-window" className="sensor-num text-[11px]" style={{ color: 'var(--sensor-ink3)' }}>
          {formatClock(w.from)} → {formatClock(w.to)}
        </span>
        <span data-testid="timeline-counts" className="sensor-label ml-auto">
          {split.onBand.length} on the band · {split.beyond.length} beyond · {split.untimed.length} untimed
        </span>
      </header>

      {traces.length === 0 ? (
        // Intentionally-left-blank: an empty band is a REPORT, not a blank. The
        // distinction it draws is the one that matters here — the feed may be
        // full while the band is empty, because only some producers stamp an
        // extent, and "nothing is happening" must never be confused with
        // "nothing here knows when it happens".
        <p data-testid="timeline-empty" className="mt-3 text-sm" style={{ color: 'var(--sensor-ink3)' }}>
          Nothing on the band in this window
          {split.untimed.length + split.beyond.length > 0
            ? ' — every item the feed is holding sits in the registers below.'
            : '. The feed is empty too.'}
        </p>
      ) : (
        <div className="mt-3 flex gap-2">
          {/* the hour gutter */}
          <ol
            data-testid="timeline-axis"
            className="relative h-[460px] w-12 shrink-0 list-none sm:h-[620px]"
            aria-label="Hours"
          >
            {ticks.map((t) => (
              <li
                key={t}
                data-testid="timeline-tick"
                className="sensor-num absolute right-1 -translate-y-1/2 text-[10px]"
                style={{ top: `${positionPct(t, w)}%`, color: 'var(--sensor-ink4)' }}
              >
                {formatClock(t)}
              </li>
            ))}
          </ol>

          {/* the band */}
          <div
            data-testid="timeline-band"
            data-lane-count={laneCount}
            className="sensor-band relative h-[460px] flex-1 overflow-hidden rounded sm:h-[620px]"
            style={{ border: '1px solid var(--sensor-edge)' }}
          >
            {ticks.map((t) => (
              <div
                key={t}
                aria-hidden
                className="absolute inset-x-0 h-px"
                style={{ top: `${positionPct(t, w)}%`, backgroundColor: 'var(--sensor-edge)', opacity: 0.55 }}
              />
            ))}

            {traces.map(({ item, placement, lane }) => {
              const extent = itemExtent(item)!;
              const moment = extent.end === null;
              const tone = TONE_STYLE[traceTone(item)];
              const isSelected = item.id === selectedId;
              return (
                <button
                  key={item.id}
                  type="button"
                  data-testid="timeline-trace"
                  data-kind={item.kind}
                  data-moment={moment}
                  data-lane={lane}
                  aria-pressed={isSelected}
                  onClick={() => setSelectedId(isSelected ? null : item.id)}
                  className={`absolute overflow-hidden rounded-sm px-1.5 pt-1 text-left ${clippedClass(placement)}`}
                  style={{
                    top: `${placement.topPct}%`,
                    // A MOMENT gets a fixed readable height; an INTERVAL gets
                    // its real duration, floored only so a five-minute item
                    // stays tappable. The two are never drawn the same way:
                    // `ends_at: null` means "no known end", and a bar would
                    // claim an end the data never gave.
                    height: moment ? '2.1rem' : `max(${placement.heightPct}%, 2.1rem)`,
                    left: `${lane * laneWidth}%`,
                    width: `${laneWidth}%`,
                    borderLeft: `3px solid ${tone.border}`,
                    backgroundColor: moment ? 'var(--sensor-panel)' : tone.fill,
                    borderTop: moment ? 'none' : `1px solid ${tone.border}`,
                    boxShadow: isSelected ? `0 0 0 1px ${tone.border}` : 'none',
                  }}
                >
                  {item.kind === 'weather' && <WeatherPeriods item={item} window={w} />}
                  <span className="relative flex items-baseline gap-1.5">
                    <span className="sensor-num text-[10px]" style={{ color: tone.text }}>
                      {formatExtent(extent)}
                    </span>
                    {!moment && (
                      <span data-testid="timeline-duration" className="sensor-label text-[8.5px]">
                        {formatDuration(extent)}
                      </span>
                    )}
                  </span>
                  <span
                    className="relative mt-0.5 block truncate text-[11px] leading-tight"
                    style={{ color: 'var(--sensor-ink2)' }}
                  >
                    {item.title || item.id}
                  </span>
                  <span className="sensor-label relative mt-0.5 block truncate text-[8px]">
                    {kindLabel(item.kind)}
                  </span>
                </button>
              );
            })}

            {/* NOW — the one live reading. `timelineWindow` guarantees the band
                contains the present, so this always has a place to stand. */}
            <div
              data-testid="timeline-now"
              aria-hidden
              className="sensor-now pointer-events-none absolute inset-x-0 z-10 h-0.5"
              style={{ top: `${nowPct}%` }}
            />
            <span
              data-testid="timeline-now-label"
              className="sensor-num pointer-events-none absolute right-1 z-10 text-[10px] font-bold"
              style={{ top: `calc(${nowPct}% + 3px)`, color: 'var(--sensor-teal)' }}
            >
              NOW {formatClock(clock)}
            </span>
          </div>
        </div>
      )}

      {selected && renderDetail && (
        // Depth two of the three-depth grammar (glance → expand → investigate):
        // the band is the glance, this is the expand, and it is the page's own
        // row — same component, same verbs, same hooks as the list view.
        <div data-testid="timeline-detail" className="mt-3">
          {renderDetail(selected)}
        </div>
      )}

      {split.beyond.length > 0 && (
        <Register
          testId="timeline-beyond"
          label={`Beyond this window (${split.beyond.length})`}
          note="Timed, but outside the hours the band reaches."
          items={split.beyond}
          renderDetail={renderDetail}
        />
      )}

      {split.untimed.length > 0 && (
        <Register
          testId="timeline-untimed"
          label={`Untimed (${split.untimed.length})`}
          // The sketch's own stated weakness, answered rather than inherited:
          // "genuinely untimed items must be given a time they do not really
          // have." They are not. They are reported as untimed.
          note="No time dimension — listed here rather than placed at an hour they don't have."
          items={split.untimed}
          renderDetail={renderDetail}
        />
      )}
    </section>
  );
}

function Register({
  testId,
  label,
  note,
  items,
  renderDetail,
}: {
  testId: string;
  label: string;
  note: string;
  items: readonly FeedItem[];
  renderDetail?: (item: FeedItem) => ReactNode;
}) {
  return (
    <section data-testid={testId} className="mt-4">
      <h3 className="sensor-label">{label}</h3>
      <p className="mt-0.5 text-[11px]" style={{ color: 'var(--sensor-ink4)' }}>
        {note}
      </p>
      <ul className="mt-2 flex flex-col gap-2">
        {items.map((item) =>
          renderDetail ? (
            <li key={item.id} data-testid={`${testId}-item`}>
              {renderDetail(item)}
            </li>
          ) : (
            <li
              key={item.id}
              data-testid={`${testId}-item`}
              className="rounded px-2 py-1.5 text-[11px]"
              style={{ backgroundColor: 'var(--sensor-panel)', color: 'var(--sensor-ink2)' }}
            >
              {item.title || item.id}
            </li>
          ),
        )}
      </ul>
    </section>
  );
}

