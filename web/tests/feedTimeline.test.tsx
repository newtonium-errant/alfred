import { describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { TimelineView } from '../components/feed/TimelineView';
import type { FeedItem } from '../lib/algernon/feed';

// The timeline VIEW of the Awareness feed (D7 graduated from the Waterline).
// Plain DOM assertions, no jest-dom — house idiom.
//
// Every instant is built from LOCAL date parts stamped with this machine's own
// offset, so the pins assert wall-clock facts in any timezone.

const pad = (n: number) => String(Math.abs(Math.trunc(n))).padStart(2, '0');
function localIso(y: number, mo: number, d: number, h: number, mi = 0): string {
  const dt = new Date(y, mo - 1, d, h, mi, 0, 0);
  const offMin = -dt.getTimezoneOffset();
  const sign = offMin >= 0 ? '+' : '-';
  return `${y}-${pad(mo)}-${pad(d)}T${pad(h)}:${pad(mi)}:00${sign}${pad(offMin / 60)}:${pad(offMin % 60)}`;
}
const localMs = (y: number, mo: number, d: number, h: number, mi = 0) =>
  new Date(y, mo - 1, d, h, mi, 0, 0).getTime();

const NOW = localMs(2026, 8, 12, 8);

function item(over: Partial<FeedItem> & { id: string }): FeedItem {
  return {
    kind: 'event',
    instance: 'salem',
    title: `title ${over.id}`,
    mode: 'fyi',
    attention: 'fyi',
    evidence: {},
    actions: [],
    state: 'open',
    created_at: localIso(2026, 8, 12, 6),
    acted_at: null,
    expires_at: null,
    starts_at: null,
    ends_at: null,
    source_ref: {},
    ...over,
  };
}

const traces = () => screen.queryAllByTestId('timeline-trace');
const traceFor = (id: string) => traces().find((t) => t.textContent?.includes(`title ${id}`));

describe('TimelineView — the empty band reports, never just blanks', () => {
  it('says the band is empty AND that the feed is empty too, when both are', () => {
    render(<TimelineView items={[]} now={NOW} />);
    const empty = screen.getByTestId('timeline-empty');
    expect(empty.textContent).toContain('Nothing on the band in this window');
    expect(empty.textContent).toContain('The feed is empty too');
    // No band is drawn at all — an axis with nothing on it would read as a bug.
    expect(screen.queryByTestId('timeline-band')).toBeNull();
  });

  it('distinguishes an empty BAND from an empty FEED — the whole point of the signal', () => {
    // A feed full of untimed items is the common case today (only the event and
    // weather producers stamp extents). "Nothing is happening" and "nothing here
    // knows when it happens" are different facts and must not read alike.
    render(<TimelineView items={[item({ id: 'u1' }), item({ id: 'u2' })]} now={NOW} />);
    const empty = screen.getByTestId('timeline-empty');
    expect(empty.textContent).toContain('sits in the registers around it');
    expect(empty.textContent).not.toContain('The feed is empty too');
    expect(screen.getByTestId('timeline-untimed').textContent).toContain('Untimed (2)');
  });
});

describe('TimelineView — a moment and an interval are never drawn alike', () => {
  const moment = item({ id: 'run', starts_at: localIso(2026, 8, 12, 9, 30) });
  const interval = item({
    id: 'fog',
    kind: 'weather',
    starts_at: localIso(2026, 8, 12, 11),
    ends_at: localIso(2026, 8, 12, 15),
  });

  it('a MOMENT gets a fixed height and no percentage span', () => {
    render(<TimelineView items={[moment]} now={NOW} />);
    const trace = traceFor('run')!;
    expect(trace.getAttribute('data-moment')).toBe('true');
    // Fixed rem height — NOT a percentage, because `ends_at: null` means "no
    // known end" and a proportional bar would claim a duration.
    expect(trace.style.height).toBe('2.1rem');
    expect(trace.style.height).not.toContain('%');
    // Its label is a single clock time, not a span.
    expect(trace.textContent).toContain('09:30');
    expect(trace.textContent).not.toContain('–');
    // A moment has no duration chip.
    expect(trace.querySelector('[data-testid="timeline-duration"]')).toBeNull();
  });

  it('an INTERVAL gets its real duration as a proportional height', () => {
    render(<TimelineView items={[interval]} now={NOW} />);
    const trace = traceFor('fog')!;
    expect(trace.getAttribute('data-moment')).toBe('false');
    expect(trace.style.height).toContain('%');
    expect(trace.textContent).toContain('11:00 – 15:00');
    expect(trace.querySelector('[data-testid="timeline-duration"]')!.textContent).toBe('4h');
  });

  it('the two coexist on one band, each in its own form', () => {
    // The positive-control pairing: the same render produces both shapes, so
    // neither pin above can be passing because the component draws everything
    // one way.
    render(<TimelineView items={[moment, interval]} now={NOW} />);
    expect(traceFor('run')!.getAttribute('data-moment')).toBe('true');
    expect(traceFor('fog')!.getAttribute('data-moment')).toBe('false');
    expect(traces()).toHaveLength(2);
  });
});

describe('TimelineView — the three registers', () => {
  it('places timed items and lists untimed ones WITHOUT inventing an hour', () => {
    render(
      <TimelineView
        items={[item({ id: 'timed', starts_at: localIso(2026, 8, 12, 9) }), item({ id: 'proposal' })]}
        now={NOW}
      />,
    );
    expect(traces()).toHaveLength(1);
    expect(traceFor('timed')).toBeTruthy();
    expect(traceFor('proposal')).toBeUndefined();

    const untimed = screen.getByTestId('timeline-untimed');
    expect(untimed.textContent).toContain('title proposal');
    expect(untimed.textContent).toContain('No time dimension at all');
    expect(untimed.textContent).toContain('an all-day item has a date');
  });

  it('holds a far-future item in its own register rather than squashing the axis', () => {
    render(
      <TimelineView
        items={[
          item({ id: 'today', starts_at: localIso(2026, 8, 12, 9) }),
          item({ id: 'next-month', starts_at: localIso(2026, 9, 20, 9) }),
        ]}
        now={NOW}
      />,
    );
    expect(traces()).toHaveLength(1);
    expect(screen.getByTestId('timeline-beyond').textContent).toContain('Beyond this window (1)');
    expect(screen.getByTestId('timeline-counts').textContent).toBe('1 on the band · 0 all day · 1 beyond · 0 untimed');
  });

  it('a register that is empty is not rendered at all', () => {
    render(<TimelineView items={[item({ id: 'timed', starts_at: localIso(2026, 8, 12, 9) })]} now={NOW} />);
    expect(screen.queryByTestId('timeline-beyond')).toBeNull();
    expect(screen.queryByTestId('timeline-untimed')).toBeNull();
  });
});

describe('TimelineView — the all-day strip', () => {
  const allDay = item({ id: 'holiday', starts_at: '2026-08-12', ends_at: null });
  const atNine = item({ id: 'run', starts_at: localIso(2026, 8, 12, 9) });
  const nothing = item({ id: 'proposal' });

  it('renders an all-day item in the STRIP, not as a lane on the substrate', () => {
    render(<TimelineView items={[allDay, atNine]} now={NOW} />);
    const strip = screen.getByTestId('timeline-allday');
    expect(strip.textContent).toContain('All day');
    expect(strip.textContent).toContain('title holiday');

    // Not a trace, and — the load-bearing half — it did not open a second lane.
    // A day-spanning bar never frees its lane, so as a lane occupant it would
    // squeeze every clock-positioned trace. One item, one lane, full width.
    expect(traceFor('holiday')).toBeUndefined();
    expect(screen.getByTestId('timeline-band').getAttribute('data-lane-count')).toBe('1');
    expect(traceFor('run')!.style.width).toBe('100%');
  });

  it('narrows Untimed back to the genuinely timeless', () => {
    render(<TimelineView items={[allDay, nothing]} now={NOW} />);
    // The two facts are now told apart: a date without an hour vs no time at all.
    expect(screen.getByTestId('timeline-allday').textContent).toContain('title holiday');
    const untimed = screen.getByTestId('timeline-untimed');
    expect(untimed.textContent).toContain('title proposal');
    expect(untimed.textContent).not.toContain('title holiday');
    expect(screen.getByTestId('timeline-counts').textContent).toBe('0 on the band · 1 all day · 0 beyond · 1 untimed');
  });

  it('an all-day item is selectable, and reaches the same row as a trace', () => {
    render(
      <TimelineView items={[allDay]} now={NOW} renderDetail={(it) => <li data-testid="detail">detail {it.id}</li>} />,
    );
    expect(screen.queryByTestId('timeline-detail')).toBeNull();
    fireEvent.click(screen.getByTestId('timeline-allday-item'));
    expect(screen.getByTestId('timeline-detail').textContent).toContain('detail holiday');
  });

  it('the strip is absent when nothing is all-day', () => {
    render(<TimelineView items={[atNine]} now={NOW} />);
    expect(screen.queryByTestId('timeline-allday')).toBeNull();
  });
});

describe('TimelineView — weather keeps its maybe-window in its own register', () => {
  const station = item({
    id: 'taf',
    kind: 'weather',
    starts_at: localIso(2026, 8, 12, 6),
    ends_at: localIso(2026, 8, 12, 18),
    evidence: {
      station: 'CYZX',
      periods: [
        {
          starts_at: localIso(2026, 8, 12, 6),
          ends_at: localIso(2026, 8, 12, 12),
          change: 'BASE',
          probability: null,
          possible: false,
        },
        {
          starts_at: localIso(2026, 8, 12, 13),
          ends_at: localIso(2026, 8, 12, 16),
          change: 'PROB',
          probability: 30,
          possible: true,
        },
      ],
    },
  });

  it('draws a certain block and a possible block in visibly different registers', () => {
    render(<TimelineView items={[station]} now={NOW} />);
    const blocks = screen.getAllByTestId('timeline-weather-period');
    expect(blocks).toHaveLength(2);

    const certain = blocks.find((b) => b.getAttribute('data-change') === 'BASE')!;
    const maybe = blocks.find((b) => b.getAttribute('data-change') === 'PROB')!;

    // A will: tinted fill, no hatch.
    expect(certain.getAttribute('data-possible')).toBe('false');
    expect(certain.className).not.toContain('sensor-maybe');
    expect(certain.style.backgroundColor).not.toBe('transparent');

    // A maybe: hatched, untinted. A PROB30 window says there is a 30% chance of
    // weather in it, not that weather occupies it — painting the two alike would
    // state as fact what the forecast explicitly qualifies.
    expect(maybe.getAttribute('data-possible')).toBe('true');
    expect(maybe.className).toContain('sensor-maybe');
    expect(maybe.style.backgroundColor).toBe('transparent');
  });

  it('a weather item with no periods still renders its own extent', () => {
    render(<TimelineView items={[item({ id: 'bare', kind: 'weather', starts_at: localIso(2026, 8, 12, 9), ends_at: localIso(2026, 8, 12, 12) })]} now={NOW} />);
    expect(traceFor('bare')).toBeTruthy();
    expect(screen.queryByTestId('timeline-weather-period')).toBeNull();
  });

  it('survives a hostile periods payload without dropping the item', () => {
    render(
      <TimelineView
        items={[item({ id: 'junk', kind: 'weather', starts_at: localIso(2026, 8, 12, 9), evidence: { periods: 'not-an-array' } })]}
        now={NOW}
      />,
    );
    expect(traceFor('junk')).toBeTruthy();
    expect(screen.queryByTestId('timeline-weather-period')).toBeNull();
  });
});

describe('TimelineView — lanes, axis and NOW', () => {
  it('overlapping traces get side-by-side lanes so neither is hidden', () => {
    render(
      <TimelineView
        items={[
          item({ id: 'long', starts_at: localIso(2026, 8, 12, 9), ends_at: localIso(2026, 8, 12, 17) }),
          item({ id: 'inside', starts_at: localIso(2026, 8, 12, 10) }),
        ]}
        now={NOW}
      />,
    );
    expect(screen.getByTestId('timeline-band').getAttribute('data-lane-count')).toBe('2');
    expect(traceFor('long')!.getAttribute('data-lane')).toBe('0');
    expect(traceFor('inside')!.getAttribute('data-lane')).toBe('1');
    // Two lanes → each takes half the width, so neither covers the other.
    expect(traceFor('long')!.style.width).toBe('50%');
    expect(traceFor('inside')!.style.left).toBe('50%');
  });

  it('sequential traces share one full-width lane', () => {
    render(
      <TimelineView
        items={[
          item({ id: 'early', starts_at: localIso(2026, 8, 12, 9), ends_at: localIso(2026, 8, 12, 11) }),
          item({ id: 'late', starts_at: localIso(2026, 8, 12, 15), ends_at: localIso(2026, 8, 12, 17) }),
        ]}
        now={NOW}
      />,
    );
    expect(screen.getByTestId('timeline-band').getAttribute('data-lane-count')).toBe('1');
    expect(traceFor('early')!.style.width).toBe('100%');
  });

  it('the gutter labels and the gridlines are the SAME ticks', () => {
    render(<TimelineView items={[item({ id: 't', starts_at: localIso(2026, 8, 12, 10) })]} now={NOW} />);
    const labels = screen.getAllByTestId('timeline-tick');
    expect(labels.length).toBeGreaterThan(1);
    // Every gutter label sits at a position that a gridline also occupies —
    // one derivation, two consumers.
    const gridTops = Array.from(
      screen.getByTestId('timeline-band').querySelectorAll<HTMLElement>('div[aria-hidden]'),
    ).map((el) => el.style.top);
    for (const label of labels) {
      expect(gridTops).toContain((label as HTMLElement).style.top);
    }
  });

  it('NOW is on the band and carries the clock it was given', () => {
    render(<TimelineView items={[item({ id: 't', starts_at: localIso(2026, 8, 12, 10) })]} now={NOW} />);
    expect(screen.getByTestId('timeline-now')).toBeTruthy();
    expect(screen.getByTestId('timeline-now-label').textContent).toBe('NOW 08:00');
  });
});

describe('TimelineView — selection expands the page’s own row', () => {
  const timed = item({ id: 'ev', starts_at: localIso(2026, 8, 12, 10) });

  it('renders no detail until a trace is selected, then the page’s row', () => {
    const seen: string[] = [];
    render(
      <TimelineView
        items={[timed]}
        now={NOW}
        renderDetail={(it) => {
          seen.push(it.id);
          return <li data-testid="detail-row">detail for {it.id}</li>;
        }}
      />,
    );
    expect(screen.queryByTestId('timeline-detail')).toBeNull();

    fireEvent.click(traceFor('ev')!);
    expect(screen.getByTestId('timeline-detail').textContent).toContain('detail for ev');
    expect(seen).toContain('ev');
    expect(traceFor('ev')!.getAttribute('aria-pressed')).toBe('true');
  });

  it('selecting the same trace again collapses it', () => {
    render(<TimelineView items={[timed]} now={NOW} renderDetail={(it) => <li>detail {it.id}</li>} />);
    fireEvent.click(traceFor('ev')!);
    expect(screen.queryByTestId('timeline-detail')).not.toBeNull();
    fireEvent.click(traceFor('ev')!);
    expect(screen.queryByTestId('timeline-detail')).toBeNull();
    expect(traceFor('ev')!.getAttribute('aria-pressed')).toBe('false');
  });

  it('a trace is a real button, so the band is keyboard-reachable', () => {
    render(<TimelineView items={[timed]} now={NOW} />);
    expect(traceFor('ev')!.tagName).toBe('BUTTON');
    expect(traceFor('ev')!.getAttribute('type')).toBe('button');
  });

  it('carries the sensor-log surface attribute (the skin scope)', () => {
    render(<TimelineView items={[timed]} now={NOW} />);
    expect(screen.getByTestId('feed-timeline').getAttribute('data-surface')).toBe('sensor-log');
  });
});
