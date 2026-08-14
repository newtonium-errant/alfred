import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const { mockState, mockContact, mockOverride, mockReplace } = vi.hoisted(() => ({
  mockState: vi.fn(),
  mockContact: vi.fn(),
  mockOverride: vi.fn(),
  mockReplace: vi.fn(),
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
    events: { on: vi.fn(), off: vi.fn() },
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
