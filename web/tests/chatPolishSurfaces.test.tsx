import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

// THE THREE SURFACES THE OPERATOR PHOTOGRAPHED, pinned where they RENDER.
//
// tests/brightSlabCensus.test.ts bounds the whole class and checks the marker
// matrix, but it is a source-text scan: it proves the marker is written in the
// file. These prove it reaches the DOM on the surface it was reported from,
// which is a different claim and the one the screenshots were about — a marker
// on a branch that never renders would satisfy the census perfectly.
//
// AND ONE COPY PIN. The busy-guard banner's sentence is operator-ratified and
// inviolate; this lane restyled the slab under it and must not have touched a
// word of it. Restyling is exactly the kind of change that "improves" copy in
// passing, so the sentence is pinned character-for-character.

vi.mock('next/router', () => ({
  useRouter: () => ({
    replace: vi.fn(), push: vi.fn(), asPath: '/chat', query: {},
    events: { on: vi.fn(), off: vi.fn() },
  }),
}));

import { ApiError } from '../lib/algernon/http';
import { friendlyError } from '../lib/algernon/useChat';
import { NotificationList } from '../components/NotificationList';
import { FeedRow } from '../components/feed/FeedRow';
import { COMMS_SURFACE } from '../lib/algernon/commsSurface';
import { VIEWSCREEN_SURFACE } from '../lib/algernon/viewscreenSurface';
import { Layout } from '../components/Layout';
import type { FeedItem } from '../lib/algernon/feed';
import type { NotificationItem } from '../lib/algernon/types';

// ---------------------------------------------------------------------------
// (2b) the busy-guard banner — RESTYLED, NOT REWORDED
// ---------------------------------------------------------------------------
describe('the busy-guard sentence is untouched', () => {
  // The ratified copy, written out here so the pin is readable as the contract
  // rather than as a reference to one. A test that compared the source against
  // itself would agree with any rewrite.
  const RATIFIED =
    'Your previous message is still being answered. Wait for that reply before sending the next one.';

  it('is exactly the ratified sentence, character for character', () => {
    // MUTATION: change one word in useChat's `turn_in_flight` arm and this reds.
    // `toContain` would not — the existing recovery test asserts the substring
    // "still being answered", which survives a rewritten first and last clause.
    expect(friendlyError(new ApiError(409, 'turn_in_flight'))).toBe(RATIFIED);
  });

  it('says what is happening and does NOT invite a retry — the reason it was ratified', () => {
    // The properties the wording was chosen FOR, asserted separately from the
    // literal. If a future lane re-ratifies the sentence, these say what the
    // replacement still has to do, and the pin above is the thing to update.
    const msg = friendlyError(new ApiError(409, 'turn_in_flight'));
    expect(msg).not.toMatch(/wrong|error|failed|sorry/i);
    expect(msg).not.toMatch(/try again/i);
    // Positive control: the switch really does discriminate, so the assertions
    // above are about THIS arm rather than about a function returning one
    // string for everything.
    expect(friendlyError(new ApiError(500, 'engine_error'))).not.toBe(msg);
    expect(friendlyError(new ApiError(500, 'engine_error'))).toMatch(/try again/i);
  });
});

// ---------------------------------------------------------------------------
// (2a) the notification tray card, on the comms surface
// ---------------------------------------------------------------------------
function notice(over: Partial<NotificationItem> = {}): NotificationItem {
  return {
    id: 'n1',
    text: 'A ticket was filed',
    read: false,
    created_at: '2026-08-19T00:00:00Z',
    ...over,
  } as NotificationItem;
}

describe('the notification tray card adopts the comms register', () => {
  it('the row AND its control both carry a marker', () => {
    // BOTH, because the screenshot was of both: a pale mint slab with a WHITE
    // button sitting on it. Marking the card and leaving the button is the
    // guarded-half miss — the slab goes dark and the button stays a white chip
    // on it, which is more obviously wrong than what was reported.
    const { unmount } = render(
      <Layout onSignOut={() => {}} surface={COMMS_SURFACE}>
        <NotificationList notifications={[notice()]} onAck={() => {}} onDismiss={() => {}} />
      </Layout>,
    );
    const row = screen.getByTestId('notification-item');
    expect(row.className.split(/\s+/)).toContain('ui-panel');
    expect(screen.getByTestId('notification-ack').className.split(/\s+/)).toContain('ui-btn');
    // The warm classes stay ON THE SAME ELEMENT as the marker — the unmarked
    // default plus opt-in reach, which is what keeps a tray rendered on a warm
    // route byte-identical to what it always was.
    expect(row.className).toContain('bg-honeydew-100');
    unmount();
  });

  it('the READ branch marks its Dismiss control too — the other lifecycle arm', () => {
    // One control per row, and WHICH one depends on `read`. The pin above only
    // ever sees the unread branch, so it stays green against a build that
    // marked "Mark read" and forgot "Dismiss" — and Dismiss is the button in
    // the operator's screenshot, since the card he photographed was read.
    const { unmount } = render(
      <Layout onSignOut={() => {}} surface={COMMS_SURFACE}>
        <NotificationList
          notifications={[notice({ read: true })]}
          onAck={() => {}}
          onDismiss={() => {}}
        />
      </Layout>,
    );
    // Read rows live one level down, behind the history disclosure. Driven
    // with fireEvent rather than a raw dispatch — React listens for its own
    // synthetic event, and a bare MouseEvent leaves the disclosure shut (the
    // first cut of this test failed exactly that way).
    fireEvent.click(screen.getByTestId('notification-history-toggle'));
    const dismiss = screen.getByTestId('notification-dismiss');
    expect(dismiss.className.split(/\s+/)).toContain('ui-btn');
    expect(screen.getByTestId('notification-item').className.split(/\s+/)).toContain('ui-panel');
    unmount();
  });
});

// ---------------------------------------------------------------------------
// (2c) the feed row's cream slab, on the viewscreen (home)
// ---------------------------------------------------------------------------
function item(over: Partial<FeedItem> = {}): FeedItem {
  return {
    id: 'f1',
    kind: 'peer_digest',
    instance: 'salem',
    title: 'An attribution to acknowledge',
    mode: 'fyi',
    attention: 'fyi',
    evidence: { body: 'why this was attributed' },
    actions: [],
    state: 'open',
    created_at: '2026-08-19T00:00:00Z',
    acted_at: null,
    expires_at: null,
    source_ref: {},
    ...over,
  } as FeedItem;
}

describe('the feed row adopts the register it is rendered in', () => {
  it('carries the marker on home, where it was photographed as a cream slab', () => {
    const { unmount } = render(
      <Layout onSignOut={() => {}} surface={VIEWSCREEN_SURFACE}>
        <ul>
          <FeedRow item={item()} expanded onToggleEvidence={() => {}} onAck={() => {}} />
        </ul>
      </Layout>,
    );
    const row = screen.getByTestId('feed-row');
    expect(row.className.split(/\s+/)).toContain('ui-panel');
    expect(row.className).toContain('bg-cream'); // the warm default, still there
    unmount();
  });

  it('the EVIDENCE panel inside it carries one too', () => {
    // The row's own slab was the reported one; the evidence body is a second
    // light panel nested in it, and it is reached through a different mechanism
    // (EvidenceBody's per-surface SKIN map, whose `warm` entry is the default
    // this row gets). Fixing the outer slab and leaving the inner one would
    // have put a cream rectangle inside a dark card.
    const { unmount } = render(
      <Layout onSignOut={() => {}} surface={VIEWSCREEN_SURFACE}>
        <ul>
          <FeedRow item={item()} expanded onToggleEvidence={() => {}} onAck={() => {}} />
        </ul>
      </Layout>,
    );
    const body = screen.getByTestId('evidence-body');
    const framed = body.querySelector('.ui-panel');
    expect(framed, 'the evidence frame renders and carries the marker').not.toBeNull();
    unmount();
  });

  it('renders the SAME markers on the feed, which is a different dark register', () => {
    // The whole point of the marker over a per-register selector: one component,
    // one spelling, every register. sensorLog.css reached this row through a
    // testid block for months and viewscreen.css did not, which is how the same
    // component came to be correct on /feed and a cream slab on home.
    const { unmount } = render(
      <Layout onSignOut={() => {}} surface="sensor-log">
        <ul>
          <FeedRow item={item()} expanded onToggleEvidence={() => {}} onAck={() => {}} />
        </ul>
      </Layout>,
    );
    expect(screen.getByTestId('feed-row').className.split(/\s+/)).toContain('ui-panel');
    unmount();
  });
});
