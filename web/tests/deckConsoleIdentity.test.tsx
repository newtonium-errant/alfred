import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// The console identity ON THE DECK — the ratified Phase B visual/interaction
// rulings, driven through the real card and the real deck rather than through
// the token module (which has its own pins).
//
// What is worth pinning here is only the part where a WRONG render would still
// look fine: colour carries meaning on this surface, so a stamp drawn in the
// wrong role does not throw, does not break a layout, and tells the operator
// the opposite of the truth mid-gesture. Everything asserted below is a claim
// the operator relies on at a glance.

const { mockAct } = vi.hoisted(() => ({ mockAct: vi.fn() }));
vi.mock('../lib/algernon/feed', () => ({ feedApi: { act: mockAct, list: vi.fn() } }));

import { Deck } from '../components/feed/Deck';
import { DeckCard } from '../components/feed/DeckCard';
import { instanceAccentClass } from '../lib/algernon/consoleTokens';
import type { FeedItem } from '../lib/algernon/feed';
import { withServedActions } from './helpers/servedActions';

function item(overrides: Partial<FeedItem> = {}): FeedItem {
  return withServedActions({
    id: 'email_tier:note/A.md',
    kind: 'email_tier',
    instance: 'salem',
    title: 'Email tier: a@b.com — Subject',
    mode: 'decide',
    attention: 'needs_you',
    evidence: { sender: 'a@b.com' },
    actions: [],
    state: 'open',
    created_at: '2026-07-30T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...overrides,
  });
}

function card(overrides: Partial<FeedItem> = {}) {
  return render(
    <DeckCard
      item={item(overrides)}
      depth={0}
      expanded={false}
      confirming={false}
      onToggleEvidence={() => {}}
      onConfirmHeavy={() => {}}
      onCancelHeavy={() => {}}
    />,
  );
}

const stampClass = (name: string) =>
  (document.querySelector(`[data-stamp="${name}"]`) as HTMLElement).className;

beforeEach(() => {
  mockAct.mockReset();
  mockAct.mockResolvedValue({ ok: true, status: 'acted' });
});

describe('the gesture axis is legible mid-drag (D3)', () => {
  it('draws each verdict stamp in its own role', () => {
    card();
    // Right affirms, left declines, up defers. These are the three things the
    // operator reads WHILE dragging, before any commit — if the reject stamp
    // were drawn in the affirm role, a half-completed left swipe would look
    // like agreement.
    expect(stampClass('affirm')).toContain('text-affirm');
    expect(stampClass('reject')).toContain('text-negative');
    expect(stampClass('snooze')).toContain('text-caution');
  });

  it('never draws two verdicts in the same role', () => {
    card();
    // The negative control for the assertion above: three stamps all in one
    // colour would satisfy "contains text-affirm" for whichever one was
    // checked, and would make the axis unreadable.
    const roles = ['affirm', 'reject', 'snooze'].map((n) => {
      const cls = stampClass(n);
      return ['text-affirm', 'text-negative', 'text-caution', 'text-info'].find((r) => cls.includes(r));
    });
    expect(new Set(roles).size).toBe(3);
  });

  it('draws the three gesture buttons in the same roles as the stamps', () => {
    render(<Deck items={[item()]} />);
    // The button row is the accessible alternate for the swipe, so it must
    // teach the SAME axis — otherwise the keyboard user learns a different
    // language from the thumb user.
    expect(screen.getByTestId('deck-btn-affirm').className).toContain('text-affirm');
    expect(screen.getByTestId('deck-btn-reject').className).toContain('text-negative');
    expect(screen.getByTestId('deck-btn-snooze').className).toContain('text-caution');
    // The details control resolves nothing, so it carries no verdict colour.
    const details = screen.getByTestId('deck-btn-details').className;
    for (const role of ['text-affirm', 'text-negative', 'text-caution']) {
      expect(details).not.toContain(role);
    }
  });

  it('draws a defer-flavoured reject in caution rather than negative', () => {
    // A slot candidate's left gesture SKIPS (a session set-aside, no POST); it
    // is not a rejection, and drawing it as one would report a card that is
    // coming back as one that was turned down.
    render(<Deck items={[item({ kind: 'slot_suggestion', evidence: { status: 'suggested', tier: 2, candidate: true } })]} />);
    const reject = screen.getByTestId('deck-btn-reject').className;
    expect(reject).toContain('text-caution');
    expect(reject).not.toContain('text-negative');
  });
});

describe('the card rail reports weight at a glance', () => {
  it('hatches the rail when a direction will arm, and not when none will', () => {
    // Positive AND negative control in one test — "the heavy card is hatched"
    // is worthless without "the light card is not".
    card({ kind: 'proposal' }); // heavy affirm (creates/merges vault records)
    expect(screen.getByTestId('deck-card-rail').className).toContain('console-hatch');
    screen.getByTestId('deck-card-rail').remove();

    card({ kind: 'email_tier' }); // light both ways
    expect(screen.getByTestId('deck-card-rail').className).not.toContain('console-hatch');
  });

  it('hatches for a heavy REJECT even though the affirm is light', () => {
    // The attribution asymmetry, which is the reason the rail asks "can this be
    // cleared with one motion?" rather than "is this card heavy?": its confirm
    // is light and its reject strips a record body.
    card({ kind: 'attribution' });
    expect(screen.getByTestId('deck-card-rail').className).toContain('console-hatch');
    // And the per-direction chips still say WHICH one is the heavy half.
    expect(screen.queryByTestId('deck-heavy-reject')).not.toBeNull();
    expect(screen.queryByTestId('deck-heavy-affirm')).toBeNull();
  });

  it('keeps the rail out of the accessibility tree', () => {
    card();
    // It is a colour block. The weight it reports is also stated in words by
    // the "Heavy · <verb>" chip, which is the channel a screen reader gets.
    expect(screen.getByTestId('deck-card-rail').getAttribute('aria-hidden')).toBe('true');
  });
});

describe('the verb strip advertises the card its own grammar (F3)', () => {
  it('names each verb beside its gesture, in that verb’s role', () => {
    card({ kind: 'attribution' });
    expect(screen.getByTestId('deck-verb-affirm').textContent).toContain('Confirm');
    expect(screen.getByTestId('deck-verb-reject').textContent).toContain('Reject');
    expect(screen.getByTestId('deck-verb-snooze').textContent).toContain('Snooze');
    expect(screen.getByTestId('deck-verb-affirm').className).toContain('text-affirm');
    expect(screen.getByTestId('deck-verb-reject').className).toContain('text-negative');
  });

  it('shows a dash — not a live-looking verb — where a kind has no action', () => {
    // ILB, in the smallest possible place: an ACK-only card has no left verb,
    // and a strip cell that still looked like a verb would advertise a door
    // that does nothing.
    card({ kind: 'email_urgent', evidence: { high_source: 'llm' } });
    const reject = screen.getByTestId('deck-verb-reject');
    expect(reject.textContent).toBe('—');
    expect(reject.className).toContain('text-console-ink-ghost');
  });
});

describe('provenance is not a verdict', () => {
  it('draws the instance chip in an accent, never in a function role', () => {
    card({ instance: 'kalle' });
    const chip = screen.getByTestId('deck-instance');
    expect(chip.textContent).toContain('kalle');
    expect(chip.className).toContain(instanceAccentClass('kalle'));
    for (const role of ['text-affirm', 'text-negative', 'text-caution', 'text-info']) {
      expect(chip.className).not.toContain(role);
    }
  });
});

describe('D7 — a card that carries an interval renders its extent', () => {
  it('renders the span, the duration and the extent bar', () => {
    card({ starts_at: '2026-08-12T11:00:00Z', ends_at: '2026-08-12T15:00:00Z' });
    expect(screen.getByTestId('time-extent').getAttribute('data-extent-kind')).toBe('interval');
    expect(screen.getByTestId('time-extent-duration').textContent).toBe('4h');
    // The bar is the part that makes this an extent rather than a label.
    expect(screen.queryByTestId('time-extent-bar')).not.toBeNull();
  });

  it('renders an instant WITHOUT an extent bar', () => {
    // The control that keeps the assertion above honest: a start-only item is a
    // moment, and drawing a bar for it would invent a duration the backend
    // explicitly refuses to imply (`ends_at = null` is "no known end").
    card({ starts_at: '2026-08-12T09:30:00Z' });
    expect(screen.getByTestId('time-extent').getAttribute('data-extent-kind')).toBe('instant');
    expect(screen.queryByTestId('time-extent-bar')).toBeNull();
    expect(screen.queryByTestId('time-extent-duration')).toBeNull();
  });

  it('draws no time chrome at all on a card with no time dimension', () => {
    // Which is EVERY deck card in production today — the two producers that
    // stamp extents (event, weather) are both FYI kinds. A row of empty time
    // slots would bury the ones that eventually have a real interval.
    card();
    expect(screen.queryByTestId('time-extent')).toBeNull();
  });

  it('says a time is unreadable rather than silently dropping it', () => {
    card({ starts_at: 'the fourteenth' });
    const extent = screen.getByTestId('time-extent');
    expect(extent.getAttribute('data-extent-kind')).toBe('unparseable');
    expect(extent.textContent?.toLowerCase()).toContain('unreadable');
  });
});

describe('the workstation posture', () => {
  it('lists the REAL remainder of the queue, not the render slice', () => {
    // The visual stack draws two cards behind the top one. The pane must show
    // everything still to come, or the count above it and the list beside it
    // would disagree.
    const items = ['a', 'b', 'c', 'd', 'e'].map((id) => item({ id, title: `Card ${id}` }));
    render(<Deck items={items} />);
    expect(screen.getAllByTestId('deck-queue-row')).toHaveLength(4); // 5 minus the current one
    expect(screen.getByTestId('deck-queue-pane').textContent).toContain('Card e');
  });

  it('counts how many of the cards ahead will ask for a second tap', () => {
    const items = [
      item({ id: 'a' }),
      item({ id: 'b', kind: 'proposal' }),
      // Was `recurrence` — a kind the client used to offer a heavy verb for and
      // the ceiling never admitted (#102 1b-ii retired that entry). Attribution
      // is a REAL heavy card: its reject strips a section out of a record.
      item({ id: 'c', kind: 'attribution' }),
    ];
    render(<Deck items={items} />);
    expect(screen.getByTestId('deck-queue-heavy').textContent).toContain('2');
  });

  it('says the queue is empty rather than going blank (ILB)', () => {
    render(<Deck items={[item()]} />);
    expect(screen.queryByTestId('deck-queue-row')).toBeNull();
    expect(screen.getByTestId('deck-queue-empty').textContent).toContain('Nothing behind this one');
    // And the heavy line is absent rather than showing a zero.
    expect(screen.queryByTestId('deck-queue-heavy')).toBeNull();
  });

  it('is hidden on the phone posture', () => {
    // The tricorder is one card and the whole screen; peripheral vision must
    // not compete with the decision in front of you. The pane is present in the
    // DOM and gated by a breakpoint, so this asserts the gate itself.
    render(<Deck items={[item({ id: 'a' }), item({ id: 'b' })]} />);
    const pane = screen.getByTestId('deck-queue-pane');
    expect(pane.className).toContain('hidden');
    expect(pane.className).toContain('lg:flex');
  });
});
