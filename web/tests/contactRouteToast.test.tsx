import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { mockState, mockContact, mockOverride, mockReplace, routeHandlers } = vi.hoisted(() => ({
  mockState: vi.fn(),
  mockContact: vi.fn(),
  mockOverride: vi.fn(),
  mockReplace: vi.fn(),
  // Real handler registry, not a no-op spy: the toast's dismiss-on-navigate
  // cannot be driven against `events: { on: vi.fn() }`, which is exactly how
  // this fix could have shipped with a green suite and the operator's bug
  // still live.
  routeHandlers: [] as Array<(url: string) => void>,
}));

vi.mock('../lib/algernon/dayClient', () => ({
  dayApi: { state: mockState, contact: mockContact, override: mockOverride },
}));
vi.mock('next/router', () => ({
  useRouter: () => ({
    replace: mockReplace,
    push: vi.fn(),
    asPath: '/',
    query: {},
    events: {
      on: (_e: string, h: (url: string) => void) => routeHandlers.push(h),
      off: (_e: string, h: (url: string) => void) => {
        const i = routeHandlers.indexOf(h);
        if (i >= 0) routeHandlers.splice(i, 1);
      },
    },
  }),
}));

import { ContactRouteToast } from '../components/ContactRouteToast';
import { CONTACT_RULE_ORDER, type DayState } from '../lib/algernon/contactRouter';
import {
  OVERRIDE_WINDOW_MS,
  __resetContactRouteToast,
  useContactRouter,
} from '../lib/algernon/useContactRouter';

// The override layer. The spec: "Every open, the operator can override the
// rule's surface choice with one tap. Overrides are logged (with the triggering
// state) but do not silently adjust the ruleset."
//
// ONE TAP PER ALTERNATIVE is the design being pinned: a single "wrong" button
// could only record THAT the router erred, never WHERE the operator wanted to
// be — and a pattern card cannot say "you have gone to the deck instead" from
// that. Each chip carries its destination.

function dayState(overrides: Partial<DayState> = {}): DayState {
  return {
    last_session_ended: null,
    time_since_last_session_hours: 1,
    brief_read_today: true,
    unresolved_flagged_notifications: 0,
    first_unresolved_notification_id: null,
    last_active_surface: '',
    rule_order: [...CONTACT_RULE_ORDER],
    armed_rules: ['unresolved_notification', 'first_contact_after_gap', 'default'],
    unarmed_rules: {},
    adopted_defaults: {},
    levers: { gap_hours_new_day: 6, brief_read_decay_hours: 12 },
    levers_source: 'defaults',
    configured: true,
    ...overrides,
  };
}

/** Mounts the router (the publisher) beside the toast (the subscriber). */
function Harness({ enabled = true }: { enabled?: boolean }) {
  const router = {
    replace: mockReplace,
    push: vi.fn(),
    asPath: '/',
    query: {},
    events: { on: vi.fn(), off: vi.fn() },
  } as never;
  useContactRouter({ enabled, router });
  return <ContactRouteToast />;
}

beforeEach(() => {
  routeHandlers.length = 0;
  __resetContactRouteToast();
  mockState.mockReset().mockResolvedValue(dayState());
  mockContact.mockReset().mockResolvedValue({ contact_id: 'c-1', recorded: true });
  mockOverride.mockReset().mockResolvedValue({ recorded: true, patterns_surfaced: 0 });
  mockReplace.mockReset().mockResolvedValue(true);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('it renders nothing unless the router decided', () => {
  it('is absent on a page nobody routed to', () => {
    render(<ContactRouteToast />);
    expect(screen.queryByTestId('contact-route-toast')).toBeNull();
  });

  it('is absent when the router is gated off', async () => {
    render(<Harness enabled={false} />);
    await Promise.resolve();
    expect(screen.queryByTestId('contact-route-toast')).toBeNull();
  });

  it('appears after a routed open — the positive control', async () => {
    render(<Harness />);
    await waitFor(() =>
      expect(screen.getByTestId('contact-route-toast')).toBeTruthy(),
    );
  });
});

describe('it says why it opened where it did', () => {
  it('names the rule that fired', async () => {
    render(<Harness />);
    await waitFor(() =>
      expect(screen.getByTestId('contact-route-reason').textContent).toContain(
        'default',
      ),
    );
  });

  it('credits the operator when the surface came from their adopted default', async () => {
    mockState.mockResolvedValue(dayState({ adopted_defaults: { default: 'deck' } }));
    render(<Harness />);
    await waitFor(() =>
      expect(screen.getByTestId('contact-route-reason').textContent).toContain(
        'YOUR DEFAULT',
      ),
    );
  });
});

describe('one tap per alternative', () => {
  it('offers every primary surface except the one it opened', async () => {
    render(<Harness />);
    await waitFor(() => screen.getByTestId('contact-route-toast'));
    expect(screen.queryByTestId('contact-route-override-chat')).toBeNull();
    for (const s of ['home', 'feed', 'brief', 'deck']) {
      expect(screen.getByTestId(`contact-route-override-${s}`)).toBeTruthy();
    }
  });

  it('navigates AND records the surface the operator chose', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await waitFor(() => screen.getByTestId('contact-route-toast'));
    mockReplace.mockClear();

    await user.click(screen.getByTestId('contact-route-override-deck'));

    await waitFor(() => expect(mockOverride).toHaveBeenCalledWith('c-1', 'deck'));
    expect(mockReplace).toHaveBeenCalledWith('/deck');
  });

  it('closes after the tap so one open cannot log two corrections', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await waitFor(() => screen.getByTestId('contact-route-toast'));

    await user.click(screen.getByTestId('contact-route-override-feed'));
    await waitFor(() =>
      expect(screen.queryByTestId('contact-route-toast')).toBeNull(),
    );
    expect(mockOverride).toHaveBeenCalledTimes(1);
  });
});

describe('dismiss', () => {
  it('closes without recording a correction', async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await waitFor(() => screen.getByTestId('contact-route-toast'));

    await user.click(screen.getByTestId('contact-route-dismiss'));
    await waitFor(() =>
      expect(screen.queryByTestId('contact-route-toast')).toBeNull(),
    );
    expect(mockOverride).not.toHaveBeenCalled();
  });
});

describe('it never follows the operator to the next page', () => {
  // THE OPERATOR'S BUG REPORT. This component lives in `_app`, so a client-side
  // navigation does not unmount it — the affordance outlived the surface it was
  // about and was found sitting over the /ingest textarea, long after the open
  // it described. A note about THIS open has no business on the next page.
  it('dismisses on a route change', async () => {
    render(<Harness />);
    await waitFor(() => screen.getByTestId('contact-route-toast'));
    expect(routeHandlers.length).toBeGreaterThan(0); // vacuity control

    act(() => { routeHandlers.forEach((h) => h('/ingest')); });
    await waitFor(() =>
      expect(screen.queryByTestId('contact-route-toast')).toBeNull(),
    );
  });

  it('unsubscribes once dismissed, so it cannot be revived by a later nav', async () => {
    render(<Harness />);
    await waitFor(() => screen.getByTestId('contact-route-toast'));
    act(() => { routeHandlers.forEach((h) => h('/ingest')); });
    await waitFor(() =>
      expect(screen.queryByTestId('contact-route-toast')).toBeNull(),
    );
    act(() => { routeHandlers.forEach((h) => h('/deck')); });
    expect(screen.queryByTestId('contact-route-toast')).toBeNull();
  });

  it('sits clear of the composer, not over it', async () => {
    // Asserted on the RENDERED element, not the source. The first version of
    // this read the file as text and failed — because the comment explaining
    // the fix names `bottom-20`, the very string it was checking for the
    // absence of. Third time today that a text scan could not tell code from
    // prose about code; the DOM cannot be fooled that way.
    render(<Harness />);
    await waitFor(() => screen.getByTestId('contact-route-toast'));
    const cls = screen.getByTestId('contact-route-toast').className;
    expect(cls).toContain('top-20');
    expect(cls).not.toContain('bottom-20');
  });
});

describe('the chips read as surfaces, not as wire keys', () => {
  it('labels the brief chip Player', async () => {
    render(<Harness />);
    await waitFor(() => screen.getByTestId('contact-route-toast'));
    // The testid still carries the KEY (parity-pinned); the text carries the label.
    expect(screen.getByTestId('contact-route-override-brief').textContent).toBe('Player');
  });
});

describe('it does not become chrome', () => {
  it('self-dismisses once the override window lapses', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    render(<Harness />);
    await waitFor(() => screen.getByTestId('contact-route-toast'));

    vi.advanceTimersByTime(OVERRIDE_WINDOW_MS + 1);
    await waitFor(() =>
      expect(screen.queryByTestId('contact-route-toast')).toBeNull(),
    );
  });
});
